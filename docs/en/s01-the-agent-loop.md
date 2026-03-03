# s01: The Agent Loop

`[ s01 ] s02 > s03 > s04 > s05 > s06 | s07 > s08 > s09 > s10 > s11 > s12`

> *"One loop & Bash is all you need"* -- one tool + one loop = an agent.

Note: This documentation originally referenced Claude/Anthropic API. The code examples have been updated to use Qwen/OpenAI-compatible API (`client.chat.completions.create` instead of `client.messages.create`).

## Problem

A language model can reason about code, but it can't *touch* the real world -- can't read files, run tests, or check errors. Without a loop, every tool call requires you to manually copy-paste results back. You become the loop.

## Solution

```
+--------+      +-------+      +---------+
|  User  | ---> |  LLM  | ---> |  Tool   |
| prompt |      |       |      | execute |
+--------+      +---+---+      +----+----+
                    ^                |
                    |   tool_result  |
                     +----------------+
                     (loop until finish_reason != "tool_calls")
```

One exit condition controls the entire flow. The loop runs until the model stops calling tools.

## How It Works

1. User prompt becomes the first message.

```python
messages.append({"role": "user", "content": query})
```

2. Send messages + tool definitions to the LLM.

```python
response = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "system", "content": SYSTEM}] + messages,
    tools=TOOLS,
)
```

3. Append the assistant response. Check `finish_reason` -- if the model didn't call a tool, we're done.

```python
choice = response.choices[0]
assistant_message = {"role": "assistant", "content": choice.message.content}
if choice.message.tool_calls:
    assistant_message["tool_calls"] = choice.message.tool_calls
messages.append(assistant_message)
if choice.finish_reason != "tool_calls":
    return
```

4. Execute each tool call, collect results, append as tool messages. Loop back to step 2.

```python
tool_results = []
for tool_call in choice.message.tool_calls:
    function_name = tool_call.function.name
    arguments = json.loads(tool_call.function.arguments)
    output = run_bash(arguments["command"])
    tool_results.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": output,
    })
messages.extend(tool_results)
```

Assembled into one function:

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

        tool_results = []
        for tool_call in choice.message.tool_calls:
            function_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
            output = run_bash(arguments["command"])
            tool_results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })
        messages.extend(tool_results)
```

That's the entire agent in under 30 lines. Everything else in this course layers on top -- without changing the loop.

## What Changed

| Component     | Before     | After                          |
|---------------|------------|--------------------------------|
| Agent loop    | (none)     | `while True` + finish_reason   |
| Tools         | (none)     | `bash` (one tool)              |
| Messages      | (none)     | Accumulating list              |
| Control flow  | (none)     | `finish_reason != "tool_calls"`|

## Try It

```sh
cd learn-claude-code
python agents/s01_agent_loop.py
```

1. `Create a file called hello.py that prints "Hello, World!"`
2. `List all Python files in this directory`
3. `What is the current git branch?`
4. `Create a directory called test_output and write 3 files in it`
