#!/usr/bin/env python3
"""
s10_team_protocols.py - Team Protocols

Shutdown protocol and plan approval protocol, both using the same
request_id correlation pattern. Builds on s09's team messaging.

    Shutdown FSM: pending -> approved | rejected

    Lead                              Teammate
    +---------------------+          +---------------------+
    | shutdown_request     |          |                     |
    | {                    | -------> | receives request    |
    |   request_id: abc    |          | decides: approve?   |
    | }                    |          |                     |
    +---------------------+          +---------------------+
                                             |
    +---------------------+          +-------v-------------+
    | shutdown_response    | <------- | shutdown_response   |
    | {                    |          | {                   |
    |   request_id: abc    |          |   request_id: abc   |
    |   approve: true      |          |   approve: true     |
    | }                    |          | }                   |
    +---------------------+          +---------------------+
            |
            v
    status -> "shutdown", thread stops

    Plan approval FSM: pending -> approved | rejected

    Teammate                          Lead
    +---------------------+          +---------------------+
    | plan_approval        |          |                     |
    | submit: {plan:"..."}| -------> | reviews plan text   |
    +---------------------+          | approve/reject?     |
                                     +---------------------+
                                             |
    +---------------------+          +-------v-------------+
    | plan_approval_resp   | <------- | plan_approval       |
    | {approve: true}      |          | review: {req_id,    |
    +---------------------+          |   approve: true}     |
                                     +---------------------+

    Trackers: {request_id: {"target|from": name, "status": "pending|..."}}

Key insight: "Same request_id correlation pattern, two domains."
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
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"

SYSTEM = f"You are a team lead at {WORKDIR}. Manage teammates with shutdown and plan approval protocols."

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}


def _to_openai_tools(anthropic_tools: list) -> list:
    """Convert Anthropic tool format to OpenAI format."""
    openai_tools = []
    for tool in anthropic_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        })
    return openai_tools

# -- Request trackers: correlate by request_id --
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()


# -- MessageBus: JSONL inbox per teammate --
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        logger.debug(f"[MessageBus] send: {sender} -> {to}, type={msg_type}")
        if msg_type not in VALID_MSG_TYPES:
            logger.warning(f"[MessageBus] Invalid msg_type '{msg_type}' from {sender}")
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")
        logger.info(f"[MessageBus] Message sent: {sender} -> {to}, type={msg_type}")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            logger.debug(f"[MessageBus] Inbox not found: {name}")
            return []
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))
        inbox_path.write_text("")
        if messages:
            logger.info(f"[MessageBus] Read {len(messages)} messages from {name}'s inbox")
            logger.debug(f"[MessageBus] Message types: {[m.get('type') for m in messages]}")
        else:
            logger.debug(f"[MessageBus] Inbox empty: {name}")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        logger.info(f"[MessageBus] Broadcast from {sender} to {count} teammates")
        return f"Broadcast to {count} teammates"


BUS = MessageBus(INBOX_DIR)


# -- TeammateManager with shutdown + plan approval --
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        logger.info(f"[TeammateManager] Spawning teammate: {name}, role={role}")
        member = self._find_member(name)
        if member:
            logger.debug(f"[TeammateManager] Found existing member: {name}, current status={member.get('status')}")
            if member["status"] not in ("idle", "shutdown"):
                logger.warning(f"[TeammateManager] Cannot spawn {name}: status={member['status']}")
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
            logger.info(f"[TeammateManager] Reusing existing member {name}, status->working")
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
            logger.info(f"[TeammateManager] Created new member: {name}")
        self._save_config()
        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        logger.info(f"[TeammateManager] Thread started for {name}")
        return f"Spawned '{name}' (role: {role})"

    def _teammate_loop(self, name: str, role: str, prompt: str):
        logger.info(f"[{name}] Teammate loop started, role={role}")
        sys_prompt = (
            f"You are '{name}', role: {role}, at {WORKDIR}. "
            f"Submit plans via plan_approval before major work. "
            f"Respond to shutdown_request with shutdown_response."
        )
        messages = [{"role": "user", "content": prompt}]
        tools = self._teammate_tools()
        should_exit = False
        iteration = 0
        for _ in range(50):
            iteration += 1
            logger.debug(f"[{name}] Iteration {iteration}: checking inbox")
            inbox = BUS.read_inbox(name)
            for msg in inbox:
                messages.append({"role": "user", "content": json.dumps(msg)})
                logger.debug(f"[{name}] Received message: type={msg.get('type')}, from={msg.get('from')}")
            if should_exit:
                logger.info(f"[{name}] Exit flag set, breaking loop")
                break
            try:
                logger.debug(f"[{name}] Calling LLM, messages count={len(messages)}")
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "system", "content": sys_prompt}] + messages,
                    tools=tools,
                    max_tokens=8000,
                )
                logger.debug(f"[{name}] LLM response received, finish_reason={response.choices[0].finish_reason}")
            except Exception as e:
                logger.error(f"[{name}] LLM call failed: {e}")
                break
            choice = response.choices[0]
            assistant_message = {
                "role": "assistant",
                "content": choice.message.content,
            }
            if choice.message.tool_calls:
                assistant_message["tool_calls"] = choice.message.tool_calls
            messages.append(assistant_message)
            if choice.finish_reason != "tool_calls":
                logger.info(f"[{name}] No tool calls, finishing. finish_reason={choice.finish_reason}")
                break
            results = []
            for tool_call in choice.message.tool_calls:
                function_name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments)
                logger.info(f"[{name}] Executing tool: {function_name}, args={str(arguments)[:100]}...")
                output = self._exec(name, function_name, arguments)
                print(f"  [{name}] {function_name}: {str(output)[:120]}")
                results.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": str(output),
                })
                if function_name == "shutdown_response" and arguments.get("approve"):
                    should_exit = True
                    logger.info(f"[{name}] Shutdown approved, will exit after this iteration")
            messages.extend(results)
        member = self._find_member(name)
        if member:
            final_status = "shutdown" if should_exit else "idle"
            member["status"] = final_status
            self._save_config()
            logger.info(f"[{name}] Loop ended, status set to: {final_status}")

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        logger.debug(f"[{sender}] Tool execution: {tool_name}")
        # these base tools are unchanged from s02
        if tool_name == "bash":
            logger.info(f"[{sender}] Executing bash: {args['command'][:80]}...")
            return _run_bash(args["command"])
        if tool_name == "read_file":
            logger.info(f"[{sender}] Reading file: {args['path']}")
            return _run_read(args["path"])
        if tool_name == "write_file":
            logger.info(f"[{sender}] Writing file: {args['path']}, size={len(args.get('content', ''))} bytes")
            return _run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            logger.info(f"[{sender}] Editing file: {args['path']}")
            return _run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            logger.info(f"[{sender}] Sending message to: {args['to']}, type={args.get('msg_type', 'message')}")
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            logger.debug(f"[{sender}] Reading inbox")
            return json.dumps(BUS.read_inbox(sender), indent=2)
        if tool_name == "shutdown_response":
            req_id = args["request_id"]
            approve = args["approve"]
            logger.info(f"[{sender}] Shutdown response: req_id={req_id}, approve={approve}")
            with _tracker_lock:
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = "approved" if approve else "rejected"
                    logger.debug(f"[{sender}] Updated shutdown_requests[{req_id}] status to {shutdown_requests[req_id]['status']}")
            BUS.send(
                sender, "lead", args.get("reason", ""),
                "shutdown_response", {"request_id": req_id, "approve": approve},
            )
            return f"Shutdown {'approved' if approve else 'rejected'}"
        if tool_name == "plan_approval":
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            logger.info(f"[{sender}] Plan approval submitted: req_id={req_id}, plan_length={len(plan_text)}")
            with _tracker_lock:
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
                logger.debug(f"[{sender}] Created plan_requests[{req_id}]")
            BUS.send(
                sender, "lead", plan_text, "plan_approval_response",
                {"request_id": req_id, "plan": plan_text},
            )
            return f"Plan submitted (request_id={req_id}). Waiting for lead approval."
        logger.warning(f"[{sender}] Unknown tool: {tool_name}")
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        # these base tools are unchanged from s02
        anthropic_tools = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "Send message to a teammate.",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
            {"name": "read_inbox", "description": "Read and drain your inbox.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "shutdown_response", "description": "Respond to a shutdown request. Approve to shut down, reject to keep working.",
             "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}},
            {"name": "plan_approval", "description": "Submit a plan for lead approval. Provide plan text.",
             "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
        ]
        return _to_openai_tools(anthropic_tools)

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
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def _run_read(path: str, limit: int = None) -> str:
    try:
        lines = _safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def _run_write(path: str, content: str) -> str:
    try:
        fp = _safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def _run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = _safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- Lead-specific protocol handlers --
def handle_shutdown_request(teammate: str) -> str:
    logger.info(f"[Lead] Sending shutdown request to: {teammate}")
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
        logger.debug(f"[Lead] Created shutdown_requests[{req_id}] for {teammate}")
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    logger.info(f"[Lead] Shutdown request sent: req_id={req_id}, target={teammate}")
    return f"Shutdown request {req_id} sent to '{teammate}' (status: pending)"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    logger.info(f"[Lead] Processing plan review: req_id={request_id}, approve={approve}")
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        logger.warning(f"[Lead] Unknown plan request_id: {request_id}")
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
        logger.debug(f"[Lead] Updated plan_requests[{request_id}] status to {req['status']}")
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    logger.info(f"[Lead] Plan {req['status']} for '{req['from']}', req_id={request_id}")
    return f"Plan {req['status']} for '{req['from']}'"


def _check_shutdown_status(request_id: str) -> str:
    logger.debug(f"[Lead] Checking shutdown status: req_id={request_id}")
    with _tracker_lock:
        result = shutdown_requests.get(request_id, {"error": "not found"})
        if "error" not in result:
            logger.debug(f"[Lead] Shutdown status: {result}")
        return json.dumps(result)


# -- Lead tool dispatch (12 tools) --
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
}

TOOLS = _to_openai_tools([
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "spawn_teammate", "description": "Spawn a persistent teammate.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "shutdown_request", "description": "Request a teammate to shut down gracefully. Returns a request_id for tracking.",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "shutdown_response", "description": "Check the status of a shutdown request by request_id.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "plan_approval", "description": "Approve or reject a teammate's plan. Provide request_id + approve + optional feedback.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
])


def agent_loop(messages: list):
    iteration = 0
    while True:
        iteration += 1
        logger.debug(f"[Lead] Agent loop iteration {iteration}")
        inbox = BUS.read_inbox("lead")
        if inbox:
            logger.info(f"[Lead] Received {len(inbox)} messages from inbox")
            logger.debug(f"[Lead] Inbox message types: {[m.get('type') for m in inbox]}")
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
            messages.append({
                "role": "assistant",
                "content": "Noted inbox messages.",
            })
        logger.debug(f"[Lead] Calling LLM, messages count={len(messages)}")
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM}] + messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        choice = response.choices[0]
        logger.debug(f"[Lead] LLM response received, finish_reason={choice.finish_reason}")
        assistant_message = {
            "role": "assistant",
            "content": choice.message.content,
        }
        if choice.message.tool_calls:
            assistant_message["tool_calls"] = choice.message.tool_calls
        messages.append(assistant_message)
        if choice.finish_reason != "tool_calls":
            logger.info(f"[Lead] No tool calls, finishing. finish_reason={choice.finish_reason}")
            return
        results = []
        for tool_call in choice.message.tool_calls:
            function_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
            logger.info(f"[Lead] Executing tool: {function_name}")
            handler = TOOL_HANDLERS.get(function_name)
            try:
                output = handler(**arguments) if handler else f"Unknown tool: {function_name}"
            except Exception as e:
                logger.error(f"[Lead] Tool execution error: {e}")
                output = f"Error: {e}"
            print(f"> {function_name}: {str(output)[:200]}")
            results.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(output),
            })
        messages.extend(results)


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("s10_team_protocols.py started")
    logger.info(f"WORKDIR: {WORKDIR}")
    logger.info(f"TEAM_DIR: {TEAM_DIR}")
    logger.info(f"MODEL: {MODEL}")
    logger.info("=" * 60)
    
    history = []
    while True:
        try:
            query = input("\033[36ms10 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            logger.info("Received EOF/KeyboardInterrupt, exiting...")
            break
        if query.strip().lower() in ("q", "exit", ""):
            logger.info("User exited")
            break
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
        logger.info(f"User input: {query[:100]}...")
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
