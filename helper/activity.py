"""Activity tracking + exercise breaks, all local.

A background thread checks every few seconds:
  * which app is in front (app *names* only, never window titles), kept for 7 days
    in activity.json so the assistant can answer "what was I using earlier?"
  * whether you're at the PC (keyboard/mouse input in the last few minutes, or a
    full-screen game running). A long enough break resets the sitting timer.

After `exercise.every_minutes` of continuous use, a small window pops up in the
corner suggesting a quick exercise (10 push-ups, 15 squats, ...). It never steals
focus and waits while a full-screen game or presentation is running.
"""
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import threading
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32
version = ctypes.windll.version
kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TICK = 5                 # seconds between checks
KEEP_DAYS = 7
DEFAULT_EXERCISES = [
    {"name": "Push-ups", "amount": "10", "how": "Hands under shoulders, body straight. Knees down is fine."},
    {"name": "Squats", "amount": "15", "how": "Feet shoulder-width, sit back like into a chair, chest up."},
    {"name": "Plank", "amount": "30 seconds", "how": "Forearms down, body in a straight line, breathe."},
    {"name": "Lunges", "amount": "8 per leg", "how": "Step forward, both knees to about 90°, push back up."},
    {"name": "Calf raises", "amount": "20", "how": "Rise onto your toes, pause, lower slowly."},
    {"name": "Glute bridges", "amount": "12", "how": "On your back, knees bent, lift your hips and squeeze."},
    {"name": "Wall sit", "amount": "30 seconds", "how": "Back against a wall, thighs parallel to the floor."},
    {"name": "Shoulder & neck rolls", "amount": "30 seconds", "how": "Slow circles both ways, then look far away for 20 s."},
]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds():
    li = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not user32.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    return ((kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


_PROCS = {"t": 0.0, "names": set()}
_PROCS_LOCK = threading.Lock()


def running_exes(max_age=5.0):
    """Lower-case exe names of running processes (e.g. 'spotify.exe')."""
    with _PROCS_LOCK:
        if time.time() - _PROCS["t"] < max_age:
            return _PROCS["names"]
        names = set()
        snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if snap and snap != ctypes.c_void_p(-1).value:
            try:
                e = PROCESSENTRY32W()
                e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
                while ok:
                    names.add(e.szExeFile.lower())
                    ok = kernel32.Process32NextW(snap, ctypes.byref(e))
            finally:
                kernel32.CloseHandle(snap)
        _PROCS.update(t=time.time(), names=names)
        return names


def app_running(exe):
    return exe.lower() in running_exes()


def notification_state():
    """1 locked/away, 2 full-screen app, 3 full-screen D3D game, 4 presenting, 5 normal, 6 quiet time, 7 app."""
    state = ctypes.c_int(0)
    if shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return 5
    return state.value


kernel32.OpenProcess.restype = wt.HANDLE
user32.GetForegroundWindow.restype = wt.HWND
_names = {}


def _describe(exe):
    """Friendly app name from the exe's version info (e.g. 'Brave Browser'), cached."""
    if exe in _names:
        return _names[exe]
    import re
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", os.path.splitext(os.path.basename(exe))[0])  # WindowsTerminal -> Windows Terminal
    try:
        size = version.GetFileVersionInfoSizeW(exe, None)
        if size:
            buf = ctypes.create_string_buffer(size)
            if version.GetFileVersionInfoW(exe, 0, size, buf):
                ptr, ln = ctypes.c_void_p(), wt.UINT()
                if version.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(ln)) and ln.value >= 4:
                    lang, cp = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ushort * 2)).contents
                    for field in ("FileDescription", "ProductName"):
                        sub = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\{field}"
                        if version.VerQueryValueW(buf, sub, ctypes.byref(ptr), ctypes.byref(ln)) and ln.value > 1:
                            val = ctypes.wstring_at(ptr, ln.value - 1).strip()
                            if val and len(val) < 60:
                                name = val
                                break
    except OSError:
        pass
    _names[exe] = name
    return name


IGNORE = {"explorer.exe", "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
          "lockapp.exe", "applicationframehost.exe", "textinputhost.exe", "wallpaper64.exe", "wallpaper32.exe",
          "pythonw.exe", "python.exe"}


GAME_DIRS = ("/steamapps/common/", "/epic games/", "/xboxgames/", "/riot games/", "/gog galaxy/games/",
             "/ubisoft game launcher/games/", "/ea games/", "/battle.net/", "/hoyoplay/games/",
             "/windowsapps/microsoft.gaming", "/games/")  # compared against the path with forward slashes
NOT_GAMES = {"steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "galaxyclient.exe", "battle.net.exe",
             "riotclientservices.exe", "chrome.exe", "msedge.exe", "brave.exe", "firefox.exe", "vlc.exe",
             "spotify.exe", "discord.exe", "code.exe", "windowsterminal.exe", "wallpaper64.exe", "explorer.exe"}


def current_game(state=None):
    """Name of the game in front, or None: something from a games folder, or any full-screen app
    (notification state 2/3 = busy / Direct3D full screen) that isn't a browser, player or launcher."""
    exe = foreground_exe()
    if not exe or os.path.basename(exe).lower() in NOT_GAMES | IGNORE:
        return None
    low = exe.lower().replace(os.sep, "/")
    state = notification_state() if state is None else state
    if any(d in low for d in GAME_DIRS) or state in (2, 3):
        return _describe(exe)
    return None


def foreground_exe():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return None
        exe = buf.value
    finally:
        kernel32.CloseHandle(h)
    return exe


def foreground_app():
    exe = foreground_exe()
    if not exe or os.path.basename(exe).lower() in IGNORE:
        return None  # desktop, Start menu, the wallpaper itself…
    return _describe(exe)


class Activity:
    def __init__(self, app_dir, get_config, helper_port):
        self.file = os.path.join(app_dir, "activity.json")
        self.ex_file = os.path.join(app_dir, "exercise.json")
        self.get_config = get_config
        self.port = helper_port
        self.lock = threading.RLock()
        self.apps = self._load(self.file, {"sessions": []})["sessions"]
        self.ex = self._load(self.ex_file, {"log": [], "next": 0})
        self.active_since = None     # start of the current sitting session
        self.last_active = time.time()
        self.snooze_until = 0.0
        self.popup = None
        self.prompt_at = 0.0         # when the current popup appeared
        self.state = 5
        self.game = None             # name of the game in front (for Rora DJ)
        self.dirty = False
        self.rev = 0

    # ---- persistence
    @staticmethod
    def _load(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {**default, **data}
        except (OSError, ValueError):
            pass
        return default

    @staticmethod
    def _write(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def settings(self):
        c = self.get_config().get("exercise") or {}
        return {"enabled": c.get("enabled", True) is not False,
                "every": max(10, int(c.get("every_minutes", 60))),
                "break": max(2, int(c.get("break_minutes", 5))),
                "snooze": max(5, int(c.get("snooze_minutes", 15))),
                "exercises": c.get("exercises") or DEFAULT_EXERCISES}

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="activity").start()

    # ---- main loop
    def _loop(self):
        saved = time.time()
        while True:
            try:
                self._tick()
                if self.dirty and time.time() - saved > 60:
                    self.save()
                    saved = time.time()
            except Exception:
                logging.exception("activity tick failed")
            time.sleep(TICK)

    def save(self):
        with self.lock:
            cutoff = time.time() - KEEP_DAYS * 86400
            self.apps[:] = [s for s in self.apps if s["end"] > cutoff]
            self._write(self.file, {"sessions": self.apps})
            self.dirty = False

    def _tick(self):
        now = time.time()
        st = notification_state()
        idle = idle_seconds()
        s = self.settings()
        gaming = st in (2, 3, 4)
        present = st != 1 and (idle < 120 or gaming)
        with self.lock:
            self.state = st
            if present:
                if self.active_since is None or now - self.last_active > s["break"] * 60:
                    self.active_since = now  # back after a real break: start counting again
                    self.rev += 1
                self.last_active = now
                app = foreground_app() if st != 1 else None
                self.game = current_game(st) if st != 1 else None
                if app:
                    last = self.apps[-1] if self.apps else None
                    if last and last["app"] == app and now - last["end"] < TICK * 3:
                        last["end"] = now
                    else:
                        self.apps.append({"app": app, "start": now, "end": now})
                        self.rev += 1
                    self.dirty = True
            else:
                self.game = None
            if not present and self.active_since and now - self.last_active > s["break"] * 60:
                self.active_since = None  # took a break
                self.rev += 1
                self._close_popup()
            due = (s["enabled"] and self.active_since and now - self.active_since >= s["every"] * 60
                   and now >= self.snooze_until)
            if due and st in (5, 6, 7) and idle < 120 and not self._popup_open():
                self._open_popup()

    # ---- exercises
    def current(self):
        ex = self.settings()["exercises"]
        return ex[int(self.ex.get("next", 0)) % len(ex)]

    def _popup_open(self):
        return self.popup is not None and self.popup.poll() is None

    def _open_popup(self):
        e = self.current()
        here = os.path.dirname(os.path.abspath(__file__))
        minutes = int((time.time() - self.active_since) / 60)
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        self.popup = subprocess.Popen([exe, os.path.join(here, "exercise_popup.py"), "--port", str(self.port),
                                       "--name", e["name"], "--amount", str(e["amount"]), "--how", e.get("how", ""),
                                       "--minutes", str(minutes)], cwd=here, creationflags=NO_WINDOW)
        self.prompt_at = time.time()
        self.rev += 1

    def _close_popup(self):
        if self._popup_open():
            self.popup.terminate()

    def record(self, action):
        """action: done | skip | snooze | next (cycle the suggestion)."""
        s = self.settings()
        with self.lock:
            now = time.time()
            if action == "done":
                e = self.current()
                self.ex["log"].append({"t": now, "name": e["name"], "amount": str(e["amount"])})
                self.ex["log"] = self.ex["log"][-1000:]
                self.ex["next"] = int(self.ex.get("next", 0)) + 1
                self.active_since = now  # the timer starts again after moving
                self.snooze_until = 0
            elif action == "skip":
                self.ex["next"] = int(self.ex.get("next", 0)) + 1
                self.active_since = now
                self.snooze_until = 0
            elif action == "snooze":
                self.snooze_until = now + s["snooze"] * 60
            elif action == "next":
                self.ex["next"] = int(self.ex.get("next", 0)) + 1
            else:
                return self.status()
            if action != "next":
                self._close_popup()
            self._write(self.ex_file, self.ex)
            self.rev += 1
            return self.status()

    def _days_done(self):
        return {dt.date.fromtimestamp(x["t"]) for x in self.ex["log"]}

    def status(self):
        s = self.settings()
        with self.lock:
            now = time.time()
            today = dt.date.today()
            done_today = [x for x in self.ex["log"] if dt.date.fromtimestamp(x["t"]) == today]
            days = self._days_done()
            streak, d = 0, today if today in days else today - dt.timedelta(days=1)
            while d in days:
                streak += 1
                d -= dt.timedelta(days=1)
            active = int(now - self.active_since) if self.active_since else 0
            return {
                "rev": self.rev, "enabled": s["enabled"], "every": s["every"] * 60, "active": active,
                "dueIn": max(0, s["every"] * 60 - active) if self.active_since else None,
                "snoozedFor": max(0, int(self.snooze_until - now)),
                "prompting": self._popup_open(), "gaming": self.state in (2, 3, 4),
                "next": self.current(), "today": done_today, "streak": streak,
                "week": sum(1 for x in self.ex["log"] if now - x["t"] < 7 * 86400),
            }

    # ---- app history for the assistant
    def recent_apps(self, hours=24):
        with self.lock:
            cutoff = time.time() - hours * 3600
            rows = [s for s in self.apps if s["end"] > cutoff]
        totals, last_seen = {}, {}
        for r in rows:
            totals[r["app"]] = totals.get(r["app"], 0) + max(TICK, r["end"] - r["start"])
            last_seen[r["app"]] = max(last_seen.get(r["app"], 0), r["end"])
        return rows, totals, last_seen

    def describe(self, hours=24):
        rows, totals, last_seen = self.recent_apps(hours)
        if not rows:
            return "No app activity recorded yet."
        fmt = lambda t: dt.datetime.fromtimestamp(t).strftime("%a %H:%M")
        lines = [f"Apps used in the last {hours} h (time in front, last used):"]
        for app, secs in sorted(totals.items(), key=lambda kv: -kv[1])[:15]:
            lines.append(f"- {app}: {secs / 60:.0f} min, last {fmt(last_seen[app])}")
        lines.append("Most recent switches:")
        for r in [r for r in rows if r["end"] - r["start"] >= 20][-12:]:
            lines.append(f"- {fmt(r['start'])}–{dt.datetime.fromtimestamp(r['end']).strftime('%H:%M')} {r['app']}")
        return "\n".join(lines)
