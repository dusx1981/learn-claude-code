# s06: Context Compact

`s01 > s02 > s03 > s04 > s05 > [ s06 ] | s07 > s08 > s09 > s10 > s11 > s12`

> *"コンテキストはいつか溢れる、空ける手段が要る"* -- 3 層圧縮で無限セッションを実現。

## 問題

コンテキストウィンドウは有限だ。1000 行のファイルに対する `read_file`1 回で約 4000 トークンを消費する。30 ファイルを読み 20 回の bash コマンドを実行すると、100,000 トークン超。圧縮なしでは、エージェントは大規模コードベースで作業できない。

## 解決策

積極性を段階的に上げる 3 層構成:

```
Every turn:
+------------------+
| Tool call result |
+------------------+
        |
        v
[Layer 1: micro_compact]        (silent, every turn)
  Replace tool result > 3 turns old
  with "[Previous: used {tool_name}]"
        |
        v
[Check: tokens > 50000?]
   |               |
   no              yes
   |               |
   v               v
continue   [Layer 2: auto_compact]
              Save transcript to .transcripts/
              LLM summarizes conversation.
              Replace all messages with [summary].
                    |
                    v
            [Layer 3: compact tool]
              Model calls compact explicitly.
              Same summarization as auto_compact.
```

## 仕組み

1. **第 1 層 -- micro_compact**: 各 LLM 呼び出しの前に、古いツール結果をプレースホルダーに置換する。

```python
def micro_compact(messages: list) -> list:
    for msg in messages:
        if msg["role"] == "tool" and len(msg.get("content", "")) > 100:
            tool_name = msg.get("name", "unknown")
            msg["content"] = f"[Previous: used {tool_name}]"
    return messages
```

2. **第 2 層 -- auto_compact**: トークンが閾値を超えたら、完全なトランスクリプトをディスクに保存し、LLM に要約を依頼する。

```python
def auto_compact(messages: list) -> list:
    # Save transcript for recovery
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    # LLM summarizes
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content":
            "Summarize this conversation for continuity..."
            + json.dumps(messages, default=str)[:80000]}],
    )
    return [
        {"role": "user", "content": f"[Compressed]\n\n{response.choices[0].message.content}"},
        {"role": "assistant", "content": "Understood. Continuing."},
    ]
```

3. **第 3 層 -- manual compact**: `compact` ツールが同じ要約処理をオンデマンドでトリガーする。

4. ループが 3 層すべてを統合する:

```python
def agent_loop(messages: list):
    while True:
        micro_compact(messages)                        # Layer 1
        if estimate_tokens(messages) > THRESHOLD:
            messages[:] = auto_compact(messages)       # Layer 2
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM}] + messages,
            tools=TOOLS,
        )
        # ... tool execution ...
        if manual_compact:
            messages[:] = auto_compact(messages)       # Layer 3
```

トランスクリプトがディスク上に完全な履歴を保持する。何も真に失われず、アクティブなコンテキストの外に移動されるだけ。

## s05 からの変更点

| Component      | Before (s05)     | After (s06)                |
|----------------|------------------|----------------------------|
| Tools          | 5                | 5 (base + compact)         |
| Context mgmt   | None             | Three-layer compression    |
| Micro-compact  | None             | Old results -> placeholders|
| Auto-compact   | None             | Token threshold trigger    |
| Transcripts    | None             | Saved to .transcripts/     |

## 試してみる

```sh
cd learn-claude-code
python agents/s06_context_compact.py
```

1. `Read every Python file in the agents/ directory one by one` (micro-compact が古い結果を置換するのを観察する)
2. `Keep reading files until compression triggers automatically`
3. `Use the compact tool to manually compress the conversation`
