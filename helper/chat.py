"""Local AI chat for the home-screen wallpaper, backed by Ollama on this PC.

Nothing leaves the machine: the helper talks to Ollama on 127.0.0.1:11434.
The Ollama server is started on demand (hidden) the first time a message is
sent, and Ollama unloads the model from the GPU after `keep_alive` of idle
time, so it costs nothing while you game.

The assistant can *propose* actions (run a PowerShell command, open Claude Code).
Nothing runs until the user clicks Run on the wallpaper; the result is then
handed back to the model so it can explain or continue.
"""
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
MAX_HISTORY = 30          # messages sent to the model as context
MAX_STORED = 200          # messages kept on disk
MAX_INPUT = 4000
COMMAND_TIMEOUT = 120     # seconds
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, "projects")


def project_list():
    try:
        names = sorted(d for d in os.listdir(PROJECTS) if os.path.isdir(os.path.join(PROJECTS, d)) and not d.startswith("."))
        return ", ".join(names) or "(none yet)"
    except OSError:
        return "(none yet)"

SYSTEM_PROMPT = (
    "You are a friendly, capable assistant living on the user's desktop home screen (Windows 11). "
    "You run entirely on their own PC, so the conversation is private.\n"
    "You have two tools:\n"
    "- run_command: runs a Windows PowerShell 5.1 command as the user (not as administrator), "
    "starting in {home}. Use it when the user asks you to do or check something on the PC. "
    "Use Start-Process to open apps, files or folders so the command returns immediately. "
    "Never use interactive commands that wait for input. Prefer read-only commands, and never delete, "
    "overwrite or uninstall anything unless the user explicitly asked for exactly that.\n"
    "- open_claude_code: opens Claude Code (Anthropic's coding assistant) in a terminal, optionally in a "
    "project folder and with a first prompt. Use it when the user wants Claude, Claude Code or the Claude chat.\n"
    "Every tool call is shown to the user, who must click Run before it happens, so briefly say what "
    "you are about to do. If the user declines, accept it and don't retry. "
    "Make commands produce human-readable output (e.g. convert bytes to GB with [math]::Round(x/1GB,1)) "
    "and summarise results accurately from the output, never guess numbers. "
    "After a command runs you get its output: summarise the result plainly.\n"
    "The user's projects are in {projects}: {project_list}.\n"
    "You cannot browse the web or set reminders. "
    "Answer concisely; use short paragraphs or bullet points, and Markdown code blocks for code.\n"
    "Current local date and time: {now}."
)

TOOLS = [
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a Windows PowerShell command on the user's PC after they approve it. Returns its output.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The PowerShell command to run"},
            "reason": {"type": "string", "description": "One short sentence telling the user what this does"},
        }, "required": ["command", "reason"]},
    }},
    {"type": "function", "function": {
        "name": "open_claude_code",
        "description": "Open Claude Code in a new terminal window, after the user approves.",
        "parameters": {"type": "object", "properties": {
            "folder": {"type": "string", "description": "Project folder to start in (optional; default: home folder)"},
            "prompt": {"type": "string", "description": "First message to send to Claude (optional)"},
        }},
    }},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def run_powershell(command):
    """Run a command in Windows PowerShell; returns (exit code, output text)."""
    script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; $ProgressPreference = 'SilentlyContinue'; "
              f"& {{\n{command}\n}} 2>&1 | Out-String -Width 160")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    # output goes to a file, not a pipe, so apps the command starts can't hold the call open
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-EncodedCommand", encoded],
                                cwd=HOME, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW)
        try:
            code = proc.wait(COMMAND_TIMEOUT)
            note = ""
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
            code, note = -1, f"\n(stopped: still running after {COMMAND_TIMEOUT} s)"
        out.seek(0)
        text = out.read().decode("utf-8", "replace")
    text = "\n".join(line.rstrip() for line in text.strip().splitlines()) + note
    return code, text.strip()


def open_claude_code(folder, prompt):
    folder = os.path.expandvars(os.path.expanduser((folder or "").strip().strip('"'))) or HOME
    if not os.path.isdir(folder):
        return 1, f"Folder not found: {folder}"
    claude = shutil.which("claude") or os.path.join(HOME, ".local", "bin", "claude.exe")
    if not os.path.isfile(claude):
        return 1, "Claude Code isn't installed (claude.exe not found)"
    prompt = " ".join((prompt or "").split())[:2000]
    wt = shutil.which("wt")
    if wt:
        args = [wt, "new-tab", "--title", "Claude Code", "-d", folder, claude]
        if prompt:
            args.append(prompt.replace(";", "\\;"))  # ';' separates commands in Windows Terminal
        subprocess.Popen(args, cwd=folder)
    else:
        args = [claude] + ([prompt] if prompt else [])
        subprocess.Popen(args, cwd=folder, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return 0, f"Opened Claude Code in {folder}" + (" with the prompt." if prompt else ".")


class Chat:
    def __init__(self, app_dir, get_config):
        self.file = os.path.join(app_dir, "chat.json")
        self.get_config = get_config
        self.lock = threading.RLock()  # state() is also called while the lock is held
        self.stop_flag = threading.Event()
        self.data = self._load()
        self.data["busy"] = False  # a crash mid-reply shouldn't leave it stuck
        for m in self.data["messages"]:
            for a in m.get("actions") or []:
                if a.get("status") == "running":
                    a["status"], a["output"] = "error", "(interrupted)"

    # ---- persistence
    def _load(self):
        try:
            with open(self.file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("messages"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"messages": [], "rev": 0}

    def _save(self):
        tmp = self.file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"messages": self.data["messages"][-MAX_STORED:], "rev": self.data["rev"]}, f, indent=1)
        os.replace(tmp, self.file)

    def _bump(self):
        self.data["rev"] = int(self.data.get("rev", 0)) + 1

    def settings(self):
        c = self.get_config().get("chat") or {}
        return {
            "model": c.get("model") or "qwen3:30b",
            "keep_alive": c.get("keep_alive") or "5m",
            "ollama": os.path.expandvars(c.get("ollama") or r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
            "models_dir": os.path.expandvars(c.get("models_dir") or ""),
        }

    def _pending(self):
        return [a for m in self.data["messages"] for a in (m.get("actions") or []) if a["status"] == "pending"]

    def state(self):
        with self.lock:
            shown = [m for m in self.data["messages"] if m["role"] != "tool"][-60:]
            return {"rev": self.data["rev"], "messages": shown,
                    "busy": self.data.get("busy", False), "thinking": self.data.get("thinking", False),
                    "awaiting": bool(self._pending()), "error": self.data.get("error"),
                    "model": self.settings()["model"]}

    def _history(self):
        """Messages in the shape Ollama expects (tool calls + tool results included)."""
        out = []
        for m in self.data["messages"][-MAX_HISTORY:]:
            if m["role"] == "tool":
                out.append({"role": "tool", "content": m["content"], "tool_name": m.get("tool_name", "")})
            elif m.get("actions"):
                out.append({"role": "assistant", "content": m["content"],
                            "tool_calls": [{"function": {"name": a["name"], "arguments": a["args"]}}
                                           for a in m["actions"]]})
            elif m["content"]:
                out.append({"role": m["role"], "content": m["content"]})
        while out and out[0]["role"] == "tool":  # don't start mid tool exchange
            out.pop(0)
        return out

    # ---- Ollama server
    def _up(self):
        try:
            with urllib.request.urlopen(OLLAMA + "/api/version", timeout=2) as r:
                return r.status == 200
        except OSError:
            return False

    def _ensure_server(self, s):
        if self._up():
            return True
        if not os.path.isfile(s["ollama"]):
            raise RuntimeError("Ollama isn't installed")
        env = dict(os.environ)
        if s["models_dir"]:
            env["OLLAMA_MODELS"] = s["models_dir"]
        env["OLLAMA_HOST"] = "127.0.0.1:11434"
        subprocess.Popen([s["ollama"], "serve"], env=env, cwd=os.path.dirname(s["ollama"]),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        for _ in range(60):  # GPU discovery takes ~10 s on a cold start
            time.sleep(0.5)
            if self._up():
                return True
        raise RuntimeError("Ollama didn't start")

    # ---- conversation
    def send(self, text):
        text = str(text or "").strip()[:MAX_INPUT]
        if not text:
            return self.state()
        with self.lock:
            if self.data.get("busy"):
                return self.state()
            # typing a new message instead of answering Run/Don't run counts as "don't run"
            for a in self._pending():
                self._resolve(a, "declined", "The user didn't run this and moved on.")
            self.data["messages"].append({"role": "user", "content": text, "t": time.time()})
            self._start_reply()
        return self.state()

    def _start_reply(self):
        """Call with the lock held: adds an empty assistant message and streams into it."""
        msgs = self.data["messages"]
        out = {"role": "assistant", "content": "", "t": time.time()}
        history = self._history()
        msgs.append(out)
        self.data["busy"] = True
        self.data["error"] = None
        self.stop_flag.clear()
        self._bump()
        threading.Thread(target=self._reply, args=(history, out), daemon=True).start()

    def _reply(self, history, out):
        s = self.settings()
        now = dt.datetime.now().strftime("%A %d %B %Y, %H:%M")
        calls = []
        try:
            self._ensure_server(s)
            body = {"model": s["model"], "stream": True, "think": True, "keep_alive": s["keep_alive"], "tools": TOOLS,
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT.format(now=now, home=HOME, projects=PROJECTS, project_list=project_list())}] + history}
            req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            last = 0.0
            with urllib.request.urlopen(req, timeout=300) as resp:
                for line in resp:
                    if self.stop_flag.is_set():
                        break
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise RuntimeError(chunk["error"])
                    message = chunk.get("message") or {}
                    calls += message.get("tool_calls") or []
                    with self.lock:
                        out["content"] += message.get("content", "")
                        # reasoning is kept out of the reply; the widget just shows "Thinking…"
                        self.data["thinking"] = bool(message.get("thinking")) and not out["content"]
                        if time.time() - last > 0.08 or chunk.get("done"):
                            self._bump()
                            last = time.time()
                    if chunk.get("done"):
                        break
        except Exception as exc:  # shown in the widget
            logging.warning("chat failed: %s", exc)
            msg = str(exc)
            if "not found" in msg and "model" in msg:
                msg = f"Model {s['model']} isn't downloaded yet"
            elif isinstance(exc, OSError):
                msg = "Couldn't reach Ollama"
            with self.lock:
                self.data["error"] = msg
        finally:
            with self.lock:
                out["content"] = out["content"].strip()
                actions = []
                if not self.stop_flag.is_set():
                    for c in calls:
                        fn = c.get("function") or {}
                        args = fn.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except ValueError:
                                args = {}
                        if fn.get("name") in TOOL_NAMES and isinstance(args, dict):
                            if fn["name"] == "run_command" and not str(args.get("command", "")).strip():
                                continue
                            actions.append({"id": hashlib.sha1(f"{time.time()}{len(actions)}".encode()).hexdigest()[:10],
                                            "name": fn["name"], "args": args, "status": "pending"})
                if actions:
                    out["actions"] = actions
                elif not out["content"] and out in self.data["messages"]:
                    self.data["messages"].remove(out)
                self.data["busy"] = False
                self.data["thinking"] = False
                self._bump()
                self._save()

    # ---- approving / declining proposed actions
    def _find(self, action_id):
        for m in self.data["messages"]:
            for a in m.get("actions") or []:
                if a["id"] == action_id:
                    return m, a
        return None, None

    def _resolve(self, action, status, result, code=None):
        """Call with the lock held: records the outcome and the tool message for the model."""
        action["status"] = status
        action["output"] = result[-3000:]
        if code is not None:
            action["exit"] = code
        self.data["messages"].append({"role": "tool", "tool_name": action["name"], "t": time.time(),
                                      "content": (f"Exit code {code}.\n" if code is not None else "") + (result[-6000:] or "(no output)")})

    def decide(self, action_id, approve):
        with self.lock:
            msg, action = self._find(str(action_id))
            if not action or action["status"] != "pending" or self.data.get("busy"):
                return self.state()
            if not approve:
                self._resolve(action, "declined", "The user chose not to run this.")
                self._maybe_continue(msg)
                self._save()
                return self.state()
            action["status"] = "running"
            self.data["busy"] = True
            self._bump()
        threading.Thread(target=self._execute, args=(msg, action), daemon=True).start()
        return self.state()

    def _execute(self, msg, action):
        args = action["args"]
        try:
            if action["name"] == "run_command":
                logging.info("chat: running approved command: %s", args.get("command"))
                code, output = run_powershell(str(args.get("command", "")))
            else:
                code, output = open_claude_code(args.get("folder"), args.get("prompt"))
        except Exception as exc:
            code, output = -1, f"Couldn't run it: {exc}"
        with self.lock:
            self.data["busy"] = False
            if msg not in self.data["messages"]:  # chat was cleared meanwhile
                self._bump()
                return
            self._resolve(action, "done" if code == 0 else "error", output, code)
            self._maybe_continue(msg)
            self._save()

    def _maybe_continue(self, msg):
        """Call with the lock held: once every action in a message is settled, let the model respond."""
        self._bump()
        if any(a["status"] in ("pending", "running") for a in msg.get("actions") or []):
            return
        if all(a["status"] == "declined" for a in msg["actions"]):
            return  # nothing happened; no need for the model to comment
        self._start_reply()

    def stop(self):
        self.stop_flag.set()
        return self.state()

    def clear(self):
        with self.lock:
            if self.data.get("busy"):
                self.stop_flag.set()
            self.data["messages"] = []
            self.data["error"] = None
            self._bump()
            self._save()
        return self.state()
