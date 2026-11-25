"""Game guard: while a game is in front, the wallpaper is stopped entirely.

Wallpaper Engine's own "pause" keeps the page loaded (about 800 MB of VRAM, and only for exclusive
full screen). Stopping it unloads the page and frees all of that, also for borderless-window games.
The wallpaper is played again a few seconds after the game leaves the front (alt-tab, quit).
Only a stop made here is undone here: a wallpaper the user stopped themselves stays stopped.
"""
import atexit
import logging
import subprocess
import threading
import time

import activity as activity_mod

CHECK = 2      # seconds between checks
RESUME = 3     # seconds out of the game before the wallpaper comes back


class GameGuard:
    def __init__(self, enabled):
        self.enabled = enabled      # () -> bool, from config
        self.stopped = False        # True only while this guard has the wallpaper stopped
        self.game = None
        self.calm_since = 0.0
        atexit.register(self.resume)

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _control(self, cmd):
        exe = activity_mod.exe_path("wallpaper64.exe") or activity_mod.exe_path("wallpaper32.exe")
        if not exe:
            return False
        subprocess.run([exe, "-control", cmd], creationflags=0x08000000, timeout=20)  # no console window
        return True

    def resume(self):
        if self.stopped:
            self.stopped = False
            logging.info("game guard: wallpaper back on")
            try:
                self._control("play")
            except Exception:
                logging.exception("game guard: play failed")

    def _loop(self):
        while True:
            time.sleep(CHECK)
            try:
                # foreground check only (no idle rule), so controller play still counts as gaming
                self.game = activity_mod.current_game()
                if self.game and self.enabled():
                    self.calm_since = 0.0
                    if not self.stopped and self._control("stop"):
                        self.stopped = True
                        logging.info("game guard: wallpaper stopped for %s", self.game)
                elif self.stopped:
                    self.calm_since = self.calm_since or time.time()
                    if time.time() - self.calm_since >= RESUME or not self.enabled():
                        self.resume()
            except Exception:
                logging.exception("game guard check failed")
