下面通过一个具体的例子来说明 `auto_compact` 函数的压缩机制、原理以及输入输出。

> **注意**: 本文档已更新为 Qwen API (OpenAI 兼容)。原 Claude API 已不再使用。

---

## 函数功能概述

`auto_compact` 旨在**将过长的对话历史压缩为一段摘要**，从而节省上下文空间。它执行以下步骤：
1. 将完整的对话历史（`messages`）保存为一个带时间戳的 JSON Lines 文件到磁盘。
2. 调用大语言模型（LLM），要求它对对话进行摘要，重点关注：完成了什么、当前状态、关键决策。
3. 用包含摘要和文件路径的**一条用户消息**，以及一条助手的确认消息，替换掉原来的整个对话历史。

这样，后续的对话就可以在一个极小的上下文中继续，同时完整的历史被持久化，需要时可追溯。

---

## 示例场景

假设有一个多轮对话，涉及项目讨论和代码审查：

### 输入 `messages` 列表（简化表示）
```python
messages = [
    {"role": "user", "content": "我们准备开发一个新功能，需要先规划一下。"},
    {"role": "assistant", "content": "好的，请描述一下功能需求。"},
    {"role": "user", "content": "用户应该能上传图片并自动添加水印。"},
    {"role": "assistant", "content": "明白了。我们需要考虑图片存储、水印处理逻辑和性能。"},
    {"role": "user", "content": "能帮我生成一个后端 API 的设计文档吗？"},
    {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "generate_doc", "arguments": "{\"topic\":\"图片上传与水印API\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "# API 设计文档\n\n## 端点\n- POST /upload\n..."},  # 假设很长的文档
    {"role": "user", "content": "文档看起来不错。现在能帮我审查一下代码吗？"},
    {"role": "assistant", "tool_calls": [{"id": "call_2", "function": {"name": "code_review", "arguments": "{\"repo\":\"project-x\"}"}}]},
    {"role": "tool", "tool_call_id": "call_2", "content": "## 代码审查结果\n\n### 安全性\n- 发现硬编码密钥..."},  # 很长的审查报告
    # ... 更多轮对话
]
```
假设这个列表非常长，包含大量工具结果和交互，导致上下文即将耗尽。

---

## 压缩过程

### 1. 保存完整转录
函数执行：
```python
TRANSCRIPT_DIR.mkdir(exist_ok=True)
transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
with open(transcript_path, "w") as f:
    for msg in messages:
        f.write(json.dumps(msg, default=str) + "\n")
print(f"[transcript saved: {transcript_path}]")
```
此时会在磁盘上生成一个类似 `transcript_1699987654.jsonl` 的文件，每行是一个 JSON 格式的消息，完整保留了所有细节。

### 2. 调用 LLM 生成摘要
构造摘要提示（截断前 80000 字符）：
```python
conversation_text = json.dumps(messages, default=str)[:80000]
response = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content":
        "Summarize this conversation for continuity. Include: "
        "1) What was accomplished, 2) Current state, 3) Key decisions made. "
        "Be concise but preserve critical details.\n\n" + conversation_text}],
    max_tokens=2000,
)
summary = response.choices[0].message.content
```
假设 LLM 返回的摘要为：
```
对话摘要：
1. 已完成：规划了图片上传与水印功能，生成了API设计文档，并对项目代码进行了初步审查。
2. 当前状态：设计文档已就绪，代码审查发现一些安全问题需要修复，尚未开始编码实现。
3. 关键决策：采用云存储保存图片，水印处理使用服务器端库ImageMagick，计划下周一启动开发。
```

### 3. 构建压缩后的消息列表
```python
return [
    {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
    {"role": "assistant", "content": "Understood. I have the context from the summary. Continuing."},
]
```

---

## 输出 `messages` 列表
```python
[
    {
        "role": "user",
        "content": "[Conversation compressed. Transcript: ./transcripts/transcript_1699987654.jsonl]\n\n对话摘要：\n1. 已完成：规划了图片上传与水印功能，生成了API设计文档，并对项目代码进行了初步审查。\n2. 当前状态：设计文档已就绪，代码审查发现一些安全问题需要修复，尚未开始编码实现。\n3. 关键决策：采用云存储保存图片，水印处理使用服务器端库ImageMagick，计划下周一启动开发。"
    },
    {
        "role": "assistant",
        "content": "Understood. I have the context from the summary. Continuing."
    }
]
```

---

## 压缩原理分析

- **机制**：通过 LLM 的摘要能力，将大量对话浓缩为关键信息点，从而大幅减少 token 占用。原对话可能包含数千 token，而摘要通常只有几百 token。
- **保留上下文连贯性**：摘要包含“已完成”、“当前状态”和“关键决策”，这些信息足以让模型继续对话，而无需知道每个细节。
- **可追溯性**：完整对话被保存到磁盘文件，如果后续需要具体细节，用户可以引用文件或要求模型基于文件内容回答（需额外读取）。

---

## 优缺点

| 优点 | 缺点 |
|------|------|
| - 压缩效果显著，几乎可以无限扩展对话长度。<br>- 保留了完整历史，可追溯。<br>- 摘要可定制，聚焦关键信息。 | - 依赖 LLM 摘要质量，可能遗漏重要细节。<br>- 每次压缩需要额外一次 LLM 调用，增加成本和延迟。<br>- 摘要可能引入幻觉或误解。 |

---

## 与 `micro_compact` 的对比

- `micro_compact` 仅压缩旧工具结果，保留完整对话结构，是一种**局部、规则驱动**的压缩。
- `auto_compact` 将整个对话替换为摘要，是一种**全局、语义驱动**的压缩，适合长时间对话的阶段性压缩。

两者可以结合使用：先用 `micro_compact` 定期清理工具结果，当对话仍然过长时，再触发 `auto_compact` 进行整体压缩。