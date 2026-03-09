#!/usr/bin/env python3
"""
s11_autonomous_agents.py - Autonomous Agents

Idle cycle with task board polling, auto-claiming unclaimed tasks, and
identity re-injection after context compression. Builds on s10's protocols.

    Teammate lifecycle:
    +-------+
    | spawn |
    +---+---+
        |
        v
    +-------+  tool_use    +-------+
    | WORK  | <----------- |  LLM  |
    +---+---+              +-------+
        |
        | stop_reason != tool_use
        v
    +--------+
    | IDLE   | poll every 5s for up to 60s
    +---+----+
        |
        +---> check inbox -> message? -> resume WORK
        |
        +---> scan .tasks/ -> unclaimed? -> claim -> resume WORK
        |
        +---> timeout (60s) -> shutdown

    Identity re-injection after compression:
    messages = [identity_block, ...remaining...]
    "You are 'coder', role: backend, team: my-team"

Key insight: "The agent finds work itself."
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv

# Configure logging with timestamp, level and message
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

load_dotenv(override=True)
logger.info("Environment loaded from .env file")

WORKDIR = Path.cwd()
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
)
MODEL = os.environ.get("MODEL_ID", "qwen-max")
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TASKS_DIR = WORKDIR / ".tasks"

POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

SYSTEM = f"You are a team lead at {WORKDIR}. Teammates are autonomous -- they find work themselves."

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

# -- Request trackers --
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()
_claim_lock = threading.Lock()


# -- MessageBus: JSONL inbox per teammate --
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"MessageBus initialized: inbox_dir={inbox_dir}")

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict | None = None) -> str:
        logger.debug(f"MessageBus.send: sender={sender}, to={to}, msg_type={msg_type}")
        if msg_type not in VALID_MSG_TYPES:
            logger.warning(f"Invalid message type: {msg_type}")
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra is not None:
            msg.update(extra)
            logger.debug(f"Message extra fields: {list(extra.keys())}")
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")
        logger.info(f"Message sent: {sender} -> {to} (type={msg_type})")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        logger.debug(f"MessageBus.read_inbox: name={name}")
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            logger.debug(f"Inbox does not exist: {inbox_path}")
            return []
        messages = []
        lines = inbox_path.read_text().strip().splitlines()
        for line in lines:
            if line:
                messages.append(json.loads(line))
        inbox_path.write_text("")
        logger.info(f"Inbox read: {name}, {len(messages)} message(s) drained")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        logger.info(f"Broadcast: sender={sender}, recipients={len(teammates)}")
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        logger.info(f"Broadcast complete: {count} teammate(s) notified")
        return f"Broadcast to {count} teammates"


logger.info("Creating MessageBus singleton instance")
BUS = MessageBus(INBOX_DIR)


# -- Task board scanning --
def scan_unclaimed_tasks() -> list:
    logger.debug("Task scan: scanning for unclaimed tasks")
    TASKS_DIR.mkdir(exist_ok=True)
    unclaimed = []
    task_files = list(TASKS_DIR.glob("task_*.json"))
    logger.debug(f"Task scan: found {len(task_files)} task file(s)")
    for f in sorted(task_files):
        try:
            task = json.loads(f.read_text())
            task_id = task.get("id", "unknown")
            status = task.get("status")
            owner = task.get("owner")
            blocked_by = task.get("blockedBy")
            
            # Check if task is claimable
            is_pending = status == "pending"
            has_no_owner = not owner
            is_not_blocked = not blocked_by
            
            if is_pending and has_no_owner and is_not_blocked:
                logger.debug(f"Task scan: task #{task_id} is claimable (pending, no owner, not blocked)")
                unclaimed.append(task)
            else:
                logger.debug(f"Task scan: task #{task_id} not claimable (status={status}, owner={owner}, blocked={blocked_by})")
        except Exception as e:
            logger.error(f"Task scan: error reading {f}: {e}")
    logger.info(f"Task scan complete: {len(unclaimed)} unclaimed task(s) found out of {len(task_files)} total")
    return unclaimed


def claim_task(task_id: int, owner: str) -> str:
    logger.info(f"Task claim: attempting to claim task #{task_id} for {owner}")
    with _claim_lock:
        logger.debug(f"Task claim: acquired claim lock")
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            logger.error(f"Task claim: task #{task_id} not found at {path}")
            return f"Error: Task {task_id} not found"
        try:
            task = json.loads(path.read_text())
            previous_owner = task.get("owner")
            previous_status = task.get("status")
            task["owner"] = owner
            task["status"] = "in_progress"
            path.write_text(json.dumps(task, indent=2))
            logger.info(f"Task claim: task #{task_id} claimed by {owner} (prev_owner={previous_owner}, prev_status={previous_status})")
        except Exception as e:
            logger.error(f"Task claim: error claiming task #{task_id}: {e}")
            return f"Error: {e}"
    return f"Claimed task #{task_id} for {owner}"


# -- Identity re-injection after compression --
def make_identity_block(name: str, role: str, team_name: str) -> dict:
    return {
        "role": "user",
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }


# -- Autonomous TeammateManager --
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}
        logger.info(f"TeammateManager initialized: team_dir={team_dir}, team_name={self.config.get('team_name', 'default')}")

    def _load_config(self) -> dict:
        if self.config_path.exists():
            try:
                config = json.loads(self.config_path.read_text())
                logger.debug(f"Config loaded: {len(config.get('members', []))} member(s)")
                return config
            except Exception as e:
                logger.error(f"Error loading config: {e}, using default")
        logger.debug("Config not found, using default")
        return {"team_name": "default", "members": []}

    def _save_config(self):
        try:
            self.config_path.write_text(json.dumps(self.config, indent=2))
            logger.debug(f"Config saved: {len(self.config.get('members', []))} member(s)")
        except Exception as e:
            logger.error(f"Error saving config: {e}")

    def _find_member(self, name: str) -> dict | None:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        logger.debug(f"Status change: {name} -> {status}")
        member = self._find_member(name)
        if member:
            old_status = member.get("status")
            member["status"] = status
            self._save_config()
            logger.info(f"Status updated: {name} {old_status} -> {status}")
        else:
            logger.warning(f"Status change failed: member '{name}' not found")

    def spawn(self, name: str, role: str, prompt: str) -> str:
        logger.info(f"Spawn request: name={name}, role={role}, prompt_length={len(prompt)}")
        member = self._find_member(name)
        if member:
            current_status = member["status"]
            logger.debug(f"Member exists: {name}, current_status={current_status}")
            if member["status"] not in ("idle", "shutdown"):
                logger.warning(f"Spawn failed: '{name}' is currently {member['status']}")
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
            logger.debug(f"Member revived: {name}, status set to working")
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
            logger.debug(f"New member created: {name}")
        self._save_config()
        thread = threading.Thread(
            target=self._loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        logger.info(f"Spawn complete: '{name}' started (thread_id={thread.ident})")
        return f"Spawned '{name}' (role: {role})"

    def _loop(self, name: str, role: str, prompt: str):
        logger.info(f"[{name}] Teammate loop started")
        team_name = self.config["team_name"]
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
            f"Use idle tool when you have no more work. You will auto-claim new tasks."
        )
        messages = [{"role": "user", "content": prompt}]
        tools = self._teammate_tools()
        logger.info(f"[{name}] System prompt set, {len(tools)} tool(s) available")
        work_phase_count = 0
        idle_phase_count = 0

        while True:
            work_phase_count += 1
            logger.info(f"[{name}] === WORK PHASE #{work_phase_count} START ===")
            # -- WORK PHASE: standard agent loop --
            tool_call_count = 0
            for round_num in range(50):
                logger.debug(f"[{name}] Work round {round_num + 1}/50")
                inbox = BUS.read_inbox(name)
                if inbox:
                    logger.info(f"[{name}] Inbox received: {len(inbox)} message(s)")
                for msg in inbox:
                    msg_type = msg.get("type", "unknown")
                    logger.info(f"[{name}] Processing message: type={msg_type}, from={msg.get('from', 'unknown')}")
                    if msg_type == "shutdown_request":
                        logger.info(f"[{name}] Shutdown request received, shutting down")
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg)})
                try:
                    logger.debug(f"[{name}] Calling LLM API (messages={len(messages)})")
                    response = client.chat.completions.create(
                        model=MODEL,
                        messages=[{"role": "system", "content": sys_prompt}] + messages,
                        tools=tools,
                        max_tokens=8000,
                    )
                    logger.debug(f"[{name}] LLM API response received")
                except Exception as e:
                    logger.error(f"[{name}] LLM API error: {e}")
                    self._set_status(name, "idle")
                    return
                choice = response.choices[0]
                assistant_message = {
                    "role": "assistant",
                    "content": choice.message.content or "",
                }
                if choice.message.tool_calls:
                    tool_call_count += len(choice.message.tool_calls)
                    assistant_message["tool_calls"] = choice.message.tool_calls
                messages.append(assistant_message)
                finish_reason = choice.finish_reason
                logger.info(f"[{name}] LLM response: finish_reason={finish_reason}, tool_calls={len(choice.message.tool_calls) if choice.message.tool_calls else 0}")

                if finish_reason != "tool_calls":
                    logger.info(f"[{name}] Work phase ending: finish_reason={finish_reason}")
                    break
                results = []
                idle_requested = False
                for tool_call in choice.message.tool_calls:
                    try:
                        # Try to access function.name first (standard OpenAI format)
                        function_name = tool_call.function.name
                        arguments = json.loads(tool_call.function.arguments)
                    except AttributeError:
                        # Fallback: try direct access (some versions)
                        function_name = getattr(tool_call, 'name', 'unknown')
                        arguments_str = getattr(tool_call, 'arguments', '{}')
                        arguments = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
                    
                    logger.info(f"[{name}] Tool call: {function_name}({arguments})")
                    if function_name == "idle":
                        idle_requested = True
                        output = "Entering idle phase. Will poll for new tasks."
                        logger.info(f"[{name}] Idle tool called, transitioning to IDLE phase")
                    else:
                        output = self._exec(name, function_name, arguments)
                    logger.info(f"[{name}] Tool result: {str(output)[:100]}...")
                    results.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(output),
                    })
                messages.extend(results)
                if idle_requested:
                    logger.info(f"[{name}] Breaking work phase for idle transition")
                    break
            logger.info(f"[{name}] === WORK PHASE #{work_phase_count} END (tool_calls={tool_call_count}) ===")

            # -- IDLE PHASE: poll for inbox messages and unclaimed tasks --
            idle_phase_count += 1
            logger.info(f"[{name}] === IDLE PHASE #{idle_phase_count} START ===")
            self._set_status(name, "idle")
            resume = False
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)
            logger.info(f"[{name}] Idle polling: max {polls} iteration(s), interval={POLL_INTERVAL}s, timeout={IDLE_TIMEOUT}s")
            for poll_num in range(polls):
                logger.debug(f"[{name}] Idle poll {poll_num + 1}/{polls}")
                time.sleep(POLL_INTERVAL)
                inbox = BUS.read_inbox(name)
                if inbox:
                    logger.info(f"[{name}] Idle: inbox received {len(inbox)} message(s), resuming work")
                    for msg in inbox:
                        msg_type = msg.get("type", "unknown")
                        if msg_type == "shutdown_request":
                            logger.info(f"[{name}] Idle: shutdown request received")
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break
                unclaimed = scan_unclaimed_tasks()
                if unclaimed:
                    task = unclaimed[0]
                    task_id = task["id"]
                    logger.info(f"[{name}] Idle: found {len(unclaimed)} unclaimed task(s), claiming task #{task_id}")
                    claim_result = claim_task(task_id, name)
                    logger.info(f"[{name}] Idle: {claim_result}")
                    task_prompt = (
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                        f"{task.get('description', '')}</auto-claimed>"
                    )
                    if len(messages) <= 3:
                        logger.info(f"[{name}] Identity re-injection triggered (messages={len(messages)} <= 3)")
                        messages.insert(0, make_identity_block(name, role, team_name))
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                        logger.info(f"[{name}] Identity block injected")
                    messages.append({"role": "user", "content": task_prompt})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    logger.info(f"[{name}] Idle: task prompt injected, resuming work")
                    resume = True
                    break
                logger.debug(f"[{name}] Idle: no messages or tasks, continuing poll")
            
            if not resume:
                logger.info(f"[{name}] Idle: timeout ({IDLE_TIMEOUT}s) reached, shutting down")
                self._set_status(name, "shutdown")
                return
            logger.info(f"[{name}] === IDLE PHASE #{idle_phase_count} END ===")
            self._set_status(name, "working")

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        logger.debug(f"[{sender}] Executing tool: {tool_name}")
        result = None
        if tool_name == "bash":
            result = _run_bash(args["command"])
        elif tool_name == "read_file":
            result = _run_read(args["path"])
        elif tool_name == "write_file":
            result = _run_write(args["path"], args["content"])
        elif tool_name == "edit_file":
            result = _run_edit(args["path"], args["old_text"], args["new_text"])
        elif tool_name == "send_message":
            result = BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        elif tool_name == "read_inbox":
            result = json.dumps(BUS.read_inbox(sender), indent=2)
        elif tool_name == "shutdown_response":
            req_id = args["request_id"]
            with _tracker_lock:
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = "approved" if args["approve"] else "rejected"
            BUS.send(
                sender, "lead", args.get("reason", ""),
                "shutdown_response", {"request_id": req_id, "approve": args["approve"]},
            )
            result = f"Shutdown {'approved' if args['approve'] else 'rejected'}"
        elif tool_name == "plan_approval":
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            with _tracker_lock:
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
            BUS.send(
                sender, "lead", plan_text, "plan_approval_response",
                {"request_id": req_id, "plan": plan_text},
            )
            result = f"Plan submitted (request_id={req_id}). Waiting for approval."
        elif tool_name == "claim_task":
            result = claim_task(args["task_id"], sender)
        else:
            result = f"Unknown tool: {tool_name}"
            logger.warning(f"[{sender}] Unknown tool: {tool_name}")
        logger.debug(f"[{sender}] Tool {tool_name} result: {str(result)[:80]}...")
        return result

    def _teammate_tools(self) -> list:
        # these base tools are unchanged from s02
        return [
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
                        "properties": {"path": {"type": "string"}},
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
                    "name": "send_message",
                    "description": "Send message to a teammate.",
                    "parameters": {
                        "type": "object",
                        "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}},
                        "required": ["to", "content"],
                    },
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_inbox",
                    "description": "Read and drain your inbox.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "shutdown_response",
                    "description": "Respond to a shutdown request.",
                    "parameters": {
                        "type": "object",
                        "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}},
                        "required": ["request_id", "approve"],
                    },
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "plan_approval",
                    "description": "Submit a plan for lead approval.",
                    "parameters": {
                        "type": "object",
                        "properties": {"plan": {"type": "string"}},
                        "required": ["plan"],
                    },
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "idle",
                    "description": "Signal that you have no more work. Enters idle polling phase.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "claim_task",
                    "description": "Claim a task from the task board by ID.",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "integer"}},
                        "required": ["task_id"],
                    },
                }
            },
        ]

    def list_all(self) -> str:
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(TEAM_DIR)


# -- Base tool implementations (these base tools are unchanged from s02) --
def _safe_path(p: str) -> Path:
    logger.debug(f"Safe path check: {p}")
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        logger.error(f"Path escapes workspace: {p} -> {path}")
        raise ValueError(f"Path escapes workspace: {p}")
    logger.debug(f"Safe path resolved: {path}")
    return path


def _run_bash(command: str) -> str:
    logger.info(f"Bash command: {command[:80]}{'...' if len(command) > 80 else ''}")
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(d in command for d in dangerous):
        logger.warning(f"Dangerous command blocked: {command}")
        return "Error: Dangerous command blocked"
    try:
        logger.debug(f"Executing subprocess: timeout=120s, cwd={WORKDIR}")
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        result = out[:50000] if out else "(no output)"
        logger.info(f"Bash result: returncode={r.returncode}, output_length={len(result)}")
        return result
    except subprocess.TimeoutExpired:
        logger.error(f"Bash command timeout after 120s: {command[:50]}...")
        return "Error: Timeout (120s)"
    except Exception as e:
        logger.error(f"Bash command error: {e}")
        return f"Error: {e}"


def _run_read(path: str, limit: int | None = None) -> str:
    logger.info(f"Read file: {path}, limit={limit}")
    try:
        safe_path = _safe_path(path)
        lines = safe_path.read_text().splitlines()
        total_lines = len(lines)
        if limit and limit < total_lines:
            lines = lines[:limit] + [f"... ({total_lines - limit} more)"]
            logger.debug(f"Read file truncated: {total_lines} lines -> {limit} lines")
        result = "\n".join(lines)[:50000]
        logger.info(f"Read file success: {path}, {total_lines} lines, {len(result)} bytes")
        return result
    except Exception as e:
        logger.error(f"Read file error: {path} - {e}")
        return f"Error: {e}"


def _run_write(path: str, content: str) -> str:
    logger.info(f"Write file: {path}, content_length={len(content)}")
    try:
        fp = _safe_path(path)
        logger.debug(f"Write file: creating parent directories if needed")
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        logger.info(f"Write file success: {path}, {len(content)} bytes written")
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        logger.error(f"Write file error: {path} - {e}")
        return f"Error: {e}"


def _run_edit(path: str, old_text: str, new_text: str) -> str:
    logger.info(f"Edit file: {path}, old_text_length={len(old_text)}, new_text_length={len(new_text)}")
    try:
        fp = _safe_path(path)
        c = fp.read_text()
        logger.debug(f"Edit file: read {len(c)} bytes from {path}")
        if old_text not in c:
            logger.error(f"Edit file failed: old_text not found in {path}")
            return f"Error: Text not found in {path}"
        new_content = c.replace(old_text, new_text, 1)
        fp.write_text(new_content)
        logger.info(f"Edit file success: {path}, replaced {len(old_text)} bytes with {len(new_text)} bytes")
        return f"Edited {path}"
    except Exception as e:
        logger.error(f"Edit file error: {path} - {e}")
        return f"Error: {e}"


# -- Lead-specific protocol handlers --
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    logger.info(f"Shutdown request: generating request_id={req_id} for teammate={teammate}")
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
        logger.debug(f"Shutdown request: stored in tracker (pending)")
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    logger.info(f"Shutdown request: sent to '{teammate}' (request_id={req_id})")
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    logger.info(f"Plan review: request_id={request_id}, approve={approve}, feedback_length={len(feedback)}")
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        logger.error(f"Plan review: unknown request_id={request_id}")
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
        logger.info(f"Plan review: status set to {req['status']}")
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    logger.info(f"Plan review: response sent to '{req['from']}'")
    return f"Plan {req['status']} for '{req['from']}'"


def _check_shutdown_status(request_id: str) -> str:
    logger.debug(f"Shutdown status check: request_id={request_id}")
    with _tracker_lock:
        status = shutdown_requests.get(request_id, {"error": "not found"})
    logger.debug(f"Shutdown status: {status}")
    return json.dumps(status)


# -- Lead tool dispatch (14 tools) --
TOOL_HANDLERS = {
    "bash":              lambda **kw: _run_bash(kw["command"]),
    "read_file":         lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file":        lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file":         lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "spawn_teammate":    lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":    lambda **kw: TEAM.list_all(),
    "send_message":      lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":        lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":         lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request":  lambda **kw: handle_shutdown_request(kw["teammate"]),
    "shutdown_response": lambda **kw: _check_shutdown_status(kw.get("request_id", "")),
    "plan_approval":     lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    "idle":              lambda **kw: "Lead does not idle.",
    "claim_task":        lambda **kw: claim_task(kw["task_id"], "lead"),
}

# these base tools are unchanged from s02
TOOLS = [
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
            "name": "spawn_teammate",
            "description": "Spawn an autonomous teammate.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}},
                "required": ["name", "role", "prompt"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_teammates",
            "description": "List all teammates.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send a message to a teammate.",
            "parameters": {
                "type": "object",
                "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}},
                "required": ["to", "content"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_inbox",
            "description": "Read and drain the lead's inbox.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "broadcast",
            "description": "Send a message to all teammates.",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "shutdown_request",
            "description": "Request a teammate to shut down.",
            "parameters": {
                "type": "object",
                "properties": {"teammate": {"type": "string"}},
                "required": ["teammate"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "shutdown_response",
            "description": "Check shutdown request status.",
            "parameters": {
                "type": "object",
                "properties": {"request_id": {"type": "string"}},
                "required": ["request_id"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "plan_approval",
            "description": "Approve or reject a teammate's plan.",
            "parameters": {
                "type": "object",
                "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}},
                "required": ["request_id", "approve"],
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "idle",
            "description": "Enter idle state (for lead -- rarely used).",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "claim_task",
            "description": "Claim a task from the board by ID.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
        }
    },
]


def agent_loop(messages: list):
    loop_count = 0
    logger.info("=== LEAD AGENT LOOP START ===")
    while True:
        loop_count += 1
        logger.info(f"Lead loop iteration #{loop_count}")
        
        # Check inbox
        logger.debug("Lead: checking inbox")
        inbox = BUS.read_inbox("lead")
        if inbox:
            logger.info(f"Lead: received {len(inbox)} message(s) in inbox")
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
            messages.append({
                "role": "assistant",
                "content": "Noted inbox messages.",
            })
        else:
            logger.debug("Lead: inbox empty")
        
        # LLM API call
        logger.info(f"Lead: calling LLM API (messages={len(messages)})")
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": SYSTEM}] + messages,
                tools=TOOLS,
                max_tokens=8000,
            )
            logger.debug("Lead: LLM API response received")
        except Exception as e:
            logger.error(f"Lead: LLM API error: {e}")
            return
        
        choice = response.choices[0]
        assistant_message = {
            "role": "assistant",
            "content": choice.message.content,
        }
        if choice.message.tool_calls:
            assistant_message["tool_calls"] = choice.message.tool_calls
        messages.append(assistant_message)
        
        finish_reason = choice.finish_reason
        tool_call_count = len(choice.message.tool_calls) if choice.message.tool_calls else 0
        logger.info(f"Lead: LLM response - finish_reason={finish_reason}, tool_calls={tool_call_count}")
        
        if finish_reason != "tool_calls":
            logger.info(f"Lead: loop ending (finish_reason={finish_reason})")
            logger.info("=== LEAD AGENT LOOP END ===")
            return
        
        # Execute tools
        logger.info(f"Lead: executing {tool_call_count} tool(s)")
        results = []
        tool_calls = choice.message.tool_calls if choice.message.tool_calls else []
        for i, tool_call in enumerate(tool_calls, 1):
            handler = TOOL_HANDLERS.get(tool_call.function.name)
            try:
                arguments = json.loads(tool_call.function.arguments)
                logger.info(f"Lead: tool {i}/{len(tool_calls)} - {tool_call.function.name}({arguments})")
                output = handler(**arguments) if handler else f"Unknown tool: {tool_call.function.name}"
            except Exception as e:
                logger.error(f"Lead: tool execution error: {e}")
                output = f"Error: {e}"
            logger.info(f"Lead: tool result: {str(output)[:100]}...")
            print(f"> {tool_call.function.name}: {str(output)[:200]}")
            results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output),
            })
        messages.extend(results)
        logger.debug(f"Lead: tool results appended to messages, continuing loop")


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("S11 AUTONOMOUS AGENTS - Starting main program")
    logger.info(f"Work directory: {WORKDIR}")
    logger.info(f"Team directory: {TEAM_DIR}")
    logger.info(f"Tasks directory: {TASKS_DIR}")
    logger.info(f"Model: {MODEL}")
    logger.info("=" * 60)
    
    history = []
    command_count = 0
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            logger.info("Received EOF/Interrupt, exiting")
            break
        
        query_stripped = query.strip()
        if not query_stripped:
            continue
            
        command_count += 1
        logger.info(f"[Command #{command_count}] User input: {query_stripped[:80]}{'...' if len(query_stripped) > 80 else ''}")
        
        if query_stripped.lower() in ("q", "exit"):
            logger.info("Exit command received, shutting down")
            break
        if query_stripped == "/team":
            logger.debug("Command: /team")
            team_status = TEAM.list_all()
            print(team_status)
            logger.info(f"/team output: {team_status.replace(chr(10), '; ')}")
            continue
        if query_stripped == "/inbox":
            logger.debug("Command: /inbox")
            inbox = BUS.read_inbox("lead")
            print(json.dumps(inbox, indent=2))
            logger.info(f"/inbox output: {len(inbox)} message(s)")
            continue
        if query_stripped == "/tasks":
            logger.debug("Command: /tasks")
            TASKS_DIR.mkdir(exist_ok=True)
            task_count = 0
            for f in sorted(TASKS_DIR.glob("task_*.json")):
                t = json.loads(f.read_text())
                marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
                owner = f" @{t['owner']}" if t.get("owner") else ""
                print(f"  {marker} #{t['id']}: {t['subject']}{owner}")
                task_count += 1
            logger.info(f"/tasks output: {task_count} task(s) displayed")
            continue
        
        logger.info("Command: processing through agent_loop")
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
    
    logger.info("=" * 60)
    logger.info("S11 AUTONOMOUS AGENTS - Program ended")
    logger.info(f"Total commands processed: {command_count}")
    logger.info("=" * 60)
