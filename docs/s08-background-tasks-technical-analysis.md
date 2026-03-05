# S08 Background Tasks - 后台并行任务状态同步技术文档

## 1. 概述

`s08_background_tasks.py` 实现了一个**后台任务管理系统**，允许 Agent 在执行耗时操作时**不阻塞主循环**，通过线程并行执行命令，并在任务完成后通过**通知队列**机制将结果同步回主线程。

核心设计理念：**"Fire and forget -- the agent doesn't block while the command runs."**

---

## 2. 架构设计

### 2.1 线程模型

```
┌─────────────────────────────────────────────────────────────┐
│                        主线程 (Main Thread)                  │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              Agent Loop 主循环                       │   │
│  │  ┌─────────────┐    ┌──────────────────────────┐   │   │
│  │  │ 1. drain_notifications() │                  │   │   │
│  │  │    获取已完成任务结果      │                  │   │   │
│  │  └──────┬──────┘    └──────────────────────────┘   │   │
│  │         │                                          │   │
│  │  ┌──────▼──────┐    ┌──────────────────────────┐   │   │
│  │  │ 2. LLM Call │<---+  注入后台任务完成通知      │   │   │
│  │  └─────────────┘    └──────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────┘   │
│                            │                                │
│  ┌─────────────────────────▼─────────────────────────────┐ │
│  │              BackgroundManager (单例 BG)              │ │
│  │  ┌─────────────────┐    ┌────────────────────────┐   │ │
│  │  │ tasks: dict     │    │ _notification_queue    │   │ │
│  │  │ 任务状态存储     │    │ 完成通知队列            │   │ │
│  │  └─────────────────┘    └────────────────────────┘   │ │
│  └───────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                                │
                                │ 创建后台线程
                                ▼
┌─────────────────────────────────────────────────────────────┐
│                    后台线程 (Background Thread)              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │           _execute(task_id, command)                │   │
│  │  ┌─────────┐    ┌─────────┐    ┌─────────────────┐ │   │
│  │  │ 执行命令 │ → │ 捕获输出 │ → │ 推送结果到队列   │ │   │
│  │  └─────────┘    └─────────┘    └─────────────────┘ │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 核心时序图

```
时间轴:

主线程:    ───[spawn A]────[spawn B]────[spawn C]────[其他工作]──[LLM Call]───
               │              │              │                            ▲
               │              │              │                            │
               ▼              ▼              ▼                            │
后台A:      [执行中──────────完成]──┐                                      │
                                   │                                      │
后台B:      [执行中────────────────────────完成]──┐                        │
                                                 │                        │
后台C:      [执行中────────────────────────────────────完成]──┐            │
                                                             │            │
通知队列:   ──────────────────────────────────────────────────▼────────────┤
                                                              结果注入LLM  │
```

---

## 3. 核心组件详解

### 3.1 BackgroundManager 类

```python
class BackgroundManager:
    def __init__(self):
        self.tasks = {}                     # 任务状态存储: task_id -> {status, result, command}
        self._notification_queue = []       # 完成通知队列
        self._lock = threading.Lock()       # 线程锁（保护通知队列）
```

#### 数据结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `tasks` | `dict[str, dict]` | 所有任务的完整状态，包含 `status`, `result`, `command` |
| `_notification_queue` | `list[dict]` | 已完成任务的通知队列，待主线程消费 |
| `_lock` | `threading.Lock` | 保护 `_notification_queue` 的线程安全 |

---

### 3.2 任务生命周期

#### 阶段 1: 任务创建 (主线程)

```python
def run(self, command: str) -> str:
    """启动后台线程，立即返回 task_id"""
    task_id = str(uuid.uuid4())[:8]          # 生成8位短ID
    self.tasks[task_id] = {
        "status": "running",                 # 初始状态：运行中
        "result": None,
        "command": command
    }
    thread = threading.Thread(
        target=self._execute,
        args=(task_id, command),
        daemon=True                           # 守护线程，主程序退出时自动结束
    )
    thread.start()                           # 启动后台线程
    return f"Background task {task_id} started..."
```

**关键点：**
- 使用 `uuid.uuid4()` 生成唯一任务ID
- 线程设置为 `daemon=True`，确保主程序退出时不会阻塞
- **立即返回**，不等待命令执行完成

---

#### 阶段 2: 任务执行 (后台线程)

```python
def _execute(self, task_id: str, command: str):
    """线程目标函数：执行子进程，捕获输出，推送结果到队列"""
    try:
        # 执行命令，最多等待300秒
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=300
        )
        output = (r.stdout + r.stderr).strip()[:50000]  # 截断到50000字符
        status = "completed"                            # 成功完成
    except subprocess.TimeoutExpired:
        output = "Error: Timeout (300s)"
        status = "timeout"                              # 超时状态
    except Exception as e:
        output = f"Error: {e}"
        status = "error"                                # 错误状态

    # 更新任务状态
    self.tasks[task_id]["status"] = status
    self.tasks[task_id]["result"] = output or "(no output)"

    # 推送通知到队列（加锁保证线程安全）
    with self._lock:
        self._notification_queue.append({
            "task_id": task_id,
            "status": status,
            "command": command[:80],
            "result": (output or "(no output)")[:500],
        })
```

**关键点：**
- 在独立线程中执行 `subprocess.run()`
- 捕获 `stdout` 和 `stderr`
- 三种结束状态：`completed`, `timeout`, `error`
- 结果推送到 `_notification_queue`，**加锁保护**

---

#### 阶段 3: 状态同步 (主线程消费通知)

```python
def drain_notifications(self) -> list:
    """返回并清空所有待处理的通知"""
    with self._lock:
        notifs = list(self._notification_queue)    # 复制队列内容
        self._notification_queue.clear()            # 清空队列
    return notifs
```

**关键点：**
- 使用 `list()` 复制队列内容，避免返回引用
- `clear()` 清空原队列
- **加锁保护**，防止与后台线程的竞争条件

---

#### 阶段 4: Agent Loop 集成

```python
def agent_loop(messages: list):
    while True:
        # ═══════════════════════════════════════════════════
        # 每次调用LLM前，先消费后台任务通知
        # ═══════════════════════════════════════════════════
        notifs = BG.drain_notifications()
        if notifs and messages:
            # 将通知格式化为系统消息注入对话
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" 
                for n in notifs
            )
            messages.append({
                "role": "user",
                "content": f"<background-results>\n{notif_text}\n</background-results>"
            })
            messages.append({
                "role": "assistant",
                "content": "Noted background results."
            })

        # 正常调用LLM...
        response = client.chat.completions.create(...)
        # ... 处理 tool_calls ...
```

**关键点：**
- 在**每次 LLM 调用前**检查通知队列
- 将后台任务结果以**用户消息**形式注入对话
- 保持对话连贯性，让 LLM 感知后台任务完成情况

---

### 3.3 任务查询接口

```python
def check(self, task_id: str = None) -> str:
    """查询单个任务状态或列出所有任务"""
    if task_id is not None:
        # 查询特定任务
        t = self.tasks.get(task_id)
        if not t:
            return f"Error: Unknown task {task_id}"
        return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(running)'}"

    # 列出所有任务
    lines = []
    for tid, t in self.tasks.items():
        lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
    return "\n".join(lines) if lines else "No background tasks."
```

**功能：**
- `check_background(task_id="abc123")` - 查询特定任务
- `check_background()` - 列出所有任务

---

## 4. 状态机模型

```
                    ┌─────────────┐
         ┌──────────│   START     │──────────┐
         │          │  (任务创建)  │          │
         │          └──────┬──────┘          │
         │                 │                 │
         ▼                 ▼                 ▼
    ┌─────────┐      ┌─────────┐       ┌─────────┐
    │ running │      │ running │       │ running │
    │ Task A  │      │ Task B  │       │ Task C  │
    └────┬────┘      └────┬────┘       └────┬────┘
         │                 │                 │
         │ 执行完成         │ 超时            │ 错误
         ▼                 ▼                 ▼
    ┌─────────┐      ┌─────────┐       ┌─────────┐
    │completed│      │ timeout │       │  error  │
    └────┬────┘      └────┬────┘       └────┬────┘
         │                 │                 │
         └────────┬────────┴────────┬────────┘
                  │                 │
                  ▼                 ▼
         ┌─────────────────────────────┐
         │  _notification_queue.append │
         │   (推送完成通知到队列)        │
         └──────────────┬──────────────┘
                        │
                        ▼
         ┌─────────────────────────────┐
         │   agent_loop 消费通知        │
         │   (注入LLM对话上下文)         │
         └─────────────────────────────┘
```

---

## 5. 线程安全机制

### 5.1 共享资源分析

| 资源 | 访问线程 | 线程安全 |
|------|---------|---------|
| `self.tasks` | 主线程(读写) + 后台线程(写) | ⚠️ **非线程安全**（Python GIL保护字典操作） |
| `self._notification_queue` | 主线程(读写) + 后台线程(写) | ✅ **显式加锁保护** |

### 5.2 锁的使用

```python
# 后台线程写入队列时加锁
with self._lock:
    self._notification_queue.append({...})

# 主线程读取队列时加锁
with self._lock:
    notifs = list(self._notification_queue)
    self._notification_queue.clear()
```

**为什么 `self.tasks` 不加锁？**
- Python 的 GIL (全局解释器锁) 保证字典的单个操作是原子的
- 后台线程只写 `self.tasks`，主线程只读
- 写入的是完整 dict，不存在部分更新问题

---

## 6. 工具集成

### 6.1 工具定义

```python
{
    "type": "function",
    "function": {
        "name": "background_run",
        "description": "Run command in background thread. Returns task_id immediately.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }
}
```

```python
{
    "type": "function",
    "function": {
        "name": "check_background",
        "description": "Check background task status. Omit task_id to list all.",
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
        },
    }
}
```

### 6.2 工具处理器

```python
TOOL_HANDLERS = {
    "background_run":   lambda **kw: BG.run(kw["command"]),
    "check_background": handle_check_background,
}
```

---

## 7. 使用示例

### 7.1 典型工作流程

```python
# 用户输入
User: Run npm install in the background

# LLM 调用 background_run
LLM: background_run(command="npm install")
→ Returns: "Background task a1b2c3d4 started: npm install"

# 主线程继续执行其他工作...
# ... 执行其他工具调用 ...

# 后台线程完成 npm install
# _notification_queue.append({task_id: "a1b2c3d4", status: "completed", ...})

# 下次 LLM 调用前，agent_loop 调用 drain_notifications()
# 发现完成的任务，注入对话：
User: <background-results>
      [bg:a1b2c3d4] completed: added 42 packages...
      </background-results>
Assistant: Noted background results.

# LLM 现在可以基于后台结果继续工作
LLM: The npm install completed successfully. Now let's run the tests...
```

---

## 8. 设计要点总结

| 设计决策 | 理由 |
|---------|------|
| **立即返回 task_id** | Agent 不阻塞，可以并行执行多个后台任务 |
| **守护线程 (daemon=True)** | 主程序退出时自动清理，避免僵尸进程 |
| **通知队列模式** | 解耦后台线程和主线程，主线程按需消费 |
| **LLM 调用前检查队列** | 确保后台结果及时同步到对话上下文 |
| **50000字符截断** | 防止超大输出占用过多内存和上下文 |
| **300秒超时** | 防止后台任务无限挂起 |

---

## 9. 对比：同步 vs 异步

| 特性 | 同步执行 (s01-s07) | 后台执行 (s08) |
|------|-------------------|---------------|
| Agent 阻塞 | 是 | 否 |
| 并行能力 | 无 | 多任务并行 |
| 适用场景 | 快速命令 (<10s) | 耗时操作 (编译、下载等) |
| 结果获取 | 立即 | 下次 LLM 调用时 |
| 资源占用 | 低 | 额外线程开销 |

---

## 10. 扩展思考

### 10.1 潜在改进

1. **任务持久化**：将任务状态保存到磁盘，支持 Agent 重启后恢复
2. **取消任务**：添加 `cancel_background` 工具，支持终止运行中的任务
3. **进度报告**：长任务可定期推送进度更新（而非仅最终结果）
4. **任务依赖**：支持任务A完成后自动触发任务B

### 10.2 与后续会话的关系

- **s09 Agent Teams**: 后台任务为多个 Agent 并行工作奠定基础
- **s11 Autonomous Agents**: 后台任务使 Agent 能在"空闲"时执行计划
- **s12 Worktree Isolation**: 每个后台任务可在独立 worktree 中执行

---

## 11. 核心代码清单

```python
# 单例实例
BG = BackgroundManager()

# 启动后台任务
BG.run("long-running-command")

# 检查任务状态
BG.check(task_id)      # 查询特定任务
BG.check()             # 列出所有任务

# 消费完成通知（在 agent_loop 中调用）
BG.drain_notifications()
```

---

*文档生成时间: 2026-03-05*  
*对应代码: agents/s08_background_tasks.py*
