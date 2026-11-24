"""Rora DJ: song picks from your taste (and the game you're playing), queued straight into Spotify.

- Picks come from the local model (Rora), given your listening history, Spotify's top tracks /
  artists, songs you liked or disliked before, the time of day and the game in front.
- Every pick is looked up on Spotify; songs that can't be found exactly are dropped.
- Auto mode keeps one DJ song queued after whatever is playing, so music never stops.
- Feedback: Save (Liked Songs + counts as a like), Like, Dislike (skips it if it's playing).
  Rora sees all of it next time she picks.

Only Spotify is contacted (search, queue, play, save). Feedback stays in dj.json on this PC.
"""
import datetime as dt
import hashlib
import json
import logging
import os
import re
import threading
import time

import spotify_sync as spot

SCHEMA = {"type": "object", "properties": {
    "mood": {"type": "string"},
    "songs": {"type": "array", "minItems": 6, "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "artist": {"type": "string"}, "why": {"type": "string"}},
        "required": ["title", "artist", "why"]}}},
    "required": ["mood", "songs"]}


def _key(title, artist):
    t = re.sub(r"\s*[\(\[\-–].*$", "", title or "").lower()  # drop "(Remastered)", "- Live" …
    a = (artist or "").split(",")[0].lower()
    return re.sub(r"[^a-z0-9]+", "", t) + "|" + re.sub(r"[^a-z0-9]+", "", a)


class DJ:
    def __init__(self, app_dir, music, activity, llm):
        self.file = os.path.join(app_dir, "dj.json")
        self.music, self.act, self.llm = music, activity, llm
        self.lock = threading.RLock()
        self.data = self._load()
        self.auto = bool(self.data.get("auto"))
        self.request = str(self.data.get("request") or "")  # "more chill", "only Japanese songs"… (from chat)
        self.gen = 0            # bumped when picks in flight become outdated (steer / new game)
        self.buffer = []        # resolved picks waiting to be queued
        self.upcoming = []      # picks already in Spotify's queue, not yet playing
        self.current = None     # the DJ pick playing now
        self.game = None
        self.mood = ""
        self.busy = False
        self.error = None
        self.rev = 0
        self.last_seen = None   # key of the song seen playing last tick

    # ---- storage
    def _load(self):
        try:
            with open(self.file, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d.get("recs"), list):
                d.setdefault("feedback", {})
                return d
        except (OSError, ValueError):
            pass
        return {"recs": [], "feedback": {}, "auto": False}

    def save(self):
        with self.lock:
            self.data["recs"] = self.data["recs"][-400:]
            self.data["auto"] = self.auto
            self.data["request"] = self.request
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.file)

    def _bump(self):
        self.rev += 1

    # ---- what Rora knows about your taste
    def taste(self):
        lines = []
        top = self.music.top(30, 15)
        if top["songs"]:
            lines.append("Most played here (30 days): " + "; ".join(f"{s['title']} – {s['artist']}" for s in top["songs"]))
        snap = self.spotify_snapshot() or {}
        for rng, label in (("short_term", "4 weeks"), ("medium_term", "6 months"), ("long_term", "all time")):
            if snap.get(rng):
                lines.append(f"Spotify top tracks ({label}): " + "; ".join(f"{t['title']} – {t['artist']}" for t in snap[rng][:15]))
        if snap.get("artists"):
            lines.append("Spotify top artists: " + ", ".join(
                a["artist"] + (f" ({', '.join(a['genres'][:2])})" if a["genres"] else "") for a in snap["artists"][:12]))
        fb = sorted(self.data["feedback"].values(), key=lambda f: -f["t"])
        liked = [f for f in fb if f["v"] > 0][:25]
        disliked = [f for f in fb if f["v"] < 0][:25]
        if liked:
            lines.append("Liked from earlier picks: " + "; ".join(
                f"{f['title']} – {f['artist']}" + (f" (while playing {f['game']})" if f.get("game") else "") for f in liked))
        if disliked:
            lines.append("Disliked (avoid this kind of thing): " + "; ".join(
                f"{f['title']} – {f['artist']}" + (f" (while playing {f['game']})" if f.get("game") else "") for f in disliked))
        return "\n".join(lines) or "(no listening history yet)"

    spotify_snapshot = staticmethod(lambda: None)  # set by the helper (cached Spotify top lists)

    # ---- picking songs
    def pick(self, n=5, request=""):
        """Ask Rora for n songs, look them up on Spotify; returns resolved recs (also stored)."""
        game = self.act.game
        request = request or self.request
        now = dt.datetime.now()
        recent = [f"{r['title']} – {r['artist']}" for r in self.data["recs"][-60:]]
        prompt = (
            f"You are Rora, the user's personal music DJ. Pick {n + 7} songs to play next.\n"
            f"Time: {now:%A %H:%M}.\n"
            + (f"The user is playing the video game \"{game}\" right now: match its pace and atmosphere "
               f"(e.g. racing = driving energy, shooter = intense focus, survival/horror = dark ambient tension, "
               f"cozy/sim = relaxed) while staying close to their taste.\n" if game else "The user isn't gaming right now.\n")
            + (f"IMPORTANT, the user asked for this right now: \"{request}\". Every song must fit that request "
               f"(it overrides the usual mix and the game mood where they conflict).\n" if request else "")
            + f"\nTheir taste:\n{self.taste()}\n\n"
            "Rules: real, existing songs on Spotify only (exact titles and main artist); when unsure, choose that "
            "artist's best-known song rather than an obscure title. At least 7 in 10 must be songs "
            "that are NOT in their lists above: discoveries by similar artists or in the same genres; at most 3 can be "
            "favourites from the lists. "
            "No more than one song per artist. Never pick a disliked song or anything recently picked: "
            + ("; ".join(recent[-40:]) or "none") +
            ". 'why' = one short friendly sentence (max 12 words) on why it fits. 'mood' = 2-5 words for this set.")
        res = {}
        for attempt in range(2):  # the model occasionally returns a placeholder-ish answer: ask once more
            res = self.llm([{"role": "user", "content": prompt}], SCHEMA, think=True,
                           keep_alive="20s" if game else None)  # free the GPU quickly while gaming
            good = [x for x in res.get("songs") or [] if isinstance(x, dict) and x.get("title") and x.get("artist")
                    and str(x.get("why", "")).strip().lower() not in ("", "why")]
            if len(good) >= n and str(res.get("mood", "")).strip().lower() not in ("", "mood"):
                break
        res["songs"] = [x for x in res.get("songs") or [] if isinstance(x, dict)
                        and str(x.get("why", "")).strip().lower() not in ("", "why")]
        out = []
        disliked = {k for k, f in self.data["feedback"].items() if f["v"] < 0}
        seen = {_key(r["title"], r["artist"]) for r in self.data["recs"][-60:]}
        known = self.known()
        max_known, n_known = max(1, round(n * 0.34)), 0  # mostly discoveries, a few favourites
        for s in res.get("songs") or []:
            if len(out) >= n:
                break
            k = _key(s.get("title", ""), s.get("artist", ""))
            if k in disliked or k in seen:
                continue
            why = str(s.get("why", ""))[:120]
            try:
                tr = spot.find_track(s.get("title", ""), s.get("artist", ""))
                if not tr:  # the song may not exist, but the artist fits: take their best-known track
                    tr = spot.artist_track(s.get("artist", ""), exclude={r["uri"] for r in out})
                    if tr:
                        why = f"{tr['artist'].split(',')[0]} fits this: {why[:1].lower() + why[1:]}" if why else ""
            except RuntimeError as exc:
                self.error = str(exc)
                break
            if not tr:
                continue
            k = _key(tr["title"], tr["artist"])
            if k in disliked or k in seen or (k in known and n_known >= max_known) or any(r["uri"] == tr["uri"] for r in out):
                continue
            n_known += k in known
            seen.add(k)
            rec = {"id": hashlib.sha1(f"{tr['uri']}{time.time()}".encode()).hexdigest()[:10],
                   "title": tr["title"], "artist": tr["artist"], "uri": tr["uri"],
                   "why": why[:120], "game": game, "t": time.time(), "status": "suggested"}
            out.append(rec)
        with self.lock:
            self.mood = str(res.get("mood", ""))[:40]
            self.data["recs"].extend(out)
            self._bump()
        self.save()
        return out

    def known(self):
        """Songs the user already knows well: Spotify's top lists + the local history."""
        snap = self.spotify_snapshot() or {}
        return {_key(t["title"], t["artist"]) for rng in ("short_term", "medium_term", "long_term")
                for t in snap.get(rng) or []} | {_key(t["title"], t["artist"]) for t in self.music.top(3650, 200)["songs"]}

    def add_recs(self, songs, why_default="", max_known=1):
        """Rora's recommend_songs tool: look up songs she chose in the chat. Keeps at most `max_known`
        favourites the user already plays a lot (returned as `familiar`) so picks stay fresh."""
        out, missing, familiar = [], [], []
        known, n_known = self.known(), 0
        for s in songs[:10]:
            k = _key(str(s.get("title", "")), str(s.get("artist", "")))
            if k in known:
                if n_known >= max_known:
                    familiar.append(f"{s.get('title', '')} – {s.get('artist', '')}")
                    continue
                n_known += 1
            tr = spot.find_track(str(s.get("title", "")), str(s.get("artist", "")))
            if not tr:
                missing.append(f"{s.get('title', '')} – {s.get('artist', '')}")
                continue
            out.append({"id": hashlib.sha1(f"{tr['uri']}{time.time()}{len(out)}".encode()).hexdigest()[:10],
                        "title": tr["title"], "artist": tr["artist"], "uri": tr["uri"],
                        "why": str(s.get("why") or why_default)[:120], "game": self.act.game,
                        "t": time.time(), "status": "suggested"})
        with self.lock:
            self.data["recs"].extend(out)
            self._bump()
        self.save()
        return out, missing, familiar

    # ---- feedback + controls
    def _find(self, rid):
        return next((r for r in reversed(self.data["recs"]) if r["id"] == rid), None)

    def _now_song(self):
        n = self.music.now
        if not n:
            return None
        return {"title": n.get("title", ""), "artist": n.get("artist", ""), "playing": n.get("playing")}

    def _feedback(self, title, artist, v, saved=False):
        k = _key(title, artist)
        f = self.data["feedback"].get(k) or {}
        self.data["feedback"][k] = {"title": title, "artist": artist, "v": v, "saved": saved or f.get("saved", False),
                                    "game": self.act.game, "t": time.time()}

    def act_on(self, action, rid=""):
        """play / queue / like / dislike / save on a pick (rid) or, without rid, on the song playing now."""
        with self.lock:
            rec = self._find(rid) if rid else None
            if rid and not rec:
                return {"ok": False, "error": "That pick has expired."}
            target = rec or self._now_song()
            if not target:
                return {"ok": False, "error": "Nothing is playing in Spotify."}
        try:
            uri = rec["uri"] if rec else None
            if action in ("play", "queue", "save") and not uri:
                tr = spot.find_track(target["title"], target["artist"])
                if not tr:
                    return {"ok": False, "error": "Couldn't find this song on Spotify."}
                uri = tr["uri"]
            if action == "play":
                spot.play_now(uri)
                self._set(rec, "playing")
                with self.lock:
                    self.current = rec
            elif action == "queue":
                spot.queue(uri)
                self._set(rec, "queued")
            elif action == "save":
                spot.save_to_library(uri)
                with self.lock:
                    self._feedback(target["title"], target["artist"], 1, saved=True)
                self._set(rec, "saved")
            elif action == "like":
                with self.lock:
                    self._feedback(target["title"], target["artist"], 1)
                self._set(rec, "liked")
            elif action == "dislike":
                with self.lock:
                    self._feedback(target["title"], target["artist"], -1)
                    playing = self._now_song()
                    is_now = playing and _key(playing["title"], playing["artist"]) == _key(target["title"], target["artist"])
                    if rec and rec in self.upcoming:
                        rec["skip"] = True  # already in Spotify's queue: skip it when it starts
                    artist = _key("", target["artist"])
                    self.buffer = [r for r in self.buffer if _key("", r["artist"]) != artist]
                self._set(rec, "disliked")
                if is_now:
                    spot.skip()
            else:
                return {"ok": False, "error": "unknown action"}
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        self.save()
        return {"ok": True}

    def _set(self, rec, status):
        if rec:
            with self.lock:
                rec["status"] = status
                self._bump()

    # ---- auto mode
    def set_auto(self, on):
        with self.lock:
            self.auto = bool(on)
            self.error = None
            if not on:
                self.buffer.clear()
            self._bump()
        self.save()
        if on:
            threading.Thread(target=self._kickoff, daemon=True).start()
        return self.status()

    def _kickoff(self):
        """Turning auto on: if Spotify is idle, start playing a pick right away."""
        try:
            self._refill()
            n = self._now_song()
            if (not n or not n["playing"]) and self.buffer:
                rec = self.buffer.pop(0)
                spot.play_now(rec["uri"])
                self._set(rec, "playing")
                with self.lock:
                    self.current = rec
                    self.last_seen = _key(rec["title"], rec["artist"])
        except Exception as exc:
            logging.warning("dj kickoff: %s", exc)
            self.error = str(exc)
            self._bump()

    def _refill(self):
        if self.busy:
            return
        self.busy = True
        gen = self.gen
        self._bump()
        try:
            picks = self.pick(5)
            with self.lock:
                if gen != self.gen:  # steered (or game changed) while picking: these no longer fit
                    for r in picks:
                        r["status"] = "dropped"
                    picks = []
                self.buffer.extend(picks)
                for r in picks:
                    r["status"] = "lined up"
                if picks:
                    self.error = None
        except Exception as exc:
            logging.warning("dj pick failed: %s", exc)
            self.error = f"Couldn't pick songs: {exc}"
        finally:
            self.busy = False
            self._bump()
        if gen != self.gen and self.auto:
            self._refill()  # pick again for the new request right away

    def steer(self, request):
        """Change what the DJ plays from now on ('' = back to normal). Lined-up picks are replaced,
        and a pick already sitting in Spotify's queue is skipped when it comes up."""
        request = " ".join(str(request or "").split())[:200]
        if request.lower() in ("normal", "back to normal", "reset", "default", "clear", "none"):
            request = ""
        with self.lock:
            self.request = request
            self.gen += 1
            for r in self.buffer:
                r["status"] = "dropped"
            self.buffer.clear()
            for r in self.upcoming:
                r["skip"] = True
            self._bump()
        self.save()
        if self.auto:
            threading.Thread(target=self._refill, daemon=True).start()
        return self.status()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(5)
            if not self.music.app_open():
                time.sleep(10)
                continue
            try:
                self._tick()
            except Exception:
                logging.exception("dj tick failed")

    def _tick(self):
        if not self.auto or not spot.dj_ready():
            return
        game = self.act.game
        if game != self.game:  # new game (or stopped): the next picks should fit it
            with self.lock:
                self.game = game
                self.gen += 1
                self.buffer.clear()
                self._bump()
        n = self._now_song()
        if n:
            k = _key(n["title"], n["artist"])
            if k != self.last_seen:  # a new song started
                self.last_seen = k
                with self.lock:
                    hit = next((r for r in self.upcoming if _key(r["title"], r["artist"]) == k), None)
                    if hit:
                        self.upcoming = [r for r in self.upcoming if r is not hit]
                        self.current = hit
                        hit["status"] = "playing" if hit["status"] in ("queued", "lined up") else hit["status"]
                    elif not (self.current and _key(self.current["title"], self.current["artist"]) == k):
                        self.current = None
                    self._bump()
                if hit and hit.get("skip"):
                    spot.skip()
                    return
        # pick more songs (= wake the LLM on the GPU) only while music is actually playing
        if len(self.buffer) < 2 and not self.busy and n and n["playing"]:
            threading.Thread(target=self._refill, daemon=True).start()
        # keep exactly one DJ song waiting in Spotify's queue while music is playing
        left = None
        live = self.music.now or {}
        if live.get("duration"):
            left = float(live["duration"]) - float(live.get("position") or 0) - (time.time() - live.get("at", time.time()))
        if time.time() < getattr(self, "pause_until", 0):
            return  # a queue call just failed: wait a minute before trying again
        if n and n["playing"] and not self.upcoming and self.buffer and (left is None or left < 25):
            with self.lock:
                rec = self.buffer.pop(0)
            try:
                spot.queue(rec["uri"])
                with self.lock:
                    rec["status"] = "queued"
                    self.upcoming.append(rec)
                    self.error = None
                    self._bump()
            except Exception as exc:  # never re-queue blindly: put it back and report
                self.error = str(exc)
                with self.lock:
                    self.buffer.insert(0, rec)
                self.pause_until = time.time() + 60

    # ---- for the widget and Rora
    def status(self):
        with self.lock:
            fb = self.data["feedback"]
            n = self._now_song()
            now_fb = fb.get(_key(n["title"], n["artist"])) if n else None
            pub = lambda r: {k: r.get(k) for k in ("id", "title", "artist", "why", "status", "game")} if r else None
            return {"rev": self.rev, "auto": self.auto, "ready": spot.dj_ready(), "state": spot.status(),
                    "busy": self.busy, "error": self.error, "game": self.act.game, "mood": self.mood,
                    "request": self.request,
                    "current": pub(self.current), "next": [pub(r) for r in (self.upcoming + self.buffer)[:3]],
                    "now": dict(n, feedback=(now_fb or {}).get("v", 0), saved=(now_fb or {}).get("saved", False)) if n else None,
                    "likes": sum(1 for f in fb.values() if f["v"] > 0), "dislikes": sum(1 for f in fb.values() if f["v"] < 0)}

    def recs(self, ids):
        with self.lock:
            return [{k: r.get(k) for k in ("id", "title", "artist", "why", "status")} for r in self.data["recs"] if r["id"] in ids]

    def describe(self):
        st = self.status()
        lines = [f"Rora DJ auto mode: {'ON' if st['auto'] else 'off'}" + (f" (mood: {st['mood']})" if st["mood"] else "")]
        lines.append(f"DJ is steered to: \"{st['request']}\"" if st["request"] else "DJ isn't steered (normal mix of their taste).")
        if st["game"]:
            lines.append(f"Game in front right now: {st['game']}")
        if st["current"]:
            lines.append(f"DJ pick playing: {st['current']['title']} – {st['current']['artist']}")
        fb = sorted(self.data["feedback"].values(), key=lambda f: -f["t"])[:15]
        if fb:
            lines.append("Recent feedback on songs: " + "; ".join(
                f"{'♥' if f.get('saved') else '+' if f['v'] > 0 else '−'} {f['title']} – {f['artist']}" for f in fb))
        return "\n".join(lines)
