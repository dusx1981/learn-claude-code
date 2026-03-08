# s10 扩展: 子任务中断恢复机制 (Checkpoint Recovery)

> *"中断不可怕,可怕的是从头开始"* -- 检查点 + 持久化, 让子任务能从断点续跑。

`s01 > s02 > s03 > s04 > s05 > s06 | s07 > s08 > s09 > [ s10 ] s11 > s12`

## 问题

s10 中 `_teammate_loop` 和 `agent_loop` 的会话状态完全在内存中:

```python
# _teammate_loop (lines 197-238)
messages = [{"role": "user", "content": prompt}]  # 内存中
for _ in range(50):
    # ... LLM 调用, tool 执行 ...
    messages.append(assistant_message)  # 累积在内存
    messages.extend(tool_results)
```

**问题场景:**

```
时间线:
t0: 领导分配任务给 alice —— "重构认证模块"
t1: alice 开始执行, 完成步骤 1/3 (读取文件)
t2: alice 执行步骤 2/3 (修改代码)
t3: [进程崩溃/网络中断/用户 Ctrl+C]
t4: 重启进程
t5: alice 从头开始 —— 步骤 1/3 (重复读取文件) ❌

问题:
- 已完成的工作白费
- 可能产生重复写入
- 浪费 token 和时间
```

**根本原因:**

| 状态 | 存储位置 | 中断后 |
|------|---------|--------|
| `messages[]` | 内存 | 丢失 |
| `shutdown_requests` | 内存 | 丢失 |
| `plan_requests` | 内存 | 丢失 |
| 执行进度 | 无 | 无法追踪 |

---

## 解决方案

### 核心架构

```
┌─────────────────────────────────────────────────────────────────┐
│                     Checkpoint Recovery 架构                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   .checkpoints/                                                  │
│   ├── alice_session.json      # alice 的完整会话状态              │
│   ├── alice_progress.json     # alice 的执行进度                  │
│   └── lead_session.json       # 领导的会话状态                    │
│                                                                  │
│   ┌──────────────┐    save()    ┌─────────────────┐              │
│   │  Memory      │ ──────────> │  Disk (JSON)    │              │
│   │  messages[]  │              │  .checkpoints/  │              │
│   │  progress    │ <────────── │                  │              │
│   └──────────────┘    load()    └─────────────────┘              │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 检查点文件结构

```json
// .checkpoints/alice_session.json
{
  "teammate": "alice",
  "role": "coder",
  "messages": [
    {"role": "user", "content": "重构认证模块"},
    {"role": "assistant", "content": "好的,我开始..."},
    {"role": "tool", "tool_call_id": "xxx", "content": "..."}
  ],
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T10:35:00Z"
}

// .checkpoints/alice_progress.json
{
  "teammate": "alice",
  "task_id": "task_auth_refactor",
  "steps": [
    {"id": 1, "desc": "读取认证文件", "status": "completed"},
    {"id": 2, "desc": "修改认证逻辑", "status": "in_progress", "checkpoint": "line:150"},
    {"id": 3, "desc": "运行测试", "status": "pending"}
  ],
  "current_step": 2,
  "resume_hint": "继续修改 auth.py, 从第 150 行开始"
}
```

### 恢复流程

```
┌─────────────────────────────────────────────────────────────────┐
│                        Recovery Flow                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   启动 teammate_loop                                             │
│         │                                                        │
│         ▼                                                        │
│   ┌──────────────┐                                               │
│   │ 检查 .checkpoints/ │                                         │
│   │ 是否有未完成任务? │                                          │
│   └──────┬───────┘                                               │
│          │                                                        │
│     ┌────┴────┐                                                   │
│     │         │                                                   │
│    YES       NO                                                   │
│     │         │                                                   │
│     ▼         ▼                                                   │
│   ┌─────┐   ┌─────┐                                              │
│   │加载 │   │新建 │                                              │
│   │状态 │   │会话 │                                              │
│   └──┬──┘   └──┬──┘                                              │
│      │         │                                                  │
│      ▼         ▼                                                  │
│   ┌─────────────────┐                                            │
│   │ 注入恢复提示     │                                            │
│   │ "你上次做到..."  │                                            │
│   └────────┬────────┘                                            │
│            │                                                      │
│            ▼                                                      │
│   ┌─────────────────┐                                            │
│   │ 正常执行 loop   │                                            │
│   │ 每步保存 checkpoint │                                         │
│   └─────────────────┘                                            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 设计思想

### 1. 幂等性原则 (Idempotency)

**问题:** 工具调用被中断后重试,可能产生重复副作用。

**解决方案:** 为每个操作分配唯一 ID, 磁盘记录已完成操作。

```python
# 记录已完成的操作
completed_ops = set()  # 从 .checkpoints/xxx_ops.json 加载

def execute_tool(tool_call_id, tool_name, args):
    if tool_call_id in completed_ops:
        return load_cached_result(tool_call_id)  # 跳过,返回缓存结果
    
    result = _actually_execute(tool_name, args)
    completed_ops.add(tool_call_id)
    save_checkpoint()
    return result
```

### 2. 最小检查点策略 (Minimal Checkpoint)

**原则:** 不是每条消息都保存, 而是在关键节点保存:

| 检查点类型 | 触发时机 | 保存内容 |
|-----------|---------|---------|
| **Step Complete** | 完成一个步骤 | 进度 + messages |
| **Tool Success** | 工具执行成功 | tool_call_id + 结果 |
| **State Change** | 状态变更 | shutdown/plan 状态 |
| **Graceful Exit** | 正常退出 | 完整会话 |

### 3. 渐进式恢复 (Progressive Resume)

**策略:** 不追求完美恢复, 而是"足够好"的恢复:

```python
# 恢复提示注入
resume_prompt = f"""
[系统恢复提示]
你之前在执行任务: {task_desc}
已完成步骤: {completed_steps}
当前步骤: {current_step}
恢复建议: {resume_hint}

请继续执行, 不要重复已完成的工作。
"""
messages.insert(0, {"role": "user", "content": resume_prompt})
```

### 4. 失败安全 (Fail-Safe)

**原则:** 检查点保存失败不应阻塞主流程。

```python
def save_checkpoint():
    try:
        _do_save_checkpoint()
    except Exception as e:
        logger.warning(f"Checkpoint save failed: {e}")
        # 继续执行, 不抛出异常
```

---

## 实现机制

### 1. CheckpointManager 类

```python
import json
from pathlib import Path
from datetime import datetime
from typing import Optional
import threading

class CheckpointManager:
    """管理会话检查点的持久化与恢复"""
    
    def __init__(self, checkpoint_dir: Path):
        self.dir = checkpoint_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
    
    def _session_path(self, name: str) -> Path:
        return self.dir / f"{name}_session.json"
    
    def _progress_path(self, name: str) -> Path:
        return self.dir / f"{name}_progress.json"
    
    def _ops_path(self, name: str) -> Path:
        return self.dir / f"{name}_ops.json"
    
    # === 保存操作 ===
    
    def save_session(self, name: str, messages: list, metadata: dict = None):
        """保存完整会话状态"""
        with self._lock:
            data = {
                "name": name,
                "messages": messages,
                "metadata": metadata or {},
                "updated_at": datetime.now().isoformat(),
            }
            self._session_path(name).write_text(
                json.dumps(data, ensure_ascii=False, indent=2)
            )
    
    def save_progress(self, name: str, task_id: str, steps: list, current_step: int, resume_hint: str = ""):
        """保存执行进度"""
        with self._lock:
            data = {
                "name": name,
                "task_id": task_id,
                "steps": steps,
                "current_step": current_step,
                "resume_hint": resume_hint,
                "updated_at": datetime.now().isoformat(),
            }
            self._progress_path(name).write_text(
                json.dumps(data, ensure_ascii=False, indent=2)
            )
    
    def save_completed_op(self, name: str, tool_call_id: str, result: str):
        """记录已完成的操作 (幂等性保证)"""
        with self._lock:
            ops = self.load_completed_ops(name)
            ops[tool_call_id] = {
                "result": result,
                "timestamp": datetime.now().isoformat(),
            }
            self._ops_path(name).write_text(
                json.dumps(ops, ensure_ascii=False, indent=2)
            )
    
    # === 加载操作 ===
    
    def load_session(self, name: str) -> Optional[dict]:
        """加载会话状态"""
        path = self._session_path(name)
        if not path.exists():
            return None
        return json.loads(path.read_text())
    
    def load_progress(self, name: str) -> Optional[dict]:
        """加载执行进度"""
        path = self._progress_path(name)
        if not path.exists():
            return None
        return json.loads(path.read_text())
    
    def load_completed_ops(self, name: str) -> dict:
        """加载已完成操作记录"""
        path = self._ops_path(name)
        if not path.exists():
            return {}
        return json.loads(path.read_text())
    
    # === 恢复检查 ===
    
    def has_incomplete_task(self, name: str) -> bool:
        """检查是否有未完成任务"""
        progress = self.load_progress(name)
        if not progress:
            return False
        # 存在未完成的步骤
        return any(s["status"] != "completed" for s in progress["steps"])
    
    def get_resume_prompt(self, name: str) -> str:
        """生成恢复提示"""
        progress = self.load_progress(name)
        if not progress:
            return ""
        
        completed = [s for s in progress["steps"] if s["status"] == "completed"]
        current = next(
            (s for s in progress["steps"] if s["status"] == "in_progress"),
            None
        )
        
        prompt = f"""[系统恢复提示]
你之前在执行任务: {progress['task_id']}
已完成步骤: {len(completed)}/{len(progress['steps'])}
"""
        if current:
            prompt += f"当前步骤: {current['desc']}\n"
        if progress.get("resume_hint"):
            prompt += f"恢复建议: {progress['resume_hint']}\n"
        prompt += "\n请继续执行,不要重复已完成的工作。"
        
        return prompt
    
    # === 清理操作 ===
    
    def clear_checkpoint(self, name: str):
        """任务完成后清理检查点"""
        for path in [
            self._session_path(name),
            self._progress_path(name),
            self._ops_path(name),
        ]:
            if path.exists():
                path.unlink()
```

### 2. 集成到 _teammate_loop

```python
def _teammate_loop(self, name: str, role: str, prompt: str):
    # === 新增: 初始化检查点管理器 ===
    checkpoint = CheckpointManager(TEAM_DIR / ".checkpoints")
    
    # === 新增: 检查是否需要恢复 ===
    messages = []
    if checkpoint.has_incomplete_task(name):
        # 恢复模式
        session = checkpoint.load_session(name)
        messages = session["messages"] if session else []
        resume_prompt = checkpoint.get_resume_prompt(name)
        messages.append({"role": "user", "content": resume_prompt})
        print(f"  [{name}] Resuming from checkpoint...")
    else:
        # 新任务模式
        messages = [{"role": "user", "content": prompt}]
        checkpoint.save_session(name, messages)
    
    tools = self._teammate_tools()
    should_exit = False
    
    for iteration in range(50):
        inbox = BUS.read_inbox(name)
        for msg in inbox:
            messages.append({"role": "user", "content": json.dumps(msg)})
        
        if should_exit:
            break
        
        # ... LLM 调用 ...
        response = client.chat.completions.create(...)
        choice = response.choices[0]
        
        # ... 消息处理 ...
        messages.append(assistant_message)
        
        if choice.finish_reason != "tool_calls":
            # === 新增: 正常完成时清理检查点 ===
            checkpoint.clear_checkpoint(name)
            break
        
        results = []
        for tool_call in choice.message.tool_calls:
            function_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
            
            # === 新增: 幂等性检查 ===
            ops = checkpoint.load_completed_ops(name)
            if tool_call.id in ops:
                output = ops[tool_call.id]["result"]
                print(f"  [{name}] {function_name}: [CACHED]")
            else:
                output = self._exec(name, function_name, arguments)
                checkpoint.save_completed_op(name, tool_call.id, str(output))
                print(f"  [{name}] {function_name}: {str(output)[:120]}")
            
            results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output),
            })
            
            if function_name == "shutdown_response" and arguments.get("approve"):
                should_exit = True
        
        messages.extend(results)
        
        # === 新增: 每轮迭代保存检查点 ===
        checkpoint.save_session(name, messages)
    
    # 更新状态...
```

### 3. 进度追踪集成

```python
def _exec(self, sender: str, tool_name: str, args: dict) -> str:
    # ... 原有逻辑 ...
    
    # === 新增: 进度感知 ===
    if tool_name == "update_progress":
        # 允许 agent 显式更新进度
        checkpoint = CheckpointManager(TEAM_DIR / ".checkpoints")
        checkpoint.save_progress(
            sender,
            task_id=args["task_id"],
            steps=args["steps"],
            current_step=args["current_step"],
            resume_hint=args.get("resume_hint", ""),
        )
        return "Progress updated"
    
    # ... 其他工具 ...
```

---

## 举例说明

### 场景 1: 长任务中断恢复

```
时间线:

t0: 领导分配任务
    > spawn alice as coder: "重构认证模块,分为3步: 读取 -> 修改 -> 测试"

t1: alice 执行步骤 1 (读取文件)
    .checkpoints/alice_progress.json:
    {
      "steps": [
        {"id": 1, "desc": "读取认证文件", "status": "completed"},
        {"id": 2, "desc": "修改认证逻辑", "status": "pending"},
        {"id": 3, "desc": "运行测试", "status": "pending"}
      ],
      "current_step": 1
    }

t2: alice 执行步骤 2, 写到一半
    .checkpoints/alice_progress.json:
    {
      "steps": [
        {"id": 1, "desc": "读取认证文件", "status": "completed"},
        {"id": 2, "desc": "修改认证逻辑", "status": "in_progress", "checkpoint": "auth.py:150"},
        {"id": 3, "desc": "运行测试", "status": "pending"}
      ],
      "current_step": 2,
      "resume_hint": "继续修改 auth.py, 第 150 行开始"
    }

t3: [进程崩溃]

t4: 重启进程
    > spawn alice as coder: "任何任务"  # prompt 可以随便写

t5: alice 检测到未完成任务, 自动恢复
    [alice] Resuming from checkpoint...
    [系统恢复提示]
    你之前在执行任务: auth_refactor
    已完成步骤: 1/3
    当前步骤: 修改认证逻辑
    恢复建议: 继续修改 auth.py, 第 150 行开始

    请继续执行,不要重复已完成的工作。

t6: alice 从步骤 2 继续, 跳过步骤 1
    [alice] read_file: [CACHED]  # 幂等性保证,读取操作被跳过
    [alice] edit_file: 编辑成功   # 继续之前的工作
```

### 场景 2: 网络中断重连

```
时间线:

t0: alice 调用 LLM, 等待响应

t1: [网络中断, LLM 调用超时]

t2: 进程捕获异常, 保存检查点
    .checkpoints/alice_session.json:
    {
      "messages": [
        {"role": "user", "content": "重构认证模块"},
        {"role": "assistant", "content": "好的,我开始..."},
        # ... 已完成的消息 ...
      ]
    }

t3: 进程退出

t4: 网络恢复, 重启进程

t5: alice 从检查点恢复
    messages 从磁盘加载, 不需要重新生成
```

### 场景 3: 重复操作幂等性

```
时间线:

t0: alice 执行 write_file("config.json", "...")

t1: 文件写入成功, 保存操作记录
    .checkpoints/alice_ops.json:
    {
      "call_abc123": {
        "result": "Wrote 100 bytes",
        "timestamp": "..."
      }
    }

t2: [中断]

t3: 恢复执行, 遇到相同的 write_file 调用

t4: 检查 call_abc123 已完成, 跳过实际写入
    [alice] write_file: [CACHED] "Wrote 100 bytes"

结果: 文件只被写入一次, 避免重复副作用
```

---

## 相对 s10 的变更

| 组件 | 之前 (s10) | 之后 (s10 + Checkpoint) |
|------|-----------|------------------------|
| 会话状态 | 仅内存 | 内存 + 磁盘持久化 |
| 进度追踪 | 无 | steps + checkpoint |
| 幂等性 | 无保证 | tool_call_id 去重 |
| 恢复能力 | 无 | 自动检测 + 恢复提示 |
| 工具数量 | 12 | 13 (+update_progress) |
| 目录结构 | `.team/` | `.team/` + `.team/.checkpoints/` |

---

## 完整实现示例

```python
#!/usr/bin/env python3
"""
s10_team_protocols.py 的扩展: Checkpoint Recovery

在原有 s10 基础上增加:
1. CheckpointManager - 检查点持久化管理
2. 幂等性保证 - 避免重复执行
3. 自动恢复 - 检测未完成任务并续跑

存储结构:
    .team/
    ├── config.json              # 团队配置
    ├── inbox/                   # 消息收件箱
    └── .checkpoints/            # 新增: 检查点目录
        ├── alice_session.json   # alice 的会话
        ├── alice_progress.json  # alice 的进度
        └── alice_ops.json       # alice 的操作记录
"""

# ... 原有 s10 代码 ...

# === 新增: CheckpointManager 类 (见上文实现) ===

# === 修改: _teammate_loop 集成检查点 (见上文实现) ===

# === 新增: update_progress 工具 ===
{
    "name": "update_progress",
    "description": "更新任务进度, 设置检查点。用于长任务的断点续跑。",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "desc": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        "checkpoint": {"type": "string"}
                    }
                }
            },
            "current_step": {"type": "integer"},
            "resume_hint": {"type": "string"}
        },
        "required": ["task_id", "steps", "current_step"]
    }
}
```

---

## 试一试

```sh
cd learn-claude-code
python agents/s10_team_protocols_checkpoint.py  # 假设的扩展版本
```

测试场景:

1. **中断恢复测试**
   ```
   > spawn alice as coder: "创建一个文件 test.txt, 然后写入内容, 最后读取验证"
   > # 在 alice 执行过程中 Ctrl+C 中断
   > # 重新启动
   > spawn alice as coder: "继续"
   > # 观察 alice 是否从断点续跑
   ```

2. **幂等性测试**
   ```
   > spawn bob as coder: "写入文件 a.txt, 然后再次写入相同内容"
   > # 中断后恢复
   > # 观察第二次写入是否被跳过 (CACHED)
   ```

3. **进度追踪测试**
   ```
   > spawn charlie as coder: "分3步完成任务: 1.创建目录 2.写入文件 3.验证"
   > # 每步完成后检查 .team/.checkpoints/charlie_progress.json
   ```

---

## 关键洞察

1. **状态外置**: 把会话状态从内存移到磁盘, 中断后可恢复
2. **幂等设计**: 操作 ID 去重, 避免重复副作用
3. **渐进恢复**: 不追求完美, "足够好"的恢复比没有恢复强
4. **最小检查点**: 关键节点保存, 不是每条消息都存
5. **失败安全**: 检查点保存失败不影响主流程

---

> **下一步**: s11 的自治代理 (Autonomous Agents) 将结合检查点机制, 让队友能独立管理自己的任务队列和恢复状态。