"""'Time to move' card shown by the helper after a long stretch at the PC.

Appears bottom-right above the taskbar, slides in, never takes keyboard focus
(so it can't swallow what you're typing), and reports the choice back to the helper.
"""
import argparse
import ctypes
import json
import os
import sys
import tkinter as tk
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=48650)
ap.add_argument("--name", default="Push-ups")
ap.add_argument("--amount", default="10")
ap.add_argument("--how", default="")
ap.add_argument("--minutes", type=int, default=60)
args = ap.parse_args()

kernel32 = ctypes.windll.kernel32
_mutex = kernel32.CreateMutexW(None, False, "HomeScreenExercisePopup")
if kernel32.GetLastError() == 183:
    sys.exit(0)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except OSError:
    pass

with open(os.path.join(os.environ["LOCALAPPDATA"], "HomeScreen", "token.txt"), encoding="utf-8") as f:
    TOKEN = f.read().strip()

BG, CARD, TEXT, MUTED, ACCENT, GOOD = "#151724", "#1e2133", "#f3f4f8", "#9a9db0", "#738cff", "#5fe6ad"

root = tk.Tk()
root.withdraw()
root.overrideredirect(True)
root.attributes("-topmost", True)
root.configure(bg=BG)
scale = root.winfo_fpixels("1i") / 96.0
px = lambda v: int(round(v * scale))

frame = tk.Frame(root, bg=BG, padx=px(22), pady=px(18), highlightthickness=1, highlightbackground="#33364a")
frame.pack(fill="both", expand=True)
hours = args.minutes / 60
since = f"{args.minutes} min" if args.minutes < 90 else f"{hours:.1f} h".replace(".0 h", " h")
tk.Label(frame, text=f"TIME TO MOVE  ·  {since} at the desk", bg=BG, fg=ACCENT,
         font=("Segoe UI", 9, "bold")).pack(anchor="w")
row = tk.Frame(frame, bg=BG)
row.pack(anchor="w", pady=(px(6), 0))
tk.Label(row, text=args.amount, bg=BG, fg=TEXT, font=("Segoe UI Variable Display", 30, "bold")).pack(side="left")
tk.Label(row, text=" " + args.name, bg=BG, fg=TEXT, font=("Segoe UI Variable Display", 22)).pack(side="left", pady=(px(8), 0))
if args.how:
    tk.Label(frame, text=args.how, bg=BG, fg=MUTED, font=("Segoe UI", 10), wraplength=px(340),
             justify="left").pack(anchor="w", pady=(px(2), px(12)))

btns = tk.Frame(frame, bg=BG)
btns.pack(fill="x")


def post(action):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{args.port}/exercise/{action}?t={TOKEN}",
                                     data=b"{}", method="POST", headers={"Content-Type": "text/plain"})
        urllib.request.urlopen(req, timeout=5).read()
    except OSError:
        pass


def choose(action):
    post(action)
    if action == "done":
        for w in frame.winfo_children():
            w.destroy()
        tk.Label(frame, text="Nice one ✓", bg=BG, fg=GOOD, font=("Segoe UI Variable Display", 22, "bold")).pack(pady=px(18))
        root.after(1400, slide_out)
    else:
        slide_out()


def button(text, action, primary=False):
    b = tk.Label(btns, text=text, bg=ACCENT if primary else CARD, fg="#0d0f18" if primary else TEXT,
                 font=("Segoe UI", 10, "bold" if primary else "normal"), padx=px(14), pady=px(7), cursor="hand2")
    b.pack(side="left", padx=(0, px(8)))
    b.bind("<Button-1>", lambda _e: choose(action))


button("Done ✓", "done", True)
button("Snooze 15 min", "snooze")
button("Skip", "skip")

root.update_idletasks()
W, H = max(px(400), frame.winfo_reqwidth()), frame.winfo_reqheight()

# bottom-right of the work area (above the taskbar)
class RECT(ctypes.Structure):
    _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]


wa = RECT()
ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(wa), 0)  # SPI_GETWORKAREA
X, Y = wa.r - W - px(24), wa.b - H - px(24)
root.geometry(f"{W}x{H}+{wa.r}+{Y}")
root.deiconify()
root.update()

user32 = ctypes.windll.user32
hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x80
user32.SetWindowLongW(hwnd, GWL_EXSTYLE, user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
try:
    pref = ctypes.c_int(2)
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
except OSError:
    pass


def slide(frm, to, step=0, done=None):
    steps = 14
    t = (step + 1) / steps
    ease = 1 - (1 - t) ** 3
    root.geometry(f"{W}x{H}+{int(frm + (to - frm) * ease)}+{Y}")
    if step + 1 < steps:
        root.after(14, slide, frm, to, step + 1, done)
    elif done:
        done()


def slide_out():
    slide(X, wa.r + px(10), done=root.destroy)


slide(wa.r, X)
# don't hang around forever if nobody's there: the helper also closes it after a break
root.after(30 * 60 * 1000, lambda: (post("snooze"), slide_out()))
root.mainloop()
