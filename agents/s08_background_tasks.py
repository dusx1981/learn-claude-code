#!/usr/bin/env python3
"""
s08_background_tasks.py - Background Tasks

Run commands in background threads. A notification queue is drained
before each LLM call to deliver results.

    Main thread                Background thread
    +-----------------+        +-----------------+
    | agent loop      |        | task executes   |
    | ...             |        | ...             |
    | [LLM call] <---+------- | enqueue(result) |
    |  ^drain queue   |        +-----------------+
    +-----------------+

    Timeline:
    Agent ----[spawn A]----[spawn B]----[other work]----
                 |              |
                 v              v
              [A runs]      [B runs]        (parallel)
                 |              |
                 +-- notification queue --> [results injected]

Key insight: "Fire and forget -- the agent doesn't block while the command runs."
"""

import json
import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

load_dotenv(override=True)

WORKDIR = Path.cwd()
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
)
MODEL = os.environ.get("MODEL_ID", "qwen-max")

SYSTEM = f"You are a coding agent at {WORKDIR}. Use background_run for long-running commands."


# -- BackgroundManager: threaded execution + notification queue --
class BackgroundManager:
    def __init__(self):
        self.tasks = {}  # task_id -> {status, result, command}
        self._notification_queue = []  # completed task results
        self._lock = threading.Lock()

    def run(self, command: str) -> str:
        """Start a background thread, return task_id immediately."""
        task_id = str(uuid.uuid4())[:8]
        logger.info(f"[BG:{task_id}] Starting background task")
        logger.debug(f"[BG:{task_id}] Command: {command[:100]}...")
        
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.name = f"BG-{task_id}"  # Set thread name for logging
        logger.debug(f"[BG:{task_id}] Spawning thread: {thread.name}")
        thread.start()
        
        logger.info(f"[BG:{task_id}] Task started, thread spawned")
        return f"Background task {task_id} started: {command[:80]}"

    def _execute(self, task_id: str, command: str):
        """Thread target: run subprocess, capture output, push to queue."""
        logger.info(f"[BG:{task_id}] Thread started execution")
        logger.debug(f"[BG:{task_id}] Executing: {command[:100]}...")
        
        try:
            start_time = time.time()
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=300
            )
            elapsed = time.time() - start_time
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
            logger.info(f"[BG:{task_id}] Command completed in {elapsed:.2f}s")
            logger.debug(f"[BG:{task_id}] Output length: {len(output)} chars")
            
        except subprocess.TimeoutExpired:
            output = "Error: Timeout (300s)"
            status = "timeout"
            logger.warning(f"[BG:{task_id}] Command timed out after 300s")
            
        except Exception as e:
            output = f"Error: {e}"
            status = "error"
            logger.error(f"[BG:{task_id}] Command failed: {e}")
        
        # Update task status
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output or "(no output)"
        logger.debug(f"[BG:{task_id}] Task status updated to: {status}")
        
        # Add to notification queue
        with self._lock:
            queue_size_before = len(self._notification_queue)
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "result": (output or "(no output)")[:500],
            })
            queue_size_after = len(self._notification_queue)
            logger.info(f"[BG:{task_id}] Added to notification queue (size: {queue_size_before} -> {queue_size_after})")

    def check(self, task_id: str = None) -> str:
        """Check status of one task or list all."""
        if task_id is not None:
            t = self.tasks.get(task_id)
            if not t:
                return f"Error: Unknown task {task_id}"
            return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(running)'}"
        lines = []
        for tid, t in self.tasks.items():
            lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
        return "\n".join(lines) if lines else "No background tasks."

    def drain_notifications(self) -> list:
        """Return and clear all pending completion notifications."""
        with self._lock:
            notif_count = len(self._notification_queue)
            
            if notif_count > 0:
                logger.info(f"[QUEUE] Draining {notif_count} notification(s)")
                notifs = list(self._notification_queue)
                self._notification_queue.clear()
                logger.debug(f"[QUEUE] Drained notifications: {[n['task_id'] for n in notifs]}")
                return notifs
            else:
                logger.debug("[QUEUE] No notifications to drain")
                return []


BG = BackgroundManager()


# -- Tool implementations --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
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

def run_read(path: str, limit=None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit is not None and isinstance(limit, int) and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def handle_check_background(**kw):
    task_id = kw.get("task_id")
    if task_id is None:
        return BG.check()
    else:
        return BG.check(task_id)

TOOL_HANDLERS = {
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "background_run":   lambda **kw: BG.run(kw["command"]),
    "check_background": handle_check_background,
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command (blocking).",
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
                "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
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
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
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
                "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
                "required": ["path", "old_text", "new_text"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "background_run",
            "description": "Run command in background thread. Returns task_id immediately.",
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
            "name": "check_background",
            "description": "Check background task status. Omit task_id to list all.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
            },
        }
    },
]


def agent_loop(messages: list):
    iteration = 0
    
    while True:
        iteration += 1
        logger.info(f"[LOOP:{iteration}] === Starting iteration {iteration} ===")
        
        # Drain background notifications and inject as system message before LLM call
        logger.debug(f"[LOOP:{iteration}] Checking for background notifications...")
        notifs = BG.drain_notifications()
        
        if notifs:
            logger.info(f"[LOOP:{iteration}] Processing {len(notifs)} background notification(s)")
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})
            messages.append({"role": "assistant", "content": "Noted background results."})
            logger.debug(f"[LOOP:{iteration}] Injected background results into conversation")
        else:
            logger.debug(f"[LOOP:{iteration}] No background notifications")
        
        # Prepare messages with system message for OpenAI
        api_messages = [{"role": "system", "content": SYSTEM}] + messages
        logger.debug(f"[LOOP:{iteration}] Calling LLM with {len(api_messages)} messages")
        
        # Time the LLM call
        llm_start = time.time()
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=api_messages,
                tools=TOOLS,
                max_tokens=8000,
            )
            llm_elapsed = time.time() - llm_start
            logger.info(f"[LOOP:{iteration}] LLM call completed in {llm_elapsed:.2f}s")
        except Exception as e:
            logger.error(f"[LOOP:{iteration}] LLM call failed: {e}")
            raise
        
        choice = response.choices[0]
        message_content = choice.message.content if choice.message.content is not None else ""
        assistant_message = {
            "role": "assistant",
            "content": message_content,
        }
        if choice.message.tool_calls:
            tool_count = len(choice.message.tool_calls)
            logger.info(f"[LOOP:{iteration}] LLM requested {tool_count} tool call(s)")
            assistant_message["tool_calls"] = choice.message.tool_calls
        else:
            logger.debug(f"[LOOP:{iteration}] LLM returned text response (no tools)")
        
        messages.append(assistant_message)

        if choice.finish_reason != "tool_calls":
            logger.info(f"[LOOP:{iteration}] Finish reason: {choice.finish_reason} - Exiting loop")
            return
        
        # Execute tools
        results = []
        tool_calls = choice.message.tool_calls or []
        
        for idx, tool_call in enumerate(tool_calls, 1):
            function_name = tool_call.function.name
            logger.info(f"[LOOP:{iteration}] [TOOL:{idx}/{len(tool_calls)}] Executing: {function_name}")
            
            handler = TOOL_HANDLERS.get(function_name)
            try:
                arguments = json.loads(tool_call.function.arguments)
                logger.debug(f"[LOOP:{iteration}] [TOOL:{idx}] Parameters: {json.dumps(arguments, indent=2)[:200]}")
                
                output = handler(**arguments) if handler else f"Unknown tool: {function_name}"
                logger.info(f"[LOOP:{iteration}] [TOOL:{idx}] {function_name} completed")
                logger.debug(f"[LOOP:{iteration}] [TOOL:{idx}] Output: {str(output)[:200]}")
                
            except Exception as e:
                output = f"Error: {e}"
                logger.error(f"[LOOP:{iteration}] [TOOL:{idx}] {function_name} failed: {e}")
            
            print(f"> {function_name}: {str(output)[:200]}")
            results.append({
                "role": "tool", 
                "tool_call_id": tool_call.id, 
                "content": str(output)
            })
        
        logger.debug(f"[LOOP:{iteration}] Appending {len(results)} tool result(s) to messages")
        messages.extend(results)


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
