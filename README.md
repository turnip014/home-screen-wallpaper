# Home Screen

A phone-style, interactive home screen for the Windows desktop, built as a
[Wallpaper Engine](https://www.wallpaperengine.io/) web wallpaper with a small local Python helper.
Designed for 32:9 super-ultrawide monitors (7680×2160), and it adapts to any screen size.

Your desktop shortcuts become an animated app grid with a dock and widgets: weather,
music, markets, system stats, calendar, to-do list and a private on-device AI assistant named Rora (short for Aurora).
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
  - Mail: latest inbox messages and unread count (Gmail, read-only); click to open in the browser
  - Spending: card spending this month, parsed locally from bank transaction-alert emails
    (daily bars, budget, comparison with the same point last month)
  - Move: a sitting timer. After an hour at the desk, a small corner card suggests a quick
    exercise (push-ups, squats, plank…). It never takes focus, waits while a full-screen game runs,
    and keeps a daily streak.
  - AI chat: runs a local LLM through [Ollama](https://ollama.com), so the conversation stays on
    the PC. It **sees what the home screen sees** (calendar, tasks, music, weather, markets, PC
    stats, recently used apps, inbox headers, spending) through a local, read-only tool.
    Anything that leaves the PC (web search, reading a page, asking Claude via Claude Code) or
    changes it (non-read-only PowerShell) shows an **Allow / Run button first**.
    A boot animation plays while the model loads into the GPU, and the model starts warming up
    as soon as you open the input box.
- **Motion:** parallax background, cursor glow, click ripples, 3D icon tilt, dock
  magnification and FLIP layout animations. Everything animates only transforms and opacity, with no blur.
- **Rest mode:** with no keyboard or mouse input for a while (Wallpaper Engine setting, 2 minutes by
  default) or while a game is running, every animation freezes and the live widgets stop polling. The
  page then draws no frames and the GPU can drop to idle clocks. Any input wakes it. While Spotify is
  closed, the music monitor and DJ go quiet, and the DJ only runs the local model while music plays.
- **Low refresh when idle:** after a longer idle (5 minutes by default) the helper drops the display to
  a low refresh rate (120 Hz if offered, else 60 Hz) so VRAM can clock down too. Any input restores the
  normal rate in under half a second. The change is never saved, so a reboot always comes back at the
  normal rate. It never touches the display while a game or fullscreen app runs.
  `tools/edid_120hz.ps1` adds a 120 Hz mode through a Windows EDID override, for monitors that only
  offer 60 and 240 Hz at native resolution (`-Remove` undoes it). The GPU driver must restart once.
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
| `helper/activity.py` | Foreground-app history (names only) and the sitting timer behind the Move widget |
| `helper/exercise_popup.py` | Non-activating "time to move" card |
| `helper/spending.py` | Parses card transaction-alert emails into totals |
| `helper/music.py`, `helper/nowplaying.ps1` | Spotify listening history (top songs/artists, on repeat), read from Windows media controls and kept only on this PC |
| `helper/spotify_sync.py` | Optional Spotify connection (PKCE sign-in): your Spotify top tracks, and playlists made from the Top songs widget or by Rora after you click Create |
| `helper/dj.py` | Rora DJ: song picks from your taste and the game in front, queued in Spotify (auto mode), with Save / Like / Dislike feedback kept on this PC |
| `helper/web_tools.py` | Web search (DuckDuckGo lite, Wikipedia API, optional Brave API) and page-to-text for Rora |
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
layout, to-dos, chat history, app-usage history (app names only, 7 days), exercise log, parsed
spending totals and the encrypted Google token. The only outside requests go to Open-Meteo,
ipinfo.io (once, for approximate location), Yahoo Finance charts, your calendar feeds and
Google's APIs, plus web searches and Claude questions **only after you click Allow** in the chat.
