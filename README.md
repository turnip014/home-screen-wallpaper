# Home Screen

A phone-style, interactive home screen for the Windows desktop, built as a
[Wallpaper Engine](https://www.wallpaperengine.io/) web wallpaper with a small local Python helper.
Designed for 32:9 super-ultrawide monitors (7680×2160), and it adapts to any screen size.

Your desktop shortcuts become an animated app grid with a dock and widgets: weather,
music, markets, system stats, calendar, to-do list and a private on-device AI assistant.
It's built so it never costs gaming performance.

## Features

- **App grid from your desktop:** shortcuts, folders and files appear with their real
  Windows icons, paged like a phone. Anything new added to the desktop pops in within seconds,
  and folders open as an animated popup. Windows' own desktop icons are hidden.
- **Dock:** pinned apps (including Microsoft Store apps), the Recycle Bin with a live item
  count, and a power button that unfolds into Restart / Shut down (two clicks to confirm).
- **Widgets** you can rearrange phone-style: long-press to enter edit mode, then drag between
  columns, hide or restore. The layout is saved.
  - Clock and greeting
  - Weather: current, hourly and 5-day forecast, location from IP ([Open-Meteo](https://open-meteo.com))
  - Now playing: Spotify/media controls with an audio-reactive visualizer
  - Markets: indices, metals, crypto and FX with sparklines
  - System: CPU, RAM, GPU and VRAM (NVML), plus disk usage
  - Calendar: month view and upcoming events, from Google Calendar or any iCal feed
    (handles recurring events, exceptions and time zones)
  - To-do list: two-way sync with Google Tasks, or stored locally
  - AI chat: runs a local LLM through [Ollama](https://ollama.com), so nothing leaves the PC.
    It can propose PowerShell commands or open Claude Code, and **nothing runs until you click
    Run**. Destructive commands are flagged.
- **Motion:** parallax background, cursor glow, click ripples, 3D icon tilt, dock
  magnification and FLIP layout animations. Everything animates only transforms and opacity, with no blur.
- **Static fallbacks:** a dithered native-resolution render of the background for the Windows
  wallpaper and lock screen, so they stay sharp on large monitors.

## How it works

```
Wallpaper Engine (Chromium)                 Local helper (Python stdlib, 127.0.0.1 only)
┌───────────────────────────┐   HTTP +     ┌──────────────────────────────────────────┐
│ wallpaper/  HTML/CSS/JS   │ ── token ──▶ │ homescreen_helper.py                     │
│  grid, dock, widgets,     │              │  desktop scan + icon extraction (COM)    │
│  edit mode, animations    │ ◀── JSON ─── │  launch apps, media keys, NVML stats     │
└───────────────────────────┘              │  weather / markets / calendar caches     │
                                           │  chat.py ──▶ Ollama (local LLM)          │
   quick_input.py (tkinter) ──────────────▶│  google_sync.py ──▶ Google Tasks/Calendar│
   floating text box, because wallpapers   └──────────────────────────────────────────┘
   receive no keyboard input
```

Design choices:

- **Zero cost while gaming.** Wallpaper Engine is set to stop the wallpaper for fullscreen
  apps. The page's animation loop runs only while something moves, and polling stops when hidden.
  Ollama unloads the model from VRAM after 5 idle minutes.
- **Never block the UI.** Network data (weather, markets, calendar) is served
  stale-while-revalidate from caches that refresh on background threads, and only while the wallpaper asks.
- **Locked down.** The helper binds to `127.0.0.1` only, checks the `Host` header, and
  requires a random per-install token on every request. It can only launch items it listed itself.
  The Google OAuth token is encrypted with Windows DPAPI, and the sign-in uses loopback + PKCE.
- **No dependencies** for the helper: Python standard library plus `ctypes` for the Win32/COM
  calls (icon extraction via `IShellItemImageFactory`, PNG encoding, Recycle Bin, media keys, NVML).

## Project layout

| Path | What it is |
|---|---|
| `wallpaper/` | The wallpaper (HTML/CSS/JS) and Wallpaper Engine `project.json` |
| `helper/homescreen_helper.py` | Local HTTP helper: desktop items, icons, launching, stats, weather, markets, layout, to-dos |
| `helper/chat.py` | AI chat against a local Ollama model, with approve-before-run tool calls |
| `helper/google_sync.py` | Google Tasks (two-way) and Calendar (read-only); OAuth loopback + PKCE, DPAPI-encrypted token |
| `helper/calendar_ics.py` | iCal parser: RRULE, EXDATE, RECURRENCE-ID, TZID |
| `helper/quick_input.py` | Floating DPI-aware input box for typing tasks and chat messages |
| `install.ps1` | Creates the token, copies the wallpaper into Wallpaper Engine, starts the helper at login |
| `tools/make_static_wallpaper.py` | Renders the background at native resolution (numpy + Pillow); `--set`, `--lock` |
| `tools/set_lock_screen.ps1` | Sets the lock screen image via the WinRT `LockScreen` API |

## Setup

Requirements: Windows 11, [Wallpaper Engine](https://store.steampowered.com/app/431960/Wallpaper_Engine/),
Python 3.12+ (`pip install tzdata` for calendar time zones). Optional: Ollama for the AI chat,
and an NVIDIA GPU for GPU stats.

1. Run `powershell -ExecutionPolicy Bypass -File install.ps1`. Re-run it after any change.
2. In Wallpaper Engine, select **Home Screen** under Installed (or *Open Wallpaper → Open from File*
   → `projects\myprojects\home-screen\project.json`).
3. Wallpaper Engine settings → Performance: fullscreen / playing game → **Stop (free memory)**,
   FPS limit **30**. General: **Start with Windows**.

Settings live in `%LOCALAPPDATA%\HomeScreen\config.json` (created from `helper/config.default.json`):
dock apps, markets, weather units, iCal feeds and the chat model.

**AI chat:** install Ollama and `ollama pull qwen3:30b` (or set `chat.model` to any model).

**Google Tasks & Calendar (optional):** create a Google Cloud project, enable the Tasks and
Calendar APIs, and create an OAuth client of type *Desktop app*. Save its JSON as
`%LOCALAPPDATA%\HomeScreen\google_client.json` and run `python helper/google_sync.py connect`.
If the app stays in Testing mode, Google expires the sign-in weekly, and the widgets then show a
*Reconnect Google* button.

## Data and privacy

Everything personal stays in `%LOCALAPPDATA%\HomeScreen\` on your PC: token, config, icon cache,
layout, to-dos, chat history and the encrypted Google token. The only outside requests go to
Open-Meteo, ipinfo.io (once, for approximate location), Yahoo Finance charts, your calendar
feeds and Google's APIs.
