# s09_agent_teams.py 设计思想与实现分析

## 一、设计思想

### 1.1 核心动机

s09 实现了 **Agent Teams（智能体团队）** 机制，核心目标是：

> **"When the task is too big for one, delegate to teammates"**
> — 当任务太大一个人做不完时，委托给队友

从 s04 的临时子 agent 进化到持久化的团队成员，实现了真正的多智能体协作。

### 1.2 关键范式转变

| 版本 | 生命周期 | 状态 | 通信方式 |
|-----|---------|------|---------|
| s04 (Subagent) | 临时 | 创建→执行→返回→销毁 | 共享 messages 列表 |
| s09 (Teammate) | 持久 | 工作→空闲→工作→...→关闭 | 独立收件箱 (JSONL) |

### 1.3 架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                          主线程 (Lead)                               │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  agent_loop()                                               │   │
│  │  • 读取 lead 收件箱                                         │   │
│  │  • 与 LLM 交互                                              │   │
│  │  • 调用工具（spawn_teammate, send_message, broadcast）       │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
                    ▼               ▼               ▼
            ┌───────────┐   ┌───────────┐   ┌───────────┐
            │  alice    │   │    bob    │   │   carol   │
            │ (thread)  │   │ (thread)  │   │ (thread)  │
            ├───────────┤   ├───────────┤   ├───────────┤
            │ agent_loop│   │ agent_loop│   │ agent_loop│
            │  status   │   │  status   │   │  status   │
            │ working  │   │   idle    │   │   idle    │
            └───────────┘   └───────────┘   └───────────┘
                    │               │               │
                    └───────────────┼───────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       .team/ 目录结构                                │
├─────────────────────────────────────────────────────────────────────┤
│  .team/                                                              │
│  ├── config.json           # 团队配置 + 成员状态                      │
│  └── inbox/                                                         │
│      ├── lead.jsonl        # lead 的收件箱                           │
│      ├── alice.jsonl       # alice 的收件箱                          │
│      ├── bob.jsonl         # bob 的收件箱                            │
│      └── carol.jsonl      # carol 的收件箱                         │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 二、实现机制

### 2.1 MessageBus（消息总线）

基于 JSONL 文件的异步通信机制：

```python
class MessageBus:
    def send(self, sender, to, content, msg_type="message"):
        # 消息格式
        msg = {
            "type": msg_type,           # message / broadcast / shutdown_request 等
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        # 追加写入到目标收件箱
        with open(f"{to}.jsonl", "a") as f:
            f.write(json.dumps(msg) + "\n")

    def read_inbox(self, name):
        # 读取并清空收件箱
        messages = [json.loads(line) for line in inbox]
        inbox.write_text("")  # 清空
        return messages

    def broadcast(self, sender, content, teammates):
        # 广播给所有队友
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
```

**关键特性**：
- **持久化**：消息存储在文件中，重启不丢失
- **异步**：发送方不阻塞，等接收方主动读取
- **只读一次**：`read_inbox` 会清空收件箱（drain 模式）

### 2.2 TeammateManager（队友管理器）

管理持久化队友的生命周期：

```python
class TeammateManager:
    def spawn(self, name, role, prompt):
        # 1. 检查成员是否已存在
        # 2. 更新状态为 "working"
        # 3. 启动守护线程
        thread = threading.Thread(target=self._teammate_loop, args=(name, role, prompt))
        thread.start()

    def _teammate_loop(self, name, role, prompt):
        # 每个队友独立的 agent_loop
        messages = [{"role": "user", "content": prompt}]
        for _ in range(50):
            # 1. 检查自己的收件箱
            inbox = BUS.read_inbox(name)
            messages.extend(inbox)
            
            # 2. 调用 LLM
            response = client.chat.completions.create(...)
            
            # 3. 执行工具
            for tool_call in response.tool_calls:
                output = self._exec(name, tool_call.function.name, args)
            
            # 4. 如果没有 tool_calls，退出
            if not response.tool_calls:
                break
        
        # 5. 任务完成，状态设为 idle
        member["status"] = "idle"
```

**队友生命周期**：

```
spawn(name="alice", role="coder", prompt="...")
    │
    ▼
┌─────────────────────────────────┐
│ 状态: working                   │
│ 线程: alice 运行 agent_loop    │
│ 收件箱: 不断检查新消息          │
└─────────────────────────────────┘
    │
    │ (LLM 返回 text，无 tool_calls)
    ▼
┌─────────────────────────────────┐
│ 状态: idle                      │
│ 线程: 退出                      │
│ (等待被再次唤醒或 shutdown)    │
└─────────────────────────────────┘
```

### 2.3 消息类型

| 消息类型 | 用途 | 后续处理 |
|---------|------|---------|
| `message` | 普通点对点消息 | 作为 user 消息注入 |
| `broadcast` | 广播给所有队友 | 作为 user 消息注入 |
| `shutdown_request` | 请求关闭 (s10) | s10 处理 |
| `shutdown_response` | 关闭响应 (s10) | s10 处理 |
| `plan_approval_response` | 计划审批 (s10) | s10 处理 |

---

## 三、主要功能

### 3.1 Lead 可用的工具（9个）

| 工具名称 | 功能描述 | 示例 |
|---------|---------|------|
| `bash` | 执行 shell 命令 | `bash("ls -la")` |
| `read_file` | 读取文件 | `read_file("src/main.py")` |
| `write_file` | 写入文件 | `write_file("test.py", "...")` |
| `edit_file` | 编辑文件 | `edit_file("foo.py", "old", "new")` |
| `spawn_teammate` | 创建持久化队友 | `spawn_teammate("alice", "coder", "fix the bug")` |
| `list_teammates` | 列出所有队友 | `list_teammates()` |
| `send_message` | 发送消息给队友 | `send_message("alice", "进展如何?")` |
| `read_inbox` | 读取自己收件箱 | `read_inbox()` |
| `broadcast` | 广播消息给所有人 | `broadcast("大家开始工作!")` |

### 3.2 Teammate 可用的工具（6个）

| 工具名称 | 功能描述 |
|---------|---------|
| `bash` | 执行 shell 命令 |
| `read_file` | 读取文件 |
| `write_file` | 写入文件 |
| `edit_file` | 编辑文件 |
| `send_message` | 发送消息给其他队友 |
| `read_inbox` | 读取自己收件箱 |

### 3.3 团队配置

```json
// .team/config.json
{
  "team_name": "default",
  "members": [
    {"name": "alice", "role": "coder", "status": "idle"},
    {"name": "bob", "role": "reviewer", "status": "idle"}
  ]
}
```

---

## 四、任务分配机制

### 4.1 任务分配的三种方式

在 s09 中，Lead 可以通过三种方式给队友分配任务：

#### 方式一：spawn_teammate（创建时分配）

```python
# 工具调用
spawn_teammate(
    name="alice",           # 队友名称
    role="backend",        # 角色定义
    prompt="重构 src/auth/core.py，实现用户认证逻辑"  # 具体任务
)
```

**执行流程**：

```
1. Lead 调用 spawn_teammate 工具
   ↓
2. TeammateManager.spawn() 执行：
   • 检查成员是否存在
   • 更新 config.json 中状态为 "working"
   • 创建新线程，运行 _teammate_loop
   ↓
3. 新线程中：
   • 初始化 messages = [{"role": "user", "content": prompt}]
   • 进入 agent_loop 循环
   • 调用 LLM 执行任务
   ↓
4. 任务完成：
   • LLM 返回文本（无 tool_calls）
   • 状态更新为 "idle"
   • 线程退出
```

**消息流转**：

```
┌─────────────────────────────────────────────────────────┐
│  spawn_teammate 消息流                                   │
├─────────────────────────────────────────────────────────┤
│                                                         │
│   Lead 的 prompt         队友的初始 messages              │
│  ┌──────────────┐      ┌──────────────────────────┐    │
│  │ "重构 auth    │  →   │ [                      │    │
│  │  模块..."     │      │   {role: "system",      │    │
│  └──────────────┘      │          content: "..."}, │    │
│                        │   {role: "user",         │    │
│                        │          content: prompt} │    │
│                        │ ]                        │    │
│                        └──────────────────────────┘    │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

#### 方式二：send_message（运行时分配）

对于已存在的队友，可以通过消息分配新任务：

```python
# 队友已存在且状态为 idle
send_message(
    to="alice",
    content="还有另一个模块需要重构: src/auth/oauth.py"
)
```

**执行流程**：

```
1. Lead 调用 send_message 工具
   ↓
2. MessageBus.send() 执行：
   • 构造消息 {"type": "message", "from": "lead", "content": "...", "timestamp": ...}
   • 追加写入 alice.jsonl
   ↓
3. alice 的线程在下一轮 _teammate_loop 中：
   • BUS.read_inbox("alice") 读取消息
   • messages.extend(inbox)  注入到上下文
   • LLM 处理新任务
```

**消息格式**：

```json
// alice.jsonl 中的一条消息
{
  "type": "message",
  "from": "lead",
  "content": "还有另一个模块需要重构: src/auth/oauth.py",
  "timestamp": 1234567890.123
}
```

#### 方式三：broadcast（广播分配）

```python
broadcast(
    content="大家开始今天的任务：Alice 负责后端，Bob 负责前端"
)
```

### 4.2 队友接收任务的机制

每个队友的 `_teammate_loop` 不断轮询自己的收件箱：

```python
def _teammate_loop(self, name, role, prompt):
    # 初始任务（来自 spawn 的 prompt）
    messages = [{"role": "user", "content": prompt}]
    
    for _ in range(50):  # 最多执行 50 轮
        # 关键：每轮都检查收件箱
        inbox = BUS.read_inbox(name)
        
        # 将新消息注入上下文
        for msg in inbox:
            messages.append({
                "role": "user", 
                "content": json.dumps(msg)
            })
        
        # 调用 LLM
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": sys_prompt}] + messages,
            tools=tools,
        )
        
        # 执行工具调用
        if choice.finish_reason != "tool_calls":
            break  # 任务完成，退出循环
    
    # 设置状态为 idle
    member["status"] = "idle"
```

**关键设计点**：

| 设计点 | 说明 |
|-------|------|
| 每轮都读收件箱 | 保证新任务能及时响应 |
| 消息 JSON 序列化 | 保持消息格式一致性 |
| 最多 50 轮 | 防止无限循环 |
| idle 状态 | 队友可以被重新唤醒 |

### 4.3 任务分配示例完整流程

```
场景：Lead 分配两个任务给不同队友

1. Lead: spawn_teammate(
          name="alice", 
          role="backend", 
          prompt="实现用户登录 API"
        )
   
   → 启动 alice 线程
   → alice.jsonl 创建（空）
   → config.json: alice.status = "working"

2. Lead: spawn_teammate(
          name="bob", 
          role="frontend", 
          prompt="实现登录页面 UI"
        )
   
   → 启动 bob 线程
   → bob.jsonl 创建（空）
   → config.json: bob.status = "working"

3. alice 执行中... (独立线程)
   bob 执行中... (独立线程)

4. alice 完成任务：
   → LLM 返回文本
   → alice.status = "idle"
   → 线程退出

5. bob 完成任务：
   → LLM 返回文本
   → bob.status = "idle"
   → 线程退出

6. Lead 可以继续分配新任务：
   send_message(to="alice", content="新任务: ...")
```

### 4.4 任务状态的可见性

```python
# 查看团队状态
list_teammates()

# 输出：
Team: default
  alice (backend): idle
  bob (frontend): working
```

config.json 记录了所有成员的状态，便于 Lead 了解团队情况。

---

## 五、执行流程示例

### 6.1 场景：Lead 分配任务给队友

```
用户: "用两个队友来重构 auth 模块，一个负责核心逻辑，一个负责测试"

Lead 的执行流程：

1. spawn_teammate(
     name="alice", 
     role="backend", 
     prompt="重构 src/auth/core.py，实现用户认证逻辑"
   )
   → 启动 alice 线程

2. spawn_teammate(
     name="bob", 
     role="tester", 
     prompt="为 src/auth/core.py 编写单元测试"
   )
   → 启动 bob 线程

3. 等待队友完成...

4. read_inbox()  →  检查是否有新消息

5. 收到 alice 消息: "完成核心逻辑"
6. 收到 bob 消息: "完成测试"
```

### 6.2 队友间通信

```
alice:
  send_message(to="bob", content="认证逻辑已完成，你可以开始写测试了")
  
  → 写入 bob.jsonl:
  {"type":"message","from":"alice","content":"...","timestamp":1234567890}

bob (在 agent_loop 中):
  inbox = read_inbox("bob")
  → 读取并清空 bob.jsonl
  → 作为 user 消息注入自己的 context
  → "收到 alice 的消息，继续工作"
```

---

## 六、与前序版本的对比

### 7.1 演进路径

```
s01-s04: 单 agent 循环
    ↓
s05-s08: 添加 skills、context压缩、tasks、background_tasks
    ↓
s09: 添加多 agent 团队协作
    ↓
s10-s12: 团队协议、工作流隔离
```

### 7.2 s04 vs s09 对比

| 特性 | s04 (Subagent) | s09 (Teammate) |
|-----|---------------|----------------|
| 生命周期 | 临时（单次任务） | 持久（可多次任务） |
| 状态管理 | 无 | idle/working/shutdown |
| 通信方式 | 返回值注入 | 异步消息（收件箱） |
| 并发性 | 无 | 多线程并行 |
| 适用场景 | 临时子任务 | 长期团队协作 |

---

## 七、核心价值

### 8.1 设计要点

1. **异步通信**：通过 JSONL 文件实现解耦，发送方和接收方不需要同时在线
2. **持久化状态**：队友状态记录在 config.json，可查看团队全貌
3. **线程隔离**：每个队友在独立线程中运行，互不干扰
4. **广播支持**：一对多通信场景

### 8.2 局限性（s10 解决）

- 没有 shutdown 机制（s10 添加）
- 没有任务审批流程（s10 添加）
- 队友只能被动的响应消息（s11 添加自动拾取）

---

## 八、文件结构

```
.team/
├── config.json              # 团队配置（自动创建）
└── inbox/
    ├── lead.jsonl           # lead 的收件箱
    ├── alice.jsonl           # alice 的收件箱
    ├── bob.jsonl             # bob 的收件箱
    └── ...                   # 其他成员
```

---

## 九、总结

s09 实现了从"单智能体"到"多智能体团队"的跃迁：

- **持久化**：队友不会在任务完成后销毁，可以复用
- **异步通信**：通过 JSONL 收件箱实现松耦合
- **状态可见**：team config 记录所有成员状态
- **可扩展**：可轻松添加新类型的消息协议

这是构建复杂多智能体系统的基础，后续 s10-s12 在此基础上添加协议、工作流隔离等机制。
