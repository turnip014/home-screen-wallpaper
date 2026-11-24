"""Spotify connection: your top tracks and playlists (Spotify Web API, February 2026 endpoints).

Setup (once): create an app at https://developer.spotify.com/dashboard (needs Spotify Premium),
add the redirect URI  http://127.0.0.1:48652/callback , tick "Web API", then put its Client ID in
%LOCALAPPDATA%\\HomeScreen\\config.json as  "spotify": {"client_id": "..."}  and click
"Connect Spotify" on the Top songs widget (or run:  python spotify_sync.py connect).

Sign-in uses PKCE (no client secret). The token is encrypted for this Windows user (DPAPI) and
only ever sent to Spotify. Reading: top tracks + recently played. Writing (creating playlists)
only happens when you click a button or allow Rora's request.
"""
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import google_sync as gsync  # for the DPAPI helper

APP_DIR = os.path.join(os.environ["LOCALAPPDATA"], "HomeScreen")
TOKEN_FILE = os.path.join(APP_DIR, "spotify_token.bin")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
PORT = 48652
REDIRECT = f"http://127.0.0.1:{PORT}/callback"
SCOPES = ["playlist-modify-private", "playlist-modify-public", "playlist-read-private",
          "user-top-read", "user-read-recently-played", "user-library-read",
          # Rora DJ: queue / play / skip, and "save" = add to Liked Songs
          "user-modify-playback-state", "user-read-playback-state", "user-read-currently-playing",
          "user-library-modify"]
DJ_SCOPES = {"user-modify-playback-state", "user-read-playback-state", "user-library-modify"}

_lock = threading.RLock()
_token = None
_expired = False


def client_id():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return str((json.load(f).get("spotify") or {}).get("client_id") or "").strip()
    except (OSError, ValueError):
        return ""


def connected():
    return os.path.exists(TOKEN_FILE) and bool(client_id())


def status():
    if not client_id():
        return "setup"       # no app yet
    if not os.path.exists(TOKEN_FILE):
        return "off"         # app set up, not signed in
    return "expired" if _expired else "ok"


def dj_ready():
    """Signed in with the playback + library permissions (added later: needs one reconnect)."""
    if status() != "ok":
        return False
    try:
        with _lock:
            return DJ_SCOPES <= set((_load().get("scope") or "").split())
    except (OSError, ValueError):
        return False


def _save(tok):
    global _token, _expired
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(gsync._dpapi(json.dumps(tok).encode(), True))
    os.replace(tmp, TOKEN_FILE)
    _token, _expired = tok, False


def _load():
    global _token
    if _token is None:
        with open(TOKEN_FILE, "rb") as f:
            _token = json.loads(gsync._dpapi(f.read(), False))
    return _token


def _post_form(fields):
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def access_token():
    global _expired
    with _lock:
        tok = _load()
        if tok.get("expires_at", 0) - 60 > time.time():
            return tok["access_token"]
        if _expired:
            raise RuntimeError("Spotify sign-in expired: click Connect Spotify again")
        try:
            new = _post_form({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                              "client_id": client_id()})
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):
                _expired = True
                raise RuntimeError("Spotify sign-in expired: click Connect Spotify again") from e
            raise
        tok.update(access_token=new["access_token"], expires_at=time.time() + int(new.get("expires_in", 3600)))
        if new.get("refresh_token"):  # Spotify may rotate it
            tok["refresh_token"] = new["refresh_token"]
        _save(tok)
        return tok["access_token"]


def api(method, path, body=None, params=None, _retry=True):
    url = (path if path.startswith("http") else API + path) + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {access_token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 429 and _retry:  # rate limited
            time.sleep(min(10, int(e.headers.get("Retry-After") or 2)))
            return api(method, path, body, params, False)
        if e.code in (500, 502, 503, 504) and _retry:  # Spotify hiccup: try once more
            time.sleep(1.5)
            return api(method, path, body, params, False)
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 403:
            raise RuntimeError("Spotify refused (403). In the Spotify developer dashboard, add your Spotify "
                               "account's email under User Management, and make sure your account has Premium. "
                               f"Details: {detail}") from e
        raise RuntimeError(f"Spotify error {e.code}: {detail}") from e
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}  # some player endpoints answer with plain text (e.g. the queue's snapshot id)


# ---------------------------------------------------------------- reading
def _track(t):
    return {"title": t.get("name", ""), "artist": ", ".join(a.get("name", "") for a in t.get("artists") or []),
            "album": (t.get("album") or {}).get("name", ""), "uri": t.get("uri", "")}


def top_tracks(time_range="short_term", limit=30):
    """time_range: short_term (~4 weeks), medium_term (~6 months), long_term (~1 year+)."""
    res = api("GET", "/me/top/tracks", params={"time_range": time_range, "limit": min(50, limit)})
    return [_track(t) for t in res.get("items") or []]


def top_artists(time_range="short_term", limit=20):
    res = api("GET", "/me/top/artists", params={"time_range": time_range, "limit": min(50, limit)})
    return [{"artist": a.get("name", ""), "genres": a.get("genres") or []} for a in res.get("items") or []]


def recently_played(limit=50):
    res = api("GET", "/me/player/recently-played", params={"limit": min(50, limit)})
    return [dict(_track(i.get("track") or {}), played_at=i.get("played_at")) for i in res.get("items") or []]


def my_playlists(limit=50):
    res = api("GET", "/me/playlists", params={"limit": min(50, limit)})
    return [{"id": p["id"], "name": p.get("name", ""), "url": (p.get("external_urls") or {}).get("spotify", "")}
            for p in res.get("items") or [] if p]


def snapshot():
    """What the helper keeps (refreshed every few hours) so the widget and Rora can use it offline."""
    out = {"at": time.time()}
    for rng in ("short_term", "medium_term", "long_term"):
        out[rng] = top_tracks(rng, 30)
    out["artists"] = top_artists("short_term", 15)
    try:
        out["recent"] = recently_played(30)
    except RuntimeError:
        out["recent"] = []
    return out


# ---------------------------------------------------------------- playlists
def _norm(s):
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").split()


def _latin(s):
    return any("a" <= ch.lower() <= "z" for ch in s)


def _search(q):
    return (api("GET", "/search", params={"q": q, "type": "track", "limit": 10}).get("tracks") or {}).get("items") or []


def _core_title(t):
    """'Plastic Love - 2021 Remaster' / 'Sparkle (Live)' -> the song name itself."""
    return re.sub(r"\s*(?:[-–—]\s.*|\(.*?\)|\[.*?\])\s*", " ", t or "").strip() or t


def artist_track(artist, exclude=()):
    """A well-known track by this artist on Spotify (search results come roughly by popularity)."""
    main = artist.split(",")[0].split(" feat")[0].strip()
    want = set(_norm(main))
    if not want:
        return None
    for t in _search(f'artist:"{main}"'):
        names = [a.get("name", "") for a in t.get("artists") or []]
        first = names[0] if names else ""
        if not (want <= set(_norm(first)) or not _latin(first)):
            continue
        tr = _track(t)
        if tr["uri"] not in exclude and f"{_core_title(tr['title'])}|{first}".lower() not in exclude:
            return tr
    return None


def find_track(title, artist=""):
    """Best match on Spotify for a title (+ artist); returns a track dict or None.
    Prefers the version where the wanted artist is the main artist (not a remix / cover / feature).
    Spotify lists many artists in their own script (山下達郎 = Tatsuro Yamashita): when Spotify's
    artist-filtered search returns one of those, it's trusted."""
    main = artist.split(",")[0].split(" feat")[0].strip()
    batches = []
    if main:
        batches.append((True, _search(f'track:"{title}" artist:"{main}"')))
        batches.append((True, _search(f'artist:"{main}" {title}')))
    batches.append((False, _search(f"{title} {main}".strip())))
    want_t, want_a = set(_norm(title)), set(_norm(main))
    best, score = None, 0.0
    for filtered, items in batches:
        for t in items:
            tr = _track(t)
            names = [a.get("name", "") for a in t.get("artists") or []]
            core = set(_norm(_core_title(tr["title"])))
            # similar both ways: "Moonlight" is not "Moonlight Feels Right"
            ts = len(want_t & core) / max(1, len(want_t), len(core))
            if ts < (0.75 if want_a else 0.6):
                continue
            if want_a:
                first = names[0] if names else ""
                if want_a <= set(_norm(first)):
                    a_s = 1.0                      # wanted artist is the main artist
                elif filtered and not _latin(first):
                    a_s = 0.9                      # same artist under their native-script name
                elif any(want_a <= set(_norm(n)) for n in names[1:]) and ts >= 1:
                    a_s = 0.5                      # only featured / remixed (same exact title)
                else:
                    continue                       # someone else's version
                ts += a_s
            if ts > score:
                best, score = tr, ts
        if score >= 1.9:
            break  # exact title + main artist: no need for more searches
    return best


def create_playlist(name, songs, description="", public=False):
    """songs: [{title, artist, uri?}]. Returns {url, added, missing}."""
    uris, added, missing = [], [], []
    for s in songs[:100]:
        tr = s if str(s.get("uri", "")).startswith("spotify:track:") else find_track(s.get("title", ""), s.get("artist", ""))
        if tr and tr["uri"] not in uris:
            uris.append(tr["uri"])
            added.append(f"{tr['title']} – {tr['artist']}")
        elif not tr:
            missing.append(f"{s.get('title', '')} – {s.get('artist', '')}")
    if not uris:
        raise RuntimeError("None of the songs could be found on Spotify, so no playlist was made.")
    pl = api("POST", "/me/playlists", {"name": name[:100], "description": description[:300], "public": bool(public)})
    for i in range(0, len(uris), 100):
        api("POST", f"/playlists/{pl['id']}/items", {"uris": uris[i:i + 100]})
    return {"id": pl["id"], "name": pl.get("name", name), "url": (pl.get("external_urls") or {}).get("spotify", ""),
            "added": added, "missing": missing}


# ---------------------------------------------------------------- playback (Rora DJ)
def _computer_device():
    devs = api("GET", "/me/player/devices").get("devices") or []
    pick = next((d for d in devs if d.get("is_active")), None) or         next((d for d in devs if (d.get("type") or "").lower() == "computer"), None) or (devs[0] if devs else None)
    return pick.get("id") if pick else None


def _player(method, path, body=None, params=None):
    """Player call; if Spotify has no active device, aim it at this PC's Spotify app."""
    try:
        return api(method, path, body, params)
    except RuntimeError as exc:
        if "404" not in str(exc) and "NO_ACTIVE_DEVICE" not in str(exc):
            raise
    dev = _computer_device()
    if not dev:
        raise RuntimeError("Spotify isn't open on any device. Open the Spotify app and try again.")
    return api(method, path, body, dict(params or {}, device_id=dev))


def queue(uri):
    return _player("POST", "/me/player/queue", params={"uri": uri})


def play_now(uri):
    return _player("PUT", "/me/player/play", {"uris": [uri]})


def skip():
    return _player("POST", "/me/player/next")


def save_to_library(uri):
    """Add to Liked Songs (Feb 2026 endpoint: PUT /me/library with Spotify URIs)."""
    try:
        return api("PUT", "/me/library", params={"uris": uri})
    except RuntimeError as exc:
        if "400" not in str(exc):
            raise
        return api("PUT", "/me/library", {"uris": [uri]})


def playing():
    """What Spotify is playing right now (title, artist, uri, progress) or None."""
    res = api("GET", "/me/player/currently-playing")
    t = (res or {}).get("item")
    if not t:
        return None
    return dict(_track(t), playing=bool(res.get("is_playing")), progress=(res.get("progress_ms") or 0) / 1000,
                duration=(t.get("duration_ms") or 0) / 1000)


# ---------------------------------------------------------------- sign-in
def connect():
    cid = client_id()
    if not cid:
        raise SystemExit('Add "spotify": {"client_id": "..."} to config.json first.')
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/callback" and q.get("state", [""])[0] == state:
                result.update({k: v[0] for k, v in q.items()})
            ok = "code" in result
            page = ("<h2>Spotify connected ✓</h2><p>You can close this tab. Rora can now make playlists for you "
                    "(she'll always ask first).</p>" if ok else
                    "<h2>Not connected</h2><p>Sign-in was cancelled or failed. Try Connect Spotify again.</p>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<body style='font-family:Segoe UI;background:#11131c;color:#eee;padding:40px'>{page}</body>".encode())

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    webbrowser.open(AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": REDIRECT, "scope": " ".join(SCOPES),
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state, "show_dialog": "true"}))
    server.timeout = 300
    deadline = time.time() + 300
    while "code" not in result and "error" not in result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        raise SystemExit(f"Sign-in didn't complete: {result.get('error', 'timed out')}")
    tok = _post_form({"grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT,
                      "client_id": cid, "code_verifier": verifier})
    _save({"access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
           "expires_at": time.time() + int(tok.get("expires_in", 3600)), "scope": tok.get("scope", "")})
    gsync.say("Spotify connected.")


if __name__ == "__main__":
    if sys.argv[1:] == ["connect"]:
        connect()
    elif sys.argv[1:] == ["test"]:
        me = api("GET", "/me")
        print("signed in as", me.get("display_name") or me.get("id"))
        print([f"{t['title']} – {t['artist']}" for t in top_tracks("short_term", 5)])
    else:
        print(__doc__)
