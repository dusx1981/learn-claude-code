# s10 团队协议问题分析

> *"一个关于"我以为他懂了"的故事"*

---

## 背景介绍

### 什么是 s10?

s10 是一个**多智能体协作系统**,模拟一个领导(Lead)管理多个队员(Teammate)的工作场景。

**核心组件:**

```
┌─────────────────────────────────────────────────────────────┐
│                         系统架构                              │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│    用户                                                       │
│      │                                                       │
│      ▼                                                       │
│  ┌─────────────┐                                           │
│  │   Lead      │ ◄── 团队的领导者,管理所有队员                │
│  │  (主线程)    │                                           │
│  └─────────────┘                                           │
│      │                                                       │
│      │ spawn / send_message / shutdown_request              │
│      ▼                                                       │
│  ┌─────────────┐     ┌─────────────┐     ┌─────────────┐   │
│  │   alice     │     │    bob      │     │   charlie   │   │
│  │  (线程 1)   │     │  (线程 2)   │     │  (线程 3)   │   │
│  └─────────────┘     └─────────────┘     └─────────────┘   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 什么是关机协议?

在团队协作中,领导需要能够安全地停止队员的工作,就像这样:

```
领导说: "alice, 请关机"
    │
    ▼
alice 回应: "好的,我同意关机" (或 "拒绝关机,我要继续工作")
    │
    ▼
如果 alice 同意,她的工作线程就退出
```

---

## 这次实验的目标

**用户输入**: "Spawn alice as a coder. Then request her shutdown."

**期望流程**:

```
1. 领导创建一个叫 alice 的队员
2. 领导发送关机请求给 alice
3. alice 收到请求,回复 shutdown_response
4. alice 的线程退出
5. 领导继续工作
```

---

## 实际发生了什么

### 时间线(简化版)

```
时间    角色      事件
────────────────────────────────────────────────────────────
11:33   用户      输入命令
11:33   领导      创建 alice 线程
11:33   领导      发送关机请求给 alice
11:33   alice    收到关机请求
11:40   alice    线程退出 (没有回复关机请求!)
11:40   领导      线程也退出了
```

### 用通俗的话来说

**这就是发生的事:**

1. **领导**叫 alice 过来工作 ✅
2. **领导**对 alice 说 "我要你关机" ✅
3. **alice** 收到了 "关机" 的消息 ❌
4. **alice** 什么都没说,直接走掉了 ❌
5. **领导**以为工作完成了,也走掉了 ❌

---

## 问题分析

### 问题 1: alice 为什么不回复?

**原因**: alice 收到关机请求后,她的"大脑"(LLM) 觉得:

> "嗯,我现在没有具体的工作要做。那个关机请求只是一个通知,不是命令。我可以直接退出。"

**这就好像:**
- 领导发邮件给 alice: "请确认收到"
- alice 看了邮件,觉得"这不是紧急的事"
- alice 直接关闭电脑走人了
- 没有回复"已收到"

### 问题 2: 领导为什么也不等?

**原因**: 领导发送关机请求后,立刻继续做其他事:

```python
# 领导的代码(简化)
def handle_shutdown_request(teammate):
    send_message(teammate, "请关机")  # 发送消息
    return "已发送请求"                  # 立即返回,不等回复!
```

**这就好像:**
- 领导发邮件: "alice, 请关机"
- 领导没有等 alice 回复
- 领导直接去做别的事了
- 即使 alice 永远不会回复,领导也不知道

---

## 完整的错误流程图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        实际发生的事                                  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  用户                                                                │
│    │                                                                 │
│    ▼                                                                 │
│  领导                                                                │
│    │                                                                 │
│    ├─► 创建 alice ────────────────────────────────────────────┐    │
│    │                                                          │    │
│    │                                                          ▼    │
│    │  发送 shutdown_request ──────────────────────────────────┐    │
│    │  (请求ID: f13b95b9)                                     │    │
│    │                                                          │    │
│    │  ▼                                                       │    │
│    │  立即返回,继续工作 ─────────────────────────────────────┐ │    │
│    │  (不等待回复)                                          │ │    │
│    │                                                        │ │    │
│    │                                                        ▼ ▼    │
│  alice                                                             │
│    │                                                            │
│    ├─ 收到 shutdown_request                                    │
│    │                                                            │
│    ├─ 她的"大脑"想: "没有具体工作要做,我可以退出"              │
│    │                                                            │
│    └─ 直接退出 (没有回复!) ❌                                    │
│                                                                      │
│    ▼                                                                 │
│  领导的下一次循环                                                     │
│    │                                                              │
│    ├─ 检查收件箱: 空                                               │
│    │                                                              │
│    └─ "没有新消息,工作完成,退出" ❌                               │
│                                                                      │
│                                                                      │
│  结果: 协议从未真正完成,但双方都"正常"退出了                      │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 问题的根本原因

### 原因 1: 协议是"建议"而不是"命令"

**现状**:
- 领导发送关机请求是"建议 alice 考虑关机"
- alice 可以选择忽略或接受

**问题**:
- 没有强制 alice 必须回复
- 没有强制 alice 必须服从

**就像:**
- 领导发邮件: "建议你考虑关机"
- alice 可以回: "好的" 或 直接无视

### 原因 2: 领导不知道要等待确认

**现状**:
- 领导发送请求后立刻返回
- 领导不检查请求是否被处理

**问题**:
- 领导假设 alice 会回复
- 领导不知道 alice 可能根本不回复

**就像:**
- 领导发邮件后,不检查是否收到回复
- 直接去做其他事

### 原因 3: 没有同步机制

**现状**:
- 领导 → alice 的通信是"发信"
- 没有"收信回执"

**问题**:
- 领导无法确认请求是否被处理
- 双方无法协调工作

---

## 解决方案(简单版)

### 方案 1: 强制回复

**alice 端修改**:
- 当收到 shutdown_request,必须回复
- 不回复就不能退出

### 方案 2: 领导等待确认

**领导端修改**:
- 发送请求后,等待 alice 的回复
- 超时或收到回复后才继续

### 方案 3: 明确指令

**修改 system prompt**:
- 告诉 alice: "收到关机请求必须回复"
- 用明确的规则,而不是建议

---

## 预期的正确流程

```
修改后应该发生的事:

1. 用户输入命令
2. 领导创建 alice
3. 领导发送 shutdown_request
4. 【关键】领导等待 alice 的回复
5. alice 收到请求
6. 【关键】alice 必须调用 shutdown_response 工具回复
7. 领导收到回复,确认协议完成
8. alice 退出
9. 领导继续工作
```

---

## 对比表

| 方面 | 当前实现 | 期望实现 |
|-----|---------|---------|
| alice 收到关机请求 | 可以忽略 | 必须回复 |
| 领导发送请求后 | 立即返回 | 等待确认 |
| 协议状态 | 不追踪 | 明确追踪 |
| 退出条件 | inbox 为空 | 协议完成 + inbox 为空 |

---

## 总结

**这次实验暴露的问题:**

1. **沟通是单向的**: 领导发了消息,但不知道对方是否收到
2. **没有强制机制**: alice 可以选择不回复
3. **假设代替确认**: 领导假设 alice 会回复,而不是确认她真的回复了

**教训:**

- 在分布式系统中,不要假设消息被处理
- 重要的操作需要确认,不能只是发送
- 协议必须是强制性的,不能是可选的

**下一步:**

- 实现同步等待机制
- 强制协议响应
- 添加状态追踪

---

## 技术细节分析

### 实际日志时序

```
时间              线程          日志内容
────────────────────────────────────────────────────────────────────
11:33:29.000     MainThread    用户输入: Spawn alice as a coder...
11:33:33.000     MainThread    [Lead] 执行 spawn_teammate
11:33:33.000     Thread-6      [alice] Teammate loop started
11:33:33.000     MainThread    [Lead] 执行 shutdown_request
11:33:33.000     MainThread    [MessageBus] Message sent: lead -> alice
11:33:33.001     Thread-6      [alice] Iteration 1: checking inbox
11:33:33.001     Thread-6      [alice] 收到消息 (shutdown_request)
11:33:40.000     Thread-6      [alice] LLM response: finish_reason=stop
11:33:40.000     Thread-6      [alice] No tool calls, finishing
11:33:40.001     MainThread    [Lead] No tool calls, finishing
```

### Lead 端代码分析

**当前实现 (有问题):**

```python
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "Please shut down gracefully.",
             "shutdown_request", {"request_id": req_id})
    return f"Shutdown request {req_id} sent to '{teammate}' (status: pending)"
    # ↑ 问题: 立即返回,不等待响应!
```

**问题:**
1. 函数发送请求后立即返回
2. 不检查 `shutdown_requests[req_id]["status"]` 是否变为 "approved" 或 "rejected"
3. Lead 的下一次循环只检查 inbox,不检查协议状态

### alice 端代码分析

**当前实现 (有问题):**

```python
def _teammate_loop(self, name: str, role: str, prompt: str):
    for _ in range(50):
        inbox = BUS.read_inbox(name)
        for msg in inbox:
            messages.append({"role": "user", "content": json.dumps(msg)})
        
        # ... LLM 调用 ...
        
        if choice.finish_reason != "tool_calls":
            break  # ↑ 问题: LLM 不调用工具就直接退出!
```

**问题:**
1. 即使收到协议消息,如果 LLM 不调用工具,循环直接退出
2. 没有强制处理特定消息类型的机制
3. System prompt 不够明确,LLM 可能忽略协议

---

## 漏洞总结表

| 漏洞 | 位置 | 严重程度 | 描述 |
|-----|------|---------|------|
| Lead 不等待响应 | `handle_shutdown_request()` | **极高** | 发送请求后立即返回 |
| Lead 不检查状态 | `agent_loop()` | 高 | 只检查 inbox,不检查 `shutdown_requests` |
| alice 无强制响应 | `_teammate_loop()` | **极高** | LLM 可选择不调用工具 |
| System Prompt 不明确 | `_teammate_loop()` | 中 | 没有强制要求响应协议 |

---

## 完整修复方案

### 修复 1: Lead 端同步等待

```python
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "Please shut down gracefully.",
             "shutdown_request", {"request_id": req_id})
    
    # 新增: 等待响应
    logger.info(f"[Lead] Waiting for shutdown response from {teammate}")
    timeout = 30
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        with _tracker_lock:
            status = shutdown_requests.get(req_id, {}).get("status")
        if status == "approved":
            return f"Shutdown approved by {teammate}"
        if status == "rejected":
            return f"Shutdown rejected by {teammate}"
        time.sleep(0.5)
    
    return f"Timeout: no response from {teammate}"
```

### 修复 2: alice 端强制响应

```python
def _teammate_loop(self, name: str, role: str, prompt: str):
    # 追踪协议请求
    pending_protocols = {}  # request_id -> msg_type
    
    for _ in range(50):
        inbox = BUS.read_inbox(name)
        for msg in inbox:
            messages.append({"role": "user", "content": json.dumps(msg)})
            
            # 记录协议请求
            if msg.get("type") == "shutdown_request":
                req_id = msg.get("extra", {}).get("request_id")
                if req_id:
                    pending_protocols[req_id] = "shutdown_request"
                    logger.info(f"[{name}] Pending shutdown: {req_id}")
        
        # ... LLM 调用 ...
        
        # 检查退出条件
        if choice.finish_reason != "tool_calls":
            if pending_protocols:
                # 还有未处理的协议,不能退出!
                logger.warning(f"[{name}] Cannot exit: pending protocols {pending_protocols}")
                continue  # 继续循环
            break
```

### 修复 3: 增强 System Prompt

```python
sys_prompt = f"""
You are '{name}', role: {role}, at {WORKDIR}.

## 强制规则 (必须遵守)

当收到 shutdown_request 消息时:
1. 立即提取 request_id
2. 调用 shutdown_response 工具,参数: request_id, approve=true
3. 这是强制要求,不能忽略

当收到 plan_approval_response 消息时:
1. 立即提取 request_id
2. 调用 plan_approval 工具提交计划
3. 这是强制要求,不能忽略

## 重要
- 你不能忽略协议消息
- 在回复协议之前不能退出

## 工作规则
- Submit plans via plan_approval before major work
"""
```

---

## 验证测试用例

### 测试 1: 正常关机流程

```
输入: Spawn alice as coder. Then request her shutdown.

期望输出:
  [Lead] Spawning teammate: alice
  [Lead] Sending shutdown request to: alice
  [Lead] Waiting for shutdown response...
  [alice] Pending shutdown: xxxxxxxx
  [alice] Executing tool: shutdown_response
  [Lead] Shutdown approved by alice
  [alice] Loop ended, status: shutdown
```

### 测试 2: alice 拒绝关机

```
输入: Spawn bob as coder. Send him a difficult task. Then request shutdown.

期望输出:
  [bob] Executing task...
  [Lead] Sending shutdown request to: bob
  [bob] Pending shutdown: xxxxxxxx
  [bob] shutdown_response: approve=false, reason="I'm still working"
  [Lead] Shutdown rejected by bob
  [bob] Continuing work...
```

### 测试 3: 超时处理

```
输入: Spawn charlie. Request shutdown (模拟 charlie 不响应)

期望输出:
  [Lead] Sending shutdown request to: charlie
  [Lead] Waiting for shutdown response...
  (30秒后)
  [Lead] Timeout: no response from charlie
```

---

## 架构图: 修复前后对比

### 修复前

```
Lead                          alice
  │                             │
  ├── spawn ──────────────────►│
  │                             │
  ├── shutdown_request ───────►│ (收到)
  │                             │
  │◄── 立即返回 ────────────────┤ (不等)
  │                             │
  │                             ├── 不响应
  │                             │
  ├── 检查 inbox ──────────────►│ 空
  │                             │
  └── 退出 ❌                    └── 退出 ❌
```

### 修复后

```
Lead                          alice
  │                             │
  ├── spawn ──────────────────►│
  │                             │
  ├── shutdown_request ───────►│ (收到)
  │                             │
  │   等待中...                 ├── 检测到协议
  │   (检查 status)            │
  │                             ├── shutdown_response ──►│
  │                             │
  │◄── 收到响应 ────────────────┤ status=approved
  │                             │
  │                             └── 退出 ✓
  └── 继续工作 ✓
```

---

## 关键结论

1. **协议必须是同步的**: 发送方必须等待确认,不能假设接收方会响应
2. **协议响应是强制的**: 接收方不能选择忽略,必须有代码级别的强制机制
3. **状态追踪是必要的**: 双方都需要知道协议的当前状态
4. **超时处理是必需的**: 防止永久等待,提供回退机制
