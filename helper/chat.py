"""Local AI chat for the home-screen wallpaper, backed by Ollama on this PC.

Nothing leaves the machine: the helper talks to Ollama on 127.0.0.1:11434.
The Ollama server is started on demand (hidden) the first time a message is
sent, and Ollama unloads the model from the GPU after `keep_alive` of idle
time, so it costs nothing while you game.

The assistant can read what the home screen shows (calendar, music, weather,
markets, PC stats, recent apps, email headers, spending) without asking: that's
all local. Anything that leaves the PC (web search, reading a web page, asking
Claude) or changes the PC (a PowerShell command that isn't read-only) waits for
the user's click on the wallpaper; the result is then handed back to the model.
"""
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

import web_tools

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
    "You are Rora (short for Aurora), a friendly, capable assistant living on the user's desktop home screen "
    "(Windows 11). {about}Introduce yourself as Rora when asked "
    "who you are. Rora is female (she/her). "
    "You run entirely on their own PC, so the conversation is private.\n"
    "What the home screen shows right now:\n{snapshot}\n\n"
    "Tools:\n"
    "- desktop_info: details from the home screen: calendar, tasks, music (what's playing in Spotify plus the user's "
    "listening history: top songs and artists for the week / month / all time, songs on repeat, often-skipped songs), "
    "weather, markets, system (CPU/GPU/RAM/disks), apps (what the user used recently), email (latest inbox subjects), spending "
    "(card transactions this month), exercise. Local and instant; use it whenever the question touches these.\n"
    "- web_search / read_webpage: search the internet or read a page. web_search already returns the top results "
    "plus excerpts from the best pages, which is usually enough to answer. Just call the tool: the wallpaper itself "
    "shows the user an Allow button, so NEVER ask for permission in words. Say which sites you used.\n"
    "IMPORTANT: your built-in knowledge ends around 2024 and is out of date. Today is {now}. For anything that can "
    "change over time (who holds an office or title, news, prices, scores, releases, versions, laws, opening hours, "
    "'current'/'latest'/'now' questions, anything after 2024) or that you are not sure about, call web_search FIRST "
    "instead of answering from memory. Never answer with 'as of my knowledge cutoff' or tell the user to check "
    "elsewhere: search instead. Search results are real and current (the year really is {year}): trust them over your "
    "memory, never call them fictional, hypothetical or speculative, and don't add caveats about your training. If the results don't "
    "answer the question, read the most promising page with read_webpage or search again with different words; "
    "never present ads, shop listings or unrelated pages as the answer.\n"
    "- add_calendar_event: add appointments / events / reminders-with-a-time to their Google Calendar. Turn "
    "'next Friday 3pm' etc. into an exact date (YYYY-MM-DD) and 24-hour time using today's date; if the time "
    "or day is unclear, ask first. Something that happens on several days ('weekdays in January', 'every "
    "Tuesday until March') is ONE call with repeat + repeat_until (e.g. date = the 1st, repeat 'weekdays', "
    "repeat_until = the 31st): the tool works out the days and skips public holidays by itself. Set tentative "
    "for 'to be confirmed'. Never web-search dates or weekdays: work them out from today's date. It shows the "
    "user an Add button; just call it (never open a calendar app or run a command for this). Afterwards "
    "confirm what was added, including any public holidays it skipped and any clash it reports.\n"
    "- spotify_playlist: make a playlist in the user's Spotify from their most played songs (week / month / all "
    "time / on repeat), Spotify's own top tracks, or a list of songs you choose (e.g. 'chill songs like the ones I "
    "play'). Check desktop_info music first to see what they listen to. It shows the user a Create button; "
    "just call it. Afterwards share the playlist link.\n"
    "- recommend_songs: you're also the user's music DJ. When they want music, recommendations or 'something "
    "for this game', first call desktop_info music (their taste, likes/dislikes, the game they're playing), then "
    "recommend 3-6 real songs that fit. Most (at least 2 in 3) must be songs NOT already in their top lists: "
    "discoveries by similar artists or genres. ALWAYS pass songs through recommend_songs (each becomes a card with "
    "Play / Queue / Save / Like / Dislike buttons); never write a song list in your reply. After the cards, reply "
    "with one short line about the vibe.\n"
    "- dj_control: turn Rora DJ auto mode on/off (it keeps queueing picks that fit their taste and current game), "
    "skip / like / save the song playing now, or STEER it: whenever the user says what they want to hear while the DJ "
    "is on ('more upbeat', 'something like Clairo', 'calmer', 'no more rap'), call dj_control steer with that "
    "request; it changes from the next song. 'back to normal' = steer with request 'normal'.\n"
    "- ask_claude: ask Claude (Anthropic's cloud assistant, via Claude Code) a hard coding or research question "
    "and get its answer back. Like the web tools it shows an Allow button; just call it when the task is beyond you.\n"
    "- run_command: runs a Windows PowerShell 5.1 command as the user (not as administrator), "
    "starting in {home}. Use it when the user asks you to do or check something on the PC. "
    "Use Start-Process to open apps, files or folders so the command returns immediately. "
    "Never use interactive commands that wait for input. Prefer read-only commands, and never delete, "
    "overwrite or uninstall anything unless the user explicitly asked for exactly that.\n"
    "- open_claude_code: opens Claude Code (Anthropic's coding assistant) in a terminal, optionally in a "
    "project folder and with a first prompt. Use it when the user wants Claude, Claude Code or the Claude chat.\n"
    "Read-only commands (Get-*, Select-*, ipconfig, systeminfo and similar) run immediately. "
    "Anything that changes the PC is shown to the user, who must click Run before it happens, so briefly say what "
    "you are about to do. Never tell the user to click Run for read-only commands. If the user declines, accept it and don't retry. "
    "Make commands produce human-readable output (e.g. convert bytes to GB with [math]::Round(x/1GB,1)) "
    "and summarise results accurately from the output, never guess numbers. "
    "After a command runs you get its output: summarise the result plainly.\n"
    "The user's projects are in {projects}: {project_list}.\n"
    "You can't set reminders or send email. Never invent events, prices, emails or numbers: look them up. "
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
TOOLS += [
    {"type": "function", "function": {
        "name": "desktop_info",
        "description": "Read live information from the user's home screen. Runs instantly, no approval needed.",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "enum": ["overview", "calendar", "tasks", "music", "weather", "markets",
                                                 "system", "apps", "email", "spending", "exercise"]},
        }, "required": ["topic"]},
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web (after the user allows it). Returns titles, links and snippets.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to search for"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "read_webpage",
        "description": "Download a public web page and return its text (after the user allows it).",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "ask_claude",
        "description": "Ask Claude Code a question in the background and get its answer (after the user allows it).",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "The full question, with all context Claude needs"},
            "folder": {"type": "string", "description": "Project folder Claude may read (optional)"},
        }, "required": ["question"]},
    }},
]
TOOLS.append({"type": "function", "function": {
    "name": "add_calendar_event",
    "description": "Add an appointment / event to the user's Google Calendar (the user clicks Add first). "
                   "Work out the exact date from today's date; leave start_time empty for an all-day event.",
    "parameters": {"type": "object", "properties": {
        "title": {"type": "string", "description": "Event name, e.g. 'Dentist'"},
        "date": {"type": "string", "description": "YYYY-MM-DD"},
        "start_time": {"type": "string", "description": "HH:MM, 24-hour (empty = all day)"},
        "end_time": {"type": "string", "description": "HH:MM, 24-hour (optional)"},
        "duration_minutes": {"type": "integer", "description": "If no end_time (default 60)"},
        "end_date": {"type": "string", "description": "For multi-day all-day events: last day, YYYY-MM-DD"},
        "repeat": {"type": "string", "enum": ["none", "daily", "weekdays", "weekly", "monthly"],
                   "description": "Repeat the event; 'weekdays' = Monday to Friday"},
        "repeat_until": {"type": "string", "description": "With repeat: the last possible day, YYYY-MM-DD"},
        "skip_holidays": {"type": "boolean", "description": "With repeat: skip public holidays (default true)"},
        "tentative": {"type": "boolean", "description": "Not confirmed yet ('to be confirmed', 'maybe')"},
        "location": {"type": "string"},
        "notes": {"type": "string"},
    }, "required": ["title", "date"]},
}})
TOOLS.append({"type": "function", "function": {
    "name": "spotify_playlist",
    "description": "Create a new playlist in the user's Spotify account (the user clicks Create first). "
                   "Prefer a source built from their real listening; use source 'songs' with a list only for "
                   "hand-picked playlists, and only include songs you're sure exist.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "Playlist name"},
        "source": {"type": "string", "enum": ["my_top_week", "my_top_month", "my_top_all_time", "on_repeat",
                                              "spotify_top_4_weeks", "spotify_top_6_months", "spotify_top_all_time",
                                              "songs"]},
        "songs": {"type": "array", "description": "For source 'songs': the songs to add",
                  "items": {"type": "object", "properties": {"title": {"type": "string"}, "artist": {"type": "string"}},
                            "required": ["title", "artist"]}},
        "limit": {"type": "integer", "description": "How many songs (default 25)"},
        "description": {"type": "string"},
    }, "required": ["name", "source"]},
}})
TOOLS += [
    {"type": "function", "function": {
        "name": "recommend_songs",
        "description": "Recommend songs. They are looked up on Spotify and shown as cards with Play, Queue, Save, "
                       "Like and Dislike buttons. Real songs only.",
        "parameters": {"type": "object", "properties": {
            "songs": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "artist": {"type": "string"},
                "why": {"type": "string", "description": "One short sentence: why it fits the user"}},
                "required": ["title", "artist", "why"]}},
        }, "required": ["songs"]},
    }},
    {"type": "function", "function": {
        "name": "dj_control",
        "description": "Control Rora DJ: auto_on (keep playing picks automatically), auto_off, steer (change what "
                       "auto mode plays from the next song on, e.g. 'more chill', 'only Japanese songs', 'boss fight "
                       "energy'; request 'normal' undoes it), skip (skip and dislike the current song), like / save.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["auto_on", "auto_off", "steer", "skip", "like", "dislike", "save"]},
            "request": {"type": "string", "description": "For steer: what the user wants to hear, in a few words"},
        }, "required": ["action"]},
    }},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
AUTO_TOOLS = {"desktop_info", "recommend_songs", "dj_control"}  # local look-ups / music the user asked for
ONLINE_TOOLS = {"web_search", "read_webpage", "ask_claude", "spotify_playlist"}


# ---- read-only commands may run without a click (setting: chat.auto_run_readonly, default on)
SAFE_VERBS = {"get", "select", "where", "sort", "format", "measure", "group", "compare", "test", "resolve",
              "convertto", "convertfrom", "out"}
SAFE_EXES = {"ipconfig", "systeminfo", "hostname", "whoami", "nvidia-smi", "ping", "tracert", "nslookup",
             "netstat", "getmac", "driverquery", "tasklist", "ver", "where.exe", "findstr"}
SAFE_ALIASES = {"gci", "ls", "dir", "gc", "cat", "type", "gps", "ps", "gsv", "gwmi", "gcim", "select", "where",
                "sort", "ft", "fl", "measure", "group", "echo", "write-output"}
UNSAFE_VERB_NOUNS = {"out-file", "out-printer", "out-gridview", "get-credential", "test-path"}  # writes / prompts / (test-path is fine but rarely needed)
SAFE_METHODS = {"tostring", "trim", "trimend", "trimstart", "split", "replace", "substring", "tolower", "toupper",
                "contains", "startswith", "endswith", "indexof", "padleft", "padright", "toshortdatestring",
                "tolongdatestring", "round"}


def is_read_only(command):
    """Conservative check that a PowerShell command only reads information.

    Every command in the pipeline (including inside { } blocks) must be a Get-/Select-/
    Where-/Format-style cmdlet or a known diagnostic program; no redirection, no call
    operator, no .NET static calls except [math], and only harmless string methods.
    Anything unusual returns False, which just means the user is asked first.
    """
    c = command.strip()
    if not c or len(c) > 600:
        return False
    if re.search(r"[<>`&]|\$\(|@\(", c):
        return False  # redirection, call operator, subexpressions
    if c.count("::") != len(re.findall(r"\[math\]::", c, re.I)):
        return False  # .NET static calls other than [math]::Round etc.
    for m in re.finditer(r"\.(\w+)\s*\(", c):
        if m.group(1).lower() not in SAFE_METHODS:
            return False
    # strip quoted strings so their contents aren't mistaken for commands
    bare = re.sub(r"'[^']*'|\"[^\"]*\"", "''", c)
    parts = re.split(r"([|;{}()\n=])", bare)  # keep delimiters so we know what follows each segment
    for i in range(0, len(parts), 2):
        seg = parts[i]
        words = seg.strip().split()
        if not words:
            continue
        first = words[0].lower()
        if first in SAFE_EXES or first.removesuffix(".exe") in SAFE_EXES:
            continue
        nxt = parts[i + 1] if i + 1 < len(parts) else ""
        if nxt == "=" and len(words) == 1 and re.fullmatch(r"\$?[\w:]+", words[0]):
            continue  # hashtable key or variable name; what's assigned is checked as the next segment
        if first[0] in "$'\"[-0123456789,.=" or first in ("''", "%", "?"):
            continue  # expression / value, not a command
        if first in ("foreach-object", "foreach", "where-object"):
            continue  # their { } blocks are checked as segments too
        if "-" in first and first not in UNSAFE_VERB_NOUNS:
            if first.split("-", 1)[0] in SAFE_VERBS and (not first.startswith("out-") or first == "out-string"):
                continue
            return False
        if first in SAFE_EXES or first.removesuffix(".exe") in SAFE_EXES or first in SAFE_ALIASES:
            continue
        return False
    return True


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


def ask_claude(question, folder):
    """Runs Claude Code headless (claude -p) and returns its answer."""
    folder = os.path.expandvars(os.path.expanduser((folder or "").strip().strip('"'))) or HOME
    if not os.path.isdir(folder):
        folder = HOME
    claude = shutil.which("claude") or os.path.join(HOME, ".local", "bin", "claude.exe")
    if not os.path.isfile(claude):
        return 1, "Claude Code isn't installed (claude.exe not found)"
    q = str(question or "").strip()[:8000]
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen([claude, "-p", q, "--output-format", "text"], cwd=folder, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        try:
            code = proc.wait(300)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
            return -1, "Claude didn't answer within 5 minutes."
        out.seek(0)
        return code, out.read().decode("utf-8", "replace").strip()[-12000:]


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


# "- **Title** – Artist" / "1. "Title" by Artist" lines in a reply (Rora listing songs instead of using the tool)
SONG_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*(?:\*\*|\"|“)(?P<t>[^*\"“”\n]{1,80}?)(?:\*\*|\"|”)\s*(?:[–—-]|\bby\b)\s*"
                       r"(?P<a>[^\n*(]{1,60}?)\s*(?:\(.*\))?\s*$", re.M)


def songs_in_text(text):
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = SONG_LINE.match(line)
        if m:
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            why = nxt.strip("*_ ") if nxt.startswith(("*", "_")) and not SONG_LINE.match(nxt) else ""
            out.append({"title": m.group("t").strip(), "artist": m.group("a").strip(" *_"), "why": why[:120]})
    return out


# replies that mean "I don't know this / it may be out of date": the chat then offers a web search
HEDGE = re.compile(r"knowledge cut-?off|training data|as of my (last|latest|most recent) (update|knowledge|training)|"
                   r"my (knowledge|information) (only )?(goes|extends|is limited|ends)|"
                   r"i (don't|do not|can't|cannot) (know|confirm|verify|be sure|access (real-time|current|the internet))|"
                   r"(don't|do not) have (access to )?(real-time|current|up-to-date|live) (information|data)|"
                   r"(check|consult|visit) (an? |the )?(official|reliable|latest|up-to-date)|"
                   r"i'?m not (sure|certain)|may have changed since", re.I)

TEXT_CALL = re.compile(r"<(function[-_]call|tool[-_]call)>\s*(\{.*?\})\s*</\1>|```(?:json)?\s*(\{\s*\"name\".*?\})\s*```", re.S)


def extract_text_calls(content):
    """Sometimes the model writes a tool call as text (<function-call>{...}</function-call>) instead of
    using Ollama's tool format. Turn those into real calls and remove them from the visible reply."""
    calls = []

    def take(m):
        try:
            obj = json.loads(m.group(2) or m.group(3))
        except ValueError:
            return m.group(0)
        if isinstance(obj, dict) and obj.get("name") in TOOL_NAMES:
            calls.append({"function": {"name": obj["name"], "arguments": obj.get("arguments") or obj.get("parameters") or {}}})
            return ""
        return m.group(0)

    return TEXT_CALL.sub(take, content), calls


class Chat:
    def __init__(self, app_dir, get_config, context=None, snapshot=None, tools=None):
        self.file = os.path.join(app_dir, "chat.json")
        self.get_config = get_config
        self.context = context or (lambda topic: "Not available.")
        self.snapshot = snapshot or (lambda: "(not available)")
        self.tools = tools or {}  # extra tools run by the helper, e.g. spotify_playlist
        self.phase, self.phase_t = None, 0.0  # boot progress: "starting" (Ollama) -> "loading" (model into GPU)
        self.warming = False
        self.gaming = lambda: False  # set by the helper: a game runs, keep the GPU free
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
            json.dump({"messages": self.data["messages"][-MAX_STORED:], "rev": self.data["rev"],
                       "load_secs": self.data.get("load_secs", 45)}, f, indent=1)
        os.replace(tmp, self.file)

    def _bump(self):
        self.data["rev"] = int(self.data.get("rev", 0)) + 1

    def settings(self):
        c = self.get_config().get("chat") or {}
        return {
            "model": c.get("model") or "qwen3:30b",
            # while gaming, free the GPU 20 s after an answer instead of keeping the model loaded
            "keep_alive": "20s" if self.gaming() else (c.get("keep_alive") or "5m"),
            "ollama": os.path.expandvars(c.get("ollama") or r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
            "models_dir": os.path.expandvars(c.get("models_dir") or ""),
            "auto_run": c.get("auto_run_readonly", True) is not False,
        }

    def _pending(self):
        return [a for m in self.data["messages"] for a in (m.get("actions") or []) if a["status"] == "pending"]

    def state(self):
        with self.lock:
            shown = [m for m in self.data["messages"] if m["role"] != "tool"][-60:]
            return {"rev": self.data["rev"], "messages": shown,
                    "busy": self.data.get("busy", False), "thinking": self.data.get("thinking", False),
                    "awaiting": bool(self._pending()), "error": self.data.get("error"),
                    "model": self.settings()["model"],
                    "phase": self.phase, "phaseFor": round(time.time() - self.phase_t, 1) if self.phase else 0,
                    "loadSecs": self.data.get("load_secs", 45)}

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

    def _set_phase(self, phase):
        with self.lock:
            if phase != self.phase:
                self.phase, self.phase_t = phase, time.time()
                self._bump()

    def _loaded(self, model):
        try:
            with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=3) as r:
                running = json.load(r).get("models") or []
            return any(m.get("name") == model or m.get("model") == model for m in running)
        except (OSError, ValueError):
            return False

    def warm(self):
        """Start Ollama and load the model while the user is still typing (Ask box opened)."""
        if self.gaming():
            return  # don't preload the model mid-game; it loads only if a message is actually sent
        with self.lock:
            if self.warming or self.data.get("busy"):
                return
            self.warming = True
        threading.Thread(target=self._warm, daemon=True).start()

    def _warm(self):
        s = self.settings()
        try:
            self._ensure_server(s)
            if not self._loaded(s["model"]):
                self._set_phase("loading")
                t0 = time.time()
                body = json.dumps({"model": s["model"], "keep_alive": s["keep_alive"]}).encode()
                req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    r.read()
                with self.lock:
                    self.data["load_secs"] = round(time.time() - t0, 1)
        except Exception as exc:
            logging.warning("warm-up failed: %s", exc)
        finally:
            with self.lock:
                self.warming = False
                if not self.data.get("busy"):
                    self._set_phase(None)

    def _ensure_server(self, s):
        if self._up():
            return True
        self._set_phase("starting")
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

    def unload(self):
        """Free the model's VRAM now (e.g. a game started). Harmless if it isn't loaded."""
        try:
            body = json.dumps({"model": self.settings()["model"], "keep_alive": 0}).encode()
            req = urllib.request.Request(OLLAMA + "/api/generate", data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass

    def complete_json(self, messages, schema, keep_alive=None, temperature=0.8, think=False):
        """One-off structured answer from the local model (used by Rora DJ; not shown in the chat)."""
        s = self.settings()
        self._ensure_server(s)
        self._set_phase(None)
        body = {"model": s["model"], "stream": False, "think": think, "format": schema, "messages": messages,
                "keep_alive": keep_alive or s["keep_alive"], "options": {"temperature": temperature}}
        req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=240) as r:
            res = json.load(r)
        return json.loads((res.get("message") or {}).get("content") or "{}")

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
            cold = not self._loaded(s["model"])
            if cold and not self.warming:
                self._set_phase("loading")
            t0 = time.time()
            try:
                snap = self.snapshot()
            except Exception:
                logging.exception("snapshot failed")
                snap = "(not available)"
            body = {"model": s["model"], "stream": True, "think": True, "keep_alive": s["keep_alive"], "tools": TOOLS,
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT.format(
                        now=now, year=dt.date.today().year, about=(str((self.get_config().get('chat') or {}).get('about') or '').strip() + ' ').lstrip(), home=HOME, projects=PROJECTS, project_list=project_list(), snapshot=snap)}] + history}
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
                    if self.phase:
                        if cold and not self.warming:
                            with self.lock:
                                self.data["load_secs"] = round(time.time() - t0, 1)
                        self._set_phase(None)
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
            self._set_phase(None)
            with self.lock:
                out["content"], text_calls = extract_text_calls(out["content"])
                if not calls:
                    calls = text_calls
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
                            if fn["name"] in ONLINE_TOOLS and not str(
                                    args.get("query") or args.get("url") or args.get("question") or args.get("name") or "").strip():
                                continue
                            if fn["name"] == "add_calendar_event" and not str(args.get("title") or "").strip():
                                continue
                            actions.append({"id": hashlib.sha1(f"{time.time()}{len(actions)}".encode()).hexdigest()[:10],
                                            "name": fn["name"], "args": args, "status": "pending"})
                listed = songs_in_text(out["content"]) if "recommend_songs" in self.tools and not actions else []
                if len(listed) >= 2 and not self.stop_flag.is_set():
                    actions.append({"id": hashlib.sha1(f"{time.time()}rs".encode()).hexdigest()[:10],
                                    "name": "recommend_songs", "args": {"songs": listed[:8]}, "status": "pending"})
                    out["content"] = "Here are some picks for you 🎧"  # the cards replace the text list
                if not actions and not self.stop_flag.is_set() and out["content"] and HEDGE.search(out["content"]):
                    question = self._fallback_query()
                    if question:
                        actions.append({"id": hashlib.sha1(f"{time.time()}fb".encode()).hexdigest()[:10],
                                        "name": "web_search", "args": {"query": question}, "status": "pending",
                                        "fallback": True})
                if actions:
                    out["actions"] = actions
                elif not out["content"] and out in self.data["messages"]:
                    self.data["messages"].remove(out)
                self.data["busy"] = False
                self.data["thinking"] = False
                self._bump()
                self._save()
                # local look-ups and read-only commands run straight away; anything else waits for a click
                auto = [a for a in actions if a["name"] in AUTO_TOOLS or (
                    s["auto_run"] and a["name"] == "run_command" and is_read_only(str(a["args"].get("command", ""))))]
                if auto:
                    for a in auto:
                        a["auto"], a["status"] = True, "running"
                    self.data["busy"] = True
                    self._bump()
                    threading.Thread(target=self._run_auto, args=(out, auto), daemon=True).start()

    def _fallback_query(self):
        """Call with the lock held: the user's latest question, unless the web was already tried for it."""
        for m in reversed(self.data["messages"]):
            if m["role"] == "user":
                q = re.sub(r"(?i)^\s*(please\s+)?(search|look up|google|check)( the web| online| the internet)?( for)?\s*", "", m["content"])
                return " ".join(q.split())[:200]
            if any(a["name"] in ("web_search", "read_webpage") for a in m.get("actions") or []):
                return ""
        return ""

    def _run_auto(self, msg, actions):
        for a in actions:
            try:
                res = self._do(a)
                code, output = res[0], res[1]
                if len(res) > 2 and isinstance(res[2], dict):
                    a.update(res[2])
            except Exception as exc:
                code, output = -1, f"Couldn't run it: {exc}"
            with self.lock:
                if msg not in self.data["messages"]:  # chat was cleared meanwhile
                    self.data["busy"] = False
                    self._bump()
                    return
                self._resolve(a, "done" if code == 0 else "error", output, code)
                self._bump()
        with self.lock:
            self.data["busy"] = False
            self._maybe_continue(msg)
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
                                      "content": (f"Exit code {code}.\n" if code is not None and action["name"] == "run_command" else "")
                                      + (result[-6000:] or "(no output)")})

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
        try:
            res = self._do(action)
            code, output = res[0], res[1]
            if len(res) > 2 and isinstance(res[2], dict):
                action.update(res[2])
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

    def _do(self, action):
        """Runs one tool call; returns (exit code, text for the model)."""
        name, args = action["name"], action["args"]
        logging.info("chat: %s %s", name, json.dumps(args)[:300])
        if name == "run_command":
            return run_powershell(str(args.get("command", "")))
        if name == "open_claude_code":
            return open_claude_code(args.get("folder"), args.get("prompt"))
        if name == "desktop_info":
            return 0, self.context(str(args.get("topic") or "overview"))
        if name == "web_search":
            key = str((self.get_config().get("web") or {}).get("brave_key") or "").strip()
            return 0, web_tools.search_text(args.get("query", ""), brave_key=key)
        if name == "read_webpage":
            return 0, web_tools.read_page(args.get("url", ""))
        if name == "ask_claude":
            return ask_claude(args.get("question"), args.get("folder"))
        if name in self.tools:
            return self.tools[name](args)
        return 1, f"Unknown tool {name}"

    def _maybe_continue(self, msg):
        """Call with the lock held: once every action in a message is settled, let the model respond."""
        self._bump()
        if any(a["status"] in ("pending", "running") for a in msg.get("actions") or []):
            return
        if all(a["status"] == "declined" for a in msg["actions"]) and \
                not any(a["name"] in ONLINE_TOOLS for a in msg["actions"]):
            return  # nothing happened (for a declined look-up the model still answers without it)
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
