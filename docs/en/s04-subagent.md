# s04: Subagents

`s01 > s02 > s03 > [ s04 ] s05 > s06 | s07 > s08 > s09 > s10 > s11 > s12`

> *"Break big tasks down; each subtask gets a clean context"* -- subagents use independent messages[], keeping the main conversation clean.

## Problem

As the agent works, its messages array grows. Every file read, every bash output stays in context permanently. "What testing framework does this project use?" might require reading 5 files, but the parent only needs the answer: "pytest."

## Solution

```
Parent agent                     Subagent
+------------------+             +------------------+
| messages=[...]   |             | messages=[]      | <-- fresh
|                  |  dispatch   |                  |
| tool: task       | ----------> | while tool_use:  |
|   prompt="..."   |             |   call tools     |
|                  |  summary    |   append results |
|   result = "..." | <---------- | return last text |
+------------------+             +------------------+

Parent context stays clean. Subagent context is discarded.
```

## How It Works

1. The parent gets a `task` tool. The child gets all base tools except `task` (no recursive spawning).

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

2. The subagent starts with `messages=[]` and runs its own loop. Only the final text returns to the parent.

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
        tool_results = []
        for tool_call in choice.message.tool_calls:
            handler = TOOL_HANDLERS.get(tool_call.function.name)
            arguments = json.loads(tool_call.function.arguments)
            output = handler(**arguments)
            tool_results.append({"role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output)[:50000]})
        sub_messages.extend(tool_results)
    return choice.message.content or "(no summary)"
```

The child's entire message history (possibly 30+ tool calls) is discarded. The parent receives a one-paragraph summary as a normal `tool_result`.

## What Changed From s03

| Component      | Before (s03)     | After (s04)               |
|----------------|------------------|---------------------------|
| Tools          | 5                | 5 (base) + task (parent)  |
| Context        | Single shared    | Parent + child isolation  |
| Subagent       | None             | `run_subagent()` function |
| Return value   | N/A              | Summary text only         |

## Try It

```sh
cd learn-claude-code
python agents/s04_subagent.py
```

1. `Use a subtask to find what testing framework this project uses`
2. `Delegate: read all .py files and summarize what each one does`
3. `Use a task to create a new module, then verify it from here`
