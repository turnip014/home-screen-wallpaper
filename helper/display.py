"""Lower the main display's refresh rate while the wallpaper rests, and restore it on any input.

On very high resolution / high refresh rate displays the GPU driver can keep VRAM at full clock even on
an idle desktop; at a lower refresh rate it can clock down. The switch is "dynamic" (not saved to the registry), so a
reboot or sign-out always comes back at the normal rate. The helper restores the rate itself as
soon as there's keyboard / mouse input, without waiting for the wallpaper.
"""
import atexit
import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

user32 = ctypes.windll.user32
ENUM_CURRENT = -1
DM_DISPLAYFREQUENCY = 0x400000


class DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wt.WCHAR * 32), ("dmSpecVersion", wt.WORD), ("dmDriverVersion", wt.WORD),
                ("dmSize", wt.WORD), ("dmDriverExtra", wt.WORD), ("dmFields", wt.DWORD),
                ("dmPositionX", wt.LONG), ("dmPositionY", wt.LONG), ("dmDisplayOrientation", wt.DWORD),
                ("dmDisplayFixedOutput", wt.DWORD), ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                ("dmFormName", wt.WCHAR * 32), ("dmLogPixels", wt.WORD), ("dmBitsPerPel", wt.DWORD),
                ("dmPelsWidth", wt.DWORD), ("dmPelsHeight", wt.DWORD), ("dmDisplayFlags", wt.DWORD),
                ("dmDisplayFrequency", wt.DWORD), ("dmICMMethod", wt.DWORD), ("dmICMIntent", wt.DWORD),
                ("dmMediaType", wt.DWORD), ("dmDitherType", wt.DWORD), ("dmReserved1", wt.DWORD),
                ("dmReserved2", wt.DWORD), ("dmPanningWidth", wt.DWORD), ("dmPanningHeight", wt.DWORD)]


def _mode(i):
    d = DEVMODEW()
    d.dmSize = ctypes.sizeof(DEVMODEW)
    return d if user32.EnumDisplaySettingsW(None, i, ctypes.byref(d)) else None


def current_hz():
    m = _mode(ENUM_CURRENT)
    return m.dmDisplayFrequency if m else 0


def rates():
    """Refresh rates offered at the current resolution and colour depth."""
    cur = _mode(ENUM_CURRENT)
    out, i = set(), 0
    while cur and i < 1000:
        m = _mode(i)
        if not m:
            break
        if (m.dmPelsWidth, m.dmPelsHeight, m.dmBitsPerPel) == (cur.dmPelsWidth, cur.dmPelsHeight, cur.dmBitsPerPel):
            out.add(m.dmDisplayFrequency)
        i += 1
    return sorted(out)


def _set_hz(hz):
    d = _mode(ENUM_CURRENT)
    if not d or d.dmDisplayFrequency == hz:
        return True
    d.dmDisplayFrequency = hz
    d.dmFields = DM_DISPLAYFREQUENCY
    return user32.ChangeDisplaySettingsExW(None, ctypes.byref(d), None, 0, None) == 0  # 0 = not saved


class Display:
    def __init__(self, idle_seconds, busy):
        self.idle_seconds = idle_seconds  # () -> seconds since the last keyboard / mouse input
        self.busy = busy                  # () -> True while a game / fullscreen app runs (never touch it then)
        self.lock = threading.Lock()
        self.normal = 0                   # the rate to go back to (0 = not lowered)
        self.low = 0
        atexit.register(self.restore)

    def status(self):
        return {"hz": current_hz(), "lowered": bool(self.normal), "normal": self.normal or current_hz()}

    def lower(self, target=120):
        """Drop to the lowest offered rate >= 60 that's <= target (120 if offered, else 60)."""
        with self.lock:
            if self.normal or self.busy():
                return self.status()
            now = current_hz()
            ok = [r for r in rates() if 60 <= r <= max(60, target) and r < now]
            if not ok:
                return self.status()
            low = max(ok)
            if self.idle_seconds() < 5:
                return self.status()  # they're back already
            if _set_hz(low):
                self.normal, self.low = now, low
                logging.info("display: resting at %s Hz (normal %s Hz)", low, now)
                threading.Thread(target=self._watch, daemon=True).start()
            return self.status()

    def restore(self):
        with self.lock:
            if not self.normal:
                return self.status()
            normal, self.normal = self.normal, 0
            if not _set_hz(normal):
                logging.warning("display: couldn't restore %s Hz", normal)
            else:
                logging.info("display: back to %s Hz", normal)
            return self.status()

    def _watch(self):
        """Restore the moment there's input (or a game starts), even if the wallpaper is paused."""
        while self.normal:
            time.sleep(0.25)
            try:
                if self.idle_seconds() < 1.5 or self.busy():
                    self.restore()
            except Exception:
                logging.exception("display watch failed")
                self.restore()
