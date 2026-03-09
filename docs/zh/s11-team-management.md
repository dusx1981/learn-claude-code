# S11 Autonomous Agents - 团队管理机制详解

> 本文档详细解析 `agents/s11_autonomous_agents.py` 中的团队管理系统，包括成员生命周期、状态管理、配置持久化等核心机制。

---

## 目录

1. [架构概览](#架构概览)
2. [TeammateManager 核心类](#teammatemanager-核心类)
3. [成员生命周期管理](#成员生命周期管理)
4. [配置持久化系统](#配置持久化系统)
5. [线程管理与并发控制](#线程管理与并发控制)
6. [实际运行示例](#实际运行示例)
7. [设计决策与考量](#设计决策与考量)

---

## 架构概览

### 团队管理整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                    Team Management System                   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────────┐        ┌──────────────────────────┐  │
│  │  TeammateManager │        │      config.json         │  │
│  │  ─────────────── │        │      ───────────         │  │
│  │  • spawn()       │◄──────►│  team_name: "default"    │  │
│  │  • _loop()       │        │  members: [              │  │
│  │  • _set_status() │        │    {name, role, status}  │  │
│  └────────┬─────────┘        │  ]                       │  │
│           │                   └──────────────────────────┘  │
│           │                          ▲                      │
│           │                          │ _save_config()       │
│           ▼                          │ _load_config()       │
│  ┌──────────────────┐               │                      │
│  │   Member Thread  │               │                      │
│  │   ─────────────  │               │                      │
│  │  ┌────────────┐  │               │                      │
│  │  │  WORK      │  │               │                      │
│  │  │  (active)  │  │               │                      │
│  │  └─────┬──────┘  │               │                      │
│  │        │ idle    │               │                      │
│  │        ▼         │               │                      │
│  │  ┌────────────┐  │               │                      │
│  │  │  IDLE      │  │               │                      │
│  │  │ (polling)  │  │               │                      │
│  │  └─────┬──────┘  │               │                      │
│  │        │ timeout │               │                      │
│  │        ▼         │               │                      │
│  │  ┌────────────┐  │               │                      │
│  │  │  SHUTDOWN  │  │───────────────┘                      │
│  │  │ (terminal) │  │     _set_status(name, "shutdown")    │
│  │  └────────────┘  │                                      │
│  └──────────────────┘                                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 核心组件关系

```
┌─────────────────┐     spawn      ┌──────────────────┐
│   User/Lead     │───────────────►│  TeammateManager │
│                 │                │                  │
│ spawn_teammate  │                │  • _load_config  │
│ list_teammates  │◄───────────────│  • _save_config  │
│ shutdown_request│                │  • _set_status   │
└─────────────────┘                └────────┬─────────┘
                                            │
                              ┌─────────────┼─────────────┐
                              │             │             │
                              ▼             ▼             ▼
                        ┌─────────┐  ┌─────────┐  ┌─────────┐
                        │ Thread  │  │ Thread  │  │ Thread  │
                        │ (alice) │  │  (bob)  │  │ (charlie)
                        └────┬────┘  └────┬────┘  └────┬────┘
                             │            │            │
                             └────────────┴────────────┘
                                          │
                                          ▼
                                  ┌───────────────┐
                                  │  config.json  │
                                  │   (shared)    │
                                  └───────────────┘
```

---

## TeammateManager 核心类

### 类定义与初始化

```python
class TeammateManager:
    """自治队友管理器 - 负责团队成员的生命周期和状态管理"""
    
    def __init__(self, team_dir: Path):
        """
        初始化团队管理器
        
        Args:
            team_dir: 团队配置目录路径 (.team/)
        """
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)                    # 确保目录存在
        self.config_path = self.dir / "config.json"      # 配置文件路径
        self.config = self._load_config()                # 加载现有配置
        self.threads = {}                                # 线程缓存 {name: Thread}
        
        logger.info(f"TeammateManager initialized: team_dir={team_dir}, "
                   f"team_name={self.config.get('team_name', 'default')}")
```

**关键设计点**：
- `team_dir` 默认为 `.team/` 目录
- `threads` 字典缓存活动线程，但不保存线程句柄到配置文件
- 配置延迟加载，只在需要时读取文件

### 配置加载与保存

```python
def _load_config(self) -> dict:
    """从磁盘加载团队配置"""
    if self.config_path.exists():
        try:
            config = json.loads(self.config_path.read_text())
            logger.debug(f"Config loaded: {len(config.get('members', []))} member(s)")
            return config
        except Exception as e:
            logger.error(f"Error loading config: {e}, using default")
    
    # 默认配置
    logger.debug("Config not found, using default")
    return {"team_name": "default", "members": []}

def _save_config(self):
    """保存团队配置到磁盘"""
    try:
        self.config_path.write_text(json.dumps(self.config, indent=2))
        logger.debug(f"Config saved: {len(self.config.get('members', []))} member(s)")
    except Exception as e:
        logger.error(f"Error saving config: {e}")
```

**持久化策略**：
- 使用 JSON 格式，便于人工阅读和调试
- 原子写入：先写内存，再一次性写文件
- 失败回退：加载失败时使用默认空配置

---

## 成员生命周期管理

### 状态机定义

```
                    ┌─────────┐
     spawn()        │  INIT   │
    ┌──────────────►│ (trans) │
    │               └────┬────┘
    │                    │
    │                    ▼
    │               ┌─────────┐
    │    ┌─────────│ WORKING │◄────────────┐
    │    │         │ (active)│             │
    │    │ resume  └────┬────┘             │
    │    │               │ idle             │
    │    │               ▼                  │
    │    │          ┌─────────┐   timeout   │
    │    │          │  IDLE   │─────────────┼──► SHUTDOWN
    │    │          │ (poll)  │             │
    │    │          └────┬────┘             │
    │    │               │ message/task     │
    │    └───────────────┘                  │
    │                                       │
    └───────────────────────────────────────┘
         (shutdown_request or timeout)
```

### 成员数据结构

```python
# config.json 中的成员格式
{
  "name": "alice",        # 成员名称（唯一标识）
  "role": "coder",        # 角色描述
  "status": "working"     # 状态: working | idle | shutdown
}
```

**状态说明**：

| 状态 | 含义 | 转换条件 |
|------|------|----------|
| `working` | 正在执行工作 | spawn() 或从 idle 恢复 |
| `idle` | 空闲轮询中 | WORK 阶段调用 idle 工具 |
| `shutdown` | 已停止 | 超时或收到关机请求 |

### 创建成员 (spawn)

```python
def spawn(self, name: str, role: str, prompt: str) -> str:
    """
    创建并启动一个新的队友
    
    Args:
        name: 队友名称（唯一标识）
        role: 角色描述（如 "coder", "tester"）
        prompt: 初始任务提示
    
    Returns:
        操作结果消息
    """
    logger.info(f"Spawn request: name={name}, role={role}, prompt_length={len(prompt)}")
    
    # 1. 检查是否已存在同名成员
    member = self._find_member(name)
    if member:
        current_status = member["status"]
        logger.debug(f"Member exists: {name}, current_status={current_status}")
        
        # 只允许复活 idle 或 shutdown 状态的成员
        if member["status"] not in ("idle", "shutdown"):
            logger.warning(f"Spawn failed: '{name}' is currently {member['status']}")
            return f"Error: '{name}' is currently {member['status']}"
        
        # 复活现有成员
        member["status"] = "working"
        member["role"] = role
        logger.debug(f"Member revived: {name}, status set to working")
    else:
        # 创建新成员
        member = {"name": name, "role": role, "status": "working"}
        self.config["members"].append(member)
        logger.debug(f"New member created: {name}")
    
    # 2. 保存配置
    self._save_config()
    
    # 3. 创建并启动守护线程
    thread = threading.Thread(
        target=self._loop,           # 目标函数
        args=(name, role, prompt),   # 传递参数
        daemon=True,                 # 守护线程
    )
    self.threads[name] = thread
    thread.start()
    
    logger.info(f"Spawn complete: '{name}' started (thread_id={thread.ident})")
    return f"Spawned '{name}' (role: {role})"
```

**关键行为**：
1. **成员复用**：同名成员如果处于 idle/shutdown 状态可以复活
2. **状态互斥**：working 状态的成员不能重复 spawn
3. **守护线程**：daemon=True 确保主程序退出时线程自动终止
4. **线程缓存**：threads 字典保存线程对象便于追踪

### 状态更新 (_set_status)

```python
def _set_status(self, name: str, status: str):
    """
    更新成员状态并持久化
    
    Args:
        name: 成员名称
        status: 新状态 (working | idle | shutdown)
    """
    logger.debug(f"Status change: {name} -> {status}")
    member = self._find_member(name)
    
    if member:
        old_status = member.get("status")
        member["status"] = status
        self._save_config()  # 立即持久化
        logger.info(f"Status updated: {name} {old_status} -> {status}")
    else:
        logger.warning(f"Status change failed: member '{name}' not found")
```

**设计考量**：
- 每次状态变更立即写盘，确保数据一致性
- 记录状态转换日志，便于调试
- 静默处理不存在的成员（记录警告而非报错）

---

## 配置持久化系统

### 文件结构

```
.team/
├── config.json          # 团队配置（成员列表、状态）
└── inbox/
    ├── alice.jsonl      # alice 的收件箱
    ├── bob.jsonl        # bob 的收件箱
    └── lead.jsonl       # 领导的收件箱
```

### config.json 示例

```json
{
  "team_name": "my-dev-team",
  "members": [
    {
      "name": "alice",
      "role": "frontend-developer",
      "status": "working"
    },
    {
      "name": "bob",
      "role": "backend-developer", 
      "status": "idle"
    },
    {
      "name": "charlie",
      "role": "tester",
      "status": "shutdown"
    }
  ]
}
```

### 配置操作工具

```python
def _find_member(self, name: str) -> dict | None:
    """根据名称查找成员"""
    for m in self.config["members"]:
        if m["name"] == name:
            return m
    return None

def list_all(self) -> str:
    """列出所有团队成员（CLI 输出格式）"""
    if not self.config["members"]:
        return "No teammates."
    
    lines = [f"Team: {self.config['team_name']}"]
    for m in self.config["members"]:
        lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
    return "\n".join(lines)

def member_names(self) -> list:
    """获取所有成员名称列表"""
    return [m["name"] for m in self.config["members"]]
```

### 持久化策略分析

```
┌─────────────────────────────────────────────────────────────┐
│                     持久化流程                               │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Memory (RAM)              File System                      │
│  ─────────────             ──────────                      │
│                                                             │
│  self.config  ◄──────────  _load_config()                   │
│       │                                                     │
│       │ _save_config()                                      │
│       ▼                                                     │
│  config.json  ──────────►  .team/config.json               │
│                                                             │
│  触发时机：                                                  │
│  1. spawn() - 新成员加入                                     │
│  2. _set_status() - 状态变更                                 │
│  3. __init__() - 初始化加载                                  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**设计权衡**：
- ✅ **简单性**：JSON 文件易于调试和版本控制
- ✅ **原子性**：整个配置一次性写入，不会半写
- ❌ **并发性**：多进程写入可能产生竞态（当前通过 GIL 和单进程规避）
- ❌ **规模限制**：成员过多时配置加载会变慢（未实现分页）

---

## 线程管理与并发控制

### 线程模型

```
Main Thread (Lead)
    │
    ├─► Input Loop (等待用户输入)
    │
    ├─► spawn_teammate("alice")
    │       │
    │       ▼
    │   Thread-1 (alice) ──► _loop(alice)
    │       │                      │
    │       │    ┌─────────────────┼─────────────────┐
    │       │    │                 │                 │
    │       │    ▼                 ▼                 ▼
    │       │  WORK            IDLE (poll)      SHUTDOWN
    │       │    │                 │                 │
    │       │    │ message/task   timeout          │
    │       │    └─────────────────┘                 │
    │       │                                        │
    │       └────────────────────────────────────────┘
    │
    ├─► spawn_teammate("bob")
    │       │
    │       ▼
    │   Thread-2 (bob) ──► _loop(bob)
    │       │
    │      ... (同上生命周期)
    │
    └─► list_teammates() (读取 config.json)
```

### 并发控制机制

```python
# 全局锁（模块级别）
_tracker_lock = threading.Lock()  # 用于 shutdown_requests/plan_requests
_claim_lock = threading.Lock()    # 用于任务认领
```

**为什么需要 `_claim_lock`**：

```python
def claim_task(task_id: int, owner: str) -> str:
    """
    认领任务 - 需要线程安全
    
    场景：alice 和 bob 同时发现同一个未认领任务
    如果没有锁，可能两人都认为自己认领成功
    """
    with _claim_lock:  # 互斥锁确保原子性
        path = TASKS_DIR / f"task_{task_id}.json"
        task = json.loads(path.read_text())
        
        # 检查是否已被他人认领
        if task.get("owner"):
            return f"Error: Task already claimed by {task['owner']}"
        
        task["owner"] = owner
        task["status"] = "in_progress"
        path.write_text(json.dumps(task, indent=2))
    
    return f"Claimed task #{task_id} for {owner}"
```

**线程安全设计**：

| 资源 | 并发风险 | 解决方案 |
|------|----------|----------|
| `config.json` | 多线程同时写入 | GIL + 单进程模型（简化设计） |
| 任务认领 | 多成员竞争同一任务 | `_claim_lock` 互斥锁 |
| 收件箱 | 并发读写 JSONL | 文件系统原子追加 + Drain 模式 |
| shutdown_requests | 并发修改字典 | `_tracker_lock` |

### 线程生命周期绑定

```python
def _loop(self, name: str, role: str, prompt: str):
    """
    成员主循环 - 在线程中运行
    
    当此函数返回时，线程自动结束（因为是 daemon thread）
    """
    logger.info(f"[{name}] Teammate loop started")
    
    try:
        while True:
            # WORK PHASE
            for round_num in range(50):
                # ... LLM 调用和工具执行 ...
                
                if idle_requested:
                    break
            
            # IDLE PHASE
            self._set_status(name, "idle")
            resume = False
            
            for poll_num in range(12):  # 60s / 5s = 12
                time.sleep(5)
                
                # 检查收件箱
                inbox = BUS.read_inbox(name)
                if inbox:
                    resume = True
                    break
                
                # 扫描任务
                unclaimed = scan_unclaimed_tasks()
                if unclaimed:
                    claim_task(unclaimed[0]["id"], name)
                    resume = True
                    break
            
            if not resume:
                # 超时 - 结束线程
                self._set_status(name, "shutdown")
                return  # <-- 线程结束
            
            self._set_status(name, "working")
            
    except Exception as e:
        logger.error(f"[{name}] Loop error: {e}")
        self._set_status(name, "shutdown")
        return  # <-- 异常结束线程
```

---

## 实际运行示例

### 示例 1：创建团队并监控状态

```python
# 用户输入：
# > spawn alice as a coder to implement login API

# 执行流程：
1. spawn("alice", "coder", "implement login API")
   ├─► 检查 config.json - 不存在 alice
   ├─► 创建成员: {"name": "alice", "role": "coder", "status": "working"}
   ├─► 保存 config.json
   ├─► 创建 Thread(target=_loop, args=("alice", "coder", "implement login API"))
   ├─► thread.start()
   └─► 返回 "Spawned 'alice' (role: coder)"

# config.json 状态：
{
  "team_name": "default",
  "members": [
    {"name": "alice", "role": "coder", "status": "working"}
  ]
}

# 用户输入：
# > /team

# 输出：
Team: default
  alice (coder): working
```

### 示例 2：成员空闲和超时关机

```python
# alice 完成工作后调用 idle 工具
# 执行流程：

1. WORK PHASE 结束（idle_requested = True）
   └─► 进入 IDLE PHASE

2. _set_status("alice", "idle")
   ├─► 更新 member["status"] = "idle"
   └─► 保存 config.json

3. IDLE PHASE 轮询（每 5 秒）
   ├─► Poll 1: inbox 为空，无未认领任务
   ├─► Poll 2: inbox 为空，无未认领任务
   ├─► ...
   └─► Poll 12: 超时（60秒）

4. _set_status("alice", "shutdown")
   ├─► 更新 member["status"] = "shutdown"
   ├─► 保存 config.json
   └─► return（线程结束）

# config.json 最终状态：
{
  "team_name": "default",
  "members": [
    {"name": "alice", "role": "coder", "status": "shutdown"}
  ]
}
```

### 示例 3：复活已关机的成员

```python
# 用户输入：
# > spawn alice as a senior coder to review code

# 执行流程：
1. spawn("alice", "senior coder", "review code")
   ├─► 检查 config.json - 存在 alice，status="shutdown"
   ├─► 状态检查通过（shutdown 允许复活）
   ├─► 更新 role="senior coder", status="working"
   ├─► 保存 config.json
   ├─► 创建新线程 Thread(target=_loop, ...)
   └─► 返回 "Spawned 'alice' (role: senior coder)"

# 注意：旧线程已结束，新线程是新实例
# config.json 状态：
{
  "team_name": "default",
  "members": [
    {"name": "alice", "role": "senior coder", "status": "working"}
  ]
}
```

### 示例 4：并发成员管理

```python
# 用户输入：
# > spawn alice as frontend
# > spawn bob as backend
# > spawn charlie as tester

# 执行流程（并发）：
spawn("alice", "frontend", prompt1)
    ├─► 创建 Thread-1
    ├─► Thread-1.start() ──► _loop("alice") 
    └─► config.json: alice=working

spawn("bob", "backend", prompt2)
    ├─► 创建 Thread-2
    ├─► Thread-2.start() ──► _loop("bob")
    └─► config.json: bob=working

spawn("charlie", "tester", prompt3)
    ├─► 创建 Thread-3
    ├─► Thread-3.start() ──► _loop("charlie")
    └─► config.json: charlie=working

# 三个线程并行运行，各自独立：
# - Thread-1: WORK -> IDLE -> (等待消息/任务)
# - Thread-2: WORK -> IDLE -> (等待消息/任务)
# - Thread-3: WORK -> IDLE -> (等待消息/任务)

# 用户输入：
# > /team

# 输出可能：
Team: default
  alice (frontend): idle
  bob (backend): working
  charlie (tester): idle
```

---

## 设计决策与考量

### 1. 为什么使用文件系统而非数据库？

**决策**：使用 JSON 文件存储配置和状态

**理由**：
- ✅ **简单性**：零依赖，无需数据库服务
- ✅ **可见性**：可直接查看和编辑文件
- ✅ **版本控制**：config.json 可纳入 git 管理
- ✅ **调试便利**：可直接检查 .team/ 目录内容

**代价**：
- ❌ 不适合高并发写入（当前通过单进程模型规避）
- ❌ 查询效率低（成员多了线性搜索变慢）

### 2. 为什么使用守护线程 (daemon=True)？

```python
thread = threading.Thread(target=self._loop, ..., daemon=True)
```

**决策**：所有成员线程设为守护线程

**理由**：
- ✅ **优雅退出**：主程序退出时自动清理所有成员
- ✅ **无资源泄漏**：不需要手动 join 线程
- ✅ **简化设计**：不需要复杂的关机协调

**代价**：
- ❌ 成员可能在工作中途被终止（主程序退出时）
- ❌ 无法保证任务完成（需要额外协调机制）

### 3. 为什么状态变更立即写盘？

```python
def _set_status(self, name: str, status: str):
    member["status"] = status
    self._save_config()  # 立即写盘
```

**决策**：同步持久化，而非批量或延迟写入

**理由**：
- ✅ **数据安全**：程序崩溃不会丢失状态
- ✅ **简单性**：无需复杂的缓冲或事务机制
- ✅ **一致性**：/team 命令总能看到最新状态

**代价**：
- ❌ 频繁 IO（每个状态变更都写文件）
- ❌ 性能开销（成员多时可能成为瓶颈）

### 4. 为什么允许复活已关机的成员？

```python
if member["status"] in ("idle", "shutdown"):
    # 允许复活
    member["status"] = "working"
```

**决策**：shutdown 状态可复活，working 状态不可

**理由**：
- ✅ **资源复用**：避免不断创建新成员记录
- ✅ **历史保持**：保留成员的角色和之前的交互记录
- ✅ **语义清晰**：shutdown 是"可重启的终止"

**限制**：
- 复活后是新线程，与之前的执行上下文无关
- 如果需要恢复之前的对话，需要额外的上下文管理

### 5. 为什么没有实现成员删除？

**观察**：代码中没有 `delete_member()` 方法

**原因**：
- 设计上选择保留历史成员（审计目的）
- shutdown 状态足以表示成员不活跃
- 可通过手动编辑 config.json 删除（高级用户）

**如果需要删除功能**：
```python
def delete_member(self, name: str) -> str:
    """删除成员（谨慎使用）"""
    member = self._find_member(name)
    if not member:
        return f"Error: '{name}' not found"
    
    if member["status"] == "working":
        return f"Error: '{name}' is working. Request shutdown first."
    
    self.config["members"].remove(member)
    self._save_config()
    return f"Deleted '{name}'"
```

---

## 总结

`TeammateManager` 实现了一个**轻量级、文件驱动的团队管理系统**：

1. **成员生命周期**：spawn → working → idle → shutdown，支持复活
2. **配置持久化**：JSON 文件简单可靠，即时同步
3. **线程管理**：守护线程模型，主控生命周期
4. **并发控制**：锁保护关键区域（任务认领）
5. **设计哲学**：简单优于复杂，可见性优于抽象

**适用场景**：
- ✅ 中小规模团队（< 20 成员）
- ✅ 长时间运行的服务
- ✅ 需要人工监控和干预的场景

**不适用场景**：
- ❌ 高并发成员创建/销毁
- ❌ 分布式多节点部署
- ❌ 严格的事务一致性要求

---

**文档版本**: 1.0  
**最后更新**: 2026-03-09  
**对应代码**: agents/s11_autonomous_agents.py (lines 212-602)
