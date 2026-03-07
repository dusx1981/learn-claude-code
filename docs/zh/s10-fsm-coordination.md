# s10: 简单 FSM 为何能协调复杂多代理协作

> **核心洞见**: 协议的复杂性不在状态机本身，而在请求追踪和协议语义。三个状态足以驱动任意复杂的多代理协作。

`S01 > S02 > S03 > S04 > S05 > S06 | S07 > S08 > S09 > [ S10 ] S11 > S12`

---

## 一、问题：简单状态机如何承载复杂协作？

直观上，复杂的多代理协作需要复杂的状态机：

```
直观错误认知:
  复杂协作 = 复杂状态机 (几十个状态 + 复杂转移条件)
  简单状态机 (3 个状态) = 只能处理简单场景
```

但 s10 展示了完全不同的设计哲学：

```
S10 的设计:
  复杂协作 = 简单状态机 × 请求追踪 × 协议语义
  
  状态机本身：pending -> approved | rejected  (仅 3 个状态)
  复杂性承载：request_id 追踪 + 协议上下文 + 并发控制
```

**为什么三个状态足够？**

因为每个协议实例都是独立的。复杂协作不是单个状态机的复杂转移，而是多个独立协议实例的并行执行。每个实例只需要三个状态：

- `pending`: 请求已发出，等待响应
- `approved`: 请求已批准
- `rejected`: 请求已拒绝

复杂性不在于状态转移，而在于：
1. 如何追踪多个并发请求 (request_id 关联)
2. 如何保证并发安全 (线程锁)
3. 如何定义协议语义 (关机协议 vs 计划审批协议)

---

## 二、FSM 设计：三状态足以

### 2.1 状态机定义

```
          +-----------+
          |  pending  |
          +-----+-----+
                |
      +---------+---------+
      |                   |
      v                   v
+-----------+       +-----------+
| approved  |       | rejected  |
+-----------+       +-----------+
```

代码实现：

```python
# s10_team_protocols.py:96-99
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()

# 状态转移 (以关机协议为例)
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        # 初始状态：pending
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    # ... 发送请求到收件箱
    return f"Shutdown request {req_id} sent (status: pending)"

# 状态转移：pending -> approved | rejected
if tool_name == "shutdown_response":
    req_id = args["request_id"]
    approve = args["approve"]
    with _tracker_lock:
        if req_id in shutdown_requests:
            shutdown_requests[req_id]["status"] = "approved" if approve else "rejected"
```

### 2.2 为什么不需要更多状态？

常见的状态机设计误区是添加"中间状态"：

```
错误设计 (状态膨胀):
  pending -> reviewing -> approved | rejected | needs_revision
  (5 个状态，还需要处理 reviewing->needs_revision 的循环)
```

s10 的设计证明这是不必要的：

```
正确设计 (三状态足够):
  pending -> approved | rejected
  
  需要"修改后重提"？
  - 领导：reject (状态->rejected)
  - 队友：重新提交新请求 (生成新 request_id, 新实例状态=pending)
  
  需要"审查中"状态？
  - 不需要。审查是协议语义，不是状态。
  - 领导收到请求后自然进入"审查"行为，状态仍是 pending。
```

**关键区分**:

- **状态 (State)**: 协议实例的生命周期阶段 (pending/approved/rejected)
- **行为 (Behavior)**: 参与方在特定状态下的动作 (审查、修改、重提)

状态机只跟踪生命周期，行为由协议语义定义。分离两者，状态机保持简洁。

---

## 三、request_id 关联：并发请求追踪的关键

### 3.1 request_id 的作用

每个协议请求都有一个唯一的 `request_id`：

```python
# 关机请求：领导生成 request_id
req_id = str(uuid.uuid4())[:8]  # 例如："a1b2c3d4"

# 计划请求：队友生成 request_id
req_id = str(uuid.uuid4())[:8]  # 例如："x7y8z9w0"
```

这个 8 字符的 UUID 前缀是并发追踪的核心：

```
场景：领导同时向 3 个队友发起关机请求

请求 1: request_id="a1b2c3d4", target="alice", status="pending"
请求 2: request_id="e5f6g7h8", target="bob",   status="pending"
请求 3: request_id="i9j0k1l2", target="carol", status="pending"

当 alice 响应时，携带 request_id="a1b2c3d4":
  -> 领导查找 shutdown_requests["a1b2c3d4"]
  -> 更新状态为 approved 或 rejected
  -> bob 和 carol 的请求不受影响 (各自独立的 request_id)
```

### 3.2 追踪器数据结构

```python
# 关机请求追踪器
shutdown_requests = {
    "a1b2c3d4": {"target": "alice", "status": "pending"},
    "e5f6g7h8": {"target": "bob",   "status": "approved"},
    "i9j0k1l2": {"target": "carol", "status": "pending"},
}

# 计划请求追踪器
plan_requests = {
    "x7y8z9w0": {"from": "dave", "plan": "重构认证模块", "status": "pending"},
    "m3n4o5p6": {"from": "eve",  "plan": "添加日志系统", "status": "rejected"},
}
```

**设计特点**:

1. **字典 keyed by request_id**: O(1) 查找，快速定位请求
2. **扁平结构**: 不嵌套，只存储追踪所需的最小信息
3. **协议分离**: `shutdown_requests` 和 `plan_requests` 独立，互不干扰

### 3.3 请求 - 响应关联流程

```
领导发起关机请求                    队友响应关机请求
====================                    ====================

1. 生成 request_id
   req_id = uuid.uuid4()[:8]
   
2. 创建追踪记录 (status=pending)
   shutdown_requests[req_id] = {
       "target": "alice",
       "status": "pending"
   }
   
3. 发送请求 (带 request_id)
   BUS.send("lead", "alice", "...", 
            "shutdown_request", {"request_id": req_id})

                                    4. 队友收到请求，读取 request_id
                                       msg = read_inbox()
                                       req_id = msg["request_id"]
                                       
                                    5. 决定批准或拒绝
                                       approve = True
                                       
                                    6. 响应 (携带同一个 request_id)
                                       BUS.send("alice", "lead", "...",
                                                "shutdown_response",
                                                {"request_id": req_id,
                                                 "approve": True})

7. 领导收到响应，更新状态
   shutdown_requests[req_id]["status"] = "approved"
```

---

## 四、双向协议：Lead->Teammate 与 Teammate->Lead

s10 实现了两个方向的协议，共享相同的 FSM 模式：

```
协议方向矩阵:
                    发起方          接收方          协议类型
                    ------          ------          ----------
方向 1 (向下):      Lead        ->  Teammate    shutdown_request
方向 2 (向上):      Teammate    ->  Lead        plan_approval
```

### 4.1 方向 1: Lead -> Teammate (关机协议)

**语义**: 领导请求队友关机，队友决定是否批准。

```
时序图:

Lead                              Teammate
  |                                   |
  |--shutdown_request---------------->|
  | {request_id: "abc",               |
  |  content: "Please shut down"}     |
  |                                   | 收件箱轮询
  |                                   | 收到 shutdown_request
  |                                   | 调用 shutdown_response 工具
  |<--shutdown_response---------------|
  | {request_id: "abc",               |
  |  approve: true,                   |
  |  reason: "Completed task"}        |
  |                                   |
状态更新：pending -> approved
```

代码实现 (领导侧):

```python
# s10_team_protocols.py:373-383
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}' (status: pending)"
```

代码实现 (队友侧):

```python
# s10_team_protocols.py:267-278
if tool_name == "shutdown_response":
    req_id = args["request_id"]
    approve = args["approve"]
    with _tracker_lock:
        if req_id in shutdown_requests:
            shutdown_requests[req_id]["status"] = "approved" if approve else "rejected"
    BUS.send(
        sender, "lead", args.get("reason", ""),
        "shutdown_response", {"request_id": req_id, "approve": approve},
    )
    return f"Shutdown {'approved' if approve else 'rejected'}"
```

### 4.2 方向 2: Teammate -> Lead (计划审批协议)

**语义**: 队友提交计划供领导审批，领导决定批准或拒绝。

```
时序图:

Teammate                            Lead
  |                                   |
  |--plan_approval------------------>|
  | {request_id: "xyz",               |
  |  plan: "重构认证模块"}             |
  |                                   | 收件箱轮询
  |                                   | 收到 plan_approval_response
  |                                   | 调用 plan_approval 工具
  |<--plan_approval_response---------|
  | {request_id: "xyz",               |
  |  approve: false,                  |
  |  feedback: "先写测试"}             |
  |                                   |
状态更新：pending -> rejected
队友行为：修改计划，重新提交 (新 request_id)
```

代码实现 (队友侧提交):

```python
# s10_team_protocols.py:269-278
if tool_name == "plan_approval":
    plan_text = args.get("plan", "")
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
    BUS.send(
        sender, "lead", plan_text, "plan_approval_response",
        {"request_id": req_id, "plan": plan_text},
    )
    return f"Plan submitted (request_id={req_id}). Waiting for lead approval."
```

代码实现 (领导侧审批):

```python
# s10_team_protocols.py:385-397
def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"Plan {req['status']} for '{req['from']}'"
```

### 4.3 双向协议的对称性

```
对称设计:
                          关机协议                     计划审批协议
                          ---------                  -----------
发起方：                   Lead                       Teammate
接收方：                   Teammate                   Lead
请求类型：                 shutdown_request           plan_approval_response
响应类型：                 shutdown_response          plan_approval_response
追踪器：                   shutdown_requests          plan_requests
追踪器键：                 target                     from
状态机：                   pending->approved/rejected pending->approved/rejected
request_id 生成：          发起方生成                 发起方生成
```

**统一模式**:

1. 发起方生成 request_id
2. 发起方创建追踪记录 (status=pending)
3. 发起方发送请求 (通过收件箱)
4. 接收方收到请求，决定批准/拒绝
5. 接收方发送响应 (携带同一个 request_id)
6. 发起方收到响应，更新状态

这个模式可以扩展到任意多的协议类型，只需：
- 新增追踪器字典
- 定义请求/响应消息类型
- 实现处理函数

状态机本身不需要修改。

---

## 五、并发请求处理：多队友、多请求并行

### 5.1 并发场景

s10 支持以下并发场景：

```
场景 A: 领导同时向多个队友发起关机请求
场景 B: 多个队友同时提交计划审批
场景 C: 关机请求 + 计划审批并行执行
场景 D: 同一队友同时处理多个请求 (罕见但可能)
```

### 5.2 并发隔离机制

**隔离级别 1: 协议隔离**

```python
shutdown_requests = {}  # 关机请求追踪器
plan_requests = {}      # 计划审批追踪器
```

两个字典完全独立，互不影响。

**隔离级别 2: request_id 隔离**

```
shutdown_requests:
  "a1": {"target": "alice", "status": "pending"}
  "b2": {"target": "bob",   "status": "approved"}
  "c3": {"target": "carol", "status": "pending"}

plan_requests:
  "x1": {"from": "dave", "plan": "...", "status": "pending"}
  "y2": {"from": "eve",  "plan": "...", "status": "rejected"}
```

每个 request_id 对应独立的协议实例，状态互不影响。

**隔离级别 3: 收件箱隔离**

```
.team/inbox/alice.jsonl  # alice 的收件箱
.team/inbox/bob.jsonl    # bob 的收件箱
.team/inbox/carol.jsonl  # carol 的收件箱
.team/inbox/lead.jsonl   # lead 的收件箱
```

每个代理有独立的收件箱，消息不会混淆。

### 5.3 并发执行示例

```
示例 C: 关机请求 + 计划审批并行

时间线:
T0: 领导 spawn alice, bob
T1: alice 提交计划 (request_id="x1", status=pending)
T2: 领导向 bob 发起关机请求 (request_id="a1", status=pending)
T3: 领导审批 alice 的计划 (request_id="x1", approve=false, feedback="重写")
    -> plan_requests["x1"]["status"] = "rejected"
T4: alice 修改计划后重新提交 (request_id="x2", status=pending)
T5: bob 批准关机 (request_id="a1", approve=true)
    -> shutdown_requests["a1"]["status"] = "approved"
T6: 领导审批 alice 的新计划 (request_id="x2", approve=true)
    -> plan_requests["x2"]["status"] = "approved"
T7: alice 完成计划，自然退出 (status=idle)
T8: bob 的线程停止 (status=shutdown)

最终状态:
  alice: idle (计划执行完毕)
  bob: shutdown (已关机)
  plan_requests: {"x1": rejected, "x2": approved}
  shutdown_requests: {"a1": approved}
```

---

## 六、线程安全：_tracker_lock 并发访问控制

### 6.1 为什么需要锁？

多个线程可能同时访问追踪器字典：

```
线程 1 (领导线程): 调用 handle_shutdown_request()
  -> 写入 shutdown_requests[req_id]

线程 2 (alice 线程): 调用 shutdown_response 工具
  -> 读取并更新 shutdown_requests[req_id]["status"]

线程 3 (bob 线程): 调用 shutdown_response 工具
  -> 读取并更新 shutdown_requests[req_id]["status"]
```

没有锁的情况下，可能出现竞态条件：

```
竞态条件示例 (没有锁):

T0: 线程 1: shutdown_requests["a1"] = {"target": "alice", "status": "pending"}
T1: 线程 2: 读取 shutdown_requests["a1"]  # 读到 {"target": "alice", "status": "pending"}
T2: 线程 1: 写入 shutdown_requests["a1"]  # 可能覆盖线程 2 的更新
T3: 线程 2: 写入 shutdown_requests["a1"]["status"] = "approved"
```

### 6.2 锁的使用

```python
# s10_team_protocols.py:99
_tracker_lock = threading.Lock()

# 写入追踪器时加锁
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:  # 获取锁
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    # 释放锁 (with 语句退出时自动释放)
    BUS.send(...)
    return f"Shutdown request {req_id} sent..."

# 更新状态时加锁
if tool_name == "shutdown_response":
    req_id = args["request_id"]
    approve = args["approve"]
    with _tracker_lock:  # 获取锁
        if req_id in shutdown_requests:
            shutdown_requests[req_id]["status"] = "approved" if approve else "rejected"
    BUS.send(...)
    return f"Shutdown {'approved' if approve else 'rejected'}"

# 读取追踪器时也加锁 (保证一致性)
def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    with _tracker_lock:  # 获取锁
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:  # 再次获取锁 (写入)
        req["status"] = "approved" if approve else "rejected"
    BUS.send(...)
    return f"Plan {req['status']} for '{req['from']}'"
```

### 6.3 锁的粒度

当前设计使用**全局锁**：

```python
_tracker_lock = threading.Lock()  # 一个锁保护所有追踪器
```

**优点**:
- 实现简单，不易出错
- 保证跨追踪器的一致性 (虽然当前不需要)

**缺点**:
- 并发度受限 (同一时刻只有一个线程能访问任何追踪器)
- 关机协议和计划审批协议互斥访问 (虽然它们操作不同的字典)

**优化方向** (当前不需要，但了解):

```python
# 方案 A: 每个追踪器独立锁
shutdown_lock = threading.Lock()
plan_lock = threading.Lock()

# 方案 B: 使用 RWMutex (读多写少场景)
# Python 没有内置 RWMutex, 可用 threading.RLock 或第三方库
```

对于 s10 的场景 (几个队友，低频请求), 全局锁足够且更简单。

---

## 七、可扩展性：如何添加新协议而不改变 FSM

### 7.1 添加新协议的步骤

假设要添加"资源请求协议" (Teammate 向 Lead 请求计算资源)：

**步骤 1: 定义新追踪器**

```python
# 新增追踪器字典
resource_requests = {}
_tracker_lock = threading.Lock()  # 复用现有锁
```

**步骤 2: 定义请求工具 (队友侧)**

```python
if tool_name == "resource_request":
    resource_type = args.get("type", "gpu")
    resource_spec = args.get("spec", "1 GPU, 16GB RAM")
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        resource_requests[req_id] = {
            "from": sender,
            "type": resource_type,
            "spec": resource_spec,
            "status": "pending"
        }
    BUS.send(
        sender, "lead", f"Request {resource_type}: {resource_spec}",
        "resource_request",
        {"request_id": req_id, "type": resource_type, "spec": resource_spec},
    )
    return f"Resource request submitted (request_id={req_id}). Waiting for approval."
```

**步骤 3: 定义审批工具 (领导侧)**

```python
def handle_resource_review(request_id: str, approve: bool, allocation: str = ""):
    with _tracker_lock:
        req = resource_requests.get(request_id)
    if not req:
        return f"Error: Unknown resource request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send(
        "lead", req["from"], allocation, "resource_response",
        {"request_id": request_id, "approve": approve, "allocation": allocation},
    )
    return f"Resource request {req['status']} for '{req['from']}'"
```

**步骤 4: 注册工具**

```python
# 添加到 TOOL_HANDLERS
TOOL_HANDLERS["resource_approval"] = lambda **kw: handle_resource_review(
    kw["request_id"], kw["approve"], kw.get("allocation", "")
)
```

**完成**。FSM 本身 (pending->approved/rejected) 无需任何修改。

### 7.2 新协议的 FSM 复用

```
资源请求协议的状态机:

          +-----------+
          |  pending  |  资源请求已提交，等待领导审批
          +-----+-----+
                |
      +---------+---------+
      |                   |
      v                   v
+-----------+       +-----------+
| approved  |       | rejected  |
+-----------+       +-----------+
领导已批准，       领导已拒绝，
分配资源           不分配资源
```

**复用模式**:

| 协议类型        | 发起方   | 接收方   | 状态机                  |
|----------------|----------|----------|-------------------------|
| shutdown       | Lead     | Teammate | pending->approved/rejected |
| plan_approval  | Teammate | Lead     | pending->approved/rejected |
| resource_request| Teammate | Lead     | pending->approved/rejected |
| (未来任意协议)  | ...      | ...      | pending->approved/rejected |

**可扩展性的关键**:

1. **状态机与协议解耦**: FSM 是通用基础设施，不绑定特定协议语义
2. **追踪器隔离**: 每个协议有独立的追踪器字典
3. **request_id 模式**: 统一的请求 - 响应关联机制
4. **工具注册模式**: 新增工具只需注册到 TOOL_HANDLERS

---

## 八、具体示例详解

### 示例 1: 领导同时请求 3 个队友关机

```
初始状态:
  TEAM: alice (working), bob (working), carol (working)
  shutdown_requests: {}

T0: 领导调用 shutdown_request(teammate="alice")
    req_id_a = uuid.uuid4()[:8]  # 假设 "a1b2c3d4"
    shutdown_requests["a1b2c3d4"] = {"target": "alice", "status": "pending"}
    BUS.send("lead", "alice", "...", "shutdown_request", {"request_id": "a1b2c3d4"})

T1: 领导调用 shutdown_request(teammate="bob")
    req_id_b = uuid.uuid4()[:8]  # 假设 "e5f6g7h8"
    shutdown_requests["e5f6g7h8"] = {"target": "bob", "status": "pending"}
    BUS.send("lead", "bob", "...", "shutdown_request", {"request_id": "e5f6g7h8"})

T2: 领导调用 shutdown_request(teammate="carol")
    req_id_c = uuid.uuid4()[:8]  # 假设 "i9j0k1l2"
    shutdown_requests["i9j0k1l2"] = {"target": "carol", "status": "pending"}
    BUS.send("lead", "carol", "...", "shutdown_request", {"request_id": "i9j0k1l2"})

T3: alice 轮询收件箱，收到 shutdown_request
    读取 request_id="a1b2c3d4"
    调用 shutdown_response(request_id="a1b2c3d4", approve=True)
    shutdown_requests["a1b2c3d4"]["status"] = "approved"
    alice 线程退出，config.json 中 alice.status = "shutdown"

T4: bob 轮询收件箱，收到 shutdown_request
    读取 request_id="e5f6g7h8"
    调用 shutdown_response(request_id="e5f6g7h8", approve=False, reason="Need to finish task")
    shutdown_requests["e5f6g7h8"]["status"] = "rejected"
    bob 继续工作

T5: carol 轮询收件箱，收到 shutdown_request
    读取 request_id="i9j0k1l2"
    调用 shutdown_response(request_id="i9j0k1l2", approve=True)
    shutdown_requests["i9j0k1l2"]["status"] = "approved"
    carol 线程退出，config.json 中 carol.status = "shutdown"

最终状态:
  shutdown_requests: {
    "a1b2c3d4": {"target": "alice", "status": "approved"},
    "e5f6g7h8": {"target": "bob",   "status": "rejected"},
    "i9j0k1l2": {"target": "carol", "status": "approved"},
  }
  TEAM: alice (shutdown), bob (idle), carol (shutdown)
```

ASCII 并发图:

```
Lead          alice            bob              carol
  |              |               |                 |
  |--req(a1)---->|               |                 |
  |--req(e5)----|---------------|>                |
  |--req(i9)----|---------------|-----------------|>
  |              |               |                 |
  |          收到 (a1)       收到 (e5)         收到 (i9)
  |          approve=True    approve=False     approve=True
  |<--resp(a1)---|               |                 |
  |              |               |                 |
  |<--resp(e5)---|---------------|>                |
  |              |               |                 |
  |<--resp(i9)---|---------------|-----------------|>
  |              |               |                 |
状态：        shutdown         idle            shutdown
              (退出)          (继续)           (退出)
```

### 示例 2: 队友提交计划，领导拒绝，队友重提

```
初始状态:
  TEAM: dave (working, 任务："重构认证模块")
  plan_requests: {}

T0: dave 调用 plan_approval(plan="直接重构 auth.py, 添加 JWT 支持")
    req_id_1 = uuid.uuid4()[:8]  # 假设 "x1y2z3w4"
    plan_requests["x1y2z3w4"] = {
        "from": "dave",
        "plan": "直接重构 auth.py, 添加 JWT 支持",
        "status": "pending"
    }
    BUS.send("dave", "lead", "...", "plan_approval_response",
             {"request_id": "x1y2z3w4", "plan": "..."})

T1: 领导轮询收件箱，收到 plan_approval_response
    读取 request_id="x1y2z3w4", plan="..."
    领导审查计划，认为风险太高
    调用 plan_approval(request_id="x1y2z3w4", approve=False,
                      feedback="先写单元测试，再重构")
    plan_requests["x1y2z3w4"]["status"] = "rejected"
    BUS.send("lead", "dave", "先写单元测试，再重构",
             "plan_approval_response",
             {"request_id": "x1y2z3w4", "approve": False, "feedback": "..."})

T2: dave 轮询收件箱，收到审批结果
    读取 request_id="x1y2z3w4", approve=False, feedback="..."
    dave 修改计划
    重新调用 plan_approval(plan="1. 写 auth_test.py 2. 重构 auth.py")
    req_id_2 = uuid.uuid4()[:8]  # 假设 "p5q6r7s8" (新 request_id)
    plan_requests["p5q6r7s8"] = {
        "from": "dave",
        "plan": "1. 写 auth_test.py 2. 重构 auth.py",
        "status": "pending"
    }
    BUS.send("dave", "lead", "...", "plan_approval_response",
             {"request_id": "p5q6r7s8", "plan": "..."})

T3: 领导轮询收件箱，收到新的审批请求
    读取 request_id="p5q6r7s8", plan="..."
    领导审查计划，认为合理
    调用 plan_approval(request_id="p5q6r7s8", approve=True,
                      feedback="可以，开始执行")
    plan_requests["p5q6r7s8"]["status"] = "approved"
    BUS.send("lead", "dave", "可以，开始执行",
             "plan_approval_response",
             {"request_id": "p5q6r7s8", "approve": True, "feedback": "..."})

T4: dave 收到批准通知
    开始执行计划 (写测试，然后重构)

最终状态:
  plan_requests: {
    "x1y2z3w4": {"from": "dave", "plan": "直接重构...", "status": "rejected"},
    "p5q6r7s8": {"from": "dave", "plan": "1. 写测试...", "status": "approved"},
  }
```

ASCII 时序图:

```
dave                              Lead
  |                                 |
  |--plan(x1): "直接重构"---------->|
  |  status: pending                |
  |                                 | 审查
  |                                 | 决定：拒绝
  |<--review(x1): reject-----------|
  |  feedback: "先写测试"           |
  |                                 |
  | 修改计划                        |
  |                                 |
  |--plan(p5): "1. 写测试..."------>|
  |  status: pending (新 request_id) |
  |                                 | 审查
  |                                 | 决定：批准
  |<--review(p5): approve----------|
  |  feedback: "可以"               |
  |                                 |
  | 执行计划                        |
  v                                 v
```

### 示例 3: 混合场景 - 关机 + 计划审批并行

```
初始状态:
  TEAM: alice (working, 任务："添加日志系统"), bob (working, 任务："清理临时文件")
  shutdown_requests: {}
  plan_requests: {}

T0: alice 提交计划
    plan_requests["x1"] = {"from": "alice", "plan": "添加日志系统", "status": "pending"}

T1: 领导向 bob 发起关机请求
    shutdown_requests["a1"] = {"target": "bob", "status": "pending"}

T2: 领导审批 alice 的计划
    plan_requests["x1"]["status"] = "approved"
    alice 开始执行计划

T3: bob 批准关机
    shutdown_requests["a1"]["status"] = "approved"
    bob 线程退出

T4: alice 执行计划中...

T5: 领导向 alice 发起关机请求 (计划执行完毕后关机)
    shutdown_requests["a2"] = {"target": "alice", "status": "pending"}

T6: alice 完成计划，收到关机请求，批准
    shutdown_requests["a2"]["status"] = "approved"
    alice 线程退出

最终状态:
  plan_requests: {"x1": {"from": "alice", "status": "approved"}}
  shutdown_requests: {
    "a1": {"target": "bob", "status": "approved"},
    "a2": {"target": "alice", "status": "approved"},
  }
  TEAM: alice (shutdown), bob (shutdown)
```

ASCII 并行图:

```
时间    alice                    bob                      Lead
----    -----                    ---                      ----
T0      |                         |                        |
        |--plan(x1)--------------|----------------------->|
        |  "添加日志系统"          |                        |
        |                         |                        |
T1      |                         |                        |
        |                         |<--shutdown_req(a1)-----|
        |                         |                        |
T2      |                         |                        |
        |<--plan_review(x1)-------|------------------------|
        |  approved               |                        |
        | 执行计划                |                        |
T3      |                         |                        |
        |                         | 批准关机               |
        |                         | shutdown_resp(a1)      |
        |                         |----------------------->|
        |                         | 线程退出               |
T4      | 执行中...               | (shutdown)             |
        |                         |                        |
T5      |                         |                        |
        |<--shutdown_req(a2)------|------------------------|
        |                         |                        |
T6      | 完成计划，批准关机      |                        |
        | shutdown_resp(a2)       |                        |
        |------------------------>|                        |
        | 线程退出               |                        |
        | (shutdown)             |                        |

最终:
  plan_requests: {x1: approved}
  shutdown_requests: {a1: approved, a2: approved}
  所有队友 shutdown
```

---

## 九、哲学洞见：协议简单性 vs 协作复杂性

### 9.1 核心洞见

```
传统认知:
  复杂协作需要复杂状态机

S10 的洞见:
  复杂协作 = 简单状态机 × 并发追踪 × 协议语义
  
  状态机保持简单 (3 个状态)
  复杂性由 request_id 追踪和协议语义承载
```

### 9.2 为什么这个设计有效？

**1. 关注点分离**

```
状态机负责：生命周期跟踪 (pending/approved/rejected)
追踪器负责：并发请求管理 (request_id -> 状态映射)
协议语义负责：业务逻辑 (关机/计划审批/资源请求)
```

**2. 组合优于扩展**

```
错误设计 (状态扩展):
  关机状态机：pending -> reviewing -> approved | rejected | needs_revision
  计划状态机：pending -> reviewing -> approved | rejected | needs_revision
  资源状态机：pending -> reviewing -> approved | rejected | needs_revision
  (每个协议都需要定义复杂状态机)

正确设计 (状态组合):
  通用状态机：pending -> approved | rejected
  协议 1: 关机 (使用通用状态机)
  协议 2: 计划审批 (使用通用状态机)
  协议 3: 资源请求 (使用通用状态机)
  (所有协议复用同一状态机)
```

**3. request_id 是并发原语**

```
单个 request_id 的作用:
  - 唯一标识一个协议实例
  - 关联请求和响应
  - 隔离并发请求的状态
  
多个 request_id 的组合:
  - 支持任意数量的并发协议实例
  - 每个实例独立追踪，互不干扰
  - 复杂性 = request_id 的数量，不是状态机的复杂度
```

### 9.3 设计原则总结

| 原则              | 说明                                              | s10 中的体现                    |
|-------------------|---------------------------------------------------|---------------------------------|
| 最小状态原则      | 状态机只跟踪必要的生命周期状态                    | 3 个状态：pending/approved/rejected |
| 请求追踪原则      | 用 request_id 追踪并发请求，而不是扩展状态机      | shutdown_requests, plan_requests |
| 协议分离原则      | 不同协议使用独立追踪器，互不干扰                  | 关机追踪器 vs 计划追踪器         |
| 线程安全原则      | 共享状态访问必须加锁                              | _tracker_lock                   |
| 可扩展原则        | 新增协议只需新增追踪器和工具，不修改状态机        | 可轻松添加资源请求等新协议       |

### 9.4 何时需要更复杂的状态机？

s10 的三状态 FSM 适用于**请求 - 响应**模式的协议。以下场景可能需要更复杂的状态机：

```
场景 1: 多阶段审批
  pending -> manager_review -> director_review -> approved | rejected
  (需要跟踪审批进度)
  
场景 2: 可撤销协议
  pending -> approved -> executing -> completed | cancelled
  (需要跟踪执行进度和撤销)
  
场景 3: 条件转移
  pending -> (条件 A) -> approved | (条件 B) -> rejected | (条件 C) -> pending_revision
  (不同条件触发不同转移)
```

但 s10 证明：对于简单的请求 - 响应协议，三状态足够。不要过早引入复杂性。

---

## 十、总结

### 10.1 关键要点

1. **三状态足够**: pending/approved/rejected 可以驱动任意复杂的多代理协作
2. **复杂性在追踪**: request_id 关联是并发请求追踪的核心
3. **双向协议对称**: Lead->Teammate 和 Teammate->Lead 共享相同模式
4. **并发隔离**: 协议隔离 + request_id 隔离 + 收件箱隔离
5. **线程安全**: _tracker_lock 保护并发访问
6. **可扩展**: 新增协议不修改状态机

### 10.2 代码模式复用

这个模式可以应用到任何需要请求 - 响应协调的场景：

```python
# 通用模板
XXX_requests = {}
_tracker_lock = threading.Lock()

# 发起方
def request_XXX(...):
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        XXX_requests[req_id] = {..., "status": "pending"}
    BUS.send(..., "xxx_request", {"request_id": req_id, ...})
    return f"Request {req_id} sent (status: pending)"

# 响应方
if tool_name == "xxx_response":
    req_id = args["request_id"]
    approve = args["approve"]
    with _tracker_lock:
        XXX_requests[req_id]["status"] = "approved" if approve else "rejected"
    BUS.send(..., "xxx_response", {"request_id": req_id, "approve": approve, ...})
```

### 10.3 下一步

s10 建立了协议基础。s11 和 s12 在此基础上构建更高级的协作机制：

- **s11 (自主代理)**: 队友主动扫描任务板，自动认领任务 (无需领导分配)
- **s12 (工作树隔离)**: 每个代理在独立的工作树中执行，互不干扰

理解 s10 的协议模式是理解 s11/s12 的基础。

---

## 附录：核心代码片段

### A1. 追踪器定义

```python
# s10_team_protocols.py:96-99
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()
```

### A2. 关机请求处理

```python
# s10_team_protocols.py:373-383
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}' (status: pending)"
```

### A3. 关机响应处理

```python
# s10_team_protocols.py:267-278
if tool_name == "shutdown_response":
    req_id = args["request_id"]
    approve = args["approve"]
    with _tracker_lock:
        if req_id in shutdown_requests:
            shutdown_requests[req_id]["status"] = "approved" if approve else "rejected"
    BUS.send(
        sender, "lead", args.get("reason", ""),
        "shutdown_response", {"request_id": req_id, "approve": approve},
    )
    return f"Shutdown {'approved' if approve else 'rejected'}"
```

### A4. 计划审批处理

```python
# s10_team_protocols.py:385-397
def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"Plan {req['status']} for '{req['from']}'"
```

### A5. 计划提交工具

```python
# s10_team_protocols.py:269-278
if tool_name == "plan_approval":
    plan_text = args.get("plan", "")
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
    BUS.send(
        sender, "lead", plan_text, "plan_approval_response",
        {"request_id": req_id, "plan": plan_text},
    )
    return f"Plan submitted (request_id={req_id}). Waiting for lead approval."
```

---

## 九、协议原语详解

协议原语是构成所有团队通信协议的基本构建块。理解这些原语的设计哲学和实现机制，是掌握 s10 协议系统的关键。

### 九、一 原语列表总览

s10 协议系统由七个核心原语构成：

```
+------------------+------------------+------------------------------------------+
| 原语             | 作用域           | 核心功能                                 |
+------------------+------------------+------------------------------------------+
| request_id       | 消息级           | 请求 - 响应关联标识                       |
| status           | 请求级           | 追踪协议生命周期状态                       |
| sender/receiver  | 消息级           | 标识协议参与者角色                         |
| msg_type         | 消息级           | 区分协议消息类型                           |
| extra payload    | 消息级           | 承载协议特定数据                           |
| tracker          | 系统级           | 集中化请求状态管理                         |
| handler          | 系统级           | 协议特定逻辑处理                           |
+------------------+------------------+------------------------------------------+
```

这些原语通过组合形成完整的协议：

```
shutdown_request  = request_id + sender + receiver + msg_type + tracker
shutdown_response = request_id + status + extra payload
plan_approval     = request_id + sender + plan + tracker
```

### 九、二 request_id (请求标识)

**目的**: 为请求和响应提供唯一关联标识，实现去耦合的请求追踪。

**实现**:
```python
# s10_team_protocols.py:375, 271
req_id = str(uuid.uuid4())[:8]
```

**为什么是 8 个字符**:
- UUID 标准格式：32 个十六进制字符 (128 位)
- 截取前 8 位：`550e8400-e29b-41d4-a716-446655440000` → `550e8400`
- 碰撞概率：8 位十六进制 = 16^8 ≈ 43 亿种可能
- 平衡点：足够短以保证可读性，足够长以保证唯一性

**设计哲学**: 请求标识与消息内容解耦

```
错误设计：用消息内容作为标识
  "shutdown_<teammate_name>" → 耦合了语义和标识
  问题：无法区分同一目标的多次请求

正确设计：独立标识符
  request_id = uuid()[:8] → 标识与语义完全分离
  优势：支持并发请求、简洁、可复用
```

**使用模式**:
```python
# 发起请求时生成
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]           # 生成标识
    with _tracker_lock:
        shutdown_requests[req_id] = {        # 记录到追踪器
            "target": teammate,
            "status": "pending"
        }
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},  # 携带标识
    )
    return f"Shutdown request {req_id} sent to '{teammate}' (status: pending)"
```

### 九、三 status (状态追踪)

**目的**: 追踪协议实例的生命周期状态。

**状态流转**:
```
        +----------+
        | pending  |  ← 初始状态：请求已发出，等待响应
        +----+-----+
             |
      +------+------+
      |             |
      v             v
+-----------+   +----------+
| approved  |   | rejected |  ← 终止状态：不再变化
+-----------+   +----------+
```

**实现**:
```python
# s10_team_protocols.py:376-377
with _tracker_lock:
    shutdown_requests[req_id] = {
        "target": teammate,
        "status": "pending"  # 初始状态
    }

# s10_team_protocols.py:390-391
with _tracker_lock:
    req["status"] = "approved" if approve else "rejected"  # 状态转移
```

**设计哲学**: 最小状态机

```
复杂状态机 (不必要):
  pending → waiting_response → reviewing → approved
  pending → waiting_response → reviewing → rejected
  问题：状态过多，维护困难，并发复杂

最小状态机 (s10 采用):
  pending → approved | rejected
  优势：状态少，转移清晰，易于并发安全控制
```

**为什么足够**: 复杂协作不依赖单个状态机的复杂性，而依赖多个独立协议实例的并行执行。每个实例只需三个状态。

### 九、四 sender/receiver (角色标识)

**目的**: 明确标识协议的发送方和接收方，实现基于角色的寻址。

**实现**:
```python
# s10_team_protocols.py:378-381
BUS.send(
    "lead",           # sender: 固定为 lead
    teammate,         # receiver: 目标队友
    "Please shut down gracefully.",
    "shutdown_request",
    {"request_id": req_id},
)

# s10_team_protocols.py:393-394
BUS.send(
    "lead",                          # sender
    req["from"],                     # receiver: 原始请求者
    feedback,
    "plan_approval_response",
    {"request_id": request_id, "approve": approve, "feedback": feedback},
)
```

**角色语义**:
```
关机协议:
  sender = "lead"      (协调者角色)
  receiver = teammate  (执行者角色)

计划审批协议:
  sender = teammate    (提交者角色)
  receiver = "lead"    (审批者角色)
```

**设计哲学**: 显式基于角色的寻址

```
隐式寻址 (不推荐):
  BUS.send(message)  # 谁发送？谁接收？从消息内容推断
  问题：语义模糊，难以路由

显式寻址 (s10 采用):
  BUS.send(sender, receiver, content, msg_type, extra)
  优势：角色清晰，路由简单，易于审计
```

### 九、五 msg_type (消息类型)

**目的**: 区分不同类型的协议消息，实现类型安全的消息分发。

**实现**:
```python
# s10_team_protocols.py:73-79
VALID_MSG_TYPES = {
    "message",              # 普通消息
    "broadcast",            # 广播消息
    "shutdown_request",     # 关机请求
    "shutdown_response",    # 关机响应
    "plan_approval_response",  # 计划审批响应
}
```

**类型分发模式**:
```python
# s10_team_protocols.py:243-278
if tool_name == "shutdown_response":
    req_id = args["request_id"]
    approve = args["approve"]
    # ... 处理关机响应

if tool_name == "plan_approval":
    plan_text = args.get("plan", "")
    req_id = str(uuid.uuid4())[:8]
    # ... 处理计划提交
```

**设计哲学**: 类型安全消息分发

```
无类型消息 (不推荐):
  message = {"content": "..."}  # 接收方需要解析内容判断类型
  问题：运行时错误，难以维护

类型化消息 (s10 采用):
  message = {"type": "shutdown_request", "payload": {...}}
  优势：编译时验证 (通过 VALID_MSG_TYPES)，分发清晰
```

### 九、六 extra payload (扩展载荷)

**目的**: 承载协议特定的数据，提供可扩展的消息格式。

**实现**:
```python
# s10_team_protocols.py:378-381 - 关机请求的 extra
extra = {"request_id": req_id}

# s10_team_protocols.py:393-394 - 计划审批响应的 extra
extra = {
    "request_id": request_id,
    "approve": approve,
    "feedback": feedback,
}

# s10_team_protocols.py:274-276 - 计划提交的 extra
extra = {
    "request_id": req_id,
    "plan": plan_text,
}
```

**设计哲学**: 可扩展消息格式

```
固定字段消息 (不灵活):
  message = {
      "sender": ...,
      "receiver": ...,
      "request_id": ...,  # 如果新协议不需要 request_id?
      "approve": ...,     # 如果新协议不需要 approve?
  }
  问题：字段膨胀，可选字段语义不清

核心 + 扩展消息 (s10 采用):
  message = {
      "sender": ...,       # 核心字段：所有消息共有
      "receiver": ...,     # 核心字段
      "content": ...,      # 核心字段
      "msg_type": ...,     # 核心字段
      "extra": {           # 扩展字段：协议特定
          "request_id": ...,
          "approve": ...,
      }
  }
  优势：核心稳定，扩展灵活
```

### 九、七 tracker (追踪器)

**目的**: 提供集中化的请求状态管理，作为单一事实来源。

**实现**:
```python
# s10_team_protocols.py:96-99
# -- Request trackers: correlate by request_id --
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()
```

**线程安全访问模式**:
```python
# s10_team_protocols.py:376-377
with _tracker_lock:
    shutdown_requests[req_id] = {
        "target": teammate,
        "status": "pending"
    }

# s10_team_protocols.py:386-387
with _tracker_lock:
    req = plan_requests.get(request_id)

# s10_team_protocols.py:390-391
with _tracker_lock:
    req["status"] = "approved" if approve else "rejected"
```

**设计哲学**: 单一事实来源 (Single Source of Truth)

```
分散状态管理 (不推荐):
  状态分散在各处：消息队列、局部变量、全局标志
  问题：状态不一致，难以调试，并发不安全

集中追踪器 (s10 采用):
  shutdown_requests = {req_id: {"target": ..., "status": ...}}
  plan_requests = {req_id: {"from": ..., "status": ...}}
  优势：
    - 单一事实来源：查询 tracker 即可获知所有状态
    - 并发安全：通过 _tracker_lock 保证原子性
    - 易于审计：所有状态变更记录在案
```

**数据结构**:
```
shutdown_requests = {
    "abc123": {
        "target": "dev1",
        "status": "pending"
    },
    "def456": {
        "target": "dev2",
        "status": "approved"
    }
}

plan_requests = {
    "ghi789": {
        "from": "dev1",
        "plan": "refactor module X",
        "status": "rejected"
    }
}
```

### 九、八 handler (处理器)

**目的**: 封装协议特定的业务逻辑，实现关注点分离。

**实现**:
```python
# s10_team_protocols.py:374-382 - 关机请求处理器
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}' (status: pending)"

# s10_team_protocols.py:385-396 - 计划审批处理器
def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"Plan {req['status']} for '{req['from']}'"
```

**工具分发映射**:
```python
# s10_team_protocols.py:405-418
TOOL_HANDLERS = {
    "bash":              lambda **kw: _run_bash(kw["command"]),
    "read_file":         lambda **kw: _run_read(kw["path"], kw.get("limit")),
    # ...
    "shutdown_request":  lambda **kw: handle_shutdown_request(kw["teammate"]),
    "shutdown_response": lambda **kw: _check_shutdown_status(kw.get("request_id", "")),
    "plan_approval":     lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
}
```

**设计哲学**: 关注点分离 (Separation of Concerns)

```
单体处理 (不推荐):
  def process_message(msg):
      if msg["type"] == "shutdown_request":
          req_id = uuid()
          tracker[req_id] = ...
          send_message(...)
      elif msg["type"] == "plan_approval":
          ...
      # 数百行代码混在一起

处理器分离 (s10 采用):
  handle_shutdown_request()  → 专注关机逻辑
  handle_plan_review()       → 专注审批逻辑
  TOOL_HANDLERS              → 专注分发逻辑
  优势：
    - 每个函数职责单一
    - 易于测试
    - 易于扩展新协议
```

### 九、九 原语组合模式

协议原语通过组合形成完整的协议实例。以下是三种典型组合模式：

**模式一：关机协议组合**
```
shutdown_request 协议:
  request_id  → "abc123"          (关联标识)
  sender      → "lead"            (协调者发起)
  receiver    → "dev1"            (目标执行者)
  msg_type    → "shutdown_request"(消息类型)
  tracker     → shutdown_requests (状态追踪)

消息结构:
  {
      "sender": "lead",
      "receiver": "dev1",
      "content": "Please shut down gracefully.",
      "msg_type": "shutdown_request",
      "extra": {"request_id": "abc123"}
  }

追踪器状态:
  shutdown_requests["abc123"] = {"target": "dev1", "status": "pending"}
```

**模式二：关机响应组合**
```
shutdown_response 协议:
  request_id  → "abc123"          (关联原请求)
  status      → "approved"        (审批结果)
  extra       → {"approve": true} (响应载荷)

消息结构:
  {
      "sender": "dev1",
      "receiver": "lead",
      "content": "Shutdown approved",
      "msg_type": "shutdown_response",
      "extra": {"request_id": "abc123", "approve": true}
  }

追踪器更新:
  shutdown_requests["abc123"]["status"] = "approved"
```

**模式三：计划审批组合**
```
plan_approval 协议:
  request_id  → "ghi789"          (关联标识)
  sender      → "dev1"            (提交者发起)
  receiver    → "lead"            (审批者)
  plan        → "refactor X"      (计划内容)
  tracker     → plan_requests     (状态追踪)

消息结构:
  {
      "sender": "dev1",
      "receiver": "lead",
      "content": "refactor X",
      "msg_type": "plan_approval_response",
      "extra": {"request_id": "ghi789", "plan": "refactor X"}
  }

追踪器状态:
  plan_requests["ghi789"] = {"from": "dev1", "plan": "refactor X", "status": "pending"}
```

### 九、十 原语关系图

**ASCII 关系图**:
```
┌─────────────────────────────────────────────────────────────────┐
│                        协议原语生态系统                          │
└─────────────────────────────────────────────────────────────────┘

┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  request_id  │────>│   tracker    │<────│    status    │
│  (标识生成)  │     │ (状态存储)   │     │ (状态值)     │
└──────────────┘     └──────┬───────┘     └──────────────┘
                           │
                           │ 被访问
                           │
┌──────────────┐     ┌──────▼───────┐     ┌──────────────┐
│ sender/      │────>│   handler    │<────│    extra     │
│ receiver     │     │  (处理逻辑)  │     │   payload    │
└──────────────┘     └──────┬───────┘     └──────────────┘
                           │
                           │ 使用
                           │
                     ┌─────▼──────┐
                     │  msg_type  │
                     │ (类型分发) │
                     └────────────┘

数据流:
  1. handler 生成 request_id
  2. handler 写入 tracker (带 status)
  3. handler 构建消息 (sender, receiver, msg_type, extra)
  4. 接收方 handler 处理消息
  5. handler 更新 tracker 的 status
```

**协议实例生命周期**:
```
时间轴:
  t0: lead 调用 handle_shutdown_request("dev1")
      ├─ 生成 request_id = "abc123"
      ├─ 写入 tracker: shutdown_requests["abc123"] = {status: "pending"}
      └─ 发送消息: BUS.send("lead", "dev1", ..., "shutdown_request", {request_id: "abc123"})

  t1: dev1 收到消息，调用 shutdown_response 工具
      ├─ 读取 tracker: shutdown_requests["abc123"]
      ├─ 更新 status: "pending" → "approved"
      └─ 发送响应: BUS.send("dev1", "lead", ..., "shutdown_response", {request_id: "abc123", approve: true})

  t2: lead 查询状态
      └─ 读取 tracker: shutdown_requests["abc123"]["status"] = "approved"
```

### 九、十一 设计原则总结

**1. 正交性原则**
每个原语只负责一个正交维度：
- `request_id`: 标识维度
- `status`: 状态维度
- `sender/receiver`: 角色维度
- `msg_type`: 类型维度
- `extra`: 数据维度
- `tracker`: 存储维度
- `handler`: 逻辑维度

**2. 最小化原则**
每个原语都是最小必要设计：
- `request_id`: 8 位 UUID，不多不少
- `status`: 三状态，不多不少
- `tracker`: 字典 + 锁，不多不少

**3. 组合性原则**
复杂协议 = 简单原语的组合：
- 关机协议 = 7 个原语的标准组合
- 计划审批 = 7 个原语的变体组合
- 新协议 = 复用现有原语的新组合

**4. 类型安全原则**
通过枚举和验证保证类型安全：
- `VALID_MSG_TYPES` 枚举所有合法类型
- 运行时验证消息类型
- 防止非法消息注入

**5. 并发安全原则**
通过锁机制保证并发安全：
- `_tracker_lock` 保护所有 tracker 访问
- `with _tracker_lock:` 统一模式
- 防止竞态条件

---

**核心洞见**: 协议系统的强大不在于原语本身的复杂，而在于原语的组合能力。七个简单原语通过正交组合，可以支撑任意复杂的团队协作场景。这正是 s10 的设计精髓：**简单 FSM × 请求追踪 × 协议语义 = 复杂协作**。
