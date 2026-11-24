"""Listening history: which songs you actually listen to in Spotify, kept only on this PC.

A small PowerShell reader (nowplaying.ps1) reports what Windows' media controls show
every few seconds. A song counts as a play once you've listened to at least 30 s of it
(or half of a very short track); shorter listens count as skips. From that we work out
top songs / artists and what's "on repeat". Nothing is sent anywhere.

config.json:  "music": {"enabled": true, "apps": ["spotify"]}
"""
import collections
import json
import logging
import os
import subprocess
import threading
import time

KEEP_DAYS = 3 * 365
MIN_LISTEN = 30          # seconds before a song counts as played
MAX_GAP = 12             # longest gap between samples we still count as listening
NO_WINDOW = 0x08000000


class Music:
    def __init__(self, app_dir, get_config, script):
        self.file = os.path.join(app_dir, "music.json")
        self.get_config = get_config
        self.script = script
        self.lock = threading.Lock()
        self.cur = None          # {key, title, artist, album, dur, secs, start, last, playing}
        self.now = None          # latest sample from the chosen app (for "what's playing")
        self.rev = 0
        self.dirty = False
        self.data = self._load()

    # ---- storage
    def _load(self):
        try:
            with open(self.file, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d.get("plays"), list):
                d.setdefault("skips", [])
                return d
        except (OSError, ValueError):
            pass
        return {"plays": [], "skips": []}

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            cut = time.time() - KEEP_DAYS * 86400
            self.data["plays"] = [p for p in self.data["plays"] if p["t"] > cut]
            self.data["skips"] = [p for p in self.data["skips"] if p["t"] > time.time() - 90 * 86400]
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, separators=(",", ":"))
            os.replace(tmp, self.file)
            self.dirty = False

    def settings(self):
        c = self.get_config().get("music") or {}
        return {"enabled": c.get("enabled", True) is not False,
                "apps": [str(a).lower() for a in (c.get("apps") or ["spotify"])]}

    # ---- reading Windows' media controls
    def start(self):
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._saver, daemon=True).start()

    def _saver(self):
        while True:
            time.sleep(60)
            try:
                self.save()
            except Exception:
                logging.exception("music save failed")

    def app_open(self):
        """Is one of the watched music apps running? (Spotify closed = nothing to monitor.)"""
        try:
            from activity import running_exes
        except Exception:
            return True
        names = running_exes()
        return any(any(a in n for n in names) for a in self.settings()["apps"])

    def _reader(self):
        delay = 5
        while True:
            if not self.settings()["enabled"] or not self.app_open():
                if self.now:
                    with self.lock:
                        if self.cur:
                            self._finish(self.cur)
                            self.cur = None
                        self.now = None
                    self.rev += 1
                time.sleep(10)  # one cheap process check every 10 s while Spotify is closed
                continue
            try:
                # ease in: first samples 15 s apart, stepping down to every 5 s
                proc = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                     "-File", self.script, "-Every", "5", "-Start", "15"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                    creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
                started = time.time()
                watch = threading.Thread(target=self._stop_when_closed, args=(proc,), daemon=True)
                watch.start()
                for line in proc.stdout:
                    try:
                        self._sample(json.loads(line))
                    except ValueError:
                        continue
                proc.wait()
                if not self.app_open():
                    delay = 5
                    continue
                if time.time() - started > 300:
                    delay = 5
            except Exception:
                logging.exception("now-playing reader failed")
            time.sleep(delay)
            delay = min(300, delay * 2)

    def _stop_when_closed(self, proc):
        while proc.poll() is None:
            time.sleep(10)
            if not self.app_open() or not self.settings()["enabled"]:
                try:
                    proc.kill()
                except OSError:
                    pass
                return

    def _sample(self, msg):
        apps = self.settings()["apps"]
        now = time.time()
        pick = None
        for s in msg.get("sessions") or []:
            if any(a in str(s.get("app", "")).lower() for a in apps) and s.get("title"):
                if pick is None or (s.get("playing") and not pick.get("playing")):
                    pick = s
        with self.lock:
            self.now = dict(pick, at=now) if pick else None
            cur = self.cur
            key = f"{pick['title']}␟{pick.get('artist') or ''}".lower() if pick else None
            if cur and (key != cur["key"] or now - cur["last"] > 30 * 60):
                self._finish(cur)
                cur = self.cur = None
            if pick and cur is None:
                cur = self.cur = {"key": key, "title": pick["title"], "artist": pick.get("artist") or "",
                                  "album": pick.get("album") or "", "dur": int(pick.get("duration") or 0),
                                  "secs": 0.0, "start": now, "last": now, "playing": False, "pos": 0}
            if cur:
                if cur["playing"] and pick.get("playing"):
                    cur["secs"] += min(now - cur["last"], MAX_GAP)
                # the same song again from the top = a repeat: count the first listen as its own play
                pos = int(pick.get("position") or 0)
                if pick.get("playing") and cur["secs"] >= MIN_LISTEN and pos + 20 < cur["pos"] and pos < 15:
                    self._finish(cur)
                    cur = self.cur = dict(cur, secs=0.0, start=now)
                cur.update(last=now, playing=bool(pick.get("playing")), pos=pos,
                           dur=int(pick.get("duration") or cur["dur"]))

    def _finish(self, cur):
        """Call with the lock held."""
        need = MIN_LISTEN if cur["dur"] <= 0 else min(MIN_LISTEN, cur["dur"] / 2)
        if cur["secs"] >= need:
            self.data["plays"].append({"t": round(cur["start"]), "title": cur["title"], "artist": cur["artist"],
                                       "album": cur["album"], "secs": round(cur["secs"]), "dur": cur["dur"]})
        elif cur["secs"] >= 3:
            self.data["skips"].append({"t": round(cur["start"]), "title": cur["title"], "artist": cur["artist"]})
        else:
            return
        self.dirty = True
        self.rev += 1

    # ---- stats
    def _plays(self, days=None):
        with self.lock:
            plays = list(self.data["plays"])
            cur = self.cur
            if cur and cur["secs"] >= MIN_LISTEN:  # the song playing right now already counts
                plays.append({"t": cur["start"], "title": cur["title"], "artist": cur["artist"],
                              "album": cur["album"], "secs": cur["secs"], "dur": cur["dur"]})
        if days:
            cut = time.time() - days * 86400
            plays = [p for p in plays if p["t"] >= cut]
        return plays

    @staticmethod
    def _main_artist(a):
        return (a or "Unknown artist").split(",")[0].split(" feat")[0].strip()

    def top(self, days=7, limit=10):
        plays = self._plays(days)
        songs, artists = collections.OrderedDict(), collections.Counter()
        mins = collections.Counter()
        for p in plays:
            k = (p["title"], p["artist"])
            s = songs.setdefault(k, {"title": p["title"], "artist": p["artist"], "album": p.get("album", ""),
                                     "plays": 0, "mins": 0.0, "last": 0})
            s["plays"] += 1
            s["mins"] += p["secs"] / 60
            s["last"] = max(s["last"], p["t"])
            a = self._main_artist(p["artist"])
            artists[a] += 1
            mins[a] += p["secs"] / 60
        ranked = sorted(songs.values(), key=lambda s: (-s["plays"], -s["mins"], -s["last"]))
        for s in ranked:
            s["mins"] = round(s["mins"], 1)
        return {"days": days, "plays": len(plays), "minutes": round(sum(p["secs"] for p in plays) / 60),
                "songs": ranked[:limit],
                "artists": [{"artist": a, "plays": n, "mins": round(mins[a])} for a, n in artists.most_common(limit)]}

    def on_repeat(self, limit=5):
        """Songs played a lot in the last 3 days compared with before."""
        recent = collections.Counter((p["title"], p["artist"]) for p in self._plays(3))
        return [{"title": t, "artist": a, "plays": n} for (t, a), n in recent.most_common(limit) if n >= 3]

    def since(self):
        p = self.data["plays"]
        return p[0]["t"] if p else None

    def status(self, days=7):
        n = self.now
        out = self.top(days, 8)
        out.update(rev=self.rev, since=self.since(), onRepeat=self.on_repeat(),
                   now={k: n.get(k) for k in ("title", "artist", "album", "playing")} if n else None)
        return out

    def describe(self):
        lines = []
        n = self.now
        if n:
            lines.append(f"Spotify now: {'playing' if n.get('playing') else 'paused'} \"{n['title']}\" by {n.get('artist') or '?'}"
                         + (f" (album {n['album']})" if n.get("album") else ""))
        since = self.since()
        if not since:
            lines.append("Listening history: just started recording, no completed plays yet.")
            return "\n".join(lines)
        lines.append(f"Listening history recorded since {time.strftime('%d %b %Y', time.localtime(since))} "
                     "(a play = at least 30 s listened in Spotify).")
        for days, label in ((7, "last 7 days"), (30, "last 30 days"), (None, "all time")):
            t = self.top(days, 10)
            if not t["plays"]:
                lines.append(f"{label}: nothing played.")
                continue
            lines.append(f"{label}: {t['plays']} plays, {t['minutes']} min. Top songs: "
                         + "; ".join(f"{s['title']} – {s['artist']} ({s['plays']}x)" for s in t["songs"]))
            lines.append(f"  Top artists ({label}): " + ", ".join(f"{a['artist']} ({a['plays']})" for a in t["artists"][:8]))
        rep = self.on_repeat()
        if rep:
            lines.append("On repeat (last 3 days): " + "; ".join(f"{s['title']} – {s['artist']} ({s['plays']}x)" for s in rep))
        recent = self._plays(2)[-12:]
        if recent:
            lines.append("Most recent plays: " + "; ".join(
                f"{time.strftime('%a %H:%M', time.localtime(p['t']))} {p['title']} – {p['artist']}" for p in reversed(recent)))
        skips = collections.Counter((s["title"], s["artist"]) for s in self.data["skips"] if s["t"] > time.time() - 30 * 86400)
        often = [f"{t} – {a} ({n}x)" for (t, a), n in skips.most_common(5) if n >= 2]
        if often:
            lines.append("Often skipped (last 30 days): " + "; ".join(often))
        return "\n".join(lines)
