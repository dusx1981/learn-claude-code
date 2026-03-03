---
name: code-review
description: 执行全面的代码审查，包括安全性、性能和可维护性分析。当用户要求审查代码、检查错误或审计代码库时使用。
---

# 代码审查技能

你现在具备进行全面的代码审查的专业知识。请遵循以下结构化方法：

## 审查清单

### 1. 安全性（关键）

检查以下内容：
- [ ] **注入漏洞**：SQL、命令、XSS、模板注入
- [ ] **身份验证问题**：硬编码凭据、弱身份验证
- [ ] **授权缺陷**：缺少访问控制、不安全的直接对象引用（IDOR）
- [ ] **数据暴露**：日志或错误消息中包含敏感数据
- [ ] **加密**：弱算法、密钥管理不当
- [ ] **依赖项**：已知漏洞（使用 `npm audit`、`pip-audit` 检查）

```bash
# 快速安全扫描
npm audit                    # Node.js
pip-audit                    # Python
cargo audit                  # Rust
grep -r "password\|secret\|api_key" --include="*.py" --include="*.js"
```

### 2. 正确性

检查以下内容：
- [ ] **逻辑错误**：差一错误、空值处理、边界情况
- [ ] **竞态条件**：无同步的并发访问
- [ ] **资源泄漏**：未关闭的文件、连接、内存
- [ ] **错误处理**：吞没的异常、缺失的错误路径
- [ ] **类型安全**：隐式转换、`any` 类型

### 3. 性能

检查以下内容：
- [ ] **N+1 查询**：循环中的数据库调用
- [ ] **内存问题**：大对象分配、保留的引用
- [ ] **阻塞操作**：异步代码中的同步 I/O
- [ ] **低效算法**：本可 O(n) 却用了 O(n²)
- [ ] **缺少缓存**：重复的昂贵计算

### 4. 可维护性

检查以下内容：
- [ ] **命名**：清晰、一致、有描述性
- [ ] **复杂度**：函数超过 50 行、嵌套深度超过 3 层
- [ ] **重复**：复制粘贴的代码块
- [ ] **死代码**：未使用的导入、不可达的分支
- [ ] **注释**：过时、冗余，或需要但缺失的注释

### 5. 测试

检查以下内容：
- [ ] **覆盖率**：关键路径已测试
- [ ] **边界情况**：空值、空、边界值
- [ ] **模拟**：外部依赖已隔离
- [ ] **断言**：有意义、具体的检查

## 审查输出格式

```markdown
## 代码审查：[文件/组件名称]

### 概述
[1-2 句总结]

### 关键问题
1. **[问题]** (第 X 行)：[描述]
   - 影响：[可能导致什么错误]
   - 修复：[建议的解决方案]

### 改进点
1. **[建议]** (第 X 行)：[描述]

### 正面评价
- [做得好的地方]

### 结论
[ ] 可以合并
[ ] 需要小修改
[ ] 需要大改
```

## 需要标记的常见模式

### Python
```python
# 错误：SQL注入
cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
# 正确：
cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))

# 错误：命令注入
os.system(f"ls {user_input}")
# 正确：
subprocess.run(["ls", user_input], check=True)

# 错误：可变的默认参数
def append(item, lst=[]):  # 错误：共享的可变默认值
# 正确：
def append(item, lst=None):
    lst = lst or []
```

### JavaScript/TypeScript
```javascript
// 错误：原型污染
Object.assign(target, userInput)
// 正确：
Object.assign(target, sanitize(userInput))

// 错误：使用 eval
eval(userCode)
// 正确：切勿对用户输入使用 eval

// 错误：回调地狱
getData(x => process(x, y => save(y, z => done(z))))
// 正确：
const data = await getData();
const processed = await process(data);
await save(processed);
```

## 审查命令

```bash
# 显示最近的更改
git diff HEAD~5 --stat
git log --oneline -10

# 查找潜在问题
grep -rn "TODO\|FIXME\|HACK\|XXX" .
grep -rn "password\|secret\|token" . --include="*.py"

# 检查复杂度（Python）
pip install radon && radon cc . -a

# 检查依赖项
npm outdated  # Node
pip list --outdated  # Python
```

## 审查工作流

1. **理解上下文**：阅读 PR 描述、关联的工单
2. **运行代码**：如果可以，构建、测试、本地运行
3. **自上而下阅读**：从主要入口点开始
4. **检查测试**：变更是否经过测试？测试是否通过？
5. **安全扫描**：运行自动化工具
6. **人工审查**：使用上述清单
7. **撰写反馈**：具体、建议修复、语气友善