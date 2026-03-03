# s01: The Agent Loop

`[ s01 ] s02 > s03 > s04 > s05 > s06 | s07 > s08 > s09 > s10 > s11 > s12`

> *"One loop & Bash is all you need"* -- 1 つのツール + 1 つのループ = エージェント。

## 問題

言語モデルはコードについて推論できるが、現実世界に触れられない。ファイルを読めず、テストを実行できず、エラーを確認できない。ループがなければ、ツール呼び出しのたびにユーザーが手動で結果をコピーペーストする必要がある。つまりユーザー自身がループになる。

## 解決策

```
+--------+      +-------+      +---------+
|  User  | ---> |  LLM  | ---> |  Tool   |
| prompt |      |       |      | execute |
+--------+      +---+---+      +----+----+
                     ^                |
                     |   tool result  |
                     +----------------+
                     (loop until finish_reason != "tool_calls")
```

1 つの終了条件がフロー全体を制御する。モデルがツール呼び出しを止めるまでループが回り続ける。

## 仕組み

1. ユーザーのプロンプトが最初のメッセージになる。

```python
messages.append({"role": "user", "content": query})
```

2. メッセージとツール定義を LLM に送信する。

```python
response = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "system", "content": SYSTEM}] + messages,
    tools=TOOLS,
)
```

3. アシスタントのレスポンスを追加し、`finish_reason` を確認する。ツールが呼ばれなければ終了。

```python
choice = response.choices[0]
assistant_message = {"role": "assistant", "content": choice.message.content}
if choice.message.tool_calls:
    assistant_message["tool_calls"] = choice.message.tool_calls
messages.append(assistant_message)

if choice.finish_reason != "tool_calls":
    return
```

4. 各ツール呼び出しを実行し、結果を収集して user メッセージとして追加。ステップ 2 に戻る。

```python
results = []
for tool_call in choice.message.tool_calls:
    function_name = tool_call.function.name
    arguments = json.loads(tool_call.function.arguments)
    output = run_bash(arguments["command"])
    results.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": output,
    })
messages.extend(results)
```

1 つの関数にまとめると:

```python
def agent_loop(query):
    messages = [{"role": "user", "content": query}]
    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM}] + messages,
            tools=TOOLS,
        )
        choice = response.choices[0]
        assistant_message = {"role": "assistant", "content": choice.message.content}
        if choice.message.tool_calls:
            assistant_message["tool_calls"] = choice.message.tool_calls
        messages.append(assistant_message)

        if choice.finish_reason != "tool_calls":
            return

        results = []
        for tool_call in choice.message.tool_calls:
            function_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
            output = run_bash(arguments["command"])
            results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })
        messages.extend(results)
```

これでエージェント全体が 30 行未満に収まる。本コースの残りはすべてこのループの上に積み重なる -- ループ自体は変わらない。

## 変更点

| Component     | Before     | After                          |
|---------------|------------|--------------------------------|
| Agent loop    | (none)     | `while True` + finish_reason     |
| Tools         | (none)     | `bash` (one tool)              |
| Messages      | (none)     | Accumulating list              |
| Control flow  | (none)     | `finish_reason != "tool_calls"`    |

## 試してみる

```sh
cd learn-claude-code
python agents/s01_agent_loop.py
```

1. `Create a file called hello.py that prints "Hello, World!"`
2. `List all Python files in this directory`
3. `What is the current git branch?`
4. `Create a directory called test_output and write 3 files in it`
