"""Render the home-screen background as a static, native-resolution image.

Draws the same gradient "blobs" as wallpaper/style.css (at the start of their
animation), at full panel resolution with dithering so smooth dark gradients
don't show visible color steps. Windows shows it whenever Wallpaper Engine
isn't running (e.g. while Windows is starting).

Usage: python make_static_wallpaper.py [width height] [--set] [--lock]

--lock also renders a mirrored variant for the lock screen and applies it
(via tools/set_lock_screen.ps1).
"""
import ctypes
import os
import sys
import winreg

import numpy as np
from PIL import Image

CSS_W, CSS_H = 5120, 1440  # the wallpaper's layout size (style.css uses vmax/vw)


def hex_rgb(h):
    return np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.float32)


def render(width, height, accent=(115, 140, 255)):
    s = width / CSS_W
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    xs = (xs + 0.5) / s  # pixel centers, in CSS px
    ys = (ys + 0.5) / s

    # base: radial-gradient(120% 90% at 50% 120%, #161a2e 0%, #0a0c14 60%)
    t = np.sqrt(((xs - 0.5 * CSS_W) / (1.2 * CSS_W)) ** 2 + ((ys - 1.2 * CSS_H) / (0.9 * CSS_H)) ** 2)
    k = np.clip(t / 0.6, 0, 1)[..., None]
    img = hex_rgb("#161a2e") * (1 - k) + hex_rgb("#0a0c14") * k

    vmax = max(CSS_W, CSS_H)
    size = 0.7 * vmax
    corner = size / 2 * np.sqrt(2)  # radial-gradient "circle" = farthest-corner
    blobs = [  # (center x, center y, color, gradient stop, opacity), matching .b1/.b2/.b3
        (-0.20 * vmax + size / 2, -0.30 * vmax + size / 2, np.array(accent, np.float32), 0.62, 0.55),
        (CSS_W + 0.25 * vmax - size / 2, -0.10 * vmax + size / 2, hex_rgb("#b45cff"), 0.60, 0.35),
        (0.25 * CSS_W + size / 2, CSS_H + 0.45 * vmax - size / 2, hex_rgb("#1fc8b0"), 0.60, 0.28),
    ]
    for cx, cy, color, stop, opacity in blobs:
        d = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
        a = (opacity * np.clip(1 - d / (stop * corner), 0, 1))[..., None]
        img = img * (1 - a) + color * a

    # triangular dither (+/- 1 level) then quantize
    rng = np.random.default_rng(7)
    noise = rng.random(img.shape, dtype=np.float32) + rng.random(img.shape, dtype=np.float32) - 1.0
    return np.clip(np.rint(img + noise), 0, 255).astype(np.uint8)


def set_wallpaper(path):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "JPEGImportQuality", 0, winreg.REG_DWORD, 100)  # stop Windows recompressing it
        winreg.SetValueEx(key, "WallpaperStyle", 0, winreg.REG_SZ, "10")      # Fill
        winreg.SetValueEx(key, "TileWallpaper", 0, winreg.REG_SZ, "0")
    SPI_SETDESKWALLPAPER, SPIF_UPDATEINIFILE, SPIF_SENDCHANGE = 20, 0x1, 0x2
    if not ctypes.windll.user32.SystemParametersInfoW(SPI_SETDESKWALLPAPER, 0, path, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE):
        raise ctypes.WinError()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    width, height = (int(args[0]), int(args[1])) if len(args) == 2 else (7680, 2160)
    out_dir = os.path.join(os.environ["LOCALAPPDATA"], "HomeScreen")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"wallpaper-{width}x{height}.png")
    Image.fromarray(render(width, height), "RGB").save(path, optimize=False, compress_level=6)
    print(path)
    if "--set" in sys.argv:
        set_wallpaper(path)
        print("set as Windows wallpaper")
    if "--lock" in sys.argv:
        lock = os.path.join(out_dir, f"lockscreen-{width}x{height}.jpg")
        # mirrored so it is recognisably the same style but not identical to the desktop
        Image.fromarray(np.ascontiguousarray(render(width, height)[:, ::-1]), "RGB").save(
            lock, quality=97, subsampling=0, optimize=True)
        print(lock)
        import subprocess
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "set_lock_screen.ps1")
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, lock], check=True)


if __name__ == "__main__":
    main()
