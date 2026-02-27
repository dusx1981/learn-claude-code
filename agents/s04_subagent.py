#!/usr/bin/env python3
"""
s04_subagent.py - Subagents (OpenAI/千问版本)

生成一个带有全新 messages=[] 的子智能体。子智能体在自己的上下文中工作，
共享文件系统，然后只向父智能体返回一个总结。

    Parent agent                     Subagent
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- fresh
    |                  |  dispatch   |                  |
    | tool: task       | ---------->| while tool_use:  |
    |   prompt="..."   |            |   call tools     |
    |   description=""  |            |   append results |
    |                  |  summary   |                  |
    |   result = "..." | <--------- | return last text  |
    +------------------+             +------------------+
              |
    Parent context stays clean.
    Subagent context is discarded.

核心洞见: "进程隔离让上下文隔离免费实现。"
"""

import os
import subprocess
import json
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(override=True)

WORKDIR = Path.cwd()

# OpenAI 兼容的 API 配置，用于接入通义千问 (Qwen)
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
)
MODEL = os.environ.get("MODEL_ID", "qwen-max")

SYSTEM = "You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."
SUBAGENT_SYSTEM = "You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."


# -- 工具实现，父和子智能体共享 --
def safe_path(p: str) -> Path:
    """确保路径不会逃逸出工作目录"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """执行 bash 命令"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    """读取文件内容"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """写入文件"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """编辑文件（替换文本）"""
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- 工具分发器 --
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# 子智能体工具列表：不包含 task（避免递归生成）
CHILD_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer"}
                },
                "required": ["path"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace exact text in file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"}
                },
                "required": ["path", "old_text", "new_text"],
            },
        }
    },
]


# -- 子智能体: 全新上下文，受限工具，只返回总结 --
def run_subagent(prompt: str) -> str:
    """
    运行子智能体执行任务。
    
    关键设计:
    - 全新 messages=[] 上下文，与父智能体隔离
    - 不包含 task 工具，避免递归生成
    - 只返回最终总结，子智能体的上下文被丢弃
    """
    sub_messages = [{"role": "user", "content": prompt}]  # fresh context
    
    for _ in range(30):  # safety limit
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SUBAGENT_SYSTEM}] + sub_messages,
            tools=CHILD_TOOLS,
            max_tokens=8000,
        )

        choice = response.choices[0]
        
        # 构建 assistant 消息
        assistant_message = {
            "role": "assistant",
            "content": choice.message.content,
        }
        if choice.message.tool_calls:
            assistant_message["tool_calls"] = choice.message.tool_calls
        sub_messages.append(assistant_message)

        if choice.finish_reason != "tool_calls":
            break

        # 执行工具调用
        tool_results = []
        for tool_call in choice.message.tool_calls:
            function_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)

            handler = TOOL_HANDLERS.get(function_name)
            output = handler(**arguments) if handler else f"Unknown tool: {function_name}"
            
            tool_results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output)[:50000]
            })

        sub_messages.extend(tool_results)

    # 只有最终文本返回给父智能体 -- 子智能体上下文被丢弃
    final_content = choice.message.content
    return final_content if final_content else "(no summary)"


# -- 父智能体工具: 基础工具 + task 分发器 --
PARENT_TOOLS = CHILD_TOOLS + [
    {
        "type": "function",
        "function": {
            "name": "task",
            "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "description": {"type": "string", "description": "Short description of the task"}
                },
                "required": ["prompt"],
            },
        }
    },
]


def agent_loop(messages: list):
    """父智能体循环"""
    while True:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM}] + messages,
            tools=PARENT_TOOLS,
            max_tokens=8000,
        )

        choice = response.choices[0]
        
        # 构建 assistant 消息
        assistant_message = {
            "role": "assistant",
            "content": choice.message.content,
        }
        if choice.message.tool_calls:
            assistant_message["tool_calls"] = choice.message.tool_calls
        messages.append(assistant_message)

        if choice.finish_reason != "tool_calls":
            return

        results = []
        for tool_call in choice.message.tool_calls:
            function_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)

            if function_name == "task":
                desc = arguments.get("description", "subtask")
                print(f"\033[33m> task ({desc}): {arguments['prompt'][:80]}\033[0m")
                output = run_subagent(arguments["prompt"])
            else:
                handler = TOOL_HANDLERS.get(function_name)
                output = handler(**arguments) if handler else f"Unknown tool: {function_name}"
            
            print(f"  {str(output[:200])}")
            
            results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output)
            })

        messages.extend(results)


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1].get("content", "")
        if response_content:
            print(response_content)
        print()
