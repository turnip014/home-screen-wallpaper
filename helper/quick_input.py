"""Floating 'New task' box for the home-screen wallpaper.

Wallpaper Engine doesn't pass keyboard input to wallpapers, so the to-do
widget's "+" button asks the helper to open this small window. Enter adds the
task (and keeps the box open for the next one); Esc or clicking elsewhere closes it.
"""
import argparse
import ctypes
import json
import os
import sys
import tkinter as tk
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--x", type=int, default=-1)
ap.add_argument("--y", type=int, default=-1)
ap.add_argument("--w", type=int, default=0)
ap.add_argument("--port", type=int, default=48650)
ap.add_argument("--mode", choices=("todo", "chat"), default="todo")
args = ap.parse_args()

# only one box at a time
kernel32 = ctypes.windll.kernel32
_mutex = kernel32.CreateMutexW(None, False, f"HomeScreenQuickInput-{args.mode}")
if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    sys.exit(0)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # crisp text, physical-pixel coordinates
except OSError:
    pass

with open(os.path.join(os.environ["LOCALAPPDATA"], "HomeScreen", "token.txt"), encoding="utf-8") as f:
    TOKEN = f.read().strip()

BG, FIELD, TEXT, MUTED, ACCENT = "#181a28", "#23263a", "#f3f4f8", "#9a9db0", "#738cff"
CHAT = args.mode == "chat"
TITLE, HINT, PATH = (("Ask AI", "Enter to send  ·  Esc to close", "/chat/send") if CHAT
                     else ("New task", "Enter to add  ·  Esc to close", "/todos/add"))

root = tk.Tk()
root.withdraw()
root.overrideredirect(True)
root.attributes("-topmost", True)
root.configure(bg=BG)
scale = root.winfo_fpixels("1i") / 96.0
px = lambda v: int(round(v * scale))

frame = tk.Frame(root, bg=BG, padx=px(18), pady=px(14), highlightthickness=1, highlightbackground="#33364a")
frame.pack(fill="both", expand=True)
tk.Label(frame, text=TITLE, bg=BG, fg=MUTED, font=("Segoe UI", 10, "bold")).pack(anchor="w")
entry = tk.Entry(frame, bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat",
                 font=("Segoe UI Variable Display", 15), highlightthickness=2,
                 highlightcolor=ACCENT, highlightbackground=FIELD)
entry.pack(fill="x", pady=(px(8), px(8)), ipady=px(7), ipadx=px(8))
hint = tk.Label(frame, text=HINT, bg=BG, fg=MUTED, font=("Segoe UI", 9))
hint.pack(anchor="w")

# place it where the wallpaper asked (the to-do widget), kept on screen
width = args.w if args.w > 0 else px(460)
root.update_idletasks()
height = frame.winfo_reqheight()
sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
x = args.x if args.x >= 0 else (sw - width) // 2
y = args.y if args.y >= 0 else sh // 3
x, y = max(0, min(x, sw - width)), max(0, min(y, sh - height))
root.geometry(f"{width}x{height}+{x}+{y}")


def post(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{args.port}{path}?t={TOKEN}",
                                 data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def submit(_=None):
    text = entry.get().strip()
    if not text:
        return close()
    try:
        post(PATH, {"text": text})
        entry.delete(0, "end")
        hint.config(text=("Sent ✓   Ask a follow-up, or Esc to close" if CHAT
                          else "Added ✓   Type another, or Esc to close"), fg="#5fe6ad")
    except OSError:
        hint.config(text="Couldn't reach the Home Screen helper", fg="#ff8592")


def close(_=None):
    root.destroy()


def on_focus_out(_=None):
    root.after(150, lambda: root.focus_get() is None and close())


entry.bind("<Return>", submit)
root.bind("<Escape>", close)
root.bind("<FocusOut>", on_focus_out)

root.deiconify()
root.attributes("-alpha", 0.0)
root.update()

# Rounded corners (Windows 11) and take keyboard focus away from the desktop
user32 = ctypes.windll.user32
hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
try:
    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
except OSError:
    pass
user32.keybd_event(0x12, 0, 0, 0)  # a tap of Alt lets a background process take the foreground
user32.keybd_event(0x12, 0, 2, 0)
user32.SetForegroundWindow(hwnd)
entry.focus_force()


def fade(a=0.0):
    a = min(1.0, a + 0.2)
    root.attributes("-alpha", a)
    if a < 1.0:
        root.after(16, fade, a)


fade()
root.mainloop()
