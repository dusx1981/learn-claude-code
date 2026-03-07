# s09: "一切皆工具" 本体论深度解析

> **注意**: 本文档是 s09-agent-teams.md 的配套深度分析, 聚焦"一切皆工具"的哲学基础与技术实现。

`s01 > s02 > s03 > s04 > s05 > s06 | s07 > s08 > [ s09 ] s10 > s11 > s12`

> *"工具是智能体唯一的抽象接口"* -- JSON Schema 驱动的类型安全 + JSONL 邮箱的原子交付。

## 问题

传统多智能体系统需要多层抽象：通信协议、消息格式、路由机制、状态管理。每层都需要独立的接口和序列化逻辑。智能体"知道"如何调用工具，但"不知道"如何与其他智能体通信 -- 这是两套完全不同的机制。

这种分裂导致：(1) 智能体需要学习多种交互模式，(2) 类型安全难以保证，(3) 消息协议与工具协议分离，(4) 扩展新交互类型需要修改多处代码。

关键问题：**能否用单一抽象统一所有智能体交互？**

## 解决方案

s09 的答案是"一切皆工具"(Everything is a Tool)。智能体不需要区分"调用工具"和"发送消息" -- 两者都是通过工具调用完成的。

```
+----------------+     +-------------------+     +------------------+
|   Agent Loop   | --> |  Tool Dispatch    | --> |   Handler        |
|                |     |  TOOL_HANDLERS    |     |   _run_bash()    |
|  finish_reason |     |  {                |     |   _run_read()    |
|  == "tool_     |     |   bash: ...       |     |   BUS.send()     |
|  calls"        |     |   send_message:   |     |   TEAM.spawn()   |
|                |     |   spawn_teammate: |     |   ...            |
|                |     |  }                |     |                  |
+----------------+     +-------------------+     +------------------+
         ^                       |                        |
         |                       +------------------------+
         |                                   |
         +------- tool_result <-------------+

所有交互都通过工具调用。工具是唯一的抽象接口。
```

核心设计原则：

1. **工具是通用接口**：智能体不需要知道工具背后是本地函数、网络调用还是其他智能体
2. **Schema 驱动精度**：JSON Schema 在工具定义时强制执行类型约束
3. **枚举约束**：VALID_MSG_TYPES 模式确保消息类型安全
4. **JSONL 邮箱**：append-only、drain-on-read 实现原子交付
5. **工具分发模式**：TOOL_HANDLERS 字典作为路由机制

## 工作原理

### 1. 工具作为通用接口 (Tool as Universal Interface)

s09 中的智能体 loop 与 s01 完全一致。唯一的区别是 TOOLS 列表和 TOOL_HANDLERS 字典的内容扩大了。

```python
# s09_agent_teams.py:368-411
def agent_loop(messages: list):
    while True:
        # 检查收件箱 -- 这也是通过工具调用完成的
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
        
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM}] + messages,
            tools=TOOLS,  # 工具列表包含 9 个工具
            max_tokens=8000,
        )
        choice = response.choices[0]
        messages.append({"role": "assistant", "content": choice.message.content})
        
        if choice.finish_reason != "tool_calls":
            return
        
        # 工具执行 -- 统一的 dispatch 模式
        for tool_call in choice.message.tool_calls:
            function_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
            handler = TOOL_HANDLERS.get(function_name)
            output = handler(**arguments) if handler else f"Unknown tool: {function_name}"
            messages.append({"role": "tool", "content": str(output)})
```

智能体不需要知道 `send_message` 背后是 MessageBus 的 `send()` 方法，也不需要知道 `spawn_teammate` 背后是 TeammateManager 的 `spawn()` 方法。对智能体而言，所有工具都是：(1) 一个名称，(2) 一组参数，(3) 一个返回结果。

```
智能体视角的工具调用:

+------------------+     +----------------+     +------------------+
| "send_message"   | --> | {"to": "alice",| --> | "Sent message    |
| (工具名称)       |     |  "content":    |     |  to alice"       |
|                  |     |  "fix bug"}    |     | (返回结果)       |
+------------------+     +----------------+     +------------------+

智能体不知道也不关心背后发生了什么。工具是黑盒接口。
```

### 2. Schema 驱动的精度 (Schema-Driven Precision)

每个工具定义都包含完整的 JSON Schema。这确保了 LLM 生成的参数具有类型安全性。

```python
# s09_agent_teams.py:345-364
TOOLS = _to_openai_tools([
    {"name": "send_message", "description": "Send a message to a teammate's inbox.",
     "input_schema": {
         "type": "object",
         "properties": {
             "to": {"type": "string"},
             "content": {"type": "string"},
             "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}
         },
         "required": ["to", "content"]
     }},
    {"name": "spawn_teammate", "description": "Spawn a persistent teammate...",
     "input_schema": {
         "type": "object",
         "properties": {
             "name": {"type": "string"},
             "role": {"type": "string"},
             "prompt": {"type": "string"}
         },
         "required": ["name", "role", "prompt"]
     }},
    # ... 其他 7 个工具
])
```

JSON Schema 的作用：

- `type: "string"` 确保参数是字符串而非数字或对象
- `required` 数组确保必填参数不会遗漏
- `enum` 约束将值限制在预定义的集合内

```
JSON Schema 验证流程:

LLM 生成参数          Schema 验证          传递给 handler
+---------------+    +---------------+    +---------------+
| {"to": 123,   | -> | VALIDATION    | -> | (不会执行)    |
|  "content":   |    | FAILED        |    |               |
|  "hello"}     |    | "to" must be  |    |               |
+---------------+    | string        |    +---------------+
                     +---------------+

+---------------+    +---------------+    +---------------+
| {"to": "bob", | -> | VALIDATION    | -> | handler(      |
|  "content":   |    | PASSED        |    |  to="bob",    |
|  "hello"}     |    |               |    |  content=     |
+---------------+    +---------------+    |  "hello")     |
                                          +---------------+
```

### 3. 枚举约束模式 (Enum Constraints: VALID_MSG_TYPES)

s09 定义了 5 种消息类型，全部通过 `VALID_MSG_TYPES` 集合声明：

```python
# s09_agent_teams.py:68-74
VALID_MSG_TYPES = {
    "message",                # 普通文本消息
    "broadcast",              # 广播给所有队友
    "shutdown_request",       # 请求优雅关闭 (s10)
    "shutdown_response",      # 批准/拒绝关闭 (s10)
    "plan_approval_response", # 批准/拒绝计划 (s10)
}
```

这个集合在两个地方使用：

**位置 1：send_message 工具的 Schema 约束**

```python
# s09_agent_teams.py:257 (队友工具定义)
{"name": "send_message", "description": "Send message to a teammate.",
 "input_schema": {
     "type": "object",
     "properties": {
         "to": {"type": "string"},
         "content": {"type": "string"},
         "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}  # <-- 枚举约束
     },
     "required": ["to", "content"]
 }}
```

**位置 2：MessageBus.send() 的运行时验证**

```python
# s09_agent_teams.py:98-114
def send(self, sender: str, to: str, content: str,
         msg_type: str = "message", extra: dict = None) -> str:
    if msg_type not in VALID_MSG_TYPES:  # <-- 运行时验证
        return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
    msg = {
        "type": msg_type,
        "from": sender,
        "content": content,
        "timestamp": time.time(),
    }
    if extra:
        msg.update(extra)
    inbox_path = self.dir / f"{to}.jsonl"
    with open(inbox_path, "a") as f:
        f.write(json.dumps(msg) + "\n")
    return f"Sent {msg_type} to {to}"
```

双重验证模式：

```
+------------------+     +------------------+     +------------------+
| LLM 生成 msg_type | --> | JSON Schema 验证 | --> | 运行时验证       |
| "invalid_type"   |     | (在 API 层面)     |     | (在 send() 内)   |
+------------------+     +------------------+     +------------------+
                                |                        |
                                v                        v
                         如果失败：API 返回错误     如果失败：返回错误字符串

两层防御确保类型安全。
```

### 4. 类型化消息协议 (Typed Message Protocol)

s09 的消息格式是严格结构化的：

```python
msg = {
    "type": msg_type,       # 消息类型，来自 VALID_MSG_TYPES
    "from": sender,         # 发送者名称
    "content": content,     # 消息内容 (字符串)
    "timestamp": time.time(), # Unix 时间戳
}
```

这个协议的关键特性：

- **type**：接收者可以基于类型决定如何处理消息
- **from**：支持回复和溯源
- **content**：统一为字符串，简化处理
- **timestamp**：支持时序分析和调试

```
消息在 JSONL 邮箱中的存储格式:

{"type": "message", "from": "alice", "content": "fix bug", "timestamp": 1709856000.123}
{"type": "broadcast", "from": "lead", "content": "status update", "timestamp": 1709856010.456}
{"type": "shutdown_request", "from": "bob", "content": "task complete", "timestamp": 1709856020.789}

每个队友的收件箱是一个 append-only 的 JSONL 文件。
```

### 5. JSONL 邮箱模式 (JSONL Mailbox Pattern)

MessageBus 实现了基于文件的 JSONL 邮箱：

```python
# s09_agent_teams.py:92-133
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        # ... 验证和构建消息 ...
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:  # <-- 追加模式
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))
        inbox_path.write_text("")  # <-- 读取后清空 (drain)
        return messages
```

关键设计决策：

| 特性              | 实现方式          | 优势                                     |
|-------------------|-------------------|------------------------------------------|
| Append-only       | `open(..., "a")`  | 无需锁，写操作天然原子                   |
| Drain-on-read     | `write_text("")`  | 确保消息不重复消费                       |
| 每队友独立文件    | `{name}.jsonl`    | 无竞争，天然分区                         |
| JSON 序列化       | `json.dumps()`    | 跨语言兼容，人类可读                     |

```
JSONL 邮箱的原子交付保证:

发送者                              接收者
  |                                   |
  | send("alice", "hello")            |
  |--------------------------------->|
  |  alice.jsonl += "..."             |
  |                                   |
  |                            read_inbox("alice")
  |<---------------------------------|
  |  返回所有消息                     |
  |  alice.jsonl = "" (清空)         |
  |                                   |
  | send("alice", "world")            |
  |--------------------------------->|
  |  alice.jsonl += "..."             |
  |                                   |

每条消息只被消费一次。发送和读取在不同线程中安全运行。
```

### 6. 工具分发模式 (Tool Dispatch Pattern)

TOOL_HANDLERS 字典是 s09 的路由核心：

```python
# s09_agent_teams.py:332-343
TOOL_HANDLERS = {
    "bash":            lambda **kw: _run_bash(kw["command"]),
    "read_file":       lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file":      lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file":       lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "spawn_teammate":  lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":  lambda **kw: TEAM.list_all(),
    "send_message":    lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":      lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":       lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
}
```

分发流程：

```
LLM 返回 tool_calls
       |
       v
+--------------+
| tool_call[0] |
| name="bash"  |
+--------------+
       |
       v
handler = TOOL_HANDLERS.get("bash")  # 查找
       |
       v
output = handler(command="ls -la")   # 调用
       |
       v
返回结果给 LLM
```

lambda 包装器的作用是将不同签名的函数统一为 `**kw` 接口。这样 agent_loop 只需要：

```python
handler = TOOL_HANDLERS.get(function_name)
output = handler(**arguments)  # 统一调用接口
```

### 7. 智能体循环集成 (Agent Loop Integration)

对队友智能体而言，工具是它们的"肢体"。队友 loop 通过工具与外界交互：

```python
# s09_agent_teams.py:181-241
def _teammate_loop(self, name: str, role: str, prompt: str):
    sys_prompt = (
        f"You are '{name}', role: {role}, at {WORKDIR}. "
        f"Use send_message to communicate. Complete your task."
    )
    messages = [{"role": "user", "content": prompt}]
    tools = self._teammate_tools()  # 队友有 6 个工具
    
    for _ in range(50):
        # 步骤 1: 检查收件箱
        inbox = BUS.read_inbox(name)
        for msg in inbox:
            messages.append({"role": "user", "content": json.dumps(msg)})
        
        # 步骤 2: 调用 LLM
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": sys_prompt}] + messages,
            tools=tools,
            max_tokens=8000,
        )
        
        choice = response.choices[0]
        messages.append({"role": "assistant", "content": choice.message.content})
        
        # 步骤 3: 如果是工具调用，执行并继续循环
        if choice.finish_reason != "tool_calls":
            break
        
        results = []
        for tool_call in choice.message.tool_calls:
            function_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
            output = self._exec(name, function_name, arguments)  # <-- 队友的 dispatch
            results.append({"role": "tool", "content": str(output)})
        messages.extend(results)
    
    # 步骤 4: 状态变为 idle
    member = self._find_member(name)
    if member and member["status"] != "shutdown":
        member["status"] = "idle"
        self._save_config()
```

队友的 `_exec()` 方法是一个简化的 dispatch：

```python
# s09_agent_teams.py:223-235
def _exec(self, sender: str, tool_name: str, args: dict) -> str:
    if tool_name == "bash":
        return _run_bash(args["command"])
    if tool_name == "read_file":
        return _run_read(args["path"])
    if tool_name == "write_file":
        return _run_write(args["path"], args["content"])
    if tool_name == "edit_file":
        return _run_edit(args["path"], args["old_text"], args["new_text"])
    if tool_name == "send_message":
        return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
    if tool_name == "read_inbox":
        return json.dumps(BUS.read_inbox(sender), indent=2)
    return f"Unknown tool: {tool_name}"
```

队友的工具集 (6 个) 比领导 (9 个) 少，因为队友不需要 spawn_teammate、list_teammates、broadcast：

```python
# s09_agent_teams.py:237-260
def _teammate_tools(self) -> list:
    anthropic_tools = [
        {"name": "bash", "description": "Run a shell command.",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "description": "Read file contents.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "write_file", "description": "Write content to file.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
        {"name": "edit_file", "description": "Replace exact text in file.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
        {"name": "send_message", "description": "Send message to a teammate.",
         "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
        {"name": "read_inbox", "description": "Read and drain your inbox.",
         "input_schema": {"type": "object", "properties": {}}},
    ]
    return _to_openai_tools(anthropic_tools)
```

```
领导 vs 队友的工具集对比:

领导 (9 工具)                          队友 (6 工具)
+------------------------+           +------------------------+
| bash                   |           | bash                   |
| read_file              |           | read_file              |
| write_file             |           | write_file             |
| edit_file              |           | edit_file              |
| spawn_teammate  <--    |           |                        | 队友不能创建新队友
| list_teammates  <--    |           |                        | 队友不能列出团队
| send_message           |           | send_message           |
| read_inbox             |           | read_inbox             |
| broadcast       <--    |           |                        | 队友不能广播
+------------------------+           +------------------------+

工具集定义了角色的能力边界。
```

## 精确交互如何实现

s09 通过以下机制确保智能体之间的精确信息交互：

### 机制 1: Schema 强制类型安全

LLM 生成的每个参数都经过 JSON Schema 验证。如果类型错误，API 会拒绝请求。

```python
# 错误示例：to 参数不是字符串
{"to": 123, "content": "hello"}
# OpenAI API 返回错误：Invalid type for 'to'

# 正确示例
{"to": "alice", "content": "hello"}
# 通过验证，传递给 handler
```

### 机制 2: 枚举约束消息类型

VALID_MSG_TYPES 确保消息类型只能是预定义的 5 种之一。这防止了"类型漂移"。

```python
# 错误示例：自定义消息类型
{"to": "bob", "content": "urgent", "msg_type": "urgent_message"}
# 验证失败：'urgent_message' not in {'message', 'broadcast', ...}

# 正确示例：使用预定义类型
{"to": "bob", "content": "urgent", "msg_type": "message"}
# 或者使用 broadcast 表示广播
{"content": "urgent", "msg_type": "broadcast"}
```

### 机制 3: 结构化消息格式

所有消息都有相同的结构，接收者可以可靠地解析和处理：

```python
# 接收到的消息总是这个格式
{
    "type": "message",        # 可以用 type 字段做 switch
    "from": "alice",          # 可以回复给 from
    "content": "fix bug",     # 字符串，直接显示或处理
    "timestamp": 1709856000.123  # 可以排序或超时
}
```

队友 loop 处理收件箱的方式：

```python
# s09_agent_teams.py:189-191
inbox = BUS.read_inbox(name)
for msg in inbox:
    messages.append({"role": "user", "content": json.dumps(msg)})
```

每条消息被序列化为 JSON 字符串注入 LLM 上下文。LLM 看到：

```
{"type": "message", "from": "alice", "content": "fix the bug in main.py", "timestamp": 1709856000.123}
```

LLM 可以理解这个消息的语义并决定如何响应。

### 机制 4: 原子交付保证

JSONL 邮箱的 append-only + drain-on-read 确保：

1. **不丢失**：发送是追加写入，不会覆盖已有消息
2. **不重复**：读取后清空，确保每条消息只消费一次
3. **无竞争**：每个队友有独立文件，多线程安全

```
并发场景下的安全性:

线程 A (alice)                          线程 B (bob)
  | send("bob", "msg1")                    |
  |--------------------------------------->|
  |  bob.jsonl += "msg1\n"                 |
  |                                        | send("alice", "msg2")
  |<---------------------------------------|
  |  alice.jsonl += "msg2\n"               |
  |                                        |
  |                              read_inbox("bob")
  |<---------------------------------------|
  |  返回 ["msg1"]                         |
  |  bob.jsonl = ""                        |
  |                                        |
  | send("bob", "msg3")                    |
  |--------------------------------------->|
  |  bob.jsonl += "msg3\n"                 |

无锁，无竞争，无消息丢失。
```

### 机制 5: 工具作为能力边界

队友和领导有不同的工具集。这定义了角色的能力边界：

- 领导可以创建队友、广播消息
- 队友只能发送点对点消息、读取自己的收件箱

这种设计防止了权限漂移和意外操作。

## ASCII 架构图

### 整体架构

```
+-----------------------------------------------------------+
|                    s09 Agent Teams                         |
+-----------------------------------------------------------+
|                                                            |
|  +---------------+       +-------------------+            |
|  |  Lead Agent   |       |  TeammateManager  |            |
|  |    (loop)     |<----->|  (config.json)    |            |
|  +-------+-------+ spawn |  + members[]      |            |
|          |               |  + statuses       |            |
|          | tools         +-------------------+            |
|          v                                                 |
|  +---------------+                                        |
|  | TOOL_HANDLERS |                                        |
|  | 9 handlers    |                                        |
|  +-------+-------+                                        |
|          |                                                 |
|          | dispatch                                        |
|          v                                                 |
|  +---------------+       +-------------------+            |
|  | MessageBus    |<----->|  JSONL Inboxes    |            |
|  | send()/read() |       |  alice.jsonl      |            |
|  +---------------+       |  bob.jsonl        |            |
|                          |  lead.jsonl       |            |
|                          +-------------------+            |
|                                                            |
|  +------------------------------------------------------+  |
|  |              Teammate Threads (N 个)                  |  |
|  |  +----------+  +----------+  +----------+            |  |
|  |  |  alice   |  |   bob    |  |   ...    |            |  |
|  |  |  (loop)  |  |  (loop)  |  |  (loop)  |            |  |
|  |  +----+-----+  +----+-----+  +----+-----+            |  |
|  |       |             |             |                   |  |
|  |       +-------------+-------------+                   |  |
|  |                     |                                 |  |
|  |             _teammate_tools() (6 工具)                |  |
|  |                     |                                 |  |
|  |                     v                                 |  |
|  |             _exec() dispatch                          |  |
|  +------------------------------------------------------+  |
|                                                            |
+-----------------------------------------------------------+
```

### 消息流动

```
场景：Lead 发送消息给 Alice, Alice 处理并回复

Step 1: Lead 调用 send_message 工具
+--------+     send_message(to="alice", content="fix bug")     +-----------+
|  Lead  | --------------------------------------------------->|MessageBus |
+--------+                                                     +-----+-----+
                                                                      |
                                                                      | append to alice.jsonl
                                                                      v
                                                            +-----------------+
                                                            | alice.jsonl     |
                                                            | {"type":...}    |
                                                            +-----------------+

Step 2: Alice 的 loop 读取收件箱
+--------+     read_inbox("alice")                           +-----------+
| Alice  | <-------------------------------------------------|MessageBus |
+--------+     返回所有消息，清空文件                        +-----------+
      |
      | 将消息注入 LLM 上下文
      v
+-------------+
| LLM 响应     |
| tool_call:  |
| send_message|
+------+------+
       |
       | 回复给 Lead
       v
+--------+     send_message(to="lead", content="done")       +-----------+
| Alice  | ------------------------------------------------->|MessageBus |
+--------+                                                   +-----+-----+
                                                                    |
                                                                    | append to lead.jsonl
                                                                    v
                                                          +-----------------+
                                                          | lead.jsonl      |
                                                          | {"type":...}    |
                                                          +-----------------+

Step 3: Lead 下次循环读取收件箱
+--------+     read_inbox("lead")                            +-----------+
|  Lead  | <-------------------------------------------------|MessageBus |
+--------+     收到 Alice 的回复                             +-----------+
```

### 工具分发层次

```
Level 1: Lead Agent
+-------------------+
| TOOLS (9 个)      |
| +-----------------+
| | name: "bash"    |
| | name: "spawn_   |
| | teammate"       |
| | name: "send_    |
| | message"        |
| | ...             |
+-------------------+
        |
        | LLM 返回 tool_calls
        v
+-------------------+
| TOOL_HANDLERS     |
| {                 |
|   "bash": lambda, |
|   "spawn_...      |
| }                 |
+-------------------+
        |
        | handler(**args)
        v
+-------------------+
| _run_bash()       |
| TEAM.spawn()      |
| BUS.send()        |
+-------------------+

Level 2: Teammate
+-------------------+
| _teammate_tools() |
| (6 个)            |
| +-----------------+
| | name: "bash"    |
| | name: "send_    |
| | message"        |
| | name: "read_    |
| | inbox"          |
| | ...             |
+-------------------+
        |
        | LLM 返回 tool_calls
        v
+-------------------+
| _exec()           |
| if tool_name ==   |
|   "bash": ...     |
|   "send_message": |
|     BUS.send()    |
+-------------------+
        |
        | 直接调用
        v
+-------------------+
| _run_bash()       |
| BUS.send()        |
+-------------------+
```

## 代码示例

### 示例 1: 使用 send_message 工具

```python
# s09_agent_teams.py:98-114
def send(self, sender: str, to: str, content: str,
         msg_type: str = "message", extra: dict = None) -> str:
    # 第 1 行：验证消息类型
    if msg_type not in VALID_MSG_TYPES:
        return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
    
    # 第 2-6 行：构建消息对象
    msg = {
        "type": msg_type,
        "from": sender,
        "content": content,
        "timestamp": time.time(),
    }
    if extra:
        msg.update(extra)
    
    # 第 7-9 行：追加到 JSONL 文件
    inbox_path = self.dir / f"{to}.jsonl"
    with open(inbox_path, "a") as f:
        f.write(json.dumps(msg) + "\n")
    
    return f"Sent {msg_type} to {to}"
```

### 示例 2: read_inbox 的 drain-on-read

```python
# s09_agent_teams.py:116-125
def read_inbox(self, name: str) -> list:
    inbox_path = self.dir / f"{name}.jsonl"
    if not inbox_path.exists():
        return []
    
    messages = []
    # 逐行读取并解析 JSON
    for line in inbox_path.read_text().strip().splitlines():
        if line:
            messages.append(json.loads(line))
    
    # 关键：读取后清空文件
    inbox_path.write_text("")
    return messages
```

### 示例 3: 队友的 send_message 处理

```python
# s09_agent_teams.py:230-231
if tool_name == "send_message":
    return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
```

注意 `sender` 参数来自队友的名字，确保 `from` 字段正确。

### 示例 4: TOOL_HANDLERS 的 lambda 包装

```python
# s09_agent_teams.py:332-343
TOOL_HANDLERS = {
    # bash 需要一个 command 参数
    "bash": lambda **kw: _run_bash(kw["command"]),
    
    # read_file 需要 path 和可选的 limit 参数
    "read_file": lambda **kw: _run_read(kw["path"], kw.get("limit")),
    
    # send_message 需要 to 和 content, 可选 msg_type
    "send_message": lambda **kw: BUS.send(
        "lead",  # sender 固定为 "lead"
        kw["to"],
        kw["content"],
        kw.get("msg_type", "message")
    ),
}
```

## 总结

s09 的"一切皆工具"本体论将多智能体协作简化为单一抽象：

| 传统方法                 | s09 方法              |
|--------------------------|-----------------------|
| 通信协议 + 工具 API 两套   | 只有工具 API          |
| 消息格式独立定义         | JSON Schema 定义      |
| 路由机制单独实现         | TOOL_HANDLERS 字典    |
| 类型安全靠文档保证       | Schema + enum 强制执行|
| 并发通信需要锁           | JSONL append-only     |

核心洞察：**智能体不需要知道工具背后的实现细节。工具是黑盒，是智能体与外界交互的唯一接口。**

这种设计使得：
- 添加新交互类型 = 添加新工具
- 修改通信协议 = 修改工具 Schema
- 扩展智能体能力 = 扩展工具集
- 保证类型安全 = 强化 Schema 约束

s09 为 s10(s10: Team Protocols) 和 s11(s11: Autonomous Agents) 奠定了基础：所有高级协议都构建在这个"一切皆工具"的核心抽象之上。
