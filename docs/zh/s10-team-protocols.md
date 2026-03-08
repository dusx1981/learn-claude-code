# s10: Team Protocols (团队协议)

> **注意**: 本文档已更新为 Qwen API (OpenAI 兼容)。原 Claude API 已不再使用。

`s01 > s02 > s03 > s04 > s05 > s06 | s07 > s08 > s09 > [ s10 ] s11 > s12`

> *"队友之间要有统一的沟通规矩"* -- 一个 request-response 模式驱动所有协商。

## 问题

s09 中队友能干活能通信, 但缺少结构化协调:

**关机**: 直接杀线程会留下写了一半的文件和过期的 config.json。需要握手 -- 领导请求, 队友批准 (收尾退出) 或拒绝 (继续干)。

**计划审批**: 领导说 "重构认证模块", 队友立刻开干。高风险变更应该先过审。

两者结构一样: 一方发带唯一 ID 的请求, 另一方引用同一 ID 响应。

## 解决方案

```
Shutdown Protocol            Plan Approval Protocol
==================           ======================

Lead             Teammate    Teammate           Lead
  |                 |           |                 |
  |--shutdown_req-->|           |--plan_req------>|
  | {req_id:"abc"}  |           | {req_id:"xyz"}  |
  |                 |           |                 |
  |<--shutdown_resp-|           |<--plan_resp-----|
  | {req_id:"abc",  |           | {req_id:"xyz",  |
  |  approve:true}  |           |  approve:true}  |

Shared FSM:
  [pending] --approve--> [approved]
  [pending] --reject---> [rejected]

Trackers:
  shutdown_requests = {req_id: {target, status}}
  plan_requests     = {req_id: {from, plan, status}}
```

## 可视化流程图

### Shutdown Protocol (关机协议)

```mermaid
sequenceDiagram
    autonumber
    participant Lead as Lead (领导)
    participant Bus as MessageBus (消息总线)
    participant Teammate as Teammate (队友)
    participant Tracker as shutdown_requests

    Lead->>Tracker: 生成 request_id
    Tracker-->>Lead: req_id = "abc"
    Lead->>Bus: send(shutdown_request)
    Note right of Lead: type: shutdown_request<br/>request_id: "abc"
    Bus->>Teammate: 投递到 inbox
    Teammate->>Teammate: 读取 inbox
    Teammate->>Teammate: 决定: approve/reject
    
    alt 批准关机
        Teammate->>Bus: send(shutdown_response)
        Note right of Teammate: request_id: "abc"<br/>approve: true
        Bus->>Lead: 投递到 lead inbox
        Teammate->>Tracker: status = "approved"
        Teammate->>Teammate: 收尾工作后退出
        Lead->>Lead: 确认关机成功
    else 拒绝关机
        Teammate->>Bus: send(shutdown_response)
        Note right of Teammate: request_id: "abc"<br/>approve: false
        Bus->>Lead: 投递到 lead inbox
        Teammate->>Tracker: status = "rejected"
        Teammate->>Teammate: 继续工作
        Lead->>Lead: 收到拒绝响应
    end
```

### Plan Approval Protocol (计划审批协议)

```mermaid
sequenceDiagram
    autonumber
    participant Teammate as Teammate (队友)
    participant Bus as MessageBus (消息总线)
    participant Lead as Lead (领导)
    participant Tracker as plan_requests

    Teammate->>Tracker: 生成 request_id
    Tracker-->>Teammate: req_id = "xyz"
    Teammate->>Bus: send(plan_approval)
    Note right of Teammate: type: plan_approval_response<br/>request_id: "xyz"<br/>plan: "重构方案..."
    Bus->>Lead: 投递到 lead inbox
    Lead->>Lead: 读取 inbox
    Lead->>Lead: 审查计划内容
    
    alt 批准计划
        Lead->>Tracker: status = "approved"
        Lead->>Bus: send(plan_approval_response)
        Note right of Lead: request_id: "xyz"<br/>approve: true<br/>feedback: "可以开始"
        Bus->>Teammate: 投递到 teammate inbox
        Teammate->>Teammate: 收到批准, 开始执行
    else 拒绝计划
        Lead->>Tracker: status = "rejected"
        Lead->>Bus: send(plan_approval_response)
        Note right of Lead: request_id: "xyz"<br/>approve: false<br/>feedback: "风险太高"
        Bus->>Teammate: 投递到 teammate inbox
        Teammate->>Teammate: 收到拒绝, 修改或放弃
    end
```

### 共享状态机 (Shared FSM)

```mermaid
stateDiagram-v2
    [*] --> pending: 创建请求<br/>生成 request_id
    pending --> approved: approve = true
    pending --> rejected: approve = false
    approved --> [*]: 关机/执行计划
    rejected --> [*]: 继续/修改
    
    note right of pending
        等待响应
        Trackers 记录状态
    end note
    
    note right of approved
        Shutdown: 队友收尾退出
        Plan: 队友开始执行
    end note
    
    note right of rejected
        Shutdown: 队友继续工作
        Plan: 队友修改或放弃
    end note
```

### 请求关联机制

```mermaid
flowchart TB
    subgraph Lead侧
        L1[handle_shutdown_request] --> L2[生成 request_id]
        L2 --> L3[记录到 shutdown_requests]
        L3 --> L4[发送 shutdown_request]
        
        L5[handle_plan_review] --> L6[查找 plan_requests]
        L6 --> L7[更新 status]
        L7 --> L8[发送 plan_approval_response]
    end
    
    subgraph Teammate侧
        T1[接收 shutdown_request] --> T2[提取 request_id]
        T2 --> T3[决定 approve/reject]
        T3 --> T4[发送 shutdown_response]
        T4 --> T5[引用同一 request_id]
        
        T6[生成计划] --> T7[生成 request_id]
        T7 --> T8[记录到 plan_requests]
        T8 --> T9[发送 plan_approval]
    end
    
    subgraph Trackers
        TR1[shutdown_requests]
        TR2[plan_requests]
    end
    
    L4 -.->|request_id| T1
    T5 -.->|request_id| L5
    T9 -.->|request_id| L6
    
    L3 --> TR1
    T4 --> TR1
    T8 --> TR2
    L7 --> TR2
```

## 工作原理

1. 领导生成 request_id, 通过收件箱发起关机请求。

```python
shutdown_requests = {}

def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "Please shut down gracefully.",
             "shutdown_request", {"request_id": req_id})
    return f"Shutdown request {req_id} sent (status: pending)"
```

2. 队友收到请求后, 用 approve/reject 响应。

```python
if tool_name == "shutdown_response":
    req_id = args["request_id"]
    approve = args["approve"]
    shutdown_requests[req_id]["status"] = "approved" if approve else "rejected"
    BUS.send(sender, "lead", args.get("reason", ""),
             "shutdown_response",
             {"request_id": req_id, "approve": approve})
```

3. 计划审批遵循完全相同的模式。队友提交计划 (生成 request_id), 领导审查 (引用同一个 request_id)。

```python
plan_requests = {}

def handle_plan_review(request_id, approve, feedback=""):
    req = plan_requests[request_id]
    req["status"] = "approved" if approve else "rejected"
    BUS.send("lead", req["from"], feedback,
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
```

一个 FSM, 两种用途。同样的 `pending -> approved | rejected` 状态机可以套用到任何请求-响应协议上。

## 相对 s09 的变更

| 组件           | 之前 (s09)       | 之后 (s10)                           |
|----------------|------------------|--------------------------------------|
| Tools          | 9                | 12 (+shutdown_req/resp +plan)        |
| 关机           | 仅自然退出       | 请求-响应握手                        |
| 计划门控       | 无               | 提交/审查与审批                      |
| 关联           | 无               | 每个请求一个 request_id              |
| FSM            | 无               | pending -> approved/rejected         |

## 试一试

```sh
cd learn-claude-code
python agents/s10_team_protocols.py
```

试试这些 prompt (英文 prompt 对 LLM 效果更好, 也可以用中文):

1. `Spawn alice as a coder. Then request her shutdown.`
2. `List teammates to see alice's status after shutdown approval`
3. `Spawn bob with a risky refactoring task. Review and reject his plan.`
4. `Spawn charlie, have him submit a plan, then approve it.`
5. 输入 `/team` 监控状态
