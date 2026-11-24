"""Google Tasks (two-way) and Google Calendar (read-only) for the home screen.

One-time setup: put your OAuth "Desktop app" client file at
%LOCALAPPDATA%\\HomeScreen\\google_client.json, then run

    python google_sync.py connect

which opens Google's sign-in page in your browser. The resulting token is
encrypted with Windows DPAPI (only your Windows account can decrypt it) and
stored in %LOCALAPPDATA%\\HomeScreen\\google_token.bin. Nothing goes anywhere
except Google's own APIs.

    python google_sync.py status | disconnect
"""
import base64
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import hashlib
import http.server
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

APP_DIR = os.path.join(os.environ["LOCALAPPDATA"], "HomeScreen")
CLIENT_FILE = os.path.join(APP_DIR, "google_client.json")
TOKEN_FILE = os.path.join(APP_DIR, "google_token.bin")
CLEARED_FILE = os.path.join(APP_DIR, "google_cleared.json")
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CAL_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"  # add events (Rora's add_calendar_event)
SCOPES = ["https://www.googleapis.com/auth/tasks", "https://www.googleapis.com/auth/calendar.readonly", GMAIL_SCOPE,
          CAL_WRITE_SCOPE]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TASKS = "https://tasks.googleapis.com/tasks/v1"
CAL = "https://www.googleapis.com/calendar/v3"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
DONE_DAYS = 3  # completed tasks stay visible (crossed out) this long


# ---------------------------------------------------------------- DPAPI (encrypt token for this Windows user)
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = _Blob(len(data), buf), _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst)):  # UI_FORBIDDEN
        raise ctypes.WinError()
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel32.LocalFree(dst.pbData)


# ---------------------------------------------------------------- tokens
_lock = threading.RLock()
_token = None
_token_mtime = 0.0
_expired = False  # refresh token rejected (Testing-mode apps: every 7 days) -> wallpaper shows "Reconnect"


def say(*args):
    if sys.stdout is not None:  # pythonw has no console
        print(*args)


def status():
    if not connected():
        return "off"
    return "expired" if _expired else "ok"


def _client():
    with open(CLIENT_FILE, encoding="utf-8") as f:
        c = json.load(f)
    c = c.get("installed") or c.get("web") or c
    return c["client_id"], c.get("client_secret", "")


def _save_token(tok):
    global _token, _token_mtime
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(_dpapi(json.dumps(tok).encode(), True))
    os.replace(tmp, TOKEN_FILE)
    global _expired
    _token, _token_mtime, _expired = tok, os.stat(TOKEN_FILE).st_mtime, False


def _load_token():
    global _token, _token_mtime, _expired
    try:
        mtime = os.stat(TOKEN_FILE).st_mtime
    except OSError:
        _token = None
        return None
    if _token is None or mtime != _token_mtime:
        with open(TOKEN_FILE, "rb") as f:
            _token = json.loads(_dpapi(f.read(), False))
        _token_mtime, _expired = mtime, False  # a new sign-in (e.g. Reconnect) replaced the token
    return _token


def connected():
    return os.path.exists(TOKEN_FILE) and os.path.exists(CLIENT_FILE)


def has_scope(scope):
    """False until the user re-signs in with a scope added later (e.g. Gmail)."""
    if not connected():
        return False
    try:
        with _lock:
            return scope in (_load_token() or {}).get("scope", "")
    except (OSError, ValueError):
        return False


def _post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def access_token():
    with _lock:
        tok = _load_token()
        if not tok:
            raise RuntimeError("Google isn't connected")
        if tok.get("expires_at", 0) - 60 > time.time():
            return tok["access_token"]
        global _expired
        if _expired:
            raise RuntimeError("Google sign-in expired")
        cid, secret = _client()
        try:
            new = _post_form(TOKEN_URL, {"client_id": cid, "client_secret": secret,
                                         "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):  # invalid_grant: expired or revoked
                _expired = True
                raise RuntimeError("Google sign-in expired") from e
            raise
        tok.update(access_token=new["access_token"], expires_at=time.time() + int(new.get("expires_in", 3600)))
        _save_token(tok)
        return tok["access_token"]


def api(method, url, body=None, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {access_token()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------- sign-in (loopback + PKCE, as Google recommends for desktop apps)
def connect():
    cid, secret = _client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                result.update({k: v[0] for k, v in q.items()})
            ok = "code" in result
            page = ("<h2>Connected ✓</h2><p>You can close this tab. Your tasks and calendar will show on the home screen.</p>"
                    if ok else "<h2>Not connected</h2><p>Sign-in was cancelled or failed. Run connect again.</p>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<body style='font-family:Segoe UI;background:#11131c;color:#eee;padding:40px'>{page}</body>".encode())

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "access_type": "offline", "prompt": "consent"})
    say("Opening Google sign-in in your browser…")
    webbrowser.open(url)
    server.timeout = 300
    deadline = time.time() + 300
    while "code" not in result and "error" not in result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        raise SystemExit(f"Sign-in didn't complete: {result.get('error', 'timed out')}")
    tok = _post_form(TOKEN_URL, {"client_id": cid, "client_secret": secret, "code": result["code"],
                                 "code_verifier": verifier, "redirect_uri": redirect,
                                 "grant_type": "authorization_code"})
    if "refresh_token" not in tok:
        raise SystemExit("Google didn't return a refresh token; remove the app's access in your Google account and retry.")
    _save_token({"access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
                 "expires_at": time.time() + int(tok.get("expires_in", 3600)), "scope": tok.get("scope", "")})
    say("Connected.")


# ---------------------------------------------------------------- Google Tasks
def _tasks_url(list_id, task_id=""):
    return f"{TASKS}/lists/{urllib.parse.quote(list_id, safe='@')}/tasks" + (f"/{urllib.parse.quote(task_id)}" if task_id else "")


def _cleared():
    try:
        with open(CLEARED_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _ts(rfc3339):
    try:
        return dt.datetime.fromisoformat(rfc3339.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def list_tasks(list_id="@default"):
    items, page = [], None
    while True:
        params = {"showCompleted": "true", "showHidden": "true", "maxResults": 100}
        if page:
            params["pageToken"] = page
        res = api("GET", _tasks_url(list_id), params=params)
        items += res.get("items", [])
        page = res.get("nextPageToken")
        if not page:
            break
    cleared, cutoff = _cleared(), time.time() - DONE_DAYS * 86400
    out = []
    for t in sorted(items, key=lambda t: (t.get("parent") is not None, t.get("position", ""))):
        if t.get("deleted") or not (t.get("title") or "").strip() or t["id"] in cleared:
            continue
        done = t.get("status") == "completed"
        done_at = _ts(t.get("completed"))
        if done and (done_at or 0) < cutoff:
            continue  # completed a while ago: Google keeps it, the home screen doesn't need to
        out.append({"id": t["id"], "text": t["title"].strip(), "done": done, "doneAt": done_at,
                    "created": _ts(t.get("updated"))})
    return out


def change_task(action, body, list_id="@default"):
    iid = str(body.get("id", ""))
    if action == "add":
        text = " ".join(str(body.get("text", "")).split())[:200]
        if text:
            api("POST", _tasks_url(list_id), {"title": text})
    elif action == "toggle" and iid:
        cur = api("GET", _tasks_url(list_id, iid))
        if cur.get("status") == "completed":
            api("PATCH", _tasks_url(list_id, iid), {"status": "needsAction", "completed": None})
        else:
            api("PATCH", _tasks_url(list_id, iid), {"status": "completed"})
    elif action == "delete" and iid:
        api("DELETE", _tasks_url(list_id, iid))
    elif action == "clear":
        done = [t["id"] for t in list_tasks(list_id) if t["done"]]
        api("POST", f"{TASKS}/lists/{urllib.parse.quote(list_id, safe='@')}/clear")
        ids = sorted(_cleared() | set(done))[-500:]
        with open(CLEARED_FILE, "w", encoding="utf-8") as f:
            json.dump(ids, f)


# ---------------------------------------------------------------- Google Calendar (read-only)
def calendar_events(start, end, local_tz):
    cals = api("GET", f"{CAL}/users/me/calendarList", params={"minAccessRole": "reader"}).get("items", [])
    events = []
    for c in cals:
        if not c.get("selected") and not c.get("primary"):
            continue  # only calendars ticked in Google Calendar
        page = None
        while True:
            params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                      "orderBy": "startTime", "maxResults": 250}
            if page:
                params["pageToken"] = page
            res = api("GET", f"{CAL}/calendars/{urllib.parse.quote(c['id'])}/events", params=params)
            for e in res.get("items", []):
                if e.get("status") == "cancelled":
                    continue
                s, en = e.get("start", {}), e.get("end", {})
                all_day = "date" in s
                if all_day:
                    sd = dt.datetime.fromisoformat(s["date"]).replace(tzinfo=local_tz)
                    ed = dt.datetime.fromisoformat(en.get("date", s["date"])).replace(tzinfo=local_tz)
                else:
                    sd = dt.datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00")).astimezone(local_tz)
                    ed = dt.datetime.fromisoformat(en.get("dateTime", s["dateTime"]).replace("Z", "+00:00")).astimezone(local_tz)
                events.append({"title": e.get("summary") or "(No title)", "location": e.get("location", ""),
                               "allDay": all_day, "start": sd.isoformat(), "end": ed.isoformat(),
                               "calendar": c.get("summaryOverride") or c.get("summary", ""),
                               "color": c.get("backgroundColor", "")})
            page = res.get("nextPageToken")
            if not page:
                break
    return events


_TZ = {}


def user_timezone():
    """The IANA time zone set in the user's Google Calendar (e.g. needed for repeating events)."""
    if "v" not in _TZ:
        _TZ["v"] = api("GET", f"{CAL}/users/me/settings/timezone").get("value") or ""
    return _TZ["v"]


def public_holidays(first, last):
    """{date: name} of public holidays between two dates (inclusive), from the holiday calendars the
    user subscribes to in Google Calendar (e.g. 'Holidays in <country>'). Observances are left out."""
    out = {}
    cals = api("GET", f"{CAL}/users/me/calendarList").get("items", [])
    for c in cals:
        if "#holiday@group.v.calendar.google.com" not in c.get("id", ""):
            continue
        res = api("GET", f"{CAL}/calendars/{urllib.parse.quote(c['id'])}/events", params={
            "timeMin": f"{first.isoformat()}T00:00:00Z", "timeMax": f"{(last + dt.timedelta(days=1)).isoformat()}T23:59:59Z",
            "singleEvents": "true", "maxResults": 250})
        for e in res.get("items", []):
            d = (e.get("start") or {}).get("date")
            if d and str(e.get("description") or "").lower().startswith("public holiday"):
                day = dt.date.fromisoformat(d)
                if first <= day <= last:
                    out[day] = e.get("summary") or "Public holiday"
    return out


def create_event(title, start, end, all_day, location="", notes="", tz_name="", recurrence=None, tentative=False):
    """Adds an event to the user's primary Google Calendar; returns Google's event (id, htmlLink...)."""
    body = {"summary": title}
    if recurrence:
        body["recurrence"] = recurrence
    if tentative:
        body["status"] = "tentative"
    if all_day:
        body["start"], body["end"] = {"date": start.isoformat()}, {"date": end.isoformat()}
    else:
        body["start"], body["end"] = {"dateTime": start.isoformat()}, {"dateTime": end.isoformat()}
        if tz_name:
            body["start"]["timeZone"] = body["end"]["timeZone"] = tz_name
    if location:
        body["location"] = location
    if notes:
        body["description"] = notes
    return api("POST", f"{CAL}/calendars/primary/events", body=body)


# ---------------------------------------------------------------- Gmail (read-only)
def _header(msg, name):
    for h in (msg.get("payload") or {}).get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _sender_name(value):
    value = value.strip()
    if "<" in value:
        name = value.split("<", 1)[0].strip().strip('"')
        return name or value.split("<", 1)[1].rstrip(">")
    return value


def inbox(limit=8):
    """Unread count + the newest inbox messages (sender, subject, snippet, time). Bodies aren't fetched."""
    label = api("GET", f"{GMAIL}/labels/INBOX")
    listed = api("GET", f"{GMAIL}/messages", params={"labelIds": "INBOX", "maxResults": limit}).get("messages") or []
    out = []
    for m in listed:
        msg = api("GET", f"{GMAIL}/messages/{m['id']}",
                  params=[("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject")])
        out.append({"id": msg["id"], "thread": msg.get("threadId", msg["id"]),
                    "from": _sender_name(_header(msg, "From"))[:80], "subject": _header(msg, "Subject")[:200] or "(no subject)",
                    "snippet": html_unescape(msg.get("snippet", ""))[:240],
                    "unread": "UNREAD" in (msg.get("labelIds") or []),
                    "time": int(msg.get("internalDate", "0")) / 1000})
    return {"unread": label.get("messagesUnread", 0), "messages": out}


def html_unescape(s):
    import html
    return html.unescape(s)


def search_messages(query, limit=100):
    ids, page = [], None
    while len(ids) < limit:
        params = {"q": query, "maxResults": min(100, limit - len(ids))}
        if page:
            params["pageToken"] = page
        res = api("GET", f"{GMAIL}/messages", params=params)
        ids += [m["id"] for m in res.get("messages") or []]
        page = res.get("nextPageToken")
        if not page:
            break
    return ids


def message_text(msg_id):
    """(internal timestamp, subject, plain text body) of one message."""
    import html
    import re
    msg = api("GET", f"{GMAIL}/messages/{msg_id}", params={"format": "full"})
    plain, rich = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data:
            text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            (plain if mime == "text/plain" else rich if mime == "text/html" else []).append(text)
        for p in part.get("parts") or []:
            walk(p)

    walk(msg.get("payload") or {})
    if plain:
        body = "\n".join(plain)
    else:
        body = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(rich))
        body = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", body)
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    body = "\n".join(" ".join(line.split()) for line in body.splitlines() if line.strip())
    return int(msg.get("internalDate", "0")) / 1000, _header(msg, "Subject"), body


# ---------------------------------------------------------------- CLI
def _migrate_local_todos():
    """Upload tasks that were on the home screen before connecting, so nothing is lost."""
    path = os.path.join(APP_DIR, "todos.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    open_items = [t for t in data.get("items", []) if not t.get("done")]
    for t in reversed(open_items):  # reversed: each insert goes to the top
        api("POST", _tasks_url("@default"), {"title": t["text"]})
    if open_items:
        say(f"Copied {len(open_items)} existing task(s) to Google Tasks.")
    os.replace(path, path + ".before-google")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "connect":
        if not os.path.exists(CLIENT_FILE):
            raise SystemExit(f"Put your OAuth client file at {CLIENT_FILE} first.")
        connect()
        _migrate_local_todos()
        say(f"{len(list_tasks())} task(s) visible from Google Tasks.")
    elif cmd == "disconnect":
        for p in (TOKEN_FILE, CLEARED_FILE):
            if os.path.exists(p):
                os.remove(p)
        print("Disconnected (token deleted). Also remove access at https://myaccount.google.com/permissions")
    else:
        print("connected" if connected() else "not connected")
        if connected():
            print(f"{len(list_tasks())} task(s) in Google Tasks")
