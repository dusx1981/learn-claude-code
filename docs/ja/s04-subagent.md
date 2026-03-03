# s04: Subagents

`s01 > s02 > s03 > [ s04 ] s05 > s06 | s07 > s08 > s09 > s10 > s11 > s12`

> *"大きなタスクを分割し、各サブタスクにクリーンなコンテキストを"* -- サブエージェントは独立した messages[] を使い、メイン会話を汚さない。

## 問題

エージェントが作業するにつれ、messages 配列は膨張し続ける。すべてのファイル読み取り、すべての bash 出力がコンテキストに永久に残る。「このプロジェクトはどのテストフレームワークを使っているか」という質問は 5 つのファイルを読む必要があるかもしれないが、親に必要なのは「pytest」という答えだけだ。

## 解決策

```
Parent agent                     Subagent
+------------------+             +------------------+
| messages=[...]   |             | messages=[]      | <-- fresh
|                  |  dispatch   |                  |
| tool: task       | ----------> | while tool_calls:  |
|   prompt="..."   |             |   call tools     |
|                  |  summary    |   append results |
|   result = "..." | <---------- | return last text |
+------------------+             +------------------+

Parent context stays clean. Subagent context is discarded.
```

## 仕組み

1. 親に `task` ツールを追加する。子は `task` を除くすべての基本ツールを取得する (再帰的な生成は不可)。

```python
PARENT_TOOLS = CHILD_TOOLS + [
    {"name": "task",
     "description": "Spawn a subagent with fresh context.",
     "input_schema": {
         "type": "object",
         "properties": {"prompt": {"type": "string"}},
         "required": ["prompt"],
     }},
]
```

2. サブエージェントは `messages=[]` で開始し、自身のループを実行する。最終テキストだけが親に返る。

```python
def run_subagent(prompt: str) -> str:
    sub_messages = [{"role": "user", "content": prompt}]
    for _ in range(30):  # safety limit
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SUBAGENT_SYSTEM}] + sub_messages,
            tools=CHILD_TOOLS,
        )
        choice = response.choices[0]
        assistant_message = {"role": "assistant", "content": choice.message.content}
        if choice.message.tool_calls:
            assistant_message["tool_calls"] = choice.message.tool_calls
        sub_messages.append(assistant_message)
        
        if choice.finish_reason != "tool_calls":
            break
        
        results = []
        for tool_call in choice.message.tool_calls:
            function_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
            handler = TOOL_HANDLERS.get(function_name)
            output = handler(**arguments) if handler else f"Unknown tool: {function_name}"
            results.append({"role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output)[:50000]})
        sub_messages.extend(results)
    
    return choice.message.content or "(no summary)"
```

子のメッセージ履歴全体 (30 回以上のツール呼び出し) は破棄される。親は 1 段落の要約を通常の tool result として受け取る。

## s03 からの変更点

| Component      | Before (s03)     | After (s04)               |
|----------------|------------------|---------------------------|
| Tools          | 5                | 5 (base) + task (parent)  |
| Context        | Single shared    | Parent + child isolation  |
| Subagent       | None             | `run_subagent()` function |
| Return value   | N/A              | Summary text only         |

## 試してみる

```sh
cd learn-claude-code
python agents/s04_subagent.py
```

1. `Use a subtask to find what testing framework this project uses`
2. `Delegate: read all .py files and summarize what each one does`
3. `Use a task to create a new module, then verify it from here`
