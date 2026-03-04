# s07 任务系统技术分析：基于 DAG 的任务图设计

> *从扁平清单到结构化任务图 — 状态持久化与依赖管理的艺术*

## 一、设计背景与问题演进

### 1.1 s03 的局限性

s03 的 TodoManager 是内存中的扁平清单，存在以下根本缺陷：

```
s03 TodoManager 问题:
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  1. 无顺序表达                                                  │
│     [ ] Task A                                                  │
│     [ ] Task B    ← 谁先做？谁后做？无法表达                      │
│     [ ] Task C                                                  │
│                                                                 │
│  2. 无依赖关系                                                  │
│     "Task B 需要 Task A 完成" ← 无法表达                        │
│                                                                 │
│  3. 状态简单                                                    │
│     只有 pending / completed ← 无法表达"进行中"                   │
│                                                                 │
│  4. 内存易失                                                    │
│     s06 上下文压缩 → 清单丢失                                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 s07 的设计目标

```
s07 TaskManager 解决方案:
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  ✓ 有向无环图 (DAG) 结构 — 表达任务关系                         │
│  ✓ 三状态机 — pending → in_progress → completed                │
│  ✓ 双向依赖 — blockedBy / blocks                               │
│  ✓ 磁盘持久化 — 压缩后仍存活                                    │
│  ✓ 自动依赖解除 — 完成时自动解锁                                 │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Motto:**
> *"大目标要拆成小任务, 排好序, 记在磁盘上"*

---

## 二、核心数据结构

### 2.1 任务模型

```json
{
  "id": 1,
  "subject": "Setup project",
  "description": "Initialize git, install dependencies",
  "status": "pending",
  "blockedBy": [],
  "blocks": [2, 3],
  "owner": ""
}
```

| 字段 | 类型 | 语义 |
|------|------|------|
| `id` | integer | 任务唯一标识 |
| `subject` | string | 任务主题 |
| `description` | string | 详细描述 |
| `status` | enum | pending / in_progress / completed |
| `blockedBy` | array | 前置依赖列表 |
| `blocks` | array | 后置依赖列表 |
| `owner` | string | 任务负责人 (预留) |

### 2.2 DAG 任务图可视化

```
.tasks/
  task_1.json  {"id":1, "status":"completed", "blocks":[2,3]}
  task_2.json  {"id":2, "blockedBy":[1], "status":"pending"}
  task_3.json  {"id":3, "blockedBy":[1], "status":"pending"}
  task_4.json  {"id":4, "blockedBy":[2,3], "status":"pending"}

图形化表示:
                 +----------+
            +--> | task 2   | --+
            |    | pending  |   |
+----------+     +----------+    +--> +----------+
| task 1   |                          | task 4   |
| completed| --> +----------+    +--> | blocked  |
+----------+     | task 3   | --+     +----------+
                 | pending  |
                 +----------+

语义解释:
  • 顺序: task 1 必须先完成，才能开始 task 2 和 task 3
  • 并行: task 2 和 task 3 可以同时执行
  • 依赖: task 4 等待 task 2 和 task 3 都完成
```

---

## 三、TaskManager 核心实现

### 3.1 类结构

```python
class TaskManager:
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1
    
    def _max_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0
    
    def _load(self, task_id: int) -> dict:
        path = self.dir / f"task_{task_id}.json"
        return json.loads(path.read_text())
    
    def _save(self, task: dict):
        path = self.dir / f"task_{task['id']}.json"
        path.write_text(json.dumps(task, indent=2))
```

**设计要点：**
- 启动时扫描现有任务，确定下一个 ID
- 每个任务独立 JSON 文件，便于并发操作
- `_load` / `_save` 封装文件 I/O

### 3.2 create() — 任务创建

```python
def create(self, subject: str, description: str = "") -> str:
    task = {
        "id": self._next_id,
        "subject": subject,
        "description": description,
        "status": "pending",
        "blockedBy": [],
        "blocks": [],
        "owner": "",
    }
    self._save(task)
    self._next_id += 1
    return json.dumps(task, indent=2)
```

**初始化状态：**
```
blockedBy: []   ← 无前置依赖，可立即执行
blocks: []      ← 初始无后置任务
status: pending ← 等待执行
```

### 3.3 update() — 状态与依赖核心

```python
def update(self, task_id: int, status: str = None,
           add_blocked_by: list = None, add_blocks: list = None) -> str:
    task = self._load(task_id)
    
    # 1. 状态变更
    if status is not None:
        if status not in ("pending", "in_progress", "completed"):
            raise ValueError(f"Invalid status: {status}")
        task["status"] = status
        # 关键：完成任务时自动解除阻塞
        if status == "completed":
            self._clear_dependency(task_id)
    
    # 2. 添加前置依赖
    if add_blocked_by is not None:
        task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
    
    # 3. 添加后置依赖（双向同步）
    if add_blocks is not None:
        task["blocks"] = list(set(task["blocks"] + add_blocks))
        # 同步更新被阻塞任务的 blockedBy
        for blocked_id in add_blocks:
            try:
                blocked = self._load(blocked_id)
                if task_id not in blocked["blockedBy"]:
                    blocked["blockedBy"].append(task_id)
                    self._save(blocked)
            except ValueError:
                pass
    
    self._save(task)
    return json.dumps(task, indent=2)
```

**核心逻辑流程：**

```
update(task_id=2, add_blocks=[4])
    │
    ├─► task[2].blocks = [4]
    │
    └─► 同步更新 task[4]:
            task[4].blockedBy.append(2)
            → task[4].blockedBy = [2] (原空列表)
    
update(task_id=2, status="completed")
    │
    └─► _clear_dependency(2):
            遍历所有任务
            从 blockedBy 中移除 2
            → task[3].blockedBy.remove(2)
            → task[4].blockedBy.remove(2)
```

### 3.4 _clear_dependency() — 自动解锁

```python
def _clear_dependency(self, completed_id: int):
    """完成任务时，自动解除对其他任务的阻塞"""
    for f in self.dir.glob("task_*.json"):
        task = json.loads(f.read_text())
        if completed_id in task.get("blockedBy", []):
            task["blockedBy"].remove(completed_id)
            self._save(task)
```

**执行效果：**

```
Before:                          After:
┌─────────────────┐            ┌─────────────────┐
│ task_2.json     │            │ task_2.json     │
│ "blockedBy": [1]│ ──完成──► │ "blockedBy": [] │
└─────────────────┘  task 1    └─────────────────┘
         ↑                             
         └─ 自动解锁 ✓
```

### 3.5 list_all() — 可视化列表

```python
def list_all(self) -> str:
    tasks = []
    for f in sorted(self.dir.glob("task_*.json")):
        tasks.append(json.loads(f.read_text()))
    
    if not tasks:
        return "No tasks."
    
    lines = []
    for t in tasks:
        marker = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]"
        }.get(t["status"], "[?]")
        
        blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
        lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")
    
    return "\n".join(lines)
```

**输出示例：**

```
[x] #1: Setup project
[ ] #2: Write code (blocked by: [1])
[ ] #3: Write tests (blocked by: [1])
[ ] #4: Deploy (blocked by: [2, 3])
```

---

## 四、四个任务工具

### 4.1 工具定义

```python
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "Create a new task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "description": {"type": "string"}
                },
                "required": ["subject"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_update",
            "description": "Update a task's status or dependencies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                    "addBlockedBy": {"type": "array", "items": {"type": "integer"}},
                    "addBlocks": {"type": "array", "items": {"type": "integer"}}
                },
                "required": ["task_id"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "List all tasks with status summary.",
            "parameters": {"type": "object", "properties": {}},
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_get",
            "description": "Get full details of a task by ID.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
        }
    },
]
```

### 4.2 工具语义

| 工具 | 语义 | 典型用法 |
|------|------|----------|
| `task_create` | 创建原子任务 | `"Setup project"`, `"Write tests"` |
| `task_update` | 状态推进 + 依赖绑定 | `status="in_progress"`, `addBlocks=[2]` |
| `task_list` | 全局视图 | 查看所有任务及阻塞关系 |
| `task_get` | 单任务详情 | 获取完整 JSON 信息 |

---

## 五、DAG 核心算法

### 5.1 可执行任务检测

```python
def get_runnable_tasks(self) -> list:
    """获取所有可执行的任务（无阻塞依赖）"""
    runnable = []
    for f in self.dir.glob("task_*.json"):
        task = json.loads(f.read_text())
        if task["status"] == "pending" and not task["blockedBy"]:
            runnable.append(task)
    return runnable
```

**逻辑：**
```
可执行条件:
    task.status == "pending" AND task.blockedBy == []
    
示例:
    task_1: pending, []       → 可执行 ✓
    task_2: pending, [1]      → 不可执行 (等 1)
    task_3: in_progress, []   → 已在执行
    task_4: pending, [2, 3]   → 不可执行 (等 2 和 3)
```

### 5.2 依赖拓扑排序

```python
def topological_sort(self) -> list:
    """返回任务的拓扑排序顺序"""
    in_degree = {t["id"]: len(t["blockedBy"]) for t in self._all_tasks()}
    queue = [t["id"] for t in self._all_tasks() if in_degree[t["id"]] == 0]
    result = []
    
    while queue:
        current = queue.pop(0)
        result.append(current)
        
        # 找到依赖当前任务 t in self._all_tasks():
            if current in t["blockedBy"]:
                in_degree[t["id"]] -=的任务
        for 1
                if in_degree[t["id"]] == 0:
                    queue.append(t["id"])
    
    return result
```

### 5.3 并行度计算

```python
def calculate_parallelism(self) -> dict:
    """计算任务的并行度"""
    waves = []
    remaining = set(t["id"] for t in self._all_tasks())
    
    while remaining:
        # 当前波：所有无阻塞的任务
        wave = [tid for tid in remaining 
                if not any(t["blockedBy"] & remaining 
                          for t in self._all_tasks() if t["id"] == tid)]
        waves.append(wave)
        remaining -= set(wave)
    
    return {"waves": waves, "total_waves": len(waves)}
```

---

## 六、持久化与压缩

### 6.1 存储结构

```
项目根目录/
│
└── .tasks/                    # 任务持久化目录
    ├── task_1.json            # 任务 1
    ├── task_2.json            # 任务 2
    ├── task_3.json            # 任务 3
    └── ...
```

### 6.2 与 s06 压缩的关系

```
s06 Context Compression:
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│   压缩前:                                                       │
│   messages = [user, assistant, tool_result, tool_result, ...]  │
│                                                                 │
│   压缩后:                                                       │
│   messages = [summary_user_msg, assistant_ack]                 │
│                                                                 │
│   问题: s03 TodoManager 在内存中 → 丢失 ✓                       │
│   解决: s07 TaskManager 在磁盘中 → 存活 ✓                       │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**关键优势：**
- 对话可以压缩，但任务状态不丢失
- 新对话可以读取现有任务，继续工作
- 支持多会话、长周期项目

---

## 七、与 s03 的演进对比

| 维度 | s03 TodoManager | s07 TaskManager |
|------|-----------------|-----------------|
| **数据结构** | 扁平列表 | DAG 图 |
| **存储** | 内存 | 磁盘文件 |
| **依赖** | 无 | `blockedBy` / `blocks` |
| **状态** | 2 态 | 3 态 |
| **并行支持** | 不支持 | 可检测 |
| **生命周期** | 会话内 | 跨会话 |
| **压缩影响** | 丢失 | 存活 |
| **适用场景** | 快速清单 | 多步复杂项目 |

---

## 八、后续机制 (s08-s12)

s07 的任务图成为后续所有机制的协调骨架：

```
s07 任务图 ──┬──► s08 后台任务
             │      └─ 任务状态写入 .tasks/
             │
             ├──► s09 多 Agent 团队
             │      └─ Agent 读取任务图，认领任务
             │
             ├──► s10 团队协议
             │      └─ 基于任务图的状态同步
             │
             ├──► s11 自主 Agent
             │      └─ 扫描任务图，自动 Claim
             │
             └──► s12 Worktree 隔离
                    └─ 任务图 + 目录隔离
```

---

## 九、总结

```
s07 TaskManager 设计要点:
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  1. DAG 结构表达任务依赖关系                                     │
│     • blockedBy: 前置依赖                                       │
│     • blocks: 后置依赖 (双向同步)                                │
│                                                                 │
│  2. 磁盘持久化                                                  │
│     • 每个任务一个 JSON 文件                                     │
│     • 存活于上下文压缩之外                                       │
│                                                                 │
│  3. 自动依赖解除                                                │
│     • 完成任务时自动解锁后置任务                                  │
│     • 无需显式干预                                               │
│                                                                 │
│  4. 三状态机                                                    │
│     • pending → in_progress → completed                        │
│     • 支持"进行中"状态                                          │
│                                                                 │
│  5. 可视化列表                                                  │
│     • 状态标记 [ ]/[>]/[x]                                      │
│     • 显示阻塞关系                                              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**核心洞见：**
> *"State that survives compression -- because it's outside the conversation."*
>
> 状态存活于压缩之外 — 因为它在对话之外。

---

## 参考资料

- [s07: Task System (中文)](../zh/s07-task-system.md)
- [s07: Task System (英文)](../en/s07-task-system.md)
- [agents/s07_task_system.py](../../agents/s07_task_system.py)