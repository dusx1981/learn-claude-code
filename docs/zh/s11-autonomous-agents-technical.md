# S11 Autonomous Agents 技术文档

## 目录

1. [概述](#概述)
2. [设计思想](#设计思想)
3. [实现机制](#实现机制)
4. [主要功能](#主要功能)
5. [核心组件详解](#核心组件详解)
6. [数据流分析](#数据流分析)
7. [演进对比](#演进对比)
8. [使用指南](#使用指南)

---

## 概述

`s11_autonomous_agents.py` 实现了**自治智能体系统**的完整架构，是整个 12-session 系列中的第 11 个里程碑。该模块在 s10 团队协议的基础上，引入了**空闲轮询机制**和**自动任务认领**能力，使智能体从"被动等待指派"演进为"主动发现工作"的自治形态。

**核心设计理念**：*"队友自己看看板，有活就认领"* —— 无需领导逐个分配，实现真正的自组织。

---

## 设计思想

### 1. 从被动到主动的范式转变

传统智能体（s01-s10）的执行模型是**响应式**的：
```
用户/领导输入 → 智能体处理 → 输出结果
```

s11 引入的自治模型是**主动式**的：
```
智能体处于空闲状态 → 主动扫描任务看板 → 发现可执行任务 → 自动认领并执行
```

这种转变的核心价值在于：
- **可扩展性**：单个领导可以管理数十个智能体，无需手动分配每个任务
- **自组织**：智能体根据任务依赖关系自动协调执行顺序
- **资源优化**：智能体在完成工作后不立即销毁，而是进入空闲等待，减少重复创建开销

### 2. 双阶段生命周期模型

智能体生命周期被明确划分为两个阶段：

```
┌─────────┐
│  SPAWN  │  初始化创建
└────┬────┘
     │
     v
┌─────────┐    ┌─────────┐
│  WORK   │───→│   LLM   │  执行阶段：主动调用工具完成工作
└────┬────┘    └─────────┘
     │
     │ stop_reason != "tool_calls" 或调用 idle 工具
     v
┌─────────┐
│  IDLE   │  空闲阶段：轮询收件箱和任务看板
└────┬────┘
     │
     ├─→ 收到消息 ─────────→ 回到 WORK
     ├─→ 发现未认领任务 ───→ 回到 WORK
     └─→ 60秒超时 ─────────→ SHUTDOWN
```

### 3. 上下文压缩后的身份保持

在 s06 上下文压缩机制中，当消息历史过长时会进行压缩（仅保留最近几条消息）。这会导致智能体**忘记自己的身份**（姓名、角色、团队）。

**解决方案**：身份重注入（Identity Re-injection）

当检测到消息历史过于简短（`len(messages) <= 3`）时，自动在对话开头插入身份块：

```python
messages.insert(0, {
    "role": "user",
    "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>"
})
messages.insert(1, {
    "role": "assistant",
    "content": f"I am {name}. Continuing."
})
```

### 4. 任务状态机与依赖管理

任务系统支持完整的依赖关系：

```
状态: pending → in_progress → completed
      ↑____________|

阻塞: blockedBy = [task_id1, task_id2]
      当所有前置任务 completed 时才可被认领
```

任务认领条件（必须同时满足）：
1. `status == "pending"`
2. `owner is None`
3. `blockedBy is None or all(blocked tasks completed)`

---

## 实现机制

### 1. 全局配置与常量

```python
# 轮询配置
POLL_INTERVAL = 5      # 空闲轮询间隔（秒）
IDLE_TIMEOUT = 60      # 空闲超时时间（秒）

# 目录结构
WORKDIR = Path.cwd()
TEAM_DIR = WORKDIR / ".team"           # 团队配置目录
INBOX_DIR = TEAM_DIR / "inbox"         # 消息收件箱目录
TASKS_DIR = WORKDIR / ".tasks"         # 任务看板目录

# LLM 配置
MODEL = os.environ.get("MODEL_ID", "qwen-max")
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
)
```

### 2. 消息总线 (MessageBus)

**设计模式**：发布-订阅模式 + 文件持久化

每个智能体拥有独立的 JSONL 格式收件箱（`{name}.jsonl`），消息采用**追加写**方式存储：

```python
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict | None = None) -> str:
        """发送消息到指定收件箱"""
        msg = {
            "type": msg_type,           # 消息类型
            "from": sender,             # 发送者
            "content": content,         # 内容
            "timestamp": time.time(),   # 时间戳
        }
        if extra:
            msg.update(extra)
        # 追加写入 JSONL
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")

    def read_inbox(self, name: str) -> list:
        """读取并清空收件箱（Drain 模式）"""
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = [json.loads(line) for line in inbox_path.read_text().strip().splitlines() if line]
        inbox_path.write_text("")  # 清空收件箱
        return messages
```

**消息类型定义**：
```python
VALID_MSG_TYPES = {
    "message",                # 普通消息
    "broadcast",             # 广播消息
    "shutdown_request",      # 关机请求（s10 协议）
    "shutdown_response",     # 关机响应（s10 协议）
    "plan_approval_response", # 计划审批响应（s10 协议）
}
```

### 3. 任务看板扫描

任务以 JSON 文件形式存储在 `.tasks/` 目录：

```python
def scan_unclaimed_tasks() -> list:
    """扫描所有可认领的任务"""
    TASKS_DIR.mkdir(exist_ok=True)
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        # 认领条件：pending + 无 owner + 无阻塞
        if (task.get("status") == "pending"
                and not task.get("owner")
                and not task.get("blockedBy")):
            unclaimed.append(task)
    return unclaimed


def claim_task(task_id: int, owner: str) -> str:
    """认领任务（线程安全）"""
    with _claim_lock:  # 互斥锁防止竞态条件
        path = TASKS_DIR / f"task_{task_id}.json"
        task = json.loads(path.read_text())
        task["owner"] = owner
        task["status"] = "in_progress"
        path.write_text(json.dumps(task, indent=2))
    return f"Claimed task #{task_id} for {owner}"
```

**任务文件格式示例**：
```json
{
  "id": 1,
  "subject": "Implement user authentication",
  "description": "Add JWT-based auth to API",
  "status": "pending",
  "owner": null,
  "blockedBy": [2, 3],
  "created_at": 1709823456.789
}
```

### 4. 智能体管理器 (TeammateManager)

#### 4.1 配置持久化

```python
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}  # 线程句柄缓存

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2))
```

**config.json 结构**：
```json
{
  "team_name": "my-team",
  "members": [
    {
      "name": "alice",
      "role": "coder",
      "status": "working"  // working | idle | shutdown
    }
  ]
}
```

#### 4.2 智能体生命周期控制

```python
def spawn(self, name: str, role: str, prompt: str) -> str:
    """创建并启动智能体线程"""
    member = self._find_member(name)
    if member:
        if member["status"] not in ("idle", "shutdown"):
            return f"Error: '{name}' is currently {member['status']}'"
        member["status"] = "working"
        member["role"] = role
    else:
        member = {"name": name, "role": role, "status": "working"}
        self.config["members"].append(member)
    self._save_config()

    # 启动守护线程
    thread = threading.Thread(
        target=self._loop,
        args=(name, role, prompt),
        daemon=True,
    )
    self.threads[name] = thread
    thread.start()
    return f"Spawned '{name}' (role: {role})"
```

#### 4.3 核心循环 (_loop)

```python
def _loop(self, name: str, role: str, prompt: str):
    team_name = self.config["team_name"]
    sys_prompt = (
        f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
        f"Use idle tool when you have no more work. You will auto-claim new tasks."
    )
    messages = [{"role": "user", "content": prompt}]
    tools = self._teammate_tools()

    while True:
        # ===== WORK PHASE =====
        for _ in range(50):  # 最多 50 轮工具调用
            # 检查收件箱
            inbox = BUS.read_inbox(name)
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    self._set_status(name, "shutdown")
                    return
                messages.append({"role": "user", "content": json.dumps(msg)})

            # LLM 调用
            try:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "system", "content": sys_prompt}] + messages,
                    tools=tools,
                    max_tokens=8000,
                )
            except Exception:
                self._set_status(name, "idle")
                return

            choice = response.choices[0]
            messages.append({
                "role": "assistant",
                "content": choice.message.content or "",
                "tool_calls": choice.message.tool_calls if choice.message.tool_calls else None
            })

            if choice.finish_reason != "tool_calls":
                break

            # 执行工具
            results = []
            idle_requested = False
            for tool_call in choice.message.tool_calls:
                function_name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments)

                if function_name == "idle":
                    idle_requested = True
                    output = "Entering idle phase. Will poll for new tasks."
                else:
                    output = self._exec(name, function_name, arguments)

                results.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": str(output),
                })
            messages.extend(results)

            if idle_requested:
                break

        # ===== IDLE PHASE =====
        self._set_status(name, "idle")
        resume = False
        polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)  # 12 次轮询

        for _ in range(polls):
            time.sleep(POLL_INTERVAL)

            # 检查收件箱
            inbox = BUS.read_inbox(name)
            if inbox:
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg)})
                resume = True
                break

            # 扫描任务看板
            unclaimed = scan_unclaimed_tasks()
            if unclaimed:
                task = unclaimed[0]
                claim_task(task["id"], name)
                task_prompt = (
                    f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                    f"{task.get('description', '')}</auto-claimed>"
                )

                # 身份重注入（关键机制）
                if len(messages) <= 3:
                    messages.insert(0, make_identity_block(name, role, team_name))
                    messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})

                messages.append({"role": "user", "content": task_prompt})
                messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                resume = True
                break

        if not resume:
            self._set_status(name, "shutdown")
            return
        self._set_status(name, "working")
```

### 5. 工具系统

#### 5.1 队友可用工具 (10个)

```python
def _teammate_tools(self) -> list:
    return [
        # 基础文件操作
        {"type": "function", "function": {"name": "bash", ...}},
        {"type": "function", "function": {"name": "read_file", ...}},
        {"type": "function", "function": {"name": "write_file", ...}},
        {"type": "function", "function": {"name": "edit_file", ...}},

        # 通信
        {"type": "function", "function": {"name": "send_message", ...}},
        {"type": "function", "function": {"name": "read_inbox", ...}},

        # 团队协议（s10）
        {"type": "function", "function": {"name": "shutdown_response", ...}},
        {"type": "function", "function": {"name": "plan_approval", ...}},

        # 自治功能（s11 新增）
        {"type": "function", "function": {"name": "idle", ...}},           # 进入空闲状态
        {"type": "function", "function": {"name": "claim_task", ...}},    # 手动认领任务
    ]
```

#### 5.2 领导可用工具 (14个)

在队友工具基础上增加：
```python
TOOL_HANDLERS = {
    # 基础工具（与队友共享）
    "bash": lambda **kw: _run_bash(kw["command"]),
    "read_file": lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),

    # 团队管理
    "spawn_teammate": lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates": lambda **kw: TEAM.list_all(),

    # 通信
    "send_message": lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox": lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast": lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),

    # 团队协议（s10）
    "shutdown_request": lambda **kw: handle_shutdown_request(kw["teammate"]),
    "shutdown_response": lambda **kw: _check_shutdown_status(kw.get("request_id", "")),
    "plan_approval": lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),

    # 自治功能
    "idle": lambda **kw: "Lead does not idle.",  # 领导不使用 idle
    "claim_task": lambda **kw: claim_task(kw["task_id"], "lead"),
}
```

### 6. 请求追踪器

用于实现团队协议中的请求-响应模式：

```python
# 关机请求追踪
shutdown_requests = {}
# 计划审批请求追踪
plan_requests = {}
# 线程安全锁
_tracker_lock = threading.Lock()


def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "Please shut down gracefully.",
             "shutdown_request", {"request_id": req_id})
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send("lead", req["from"], feedback, "plan_approval_response",
             {"request_id": request_id, "approve": approve, "feedback": feedback})
    return f"Plan {req['status']} for '{req['from']}'"
```

---

## 主要功能

### 1. 自治任务执行

```
功能描述: 智能体在空闲时自动扫描任务看板并认领任务
触发条件: IDLE 阶段轮询发现未认领任务
处理流程:
  1. 扫描 .tasks/ 目录下所有 task_*.json 文件
  2. 筛选满足条件的任务 (pending + 无 owner + 无 blockedBy)
  3. 按文件排序取第一个任务
  4. 调用 claim_task() 更新任务状态
  5. 构造任务提示并注入消息历史
  6. 返回 WORK 阶段执行
```

### 2. 消息驱动协作

```
功能描述: 支持智能体间异步消息通信
消息类型: message | broadcast | shutdown_request | shutdown_response | plan_approval_response
通信模式:
  - 点对点: send_message(sender, to, content)
  - 广播: broadcast(sender, content, teammates)
  - 拉取: read_inbox(name) 采用 Drain 模式（读取后清空）
```

### 3. 团队生命周期管理

```
功能描述: 完整的智能体生命周期控制
状态流转:
  spawn → working → idle ─┬─→ working (收到消息/认领任务)
                          └─→ shutdown (60秒超时或收到关机请求)

API:
  - spawn_teammate(name, role, prompt): 创建智能体
  - list_teammates(): 列出所有智能体状态
  - shutdown_request(teammate): 请求优雅关机
  - shutdown_response(request_id, approve): 响应关机请求
```

### 4. 计划审批流程

```
功能描述: 高风险变更需经领导审批
流程:
  1. 智能体提交计划: plan_approval(plan_text) → 生成 request_id
  2. 领导审查: plan_approval(request_id, approve, feedback)
  3. 智能体接收响应: plan_approval_response 消息
状态机: pending → approved | rejected
```

### 5. 上下文压缩感知

```
功能描述: 在上下文压缩后自动恢复智能体身份
触发条件: len(messages) <= 3
处理流程:
  1. 在消息历史开头插入 identity block
  2. 插入智能体确认响应
  3. 继续正常对话
```

### 6. 交互式命令

```
命令列表:
  /team   - 显示团队状态（所有智能体的角色和状态）
  /inbox  - 读取领导的收件箱
  /tasks  - 显示任务看板（带认领者标记）
  q/exit  - 退出程序
```

---

## 核心组件详解

### MessageBus 架构

```
┌─────────────────────────────────────────┐
│            MessageBus                   │
│  ┌─────────────┐    ┌───────────────┐  │
│  │   send()    │───→│ {name}.jsonl  │  │
│  └─────────────┘    └───────────────┘  │
│                                         │
│  ┌─────────────┐    ┌───────────────┐  │
│  │ read_inbox  │←───│   Drain 模式   │  │
│  └─────────────┘    └───────────────┘  │
│                                         │
│  ┌─────────────┐                        │
│  │ broadcast() │───→ 所有成员收件箱    │
│  └─────────────┘                        │
└─────────────────────────────────────────┘
```

### TeammateManager 架构

```
┌──────────────────────────────────────────┐
│          TeammateManager                 │
│  ┌──────────────┐    ┌──────────────┐   │
│  │   spawn()    │───→│  ThreadPool  │   │
│  └──────────────┘    └──────────────┘   │
│         │                               │
│         v                               │
│  ┌──────────────┐    ┌──────────────┐   │
│  │ config.json  │    │   _loop()    │   │
│  │ - team_name  │    │  ├─ WORK     │   │
│  │ - members[]  │    │  └─ IDLE     │   │
│  └──────────────┘    └──────────────┘   │
└──────────────────────────────────────────┘
```

### IDLE 轮询机制

```
IDLE PHASE
    │
    ├─→ sleep(POLL_INTERVAL=5s)
    │
    ├─→ check inbox
    │   ├─ has message? ──→ resume WORK
    │   └─ empty ─────────→ continue
    │
    ├─→ scan tasks
    │   ├─ unclaimed? ────→ claim & resume WORK
    │   └─ none ──────────→ continue
    │
    └─→ timeout (12轮询 × 5秒 = 60秒)? ──→ SHUTDOWN
```

---

## 数据流分析

### 1. 智能体创建流程

```
用户输入: "spawn alice as a coder to fix bugs"
         │
         v
┌─────────────────┐
│  spawn_teammate │──┐
└─────────────────┘  │
                     v
┌─────────────────────────────────────────┐
│ 1. 更新 config.json: alice/coding/working│
│ 2. 创建 Thread(target=_loop, daemon=True)│
│ 3. thread.start()                        │
└─────────────────────────────────────────┘
                     │
                     v
┌─────────────────────────────────────────┐
│ _loop():                                │
│   - 初始化 sys_prompt                   │
│   - 进入 WORK PHASE                     │
│   - LLM chat completion                 │
└─────────────────────────────────────────┘
```

### 2. 自动任务认领流程

```
智能体进入 IDLE
         │
         v
┌─────────────────┐
│ scan_unclaimed  │──┐
└─────────────────┘  │
                     v
┌─────────────────────────────────────────┐
│ 遍历 .tasks/task_*.json                │
│ 过滤: status=pending && owner=null     │
│       && blockedBy=null                │
└─────────────────────────────────────────┘
                     │
                     v
┌─────────────────┐
│ claim_task()    │──┐
└─────────────────┘  │
                     v
┌─────────────────────────────────────────┐
│ 1. 获取 _claim_lock                    │
│ 2. 更新 task["owner"] = name           │
│ 3. 更新 task["status"] = "in_progress" │
│ 4. 写回 JSON 文件                      │
└─────────────────────────────────────────┘
                     │
                     v
         构造 prompt: "<auto-claimed>Task #1: ..."
                     │
                     v
              返回 WORK PHASE
```

### 3. 消息通信流程

```
发送方调用 send_message(to="alice", content="hi")
                    │
                    v
┌─────────────────────────────────────────┐
│ BUS.send("lead", "alice", "hi", ...)    │
│   - 构造消息: {type, from, content, ts} │
│   - 追加到 .team/inbox/alice.jsonl     │
└─────────────────────────────────────────┘
                    │
                    v
接收方 (alice 线程) 在 IDLE/WORK 阶段
                    │
                    v
┌─────────────────────────────────────────┐
│ BUS.read_inbox("alice")                 │
│   - 读取 alice.jsonl 所有行            │
│   - 解析为 list[dict]                  │
│   - 清空文件 (write_text(""))           │
└─────────────────────────────────────────┘
                    │
                    v
         消息注入 messages[]
                    │
                    v
         LLM 处理响应
```

---

## 演进对比

| 特性 | s09 Agent Teams | s10 Team Protocols | s11 Autonomous Agents |
|------|----------------|-------------------|---------------------|
| **工具数量** | 9 | 12 | 14 (+2) |
| **智能体模式** | 被动响应 | 被动响应 + 协议协商 | 主动自治 + 空闲轮询 |
| **任务分配** | 手动 spawn | 手动 spawn | 自动认领 |
| **生命周期** | WORK → idle | WORK → idle | WORK → IDLE → WORK/SHUTDOWN |
| **通信协议** | 基础消息 | request-response FSM | request-response + 异步事件 |
| **上下文恢复** | 无 | 无 | 身份重注入 |
| **超时机制** | 无 | 无 | 60秒空闲超时 |
| **任务依赖** | 不支持 | 不支持 | blockedBy 支持 |

### 关键改进点

1. **从指派到自治**：智能体从"领导指派"进化为"主动发现"
2. **空闲轮询**：引入 IDLE 阶段，智能体可以持续待命
3. **身份保持**：解决上下文压缩后的失忆问题
4. **任务依赖**：支持任务间依赖关系，智能体自动等待前置任务完成

---

## 使用指南

### 快速开始

```bash
# 1. 启动程序
python agents/s11_autonomous_agents.py

# 2. 创建任务
Create a task "Implement login API" with description "Add JWT auth"

# 3. 创建智能体
Spawn alice as a backend developer

# 4. 查看任务看板
/tasks

# 5. 查看团队状态
/team
```

### 高级用法

```bash
# 创建带依赖的任务
create task "A" then create task "B" blocked by task "A"

# 创建多个智能体
Spawn alice as frontend, bob as backend, charlie as tester

# 请求智能体关机
Send shutdown request to alice

# 广播消息
Broadcast "Daily standup in 5 minutes"
```

### 任务文件示例

```bash
# 手动创建任务文件
cat > .tasks/task_1.json << 'EOF'
{
  "id": 1,
  "subject": "Setup database",
  "description": "Create PostgreSQL schema",
  "status": "pending",
  "owner": null,
  "blockedBy": null
}
EOF
```

---

## 总结

s11 Autonomous Agents 实现了智能体系统的**自治化演进**，核心贡献包括：

1. **双阶段生命周期**：WORK + IDLE 的明确划分，支持持续待命
2. **自动任务认领**：智能体主动扫描看板，实现真正的自组织
3. **身份重注入**：解决上下文压缩后的身份丢失问题
4. **完整协议栈**：消息通信 + 团队协议 + 自治机制的三层架构

这一设计为 s12 的工作目录隔离提供了坚实基础，是整个系列中"团队协同"阶段的巅峰之作。
