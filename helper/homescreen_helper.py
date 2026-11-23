"""Home Screen helper.

A tiny local server for the Wallpaper Engine home-screen wallpaper. It:
  * lists desktop shortcuts, files and folders (user + public desktop)
  * extracts their real Windows icons as PNGs (cached)
  * launches an item when you click it on the wallpaper
  * sends media keys (play/pause/next/prev) for Spotify etc.
  * reports CPU / RAM / GPU stats for the system widget

Standard library only. Listens on 127.0.0.1 and every request needs the
secret token that install.ps1 generated. It does work only when the
wallpaper asks, so while Wallpaper Engine has the wallpaper stopped (for
example during games) the helper is idle.
"""

import ctypes
import ctypes.wintypes as wt
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import shutil
import struct
import sys
import threading
import time
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlparse

import calendar_ics
import chat as chat_mod
import google_sync as gsync  # same folder

APP_DIR = os.path.join(os.environ["LOCALAPPDATA"], "HomeScreen")
ICON_DIR = os.path.join(APP_DIR, "icons")
TOKEN_FILE = os.path.join(APP_DIR, "token.txt")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
DEFAULT_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.default.json")
HOST = "127.0.0.1"
PORT = 48650
ICON_SIZE = 256

APP_EXTS = {".lnk", ".url", ".exe", ".appref-ms"}
HIDE_EXTS = {".lnk", ".url", ".exe", ".appref-ms"}
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4

os.makedirs(ICON_DIR, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(APP_DIR, "helper.log"),
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(message)s",
)

ole32 = ctypes.windll.ole32
shell32 = ctypes.windll.shell32
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32


# ---------------------------------------------------------------- paths

def known_folder(csidl):
    buf = ctypes.create_unicode_buffer(260)
    shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return buf.value


CSIDL_DESKTOPDIRECTORY = 0x10
CSIDL_COMMON_DESKTOPDIRECTORY = 0x19


# ---------------------------------------------------------------- icons

class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


IID_IShellItemImageFactory = GUID()
ole32.CLSIDFromString("{bcc18b79-ba16-442f-80c4-8a59c30c463b}", ctypes.byref(IID_IShellItemImageFactory))
shell32.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                                                ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.GetDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]

SIIGBF_ICONONLY = 0x4
GetImageProto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, SIZE, ctypes.c_int,
                                   ctypes.POINTER(ctypes.c_void_p))
ReleaseProto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)


def encode_png(width, height, rgba):
    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def crop_small_icon(w, h, rgba):
    """Apps that only ship small icons come back as a tiny icon inside a big frame.
    Find the real icon (ignoring the frame at the edges) and crop to it."""
    inset = max(4, w // 40)
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(inset, h - inset):
        row = y * w * 4
        for x in range(inset, w - inset):
            if rgba[row + x * 4 + 3] > 8:
                x0, x1 = min(x0, x), max(x1, x)
                y0, y1 = min(y0, y), max(y1, y)
    if x1 < 0:
        return w, h, rgba
    cw, ch = x1 - x0 + 1, y1 - y0 + 1
    if cw > w * 0.6 or ch > h * 0.6:
        return w, h, rgba  # normal full-size icon
    side = max(cw, ch)
    out = bytearray(side * side * 4)
    ox, oy = (side - cw) // 2, (side - ch) // 2
    for y in range(ch):
        src = ((y0 + y) * w + x0) * 4
        dst = ((oy + y) * side + ox) * 4
        out[dst:dst + cw * 4] = rgba[src:src + cw * 4]
    return side, side, out


def extract_icon_png(path, size=ICON_SIZE):
    """Return PNG bytes of the icon Explorer shows for `path`, or None."""
    factory = ctypes.c_void_p()
    hr = shell32.SHCreateItemFromParsingName(path, None, ctypes.byref(IID_IShellItemImageFactory),
                                             ctypes.byref(factory))
    if hr != 0 or not factory:
        return None
    vtbl = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    hbm = ctypes.c_void_p()
    try:
        hr = GetImageProto(vtbl[3])(factory, SIZE(size, size), SIIGBF_ICONONLY, ctypes.byref(hbm))
        if hr != 0 or not hbm:
            return None
        bm = BITMAP()
        gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm))
        w, h = bm.bmWidth, abs(bm.bmHeight)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        hdc = user32.GetDC(None)
        try:
            got = gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bmi), 0)
        finally:
            user32.ReleaseDC(None, hdc)
        if got != h:
            return None
        bgra = buf.raw
        rgba = bytearray(len(bgra))
        rgba[0::4] = bgra[2::4]
        rgba[1::4] = bgra[1::4]
        rgba[2::4] = bgra[0::4]
        alpha = bgra[3::4]
        if not any(alpha):
            rgba[3::4] = b"\xff" * (w * h)
        else:
            rgba[3::4] = alpha
            # The shell hands back premultiplied alpha; undo it for semi-transparent edges.
            for i in range(3, len(rgba), 4):
                a = rgba[i]
                if 0 < a < 255:
                    for c in (i - 3, i - 2, i - 1):
                        rgba[c] = min(255, rgba[c] * 255 // a)
        w, h, rgba = crop_small_icon(w, h, rgba)
        return encode_png(w, h, bytes(rgba))
    finally:
        if hbm:
            gdi32.DeleteObject(hbm)
        ReleaseProto(vtbl[2])(factory)


def icon_file_for(path, salt=""):
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        mtime = 0
    key = hashlib.sha1(f"{path.lower()}|{mtime}|{ICON_SIZE}|{salt}".encode()).hexdigest()[:20]
    return key, os.path.join(ICON_DIR, key + ".png")


def get_icon(path, salt=""):
    _, cache = icon_file_for(path, salt)
    if os.path.exists(cache):
        with open(cache, "rb") as f:
            return f.read()
    try:
        png = extract_icon_png(path)
    except Exception:  # never let one bad icon take the helper down
        logging.exception("icon extraction failed for %s", path)
        png = None
    if png:
        with open(cache, "wb") as f:
            f.write(png)
    return png


# ---------------------------------------------------------------- stats

class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _ft(ft):
    return (ft.hi << 32) | ft.lo


_last_cpu = None


def cpu_percent():
    global _last_cpu
    idle, kern, usr = FILETIME(), FILETIME(), FILETIME()
    kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(usr))
    now = (_ft(idle), _ft(kern), _ft(usr))
    prev, _last_cpu = _last_cpu, now
    if prev is None:
        return None
    d_idle = now[0] - prev[0]
    d_total = (now[1] - prev[1]) + (now[2] - prev[2])  # kernel time includes idle
    if d_total <= 0:
        return None
    return round(100.0 * (1 - d_idle / d_total), 1)


class NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class NvmlMem(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class Gpu:
    """Reads the NVIDIA GPU through NVML (ships with the driver). Cheap, no process spawn."""

    def __init__(self):
        self.handle = None
        try:
            self.nvml = ctypes.WinDLL("nvml.dll")
            if self.nvml.nvmlInit_v2() == 0:
                h = ctypes.c_void_p()
                if self.nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) == 0:
                    self.handle = h
        except OSError:
            self.nvml = None

    def read(self):
        if not self.handle:
            return None
        temp, util, mem = ctypes.c_uint(), NvmlUtil(), NvmlMem()
        out = {}
        if self.nvml.nvmlDeviceGetTemperature(self.handle, 0, ctypes.byref(temp)) == 0:
            out["temp"] = temp.value
        if self.nvml.nvmlDeviceGetUtilizationRates(self.handle, ctypes.byref(util)) == 0:
            out["util"] = util.gpu
        if self.nvml.nvmlDeviceGetMemoryInfo(self.handle, ctypes.byref(mem)) == 0:
            out["memUsedGb"] = round(mem.used / 2**30, 1)
            out["memTotalGb"] = round(mem.total / 2**30, 1)
        return out or None


GPU = None


def read_stats():
    ms = MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(ms)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
    used = ms.ullTotalPhys - ms.ullAvailPhys
    disks = []
    for letter in "CDEFGH":
        root = f"{letter}:\\"
        if kernel32.GetDriveTypeW(root) != 3:  # fixed drives only
            continue
        try:
            du = shutil.disk_usage(root)
        except OSError:
            continue
        disks.append({"name": f"{letter}:", "usedPct": round(100 * du.used / du.total, 1),
                      "freeGb": round(du.free / 2**30), "totalGb": round(du.total / 2**30)})
    return {
        "disks": disks,
        "cpu": cpu_percent(),
        "ram": ms.dwMemoryLoad,
        "ramUsedGb": round(used / 2**30, 1),
        "ramTotalGb": round(ms.ullTotalPhys / 2**30, 1),
        "gpu": GPU.read() if GPU else None,
    }


# ---------------------------------------------------------------- recycle bin

RECYCLE_BIN = "shell:RecycleBinFolder"
RECYCLE_ID = "recycle-bin"


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def recycle_bin_count():
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    if shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return 0
    return int(info.i64NumItems)


# ---------------------------------------------------------------- online widgets (weather, markets)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HomeScreen/1"
LOCATION_FILE = os.path.join(APP_DIR, "location.json")
DEFAULT_MARKETS = [
    {"symbol": "^GSPC", "name": "S&P 500"},
    {"symbol": "^NDX", "name": "Nasdaq 100"},
    {"symbol": "GC=F", "name": "Gold"},
    {"symbol": "SI=F", "name": "Silver"},
    {"symbol": "BTC-USD", "name": "Bitcoin"},
    {"symbol": "DX-Y.NYB", "name": "US Dollar"},
]


def http_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Cached:
    """Returns the last result immediately and refreshes it on a background thread
    once it's older than `ttl`. Slow network calls never block the wallpaper, and
    nothing is fetched unless the wallpaper is visible and asking."""

    def __init__(self, fetch, ttl):
        self.fetch, self.ttl = fetch, ttl
        self.data, self.at, self.busy = None, 0.0, False
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if time.time() - self.at > self.ttl and not self.busy:
                self.busy = True
                threading.Thread(target=self._refresh, daemon=True).start()
            return self.data

    def payload(self):
        data = self.get()
        # "refreshing": the wallpaper should ask again in a few seconds for the newer data
        return {"data": data, "refreshing": self.busy}

    def _refresh(self):
        try:
            data = self.fetch()
        except Exception:
            logging.exception("refresh failed: %s", getattr(self.fetch, "__name__", "?"))
            data = None
        with self.lock:
            if data is not None:
                self.data, self.at = data, time.time()
                if isinstance(data, dict) and data.get("errors"):
                    self.at -= self.ttl - 60  # partly failed (e.g. a calendar): try again in a minute
            else:
                self.at = time.time() - self.ttl + 60  # try again in a minute
            self.busy = False


def get_location(config):
    loc = config.get("weather", {}).get("location", "auto")
    if isinstance(loc, dict):
        return loc
    try:
        with open(LOCATION_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    ip = http_json("https://ipinfo.io/json")  # one-time approximate location from the IP
    lat, lon = (float(v) for v in ip["loc"].split(","))
    loc = {"name": ip.get("city") or ip.get("region") or "", "lat": lat, "lon": lon}
    with open(LOCATION_FILE, "w", encoding="utf-8") as f:
        json.dump(loc, f)
    return loc


def fetch_weather():
    config, _ = REG.load_config()
    loc = get_location(config)
    imperial = config.get("weather", {}).get("units", "metric") == "imperial"
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,is_day,wind_speed_10m"
           "&hourly=temperature_2m,weather_code,precipitation_probability,is_day"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&forecast_days=6&timezone=auto"
           + ("&temperature_unit=fahrenheit&wind_speed_unit=mph" if imperial else ""))
    w = http_json(url)
    cur, hr, dy = w["current"], w["hourly"], w["daily"]
    hour_key = cur["time"][:13]
    start = next((i for i, t in enumerate(hr["time"]) if t[:13] >= hour_key), 0)
    hourly = [{"t": hr["time"][i][11:16], "temp": round(hr["temperature_2m"][i]),
               "code": hr["weather_code"][i], "pop": hr["precipitation_probability"][i],
               "day": hr["is_day"][i]}
              for i in range(start + 1, min(start + 9, len(hr["time"])))]
    daily = [{"date": dy["time"][i], "code": dy["weather_code"][i],
              "hi": round(dy["temperature_2m_max"][i]), "lo": round(dy["temperature_2m_min"][i]),
              "pop": dy["precipitation_probability_max"][i]}
             for i in range(len(dy["time"]))]
    return {
        "place": loc.get("name", ""),
        "unit": "°F" if imperial else "°C",
        "windUnit": "mph" if imperial else "km/h",
        "now": {"temp": round(cur["temperature_2m"]), "feels": round(cur["apparent_temperature"]),
                "humidity": cur["relative_humidity_2m"], "wind": round(cur["wind_speed_10m"]),
                "code": cur["weather_code"], "day": cur["is_day"]},
        "hourly": hourly,
        "daily": daily,
    }


def _chart(symbol, rng, interval):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}?range={rng}&interval={interval}"
    return http_json(url)["chart"]["result"][0]


def _closes(result):
    quotes = (result.get("indicators", {}).get("quote") or [{}])[0]
    return [c for c in (quotes.get("close") or []) if c is not None]


def fetch_quote(entry):
    day = _chart(entry["symbol"], "1d", "5m")
    meta = day["meta"]
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    closes = _closes(day)
    if len(closes) < 10:  # e.g. futures over the weekend: use the last 5 days for the chart
        closes = _closes(_chart(entry["symbol"], "5d", "60m"))
    step = max(1, len(closes) // 60)
    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    now = time.time()
    return {
        "symbol": entry["symbol"],
        "name": entry.get("name") or meta.get("shortName") or entry["symbol"],
        "price": price,
        "change": (price - prev) / prev * 100 if price and prev else None,
        "currency": meta.get("currency"),
        "open": bool(period) and period.get("start", 0) <= now < period.get("end", 0),
        "spark": [round(c, 6) for c in closes[::step]],
    }


def fetch_markets():
    config, _ = REG.load_config()
    out = []
    for entry in config.get("markets", DEFAULT_MARKETS):
        try:
            out.append(fetch_quote(entry))
        except Exception:
            logging.exception("quote failed for %s", entry.get("symbol"))
            out.append({"symbol": entry.get("symbol"), "name": entry.get("name") or entry.get("symbol"), "error": True})
    return {"quotes": out, "updated": time.strftime("%H:%M")}


LAYOUT_FILE = os.path.join(APP_DIR, "layout.json")


def load_layout():
    try:
        with open(LAYOUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_layout(layout):
    """Widget arrangement from the wallpaper's edit mode: {"cols": {col: [ids]}, "hidden": [ids]}."""
    def ids(v):
        return [str(x)[:40] for x in v][:30] if isinstance(v, list) else []
    cols = layout.get("cols") if isinstance(layout.get("cols"), dict) else {}
    clean = {"cols": {str(k)[:20]: ids(v) for k, v in list(cols.items())[:8]}, "hidden": ids(layout.get("hidden"))}
    tmp = LAYOUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f)
    os.replace(tmp, LAYOUT_FILE)


def fetch_calendar():
    """Events from the iCal links in config.json, from last month through next month."""
    config, _ = REG.load_config()
    feeds = config.get("calendars", [])
    now = dt.datetime.now(calendar_ics.local_tz())
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=40)
    end = now + dt.timedelta(days=75)
    events, errors = [], []
    for i, feed in enumerate(feeds):
        link = (feed.get("url") or "").strip()
        if not link:
            continue
        if link.startswith("webcal://"):
            link = "https://" + link[len("webcal://"):]
        try:
            req = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                text = resp.read().decode("utf-8", "replace")
            for ev in calendar_ics.events_between(text, start, end):
                ev["calendar"] = feed.get("name", f"Calendar {i + 1}")
                ev["color"] = feed.get("color", "")
                events.append(ev)
        except Exception:
            logging.exception("calendar feed %s failed", feed.get("name", i))
            errors.append(feed.get("name", f"Calendar {i + 1}"))
    if gsync.connected():
        try:
            events += gsync.calendar_events(start, end, calendar_ics.local_tz())
        except Exception:
            logging.exception("google calendar failed")
            errors.append("Google Calendar")
    events.sort(key=lambda e: (e["start"], not e["allDay"]))
    configured = gsync.connected() or any((f.get("url") or "").strip() for f in feeds)
    return {"configured": configured, "events": events, "errors": errors, "google": gsync.status(),
            "weekStart": config.get("week_start", "monday")}


# ---- to-do list (stored locally; adding a task opens a small input box, see quick_input.py)
TODO_FILE = os.path.join(APP_DIR, "todos.json")
TODO_LOCK = threading.Lock()


def load_todos():
    try:
        with open(TODO_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and isinstance(data.get("items"), list) else {"items": [], "rev": 0}
    except (OSError, ValueError):
        return {"items": [], "rev": 0}


def change_todos(action, body):
    with TODO_LOCK:
        data = load_todos()
        items = data["items"]
        iid = str(body.get("id", ""))
        if action == "add":
            text = " ".join(str(body.get("text", "")).split())[:200]
            if not text:
                return data
            items.insert(0, {"id": hashlib.sha1(f"{time.time()}{text}".encode()).hexdigest()[:12],
                             "text": text, "done": False, "created": time.time()})
        elif action == "toggle":
            for it in items:
                if it["id"] == iid:
                    it["done"] = not it["done"]
                    it["doneAt"] = time.time() if it["done"] else None
        elif action == "delete":
            data["items"] = items = [it for it in items if it["id"] != iid]
        elif action == "clear":
            data["items"] = items = [it for it in items if not it["done"]]
        data["rev"] = int(data.get("rev", 0)) + 1
        tmp = TODO_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, TODO_FILE)
        return data


def tasks_list_id():
    return (REG.load_config()[0].get("google") or {}).get("tasks_list") or "@default"


GTASKS = None  # Cached Google Tasks list, created after Cached is defined


def todos_rev(items):
    return hashlib.sha1(json.dumps(items, sort_keys=True).encode()).hexdigest()[:12]


def todos_state():
    if not gsync.connected():
        return load_todos()
    items = GTASKS.get()
    state = gsync.status()
    if items is None:
        if state == "expired":
            return {"items": [], "rev": "expired", "source": "google", "google": state}
        return None  # first fetch still running
    return {"items": items, "rev": todos_rev(items) + state, "source": "google", "google": state}


def todos_change(action, body):
    if not gsync.connected():
        return change_todos(action, body)
    try:
        gsync.change_task(action, body, tasks_list_id())
        items = gsync.list_tasks(tasks_list_id())
        with GTASKS.lock:
            GTASKS.data, GTASKS.at = items, time.time()
        return {"items": items, "rev": todos_rev(items), "source": "google"}
    except Exception as exc:
        logging.exception("google tasks %s failed", action)
        items = GTASKS.data or []
        return {"items": items, "rev": todos_rev(items), "source": "google", "error": type(exc).__name__}


_last_connect = 0.0


def google_connect():
    """Opens Google sign-in in the browser (used by the wallpaper's Reconnect button)."""
    global _last_connect
    if time.time() - _last_connect < 60 or not os.path.exists(gsync.CLIENT_FILE):
        return False
    _last_connect = time.time()
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    subprocess.Popen([sys.executable, os.path.join(here, "google_sync.py"), "connect"], cwd=here,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # pick up the new sign-in quickly
    CALENDAR.at = GTASKS.at = 0
    return True


def open_quick_input(body):
    """Show the floating 'New task' box. Wallpapers can't receive keyboard input,
    so typing happens in this small separate window."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quick_input.py")
    args = [sys.executable, script, "--port", str(PORT), "--mode", "chat" if body.get("mode") == "chat" else "todo"]
    for key in ("x", "y", "w"):
        try:
            args += [f"--{key}", str(int(float(body.get(key, 0))))]
        except (TypeError, ValueError):
            pass
    import subprocess
    subprocess.Popen(args, cwd=os.path.dirname(script), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


WEATHER = Cached(fetch_weather, 10 * 60)
CALENDAR = Cached(fetch_calendar, 10 * 60)
MARKETS = Cached(fetch_markets, 60)
GTASKS = Cached(lambda: gsync.list_tasks(tasks_list_id()), 20)


# ---------------------------------------------------------------- media keys

MEDIA_KEYS = {"playpause": 0xB3, "next": 0xB0, "prev": 0xB1}
KEYEVENTF_KEYUP = 0x2


def press_media(action):
    vk = MEDIA_KEYS[action]
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


# ---------------------------------------------------------------- desktop scan

def item_id(path):
    return hashlib.sha1(path.lower().encode("utf-8")).hexdigest()[:16]


def list_dir(folder):
    out = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return out
    for e in entries:
        if e.name.lower() == "desktop.ini":
            continue
        try:
            st = e.stat()
        except OSError:
            continue
        if getattr(st, "st_file_attributes", 0) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
            continue
        out.append((e.path, e.name, e.is_dir(), st.st_ctime, st.st_mtime))
    return out


def display_name(name, is_dir):
    if is_dir:
        return name
    base, ext = os.path.splitext(name)
    return base if ext.lower() in HIDE_EXTS else name


def kind_of(name, is_dir):
    if is_dir:
        return "folder"
    return "app" if os.path.splitext(name)[1].lower() in APP_EXTS else "file"


class Registry:
    """Everything the wallpaper is allowed to launch or fetch an icon for, by id."""

    def __init__(self):
        self.paths = {}      # id -> filesystem path
        self.uris = {}       # id -> uri (dock entries without a path)
        self.signature = None
        self.payload = None

    def load_config(self):
        path = CONFIG_FILE if os.path.exists(CONFIG_FILE) else DEFAULT_CONFIG_FILE
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f), os.stat(path).st_mtime
        except (OSError, ValueError):
            logging.exception("could not read config %s", path)
            return {}, 0

    def describe(self, path, name, is_dir):
        iid = item_id(path)
        self.paths[iid] = path
        key, _ = icon_file_for(path)
        return {"id": iid, "name": display_name(name, is_dir), "kind": kind_of(name, is_dir), "iconKey": key}

    def folder_children(self, folder):
        rows = sorted(list_dir(folder), key=lambda r: (not r[2], r[1].casefold()))
        return [self.describe(p, n, d) for p, n, d, _, _ in rows]

    def scan(self):
        config, config_mtime = self.load_config()
        folders = [known_folder(CSIDL_DESKTOPDIRECTORY)]
        if config.get("include_public_desktop", True):
            folders.append(known_folder(CSIDL_COMMON_DESKTOPDIRECTORY))
        rows, seen_names = [], set()
        for folder in folders:
            for row in list_dir(folder):
                if row[1].casefold() in seen_names:
                    continue
                seen_names.add(row[1].casefold())
                rows.append(row)

        show_bin = config.get("recycle_bin", True)
        bin_count = recycle_bin_count() if show_bin else 0
        sig_src = repr(sorted((r[0], r[4]) for r in rows)) + f"|{config_mtime}|{show_bin}|{bin_count}"
        signature = hashlib.sha1(sig_src.encode("utf-8")).hexdigest()[:16]
        if signature == self.signature:
            return self.payload

        if config.get("sort", "added") == "name":
            rows.sort(key=lambda r: r[1].casefold())
        else:  # "added": like a phone, new items land at the end
            rows.sort(key=lambda r: (r[3], r[1].casefold()))

        items = []
        for path, name, is_dir, _, _ in rows:
            entry = self.describe(path, name, is_dir)
            if is_dir:
                entry["preview"] = self.folder_children(path)[:4]
            items.append(entry)

        dock = []
        for n, d in enumerate(config.get("dock", [])):
            target = os.path.expandvars(d.get("path", "")) if d.get("path") else ""
            if not (target and os.path.exists(target)) and d.get("app"):
                # Store / packaged apps: launch and take the icon via their AppID
                target = "shell:AppsFolder\\" + d["app"]
            if target and (target.startswith("shell:AppsFolder\\") or os.path.exists(target)):
                iid = f"dock{n}-" + item_id(target)
                self.paths[iid] = target
                key, _ = icon_file_for(target)
                dock.append({"id": iid, "name": d.get("name", ""), "kind": "app", "iconKey": key})
            elif d.get("uri"):
                iid = f"dock{n}-uri"
                self.uris[iid] = d["uri"]
                dock.append({"id": iid, "name": d.get("name", ""), "kind": "app", "glyph": d.get("glyph", "")})

        if show_bin:
            self.paths[RECYCLE_ID] = RECYCLE_BIN
            state = "full" if bin_count else "empty"
            key, _ = icon_file_for(RECYCLE_BIN, state)
            dock.append({"id": RECYCLE_ID, "name": "Recycle Bin", "kind": "app", "iconKey": key,
                         "badge": bin_count, "divider": True})

        self.signature = signature
        self.payload = {"version": signature, "items": items, "dock": dock}
        return self.payload

    def launch(self, iid):
        if iid in self.paths:
            os.startfile(self.paths[iid])
            return True
        if iid in self.uris:
            os.startfile(self.uris[iid])
            return True
        return False


REG = Registry()
CHAT = chat_mod.Chat(APP_DIR, lambda: REG.load_config()[0])
TOKEN = ""


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "HomeScreen/1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json", cache=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", cache or "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self, query):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        return bool(TOKEN) and hmac.compare_digest(query.get("t", [""])[0], TOKEN)

    def _route(self, method):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._authorized(query):
            return self._send(403, {"error": "forbidden"})
        try:
            if method == "GET" and url.path == "/ping":
                return self._send(200, {"ok": True})
            if method == "GET" and url.path == "/items":
                payload = REG.scan()
                if query.get("v", [""])[0] == payload["version"]:
                    return self._send(200, {"version": payload["version"], "unchanged": True})
                return self._send(200, payload)
            if method == "GET" and url.path == "/folder":
                path = REG.paths.get(query.get("id", [""])[0])
                if not path or not os.path.isdir(path):
                    return self._send(404, {"error": "unknown folder"})
                return self._send(200, {"items": REG.folder_children(path)})
            if method == "GET" and url.path == "/icon":
                iid = query.get("id", [""])[0]
                path = REG.paths.get(iid)
                # the bin's icon differs when empty vs full, so cache them separately
                salt = ("full" if recycle_bin_count() else "empty") if iid == RECYCLE_ID else ""
                png = get_icon(path, salt) if path else None
                if not png:
                    return self._send(404, {"error": "no icon"})
                return self._send(200, png, "image/png", "max-age=31536000, immutable")
            if method == "GET" and url.path == "/stats":
                return self._send(200, read_stats())
            if method == "GET" and url.path == "/weather":
                return self._send(200, WEATHER.payload())
            if method == "GET" and url.path == "/markets":
                return self._send(200, MARKETS.payload())
            if method == "GET" and url.path == "/layout":
                return self._send(200, {"layout": load_layout()})
            if method == "GET" and url.path == "/calendar":
                return self._send(200, CALENDAR.payload())
            if method == "GET" and url.path == "/todos":
                data = todos_state()
                if data is None:
                    return self._send(200, {"rev": query.get("rev", [""])[0], "unchanged": True})
                if query.get("rev", [""])[0] == str(data.get("rev", 0)):
                    return self._send(200, {"rev": data.get("rev", 0), "unchanged": True})
                return self._send(200, data)
            if method == "GET" and url.path == "/chat":
                state = CHAT.state()
                if query.get("rev", [""])[0] == str(state["rev"]):
                    return self._send(200, {"rev": state["rev"], "unchanged": True})
                return self._send(200, state)
            if method == "POST":
                length = min(int(self.headers.get("Content-Length") or 0), 16384)
                body = json.loads(self.rfile.read(length) or b"{}")
                if url.path == "/launch":
                    ok = REG.launch(str(body.get("id", "")))
                    return self._send(200 if ok else 404, {"ok": ok})
                if url.path.startswith("/todos/") and isinstance(body, dict):
                    action = url.path.split("/", 2)[2]
                    if action == "prompt":
                        open_quick_input(body)
                        return self._send(200, {"ok": True})
                    if action in ("add", "toggle", "delete", "clear"):
                        return self._send(200, todos_change(action, body))
                if url.path.startswith("/chat/") and isinstance(body, dict):
                    action = url.path.split("/", 2)[2]
                    if action == "prompt":
                        open_quick_input(dict(body, mode="chat"))
                        return self._send(200, {"ok": True})
                    if action == "send":
                        return self._send(200, CHAT.send(body.get("text", "")))
                    if action == "decide":
                        return self._send(200, CHAT.decide(body.get("id", ""), bool(body.get("approve"))))
                    if action == "stop":
                        return self._send(200, CHAT.stop())
                    if action == "clear":
                        return self._send(200, CHAT.clear())
                if url.path == "/google/connect":
                    return self._send(200, {"ok": google_connect()})
                if url.path == "/layout" and isinstance(body, dict):
                    save_layout(body)
                    return self._send(200, {"ok": True})
                if url.path == "/power" and body.get("action") in ("restart", "shutdown"):
                    import subprocess
                    flag = "/r" if body["action"] == "restart" else "/s"
                    logging.info("power: %s requested from the wallpaper", body["action"])
                    subprocess.Popen(["shutdown", flag, "/t", "0"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    return self._send(200, {"ok": True})
                if url.path == "/media" and body.get("action") in MEDIA_KEYS:
                    press_media(body["action"])
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            logging.exception("request failed: %s %s", method, url.path)
            return self._send(500, {"error": type(exc).__name__})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def main():
    global TOKEN, GPU
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            TOKEN = f.read().strip()
    except OSError:
        logging.error("no token at %s; run install.ps1", TOKEN_FILE)
        sys.exit(1)
    ole32.CoInitializeEx(None, 0x2)  # apartment-threaded, needed for shell icon APIs
    GPU = Gpu()
    try:
        server = HTTPServer((HOST, PORT), Handler)
    except OSError:
        sys.exit(0)  # already running
    server.serve_forever()


if __name__ == "__main__":
    main()
