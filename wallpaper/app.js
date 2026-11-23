(() => {
  "use strict";

  const CFG = window.HOMESCREEN || null;
  const BASE = CFG ? `http://127.0.0.1:${CFG.port}` : null;
  const $ = (id) => document.getElementById(id);
  const url = (path) => `${BASE}${path}${path.includes("?") ? "&" : "?"}t=${encodeURIComponent(CFG.token)}`;

  const S = {
    paused: false, clock24: false, iconSize: 92, columns: 7,
    interactive: true, audioReact: true, editing: false, hidden: [],
    version: "", items: [], dock: [], page: 0, pages: 1,
    seen: new Set(), rendered: false, online: null, prevTime: "",
    timers: {}, drag: null, suppressClick: false,
    folderId: null, folderOrigin: null, closing: false,
    weather: null, prevPrices: new Map(),
  };

  const GLYPHS = {
    music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1 7 17M17 7l2.1-2.1"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    power: '<path d="M12 3v8"/><path d="M6.4 6.6a8 8 0 1 0 11.2 0"/>',
    restart: '<path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4v5h-5"/>',
  };

  function svg(inner, cls) {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    if (cls) s.setAttribute("class", cls);
    s.innerHTML = inner;
    return s;
  }

  // ================================================================ helper API
  async function api(path, body) {
    const opts = body
      ? { method: "POST", headers: { "Content-Type": "text/plain" }, body: JSON.stringify(body) }
      : {};
    const res = await fetch(url(path), opts);
    if (!res.ok && res.status !== 404) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  function showNotice(text) { const n = $("notice"); n.textContent = text; n.hidden = false; }
  function hideNotice() { $("notice").hidden = true; }

  function setOnline(on) {
    if (S.online === on) return;
    S.online = on;
    if (on) hideNotice();
    else showNotice("Home Screen helper isn't running. Run install.ps1 to start it.");
  }

  // ================================================================ clock
  const pad = (n) => String(n).padStart(2, "0");

  function renderClock() {
    const d = new Date();
    const h = d.getHours(), m = d.getMinutes();
    const t = $("time");
    const text = S.clock24 ? `${pad(h)}:${pad(m)}` : `${h % 12 || 12}:${pad(m)}`;
    const prev = S.prevTime;
    t.textContent = "";
    [...text].forEach((ch, i) => {
      const span = document.createElement("span");
      span.className = "d";
      span.textContent = ch;
      // digits that changed since last minute roll in
      if (prev && (prev.length !== text.length || prev[i] !== ch)) span.classList.add("roll");
      t.append(span);
    });
    S.prevTime = text;
    if (!S.clock24) {
      const ampm = document.createElement("span");
      ampm.className = "ampm";
      ampm.textContent = h < 12 ? "AM" : "PM";
      t.append(ampm);
    }
    $("date").textContent = d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
    $("greeting").textContent = h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
    // past midnight: move the calendar's "today" along
    if (typeof C !== "undefined" && C.dayKey && C.dayKey !== dayKey(d)) renderCalendar();
  }

  function scheduleClock() {
    clearTimeout(S.timers.clock);
    renderClock();
    const d = new Date();
    S.timers.clock = setTimeout(scheduleClock, (60 - d.getSeconds()) * 1000 - d.getMilliseconds() + 50);
  }

  // ================================================================ app tiles
  function fallback(item, mini) {
    const el = document.createElement(mini ? "div" : "span");
    if (mini) { el.className = "mini"; return el; }
    el.className = "letter";
    el.textContent = (item.name || "?").trim().charAt(0).toUpperCase();
    return el;
  }

  function iconImage(item, mini) {
    if (item.glyph && GLYPHS[item.glyph]) return svg(GLYPHS[item.glyph]);
    if (!item.iconKey) return fallback(item, mini);
    const img = new Image();
    img.alt = "";
    img.decoding = "async";
    img.draggable = false;
    img.src = url(`/icon?id=${encodeURIComponent(item.id)}&k=${item.iconKey}`);
    img.onerror = () => img.replaceWith(fallback(item, mini));
    return img;
  }

  function iconEl(item) {
    const box = document.createElement("div");
    box.className = "icon";
    if (item.kind === "folder") {
      box.classList.add("folder-icon");
      const preview = (item.preview || []).slice(0, 4);
      preview.forEach((p) => box.append(iconImage(p, true)));
      for (let i = preview.length; i < 4; i++) box.append(fallback(item, true));
    } else {
      box.append(iconImage(item, false));
    }
    return box;
  }

  function tile(item) {
    const b = document.createElement("button");
    b.className = "tile";
    b.dataset.id = item.id;
    if (item.divider) b.classList.add("divided");
    b.append(iconEl(item));
    if (item.badge) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = item.badge > 99 ? "99+" : String(item.badge);
      b.append(badge);
    }
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = item.name;
    b.append(label);
    b.addEventListener("click", () => { if (!S.suppressClick) activate(item, b); });
    b.addEventListener("pointermove", (e) => tilt(b, e));
    b.addEventListener("pointerleave", () => untilt(b));
    return b;
  }

  // icons lean toward the cursor, with a moving highlight
  function tilt(tileEl, e) {
    if (!S.interactive || tileEl.closest(".dock")) return;
    const icon = tileEl.firstElementChild;
    const r = icon.getBoundingClientRect();
    const px = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const py = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));
    icon.style.transition = "transform .12s ease-out, box-shadow .25s";
    icon.style.transform =
      `perspective(420px) rotateX(${((0.5 - py) * 18).toFixed(2)}deg) rotateY(${((px - 0.5) * 18).toFixed(2)}deg) translateY(-5px) scale(1.08)`;
    icon.style.setProperty("--sx", `${(px * 100).toFixed(1)}%`);
    icon.style.setProperty("--sy", `${(py * 100).toFixed(1)}%`);
  }

  function untilt(tileEl) {
    if (tileEl.closest(".dock")) return;
    const icon = tileEl.firstElementChild;
    icon.style.transition = "";
    icon.style.transform = "";
  }

  async function activate(item, el) {
    if (item.kind === "folder") return openFolder(item, el);
    if (item.id === "power") return powerMenu.hidden || powerMenu.classList.contains("closing") ? openPowerMenu(el) : closePowerMenu();
    el.classList.remove("launching");
    void el.offsetWidth; // restart the bounce animation
    el.classList.add("launching");
    try { await api("/launch", { id: item.id }); } catch { setOnline(false); }
  }

  // ================================================================ app grid + pages
  function capacity() {
    const vp = $("viewport");
    const tileW = S.iconSize + 44 + 22;           // tile width + column gap
    const tileH = S.iconSize + 12 + 10 + 36 + 30; // icon + padding + gap + 2-line label + row gap
    const cols = Math.max(1, Math.min(S.columns, Math.floor((vp.clientWidth - 16) / tileW)));
    const rows = Math.max(1, Math.floor((vp.clientHeight - 16) / tileH));
    return { cols, per: cols * rows };
  }

  function renderGrid() {
    const { cols, per } = capacity();
    const pagesEl = $("pages");
    pagesEl.textContent = "";
    const chunks = [];
    for (let i = 0; i < S.items.length; i += per) chunks.push(S.items.slice(i, i + per));
    if (!chunks.length) chunks.push([]);

    let n = 0;
    for (const chunk of chunks) {
      const page = document.createElement("div");
      page.className = "page";
      page.style.gridTemplateColumns = `repeat(${Math.min(cols, Math.max(chunk.length, 1))}, auto)`;
      for (const item of chunk) {
        const t = tile(item);
        if (!S.rendered) {
          t.classList.add("intro");
          t.style.animationDelay = `${Math.min(n++, 30) * 30}ms`;
        } else if (!S.seen.has(item.id)) {
          t.classList.add("pop"); // newly added to the desktop
        }
        page.append(t);
      }
      pagesEl.append(page);
    }
    S.items.forEach((it) => S.seen.add(it.id));
    S.rendered = true;
    S.pages = chunks.length;
    renderDots();
    setPage(Math.min(S.page, S.pages - 1));
    if (S.online && !S.items.length) showNotice("Your desktop is empty. Add a shortcut and it shows up here.");
    else if (S.online) hideNotice();
  }

  function setPage(p) {
    const next = Math.max(0, Math.min(p, S.pages - 1));
    const changed = next !== S.page;
    const dir = next > S.page ? 1 : -1;
    S.page = next;
    const el = $("pages");
    el.classList.remove("dragging");
    el.style.transform = `translateX(${-S.page * 100}%)`;
    [...$("dots").children].forEach((d, i) => d.classList.toggle("on", i === S.page));
    const page = el.children[S.page];
    if (changed && page) {
      // icons on the new page slide in one after another
      page.classList.remove("enter");
      page.style.setProperty("--dir", dir);
      [...page.children].forEach((t, i) => { t.style.animationDelay = `${Math.min(i, 24) * 16}ms`; });
      void page.offsetWidth;
      page.classList.add("enter");
    }
  }

  function renderDots() {
    const d = $("dots");
    d.textContent = "";
    if (S.pages < 2) return;
    for (let i = 0; i < S.pages; i++) {
      const b = document.createElement("button");
      b.className = "dot";
      b.setAttribute("aria-label", `Page ${i + 1}`);
      b.addEventListener("click", () => setPage(i));
      d.append(b);
    }
  }

  function renderDock() {
    const d = $("dock");
    d.textContent = "";
    S.dock.forEach((it) => d.append(tile(it)));
    d.append(powerTile());
  }

  // ---- power button: unfolds upward into Restart / Shut down (each needs a second click to confirm)
  function powerTile() {
    const item = { id: "power", name: "Power", glyph: "power", divider: true };
    const b = tile(item);
    b.classList.add("power-tile");
    b.firstElementChild.classList.add("glyph-icon");
    return b;
  }

  const powerMenu = document.createElement("div");
  powerMenu.className = "power-menu";
  powerMenu.hidden = true;
  [["restart", "Restart", GLYPHS.restart], ["shutdown", "Shut down", GLYPHS.power]].forEach(([action, label, glyph], i) => {
    const btn = document.createElement("button");
    btn.className = `power-opt ${action}`;
    btn.style.setProperty("--i", 1 - i);
    btn.append(svg(glyph));
    const text = document.createElement("span");
    text.textContent = label;
    btn.append(text);
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!btn.classList.contains("armed")) {
        powerMenu.querySelectorAll(".armed").forEach((x) => { x.classList.remove("armed"); x.lastChild.textContent = x._label; });
        btn.classList.add("armed");
        text.textContent = `${label}?  Click again`;
        clearTimeout(btn._t);
        btn._t = setTimeout(() => { btn.classList.remove("armed"); text.textContent = label; }, 4000);
        return;
      }
      text.textContent = action === "restart" ? "Restarting…" : "Shutting down…";
      try { await api("/power", { action }); } catch { setOnline(false); }
    });
    btn._label = label;
    powerMenu.append(btn);
  });
  document.body.append(powerMenu);

  function openPowerMenu(anchor) {
    const r = anchor.getBoundingClientRect();
    powerMenu.style.left = `${r.left + r.width / 2}px`;
    powerMenu.style.bottom = `${window.innerHeight - r.top + 14}px`;
    powerMenu.hidden = false;
    powerMenu.classList.remove("closing");
    void powerMenu.offsetWidth;
    powerMenu.classList.add("open");
  }

  function closePowerMenu() {
    if (powerMenu.hidden || powerMenu.classList.contains("closing")) return;
    powerMenu.classList.remove("open");
    powerMenu.classList.add("closing");
    powerMenu.querySelectorAll(".power-opt").forEach((x) => { x.classList.remove("armed"); x.lastChild.textContent = x._label; });
    setTimeout(() => { if (powerMenu.classList.contains("closing")) { powerMenu.hidden = true; powerMenu.classList.remove("closing"); } }, 220);
  }

  document.addEventListener("pointerdown", (e) => {
    if (!e.target.closest(".power-menu, .power-tile")) closePowerMenu();
  });

  async function refreshItems() {
    if (!BASE) { showNotice("Not connected yet. Run install.ps1 to set up the helper."); return; }
    try {
      const data = await api(`/items?v=${S.version}`);
      setOnline(true);
      if (data.unchanged) return;
      S.version = data.version;
      S.items = data.items || [];
      S.dock = data.dock || [];
      renderGrid();
      renderDock();
    } catch {
      setOnline(false);
    }
  }

  // mouse wheel and drag to change pages, like swiping on a phone
  let wheelLock = 0;
  $("apps").addEventListener("wheel", (e) => {
    const now = Date.now();
    const delta = Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
    if (now < wheelLock || Math.abs(delta) < 15 || S.pages < 2) return;
    wheelLock = now + 450;
    setPage(S.page + (delta > 0 ? 1 : -1));
  }, { passive: true });

  $("viewport").addEventListener("pointerdown", (e) => {
    if (e.button === 0 && !S.editing) S.drag = { x: e.clientX, moved: false };
  });
  window.addEventListener("pointermove", (e) => {
    if (!S.drag) return;
    let dx = e.clientX - S.drag.x;
    if (!S.drag.moved && Math.abs(dx) < 10) return;
    // rubber-band resistance past the first/last page
    if ((S.page === 0 && dx > 0) || (S.page === S.pages - 1 && dx < 0)) dx = Math.sign(dx) * Math.pow(Math.abs(dx), 0.75);
    S.drag.moved = true;
    const el = $("pages");
    el.classList.add("dragging");
    el.style.transform = `translateX(calc(${-S.page * 100}% + ${dx}px))`;
  });
  window.addEventListener("pointerup", (e) => {
    if (!S.drag) return;
    const { x, moved } = S.drag;
    S.drag = null;
    if (!moved) return;
    const dx = e.clientX - x;
    S.suppressClick = true;
    setTimeout(() => { S.suppressClick = false; }, 0);
    setPage(S.page + (dx < -80 ? 1 : dx > 80 ? -1 : 0));
  });

  let gridTimer;
  new ResizeObserver(() => {
    clearTimeout(gridTimer);
    gridTimer = setTimeout(() => { if (S.rendered) renderGrid(); }, 200);
  }).observe($("viewport"));

  // ================================================================ folders
  const PANEL_EASE = "cubic-bezier(.2, 1.12, .35, 1)";

  // where the folder's icon is relative to the panel's center, for zooming out of / back into it
  function originOffset(el) {
    const panel = document.querySelector(".folder-panel");
    if (!el || !el.isConnected) return { dx: 0, dy: 0 };
    const r = el.getBoundingClientRect(), p = panel.getBoundingClientRect();
    return { dx: r.left + r.width / 2 - (p.left + p.width / 2), dy: r.top + r.height / 2 - (p.top + p.height / 2) };
  }

  async function openFolder(item, el) {
    const wasOpen = !$("folder").hidden;
    S.folderId = item.id;
    if (!wasOpen) S.folderOrigin = el;
    $("ftitle").textContent = item.name;
    const grid = $("fgrid");
    grid.textContent = "";
    $("folder").hidden = false;
    if (!wasOpen) {
      const { dx, dy } = originOffset(el);
      document.querySelector(".folder-panel").animate(
        [{ transform: `translate(${dx}px, ${dy}px) scale(.12)`, opacity: 0 }, { transform: "none", opacity: 1 }],
        { duration: 480, easing: PANEL_EASE });
    }
    try {
      const data = await api(`/folder?id=${encodeURIComponent(item.id)}`);
      if (S.folderId !== item.id) return;
      grid.textContent = "";
      if (!data.items || !data.items.length) {
        const e = document.createElement("div");
        e.className = "empty";
        e.textContent = "This folder is empty.";
        grid.append(e);
        return;
      }
      data.items.forEach((it, i) => {
        const t = tile(it);
        t.classList.add("intro");
        t.style.animationDelay = `${Math.min(i, 20) * 25}ms`;
        grid.append(t);
      });
    } catch {
      setOnline(false);
    }
  }

  function closeFolder() {
    const overlay = $("folder");
    if (overlay.hidden || S.closing) return;
    S.closing = true;
    S.folderId = null;
    const { dx, dy } = originOffset(S.folderOrigin);
    const shrink = document.querySelector(".folder-panel").animate(
      [{ transform: "none", opacity: 1 }, { transform: `translate(${dx}px, ${dy}px) scale(.12)`, opacity: 0 }],
      { duration: 300, easing: "cubic-bezier(.4, 0, .7, .2)" });
    overlay.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 300, fill: "forwards" });
    shrink.finished.catch(() => {}).then(() => {
      overlay.hidden = true;
      overlay.getAnimations().forEach((a) => a.cancel());
      S.closing = false;
    });
  }

  $("folder").addEventListener("click", (e) => { if (e.target === $("folder")) closeFolder(); });
  $("fopen").addEventListener("click", async () => {
    const id = S.folderId;
    closeFolder();
    if (id) { try { await api("/launch", { id }); } catch { setOnline(false); } }
  });

  // ================================================================ system + storage
  function bar(id, pct, cls) {
    const el = $(id);
    el.style.width = `${Math.max(0, Math.min(100, pct || 0))}%`;
    el.className = cls || "";
  }
  const level = (v, warn, hot) => (v >= hot ? "hot" : v >= warn ? "warn" : "");
  const visible = (name) => !S.hidden.includes(name);

  function renderDisks(disks) {
    const box = $("disks");
    if (box.childElementCount !== disks.length) {
      box.textContent = "";
      disks.forEach(() => {
        const row = document.createElement("div");
        row.className = "stat";
        row.innerHTML = '<span class="k"></span><div class="bar"><i></i></div><span class="v"></span>';
        box.append(row);
      });
    }
    disks.forEach((d, i) => {
      const row = box.children[i];
      row.querySelector(".k").textContent = d.name;
      const fill = row.querySelector("i");
      fill.style.width = `${d.usedPct}%`;
      fill.className = level(d.usedPct, 80, 92);
      row.querySelector(".v").textContent = `${d.freeGb.toLocaleString()} GB free`;
    });
  }

  async function refreshStats() {
    if (!BASE || (!visible("system") && !visible("storage"))) return;
    try {
      const s = await api("/stats");
      if (s.cpu != null) {
        bar("cpuBar", s.cpu, level(s.cpu, 75, 90));
        $("cpuVal").textContent = `${Math.round(s.cpu)}%`;
      }
      bar("ramBar", s.ram, level(s.ram, 80, 90));
      $("ramVal").textContent = `${s.ramUsedGb} / ${Math.round(s.ramTotalGb)} GB`;
      const g = s.gpu;
      const gpuRow = $("gpuVal").closest(".stat"), vramRow = $("vramVal").closest(".stat");
      gpuRow.classList.toggle("na", !g);
      vramRow.classList.toggle("na", !g);
      if (g) {
        bar("gpuBar", g.util, level(g.temp || 0, 80, 87));
        $("gpuVal").textContent = `${g.util ?? "–"}% · ${g.temp ?? "–"}°C`;
        bar("vramBar", g.memTotalGb ? (g.memUsedGb / g.memTotalGb) * 100 : 0);
        $("vramVal").textContent = `${g.memUsedGb} / ${Math.round(g.memTotalGb)} GB`;
      }
      renderDisks(s.disks || []);
    } catch { /* the items poll reports offline state */ }
  }

  // ================================================================ weather
  const WX_TEXT = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with hail",
  };
  const CLOUD = '<path class="cloud" d="M7.2 19h9.6a4.2 4.2 0 0 0 .7-8.35 5.6 5.6 0 0 0-10.8 1.1A3.65 3.65 0 0 0 7.2 19Z"/>';
  const SUN = '<g class="sun"><g class="sun-rays"><path d="M12 1.5v2.2M12 20.3v2.2M4.6 4.6l1.5 1.5M17.9 17.9l1.5 1.5M1.5 12h2.2M20.3 12h2.2M4.6 19.4l1.5-1.5M17.9 6.1l1.5-1.5"/></g><circle cx="12" cy="12" r="4.6"/></g>';
  const SMALL_SUN = '<g class="sun"><g class="sun-rays"><path d="M8 1.6v1.6M2.2 7.6h1.6M3.9 3.5 5 4.6M12.1 3.5 11 4.6"/></g><circle cx="8" cy="7.6" r="3"/></g>';
  const SMALL_MOON = '<path class="moon" d="M12 9.2A4.6 4.6 0 1 1 6.8 3a3.7 3.7 0 0 0 5.2 6.2Z"/>';
  const LOW_CLOUD = `<g transform="translate(0 -3)">${CLOUD}</g>`;
  const WX_ART = {
    sun: SUN,
    moon: '<path class="moon" d="M20 14.6A8.2 8.2 0 1 1 9.4 4a6.6 6.6 0 0 0 10.6 10.6Z"/>',
    partly: `${SMALL_SUN}<g transform="translate(2.4 1.6) scale(.9)">${CLOUD}</g>`,
    partlyNight: `${SMALL_MOON}<g transform="translate(2.4 1.6) scale(.9)">${CLOUD}</g>`,
    cloud: CLOUD,
    fog: '<g class="fog"><path d="M4 8h16M2.5 12h19M5 16h14M7 20h10"/></g>',
    drizzle: `${LOW_CLOUD}<g class="drizzle"><path class="drop" d="M8.5 19v1.2"/><path class="drop" d="M12.5 19.5v1.2"/><path class="drop" d="M16.5 19v1.2"/></g>`,
    rain: `${LOW_CLOUD}<g class="rain"><path class="drop" d="M9 19l-1 2.6"/><path class="drop" d="M13 19l-1 2.6"/><path class="drop" d="M17 19l-1 2.6"/></g>`,
    snow: `${LOW_CLOUD}<g class="snow"><path class="drop" d="M8.5 20.5h.01"/><path class="drop" d="M12.5 21.5h.01"/><path class="drop" d="M16.5 20.5h.01"/></g>`,
    storm: `${LOW_CLOUD}<path class="bolt" d="M12.8 15.5 10.2 19.6h3.3L11.2 23.5"/>`,
  };

  function wxKind(code, day) {
    if (code === 0) return day ? "sun" : "moon";
    if (code <= 2) return day ? "partly" : "partlyNight";
    if (code === 3) return "cloud";
    if (code === 45 || code === 48) return "fog";
    if (code >= 51 && code <= 57) return "drizzle";
    if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) return "rain";
    if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "snow";
    if (code >= 95) return "storm";
    return "cloud";
  }
  const wxSvg = (code, day) => svg(WX_ART[wxKind(code, day)], "wx");

  function hourLabel(hhmm) {
    if (S.clock24) return hhmm;
    const h = Number(hhmm.slice(0, 2));
    return `${h % 12 || 12} ${h < 12 ? "AM" : "PM"}`;
  }

  function renderWeather(d) {
    S.weather = d;
    const icon = $("wIcon");
    icon.textContent = "";
    icon.append(wxSvg(d.now.code, d.now.day));
    $("wTemp").textContent = `${d.now.temp}°`;
    $("wDesc").textContent = WX_TEXT[d.now.code] || "";
    $("wPlace").textContent = d.place;
    const today = d.daily[0] || {};
    $("wMeta").textContent = "";
    [`H ${today.hi}° · L ${today.lo}°`, `Feels like ${d.now.feels}° · ${d.now.humidity}%`, `Wind ${d.now.wind} ${d.windUnit}`]
      .forEach((line) => { const div = document.createElement("div"); div.textContent = line; $("wMeta").append(div); });

    const hours = $("wHours");
    hours.textContent = "";
    d.hourly.slice(0, 8).forEach((h) => {
      const cell = document.createElement("div");
      cell.className = "w-h";
      const t = document.createElement("span"); t.textContent = hourLabel(h.t);
      const temp = document.createElement("b"); temp.textContent = `${h.temp}°`;
      const pop = document.createElement("span"); pop.className = "pop"; pop.textContent = h.pop >= 20 ? `${h.pop}%` : "";
      cell.append(t, wxSvg(h.code, h.day), temp, pop);
      hours.append(cell);
    });

    const days = $("wDays");
    days.textContent = "";
    const list = d.daily.slice(0, 5);
    const min = Math.min(...list.map((x) => x.lo)), max = Math.max(...list.map((x) => x.hi));
    const span = Math.max(1, max - min);
    list.forEach((x, i) => {
      const [y, m, dd] = x.date.split("-").map(Number);
      const row = document.createElement("div");
      row.className = "w-d";
      const name = document.createElement("span");
      name.textContent = i === 0 ? "Today" : new Date(y, m - 1, dd).toLocaleDateString(undefined, { weekday: "short" });
      const lo = document.createElement("span"); lo.className = "lo"; lo.textContent = `${x.lo}°`;
      const range = document.createElement("div"); range.className = "range";
      const fill = document.createElement("i");
      fill.style.left = `${((x.lo - min) / span) * 100}%`;
      fill.style.width = `${Math.max(4, ((x.hi - x.lo) / span) * 100)}%`;
      range.append(fill);
      const hi = document.createElement("span"); hi.className = "hi"; hi.textContent = `${x.hi}°`;
      row.append(name, wxSvg(x.code, 1), lo, range, hi);
      days.append(row);
    });
  }

  async function refreshWeather() {
    clearTimeout(S.timers.wxRetry);
    if (!BASE || !visible("weather")) return;
    try {
      const res = await api("/weather");
      if (res.data) renderWeather(res.data);
      // helper is fetching fresh data now: ask again shortly
      if (!res.data || res.refreshing) S.timers.wxRetry = setTimeout(refreshWeather, res.data ? 6000 : 4000);
    } catch { S.timers.wxRetry = setTimeout(refreshWeather, 15000); } // helper not up yet (e.g. just after login)
  }

  // ================================================================ markets
  function fmtPrice(p) {
    if (p == null) return "–";
    const digits = p >= 10000 ? 0 : p >= 1 ? 2 : 4;
    return p.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function sparkSvg(points) {
    const W = 96, H = 30;
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", `0 0 ${W} ${H}`);
    s.setAttribute("preserveAspectRatio", "none");
    if (!points || points.length < 2) return s;
    const min = Math.min(...points), max = Math.max(...points), span = max - min || 1;
    const xy = points.map((v, i) => `${((i / (points.length - 1)) * W).toFixed(1)},${(H - 2 - ((v - min) / span) * (H - 4)).toFixed(1)}`);
    s.innerHTML = `<path d="M0,${H} L${xy.join(" L")} L${W},${H} Z"/><polyline points="${xy.join(" ")}"/>`;
    return s;
  }

  function renderMarkets(data) {
    $("mkUpdated").textContent = data.updated ? `Updated ${data.updated}` : "";
    const box = $("quotes");
    box.textContent = "";
    for (const q of data.quotes) {
      const row = document.createElement("div");
      row.className = "q";
      const name = document.createElement("div");
      name.className = "q-name";
      const b = document.createElement("b"); b.textContent = q.name;
      const small = document.createElement("small");
      const price = document.createElement("div"); price.className = "q-price";
      const chg = document.createElement("div"); chg.className = "q-chg";
      name.append(b, small);
      if (q.error || q.price == null) {
        row.classList.add("err");
        small.textContent = "Unavailable";
        row.append(name, sparkSvg(null), price, chg);
        box.append(row);
        continue;
      }
      const up = (q.change ?? 0) >= 0;
      row.classList.add(up ? "up" : "down");
      if (q.open) row.classList.add("open");
      small.textContent = q.open ? "Open" : "Closed";
      price.textContent = fmtPrice(q.price);
      chg.textContent = q.change == null ? "–" : `${up ? "+" : ""}${q.change.toFixed(2)}%`;
      row.append(name, sparkSvg(q.spark), price, chg);
      // brief flash when the price moved since the last update
      const prev = S.prevPrices.get(q.symbol);
      if (prev != null && prev !== q.price) {
        row.classList.add(q.price > prev ? "tick-up" : "tick-down");
        setTimeout(() => row.classList.remove("tick-up", "tick-down"), 1400);
      }
      S.prevPrices.set(q.symbol, q.price);
      box.append(row);
    }
  }

  async function refreshMarkets() {
    clearTimeout(S.timers.mkRetry);
    if (!BASE || !visible("markets")) return;
    try {
      const res = await api("/markets");
      if (res.data) renderMarkets(res.data);
      if (!res.data || res.refreshing) S.timers.mkRetry = setTimeout(refreshMarkets, res.data ? 6000 : 4000);
    } catch { S.timers.mkRetry = setTimeout(refreshMarkets, 15000); }
  }

  // ================================================================ calendar
  const C = { month: null, selected: null, data: null, dayKey: "" };
  const dayKey = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const sameDay = (a, b) => dayKey(a) === dayKey(b);

  function eventsByDay(events) {
    const map = new Map();
    for (const ev of events) {
      const s = new Date(ev.start), e = new Date(ev.end);
      const d = new Date(s.getFullYear(), s.getMonth(), s.getDate());
      // an event shows on every day it covers (all-day end dates are exclusive)
      for (let i = 0; i < 62; i++) {
        const k = dayKey(d);
        if (!map.has(k)) map.set(k, []);
        map.get(k).push(ev);
        d.setDate(d.getDate() + 1);
        if (d >= e) break;
      }
    }
    return map;
  }

  function timeLabel(date) {
    return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", hour12: !S.clock24 });
  }

  function eventRow(ev, showDay) {
    const s = new Date(ev.start);
    const row = document.createElement("div");
    row.className = "ev";
    if (ev.color) row.style.setProperty("--evc", ev.color);
    const bar = document.createElement("div"); bar.className = "bar";
    const body = document.createElement("div");
    const when = document.createElement("div"); when.className = "when";
    const today = new Date(), tomorrow = new Date(); tomorrow.setDate(today.getDate() + 1);
    const dayText = sameDay(s, today) ? "Today" : sameDay(s, tomorrow) ? "Tomorrow"
      : s.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
    const timeText = ev.allDay ? "All day" : `${timeLabel(s)} – ${timeLabel(new Date(ev.end))}`;
    when.textContent = showDay ? `${dayText} · ${timeText}` : timeText;
    const what = document.createElement("div"); what.className = "what"; what.textContent = ev.title;
    body.append(when, what);
    if (ev.location) { const where = document.createElement("div"); where.className = "where"; where.textContent = ev.location; body.append(where); }
    row.append(bar, body);
    return row;
  }

  function renderCalendar(slide) {
    const now = new Date();
    if (!C.month) C.month = new Date(now.getFullYear(), now.getMonth(), 1);
    C.dayKey = dayKey(now);
    const data = C.data;
    const weekStart = data && data.weekStart === "sunday" ? 0 : 1;
    const byDay = eventsByDay((data && data.events) || []);

    $("calTitle").textContent = C.month.toLocaleDateString(undefined, { month: "long", year: "numeric" });
    const dow = $("calDow");
    dow.textContent = "";
    for (let i = 0; i < 7; i++) {
      const d = new Date(2026, 1, 1 + ((i + weekStart) % 7)); // Feb 1 2026 is a Sunday
      const span = document.createElement("span");
      span.textContent = d.toLocaleDateString(undefined, { weekday: "narrow" });
      dow.append(span);
    }

    const grid = $("calGrid");
    grid.textContent = "";
    const first = new Date(C.month);
    const offset = (first.getDay() - weekStart + 7) % 7;
    const d = new Date(first.getFullYear(), first.getMonth(), 1 - offset);
    for (let i = 0; i < 42; i++) {
      const cell = document.createElement("button");
      cell.className = "cal-day";
      cell.textContent = d.getDate();
      if (d.getMonth() !== C.month.getMonth()) cell.classList.add("other");
      if (sameDay(d, now)) cell.classList.add("today");
      if (C.selected && sameDay(d, C.selected)) cell.classList.add("selected");
      const evs = byDay.get(dayKey(d));
      if (evs) {
        const dots = document.createElement("span");
        dots.className = "dots";
        evs.slice(0, 3).forEach((ev) => { const i = document.createElement("i"); if (ev.color) i.style.setProperty("--dot", ev.color); dots.append(i); });
        cell.append(dots);
      }
      const date = new Date(d);
      cell.addEventListener("click", () => {
        C.selected = C.selected && sameDay(C.selected, date) ? null : date; // tap again to go back to "Up next"
        if (date.getMonth() !== C.month.getMonth()) {
          const dir = date > C.month ? "next" : "prev";
          C.month = new Date(date.getFullYear(), date.getMonth(), 1);
          return renderCalendar(dir);
        }
        renderCalendar();
      });
      grid.append(cell);
      d.setDate(d.getDate() + 1);
    }
    if (slide) {
      grid.classList.remove("slide-next", "slide-prev");
      void grid.offsetWidth;
      grid.classList.add(`slide-${slide}`);
    }

    const list = $("calEvents");
    list.textContent = "";
    if (data && !data.configured) {
      $("calListHead").textContent = "Events";
      const p = document.createElement("div");
      p.className = "cal-empty";
      p.innerHTML = "Connect a calendar to see events here: add its private iCal link under <code>calendars</code> in config.json.";
      list.append(p);
      return;
    }
    let shown;
    if (C.selected) {
      $("calListHead").textContent = C.selected.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
      shown = byDay.get(dayKey(C.selected)) || [];
      shown.forEach((ev) => list.append(eventRow(ev, false)));
    } else {
      $("calListHead").textContent = "Up next";
      shown = ((data && data.events) || []).filter((ev) => new Date(ev.end) > now).slice(0, 5);
      shown.forEach((ev) => list.append(eventRow(ev, true)));
    }
    if (!shown.length) {
      const p = document.createElement("div");
      p.className = "cal-empty";
      p.textContent = !data ? "Loading events…" : C.selected ? "Nothing planned." : "Nothing coming up.";
      list.append(p);
    }
    if (data && data.errors && data.errors.length) {
      const p = document.createElement("div");
      p.className = "cal-empty";
      p.textContent = `Couldn't load: ${data.errors.join(", ")}`;
      list.append(p);
    }
  }

  $("calPrev").addEventListener("click", () => { C.month = new Date(C.month.getFullYear(), C.month.getMonth() - 1, 1); renderCalendar("prev"); });
  $("calNext").addEventListener("click", () => { C.month = new Date(C.month.getFullYear(), C.month.getMonth() + 1, 1); renderCalendar("next"); });
  $("calToday").addEventListener("click", () => {
    const now = new Date();
    const back = C.month.getFullYear() !== now.getFullYear() || C.month.getMonth() !== now.getMonth();
    const dir = C.month < now ? "next" : "prev";
    C.month = new Date(now.getFullYear(), now.getMonth(), 1);
    C.selected = null;
    renderCalendar(back ? dir : null);
  });

  async function refreshCalendar() {
    clearTimeout(S.timers.calRetry);
    if (!visible("calendar")) return;
    if (!BASE) return renderCalendar();
    try {
      const res = await api("/calendar");
      if (res.data) { C.data = res.data; G.calendar = res.data.google || "off"; renderGoogle(); renderCalendar(); }
      else renderCalendar();
      if (!res.data || res.refreshing) S.timers.calRetry = setTimeout(refreshCalendar, res.data ? 6000 : 4000);
    } catch { renderCalendar(); S.timers.calRetry = setTimeout(refreshCalendar, 15000); }
  }

  // ================================================================ to-do
  const T = { rev: "", items: [], seen: new Set(), fastUntil: 0 };

  function renderTodos() {
    const list = $("todoList");
    list.textContent = "";
    const items = [...T.items].sort((a, b) => (a.done - b.done) || (a.done ? (b.doneAt || 0) - (a.doneAt || 0) : 0));
    const open = items.filter((i) => !i.done).length;
    $("todoCount").textContent = items.length ? `${open} left` : "";
    $("todoClear").hidden = !items.some((i) => i.done);
    if (!items.length) {
      const p = document.createElement("div");
      p.className = "todo-empty";
      p.textContent = "Nothing to do. Add a task below.";
      list.append(p);
    }
    for (const it of items) {
      const row = document.createElement("div");
      row.className = "todo-item" + (it.done ? " done" : "");
      if (T.rendered && !T.seen.has(it.id)) row.classList.add("new");
      const check = document.createElement("button");
      check.className = "check";
      check.setAttribute("aria-label", it.done ? "Mark as not done" : "Mark as done");
      check.append(svg('<path d="M5 12.5l4.5 4.5L19 7.5"/>'));
      const text = document.createElement("div");
      text.className = "text";
      text.textContent = it.text;
      const del = document.createElement("button");
      del.className = "del";
      del.setAttribute("aria-label", "Delete task");
      del.append(svg(GLYPHS.close));
      check.addEventListener("click", () => {
        row.classList.toggle("done"); // instant feedback; the list re-sorts when the helper confirms
        todoAction("toggle", { id: it.id }, 350);
      });
      del.addEventListener("click", () => {
        row.classList.add("leaving");
        todoAction("delete", { id: it.id }, 280);
      });
      row.append(check, text, del);
      list.append(row);
    }
    T.items.forEach((i) => T.seen.add(i.id));
    T.rendered = true;
  }

  async function todoAction(action, body, delay = 0) {
    try {
      const data = await api(`/todos/${action}`, body);
      setTimeout(() => { T.rev = String(data.rev); T.items = data.items; renderTodos(); }, delay);
    } catch { setOnline(false); }
  }

  // ---- Google sync status: "ok" | "expired" | "off" (from /todos and /calendar)
  const G = { todo: "off", calendar: "off" };

  function renderGoogle() {
    const expired = G.todo === "expired" || G.calendar === "expired";
    document.querySelectorAll(".google-reconnect").forEach((b) => { b.hidden = !expired; });
  }

  document.querySelectorAll(".google-reconnect").forEach((b) => b.addEventListener("click", async () => {
    if (S.editing || !BASE) return;
    b.textContent = "Opening Google sign-in…";
    try { await api("/google/connect", {}); } catch { setOnline(false); }
    T.fastUntil = Date.now() + 180000;
    setTimeout(() => { b.textContent = "Reconnect Google"; refreshCalendar(); }, 60000);
  }));

  async function refreshTodos() {
    if (!BASE || !visible("todo")) return;
    try {
      const data = await api(`/todos?rev=${T.rev}`);
      if (data.unchanged) return;
      T.rev = String(data.rev);
      T.items = data.items || [];
      G.todo = data.google || "off";
      renderGoogle();
      renderTodos();
      $("todoCount").textContent += data.source === "google" && T.items.length ? " · Google Tasks" : "";
    } catch { /* offline handled elsewhere */ }
  }

  $("todoAdd").addEventListener("click", async () => {
    if (S.editing || !BASE) return;
    // open the typing box right over this widget (in physical screen pixels)
    const r = $("todoAdd").closest(".widget").getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    try {
      await api("/todos/prompt", {
        x: Math.round((window.screenX + r.left) * dpr),
        y: Math.round((window.screenY + r.top) * dpr),
        w: Math.round(r.width * dpr),
      });
      T.fastUntil = Date.now() + 120000; // check for the new task often for the next 2 minutes
    } catch { setOnline(false); }
  });

  // ================================================================ AI chat (Ollama on this PC, via the helper)
  const CH = { rev: "", state: null, count: 0, fastUntil: 0, nodes: [] };

  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // small, safe Markdown: code blocks, inline code, bold/italic, lists, paragraphs (input is escaped first)
  function md(text) {
    const parts = text.split(/```[^\n]*\n?([\s\S]*?)(?:```|$)/);
    return parts.map((part, i) => {
      if (i % 2) return `<pre><code>${esc(part.replace(/\n$/, ""))}</code></pre>`;
      const inline = (s) => esc(s)
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
        .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>");
      return part.split(/\n{2,}/).filter((b) => b.trim()).map((block) => {
        const lines = block.split("\n");
        if (lines.every((l) => /^\s*([-*•]|\d+[.)])\s+/.test(l) || !l.trim())) {
          const ordered = /^\s*\d/.test(lines[0]);
          const items = lines.filter((l) => l.trim()).map((l) => `<li>${inline(l.replace(/^\s*([-*•]|\d+[.)])\s+/, ""))}</li>`);
          return ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`;
        }
        return `<p>${lines.map((l) => inline(l.replace(/^#+\s*/, ""))).join("<br>")}</p>`;
      }).join("");
    }).join("");
  }

  // commands that change or remove things get a red warning before you click Run
  const RISKY = /\b(remove-item|rm|rmdir|del|erase|rd|format|diskpart|reg\s+delete|remove-itemproperty|set-itemproperty|stop-process|kill|taskkill|shutdown|restart-computer|stop-computer|uninstall|clear-content|set-content|out-file|move-item|rename-item|bcdedit|cipher|takeown|icacls)\b/i;

  function actionCard(a) {
    const card = document.createElement("div");
    card.className = `action ${a.status}`;
    const head = document.createElement("div");
    head.className = "action-head";
    const isCmd = a.name === "run_command";
    const title = isCmd ? "Run in PowerShell" : "Open Claude Code";
    head.textContent = title;
    card.append(head);
    const args = a.args || {};
    if (isCmd && args.reason) {
      const why = document.createElement("div");
      why.className = "action-why";
      why.textContent = args.reason;
      card.append(why);
    }
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = isCmd ? args.command || "" :
      `Folder: ${args.folder || "home folder"}${args.prompt ? `\nPrompt: ${args.prompt}` : ""}`;
    pre.append(code);
    card.append(pre);
    if (a.status === "pending") {
      if (isCmd && RISKY.test(args.command || "")) {
        const warn = document.createElement("div");
        warn.className = "action-warn";
        warn.textContent = "⚠ This changes or removes things on your PC. Check it carefully.";
        card.append(warn);
      }
      const row = document.createElement("div");
      row.className = "action-btns";
      const run = document.createElement("button");
      run.className = "pill add";
      run.textContent = isCmd ? "Run" : "Open";
      const no = document.createElement("button");
      no.className = "pill";
      no.textContent = isCmd ? "Don't run" : "Cancel";
      run.addEventListener("click", () => { if (!S.editing) { row.remove(); chatAction("decide", { id: a.id, approve: true }); } });
      no.addEventListener("click", () => { if (!S.editing) { row.remove(); chatAction("decide", { id: a.id, approve: false }); } });
      row.append(run, no);
      card.append(row);
    } else {
      const status = document.createElement("div");
      status.className = "action-status";
      status.textContent = { running: "Running…", done: "✓ Done", error: `✕ Failed${a.exit != null && a.exit !== -1 ? ` (exit ${a.exit})` : ""}`, declined: "Not run" }[a.status] || a.status;
      card.append(status);
      if (a.output && a.status !== "declined" && (isCmd || a.status === "error")) {
        const out = document.createElement("pre");
        out.className = "action-out";
        out.textContent = a.output;
        card.append(out);
      }
    }
    return card;
  }

  function renderChat() {
    const st = CH.state;
    if (!st) return;
    const log = $("chatLog");
    const msgs = st.messages || [];
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    if (msgs.length < CH.nodes.length || !msgs.length) { log.textContent = ""; CH.nodes = []; }
    if (!msgs.length) {
      const p = document.createElement("div");
      p.className = "chat-empty";
      p.textContent = "Ask me anything. Everything stays on this PC.";
      log.append(p);
    }
    msgs.forEach((m, i) => {
      let el = CH.nodes[i];
      if (!el) {
        el = document.createElement("div");
        el.className = `msg ${m.role}`;
        if (!CH.count) el.classList.add("settled"); // no pop-in for history on first load
        log.append(el);
        CH.nodes[i] = el;
        el._text = null;
      }
      const live = st.busy && i === msgs.length - 1 && m.role === "assistant";
      const acts = m.actions || [];
      const key = (live && !m.content ? `…${!!st.thinking}` : m.content) + JSON.stringify(acts.map((a) => [a.status, a.output]));
      if (el._text !== key) {
        el._text = key;
        if (live && !m.content) {
          el.innerHTML = '<span class="typing-dots"><i></i><i></i><i></i></span>' +
            (st.thinking ? '<span class="thinking">Thinking…</span>' : "");
        }
        else el.innerHTML = m.role === "user" ? esc(m.content).replace(/\n/g, "<br>") : md(m.content);
        acts.forEach((a) => el.append(actionCard(a)));
        el.classList.toggle("has-actions", acts.length > 0);
      }
      el.classList.toggle("typing", live && !!m.content);
    });
    CH.count = msgs.length || 1;
    if (nearBottom || st.busy) log.scrollTop = log.scrollHeight;
    $("chatStop").hidden = !st.busy;
    $("chatClear").hidden = st.busy || !msgs.length;
    $("chatAsk").textContent = msgs.length ? "Reply…" : "Ask something…";
    $("chatModel").textContent = `${st.model} · on this PC`;
    const err = $("chatError");
    err.hidden = !st.error;
    err.textContent = st.error ? `⚠ ${st.error}` : "";
  }

  async function refreshChat() {
    if (!BASE || !visible("chat")) return;
    try {
      const data = await api(`/chat?rev=${CH.rev}`);
      if (data.unchanged) return;
      CH.rev = String(data.rev);
      CH.state = data;
      if (data.busy || data.awaiting) CH.fastUntil = Date.now() + 5000; // stream the reply in smoothly
      renderChat();
    } catch { /* offline handled elsewhere */ }
  }

  async function chatAction(action, body = {}) {
    try {
      const data = await api(`/chat/${action}`, body);
      if (data.rev !== undefined) { CH.rev = String(data.rev); CH.state = data; renderChat(); }
    } catch { setOnline(false); }
  }

  $("chatAsk").addEventListener("click", async () => {
    if (S.editing || !BASE) return;
    const r = $("chatAsk").closest(".widget").getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    try {
      await api("/chat/prompt", {
        x: Math.round((window.screenX + r.left) * dpr),
        y: Math.round((window.screenY + r.bottom + 10) * dpr),
        w: Math.round(r.width * dpr),
      });
      CH.fastUntil = Date.now() + 120000;
    } catch { setOnline(false); }
  });
  $("chatStop").addEventListener("click", () => { if (!S.editing) chatAction("stop"); });
  $("chatClear").addEventListener("click", () => { if (!S.editing) chatAction("clear"); });
  $("todoClear").addEventListener("click", () => {
    document.querySelectorAll(".todo-item.done").forEach((row) => row.classList.add("leaving"));
    todoAction("clear", {}, 280);
  });

  // ================================================================ media (Wallpaper Engine)
  function initMedia() {
    const m = $("media");
    document.querySelectorAll("[data-media]").forEach((b) => b.addEventListener("click", async () => {
      if (S.editing) return;
      const action = b.dataset.media;
      if (action === "playpause" && !m.classList.contains("idle")) m.classList.toggle("playing");
      try { await api("/media", { action }); } catch { setOnline(false); }
    }));

    if (typeof window.wallpaperRegisterMediaPropertiesListener !== "function") return;
    window.wallpaperRegisterMediaPropertiesListener((e) => {
      const has = !!e.title;
      $("mtitle").textContent = e.title || "Nothing playing";
      $("martist").textContent = e.artist || (has ? "" : "Play something in Spotify");
      m.classList.toggle("idle", !has);
    });
    window.wallpaperRegisterMediaThumbnailListener((e) => {
      const art = $("art");
      art.style.backgroundImage = e.thumbnail ? `url("${e.thumbnail}")` : "";
      art.classList.toggle("has-art", !!e.thumbnail);
      m.style.setProperty("--tint", e.primaryColor || "transparent");
    });
    window.wallpaperRegisterMediaPlaybackListener((e) => {
      const P = window.wallpaperMediaIntegration || {};
      m.classList.toggle("playing", e.state === (P.PLAYBACK_PLAYING ?? 1));
      if (e.state === (P.PLAYBACK_STOPPED ?? 0)) m.classList.add("idle");
    });
  }

  // ================================================================ motion engine
  // One requestAnimationFrame loop that runs only while something is moving
  // (cursor recently moved, music playing, or values still settling) and then
  // stops itself. When idle, only the CSS ambient drift is running.
  const M = {
    tx: 0.5, ty: 0.5, x: 0.5, y: 0.5,      // parallax target / smoothed (0..1 of screen)
    gtx: 0, gty: 0, gx: 0, gy: 0,          // glow target / smoothed (px)
    audio: 0, e: 0, lastAudio: -1e9,       // bass level, smoothed pulse
    running: false, glowTimer: 0,
  };
  const layers = [...document.querySelectorAll(".layer")].map((el) => ({ el, depth: Number(el.dataset.depth) }));
  const glow = $("glow");

  function kick() {
    if (M.running || S.paused) return;
    M.running = true;
    requestAnimationFrame(frame);
  }

  function frame(now) {
    M.x += (M.tx - M.x) * 0.05;
    M.y += (M.ty - M.y) * 0.05;
    M.gx += (M.gtx - M.gx) * 0.18;
    M.gy += (M.gty - M.gy) * 0.18;
    const audioLive = now - M.lastAudio < 250;
    const target = audioLive ? M.audio : 0;
    M.e += (target - M.e) * (target > M.e ? 0.55 : 0.1); // fast attack, slow release

    const ox = M.x - 0.5, oy = M.y - 0.5;
    for (const L of layers) {
      const pulse = 1 + M.e * (L.depth > 0 ? 0.07 : 0.045);
      L.el.style.transform = `translate3d(${(ox * L.depth).toFixed(1)}px, ${(oy * L.depth).toFixed(1)}px, 0) scale(${pulse.toFixed(4)})`;
    }
    glow.style.transform = `translate3d(${M.gx.toFixed(1)}px, ${M.gy.toFixed(1)}px, 0)`;

    const settling = Math.abs(M.tx - M.x) + Math.abs(M.ty - M.y) > 0.0005
      || Math.abs(M.gtx - M.gx) + Math.abs(M.gty - M.gy) > 0.6
      || audioLive || M.e > 0.002;
    if (settling && !S.paused) requestAnimationFrame(frame);
    else M.running = false;
  }

  window.addEventListener("pointermove", (e) => {
    if (!S.interactive) return;
    M.tx = e.clientX / window.innerWidth;
    M.ty = e.clientY / window.innerHeight;
    M.gtx = e.clientX;
    M.gty = e.clientY;
    if (!glow.classList.contains("on")) { M.gx = M.gtx; M.gy = M.gty; } // appear under the cursor, don't fly in
    glow.classList.add("on");
    clearTimeout(M.glowTimer);
    M.glowTimer = setTimeout(() => glow.classList.remove("on"), 2500);
    kick();
  }, { passive: true });

  window.addEventListener("pointerdown", (e) => {
    if (!S.interactive || e.button !== 0) return;
    const r = document.createElement("div");
    r.className = "ripple";
    r.style.left = `${e.clientX}px`;
    r.style.top = `${e.clientY}px`;
    r.addEventListener("animationend", () => r.remove());
    $("ripples").append(r);
  });

  function resetMotion() {
    layers.forEach((L) => { L.el.style.transform = ""; });
    glow.classList.remove("on");
    M.tx = M.ty = M.x = M.y = 0.5;
    M.e = 0;
  }

  // ---- music: background pulses with the bass, equalizer in the music widget
  const VIZ_BARS = 28;
  const vizBars = [];
  for (let i = 0; i < VIZ_BARS; i++) { const b = document.createElement("i"); $("viz").append(b); vizBars.push(b); }
  let vizSilent = true;

  function initAudio() {
    if (typeof window.wallpaperRegisterAudioListener !== "function") return;
    window.wallpaperRegisterAudioListener((a) => {
      if (!S.audioReact || S.paused) return;
      // a = 128 values: 0-63 left channel, 64-127 right, low to high frequencies
      let total = 0;
      for (let i = 0; i < 128; i++) total += a[i];
      if (total < 0.02) {
        if (!vizSilent) { vizBars.forEach((b) => { b.style.transform = "scaleY(.04)"; }); vizSilent = true; }
        return;
      }
      vizSilent = false;
      let bass = 0;
      for (let i = 0; i < 5; i++) bass += a[i] + a[64 + i];
      M.audio = Math.min(1.2, bass / 10);
      M.lastAudio = performance.now();
      kick();
      for (let i = 0; i < VIZ_BARS; i++) {
        const k = Math.floor(Math.pow(i / VIZ_BARS, 1.6) * 60); // more bars for the lows, like the ear hears
        const v = Math.min(1, (a[k] + a[k + 1] + a[64 + k] + a[65 + k]) * 0.4);
        vizBars[i].style.transform = `scaleY(${Math.max(0.04, v).toFixed(3)})`;
      }
    });
  }

  // ---- dock magnification
  const dock = $("dock");
  let dockCenters = null;
  dock.addEventListener("pointerenter", () => {
    dock.classList.remove("calm");
    dockCenters = [...dock.children].map((t) => { const r = t.getBoundingClientRect(); return r.left + r.width / 2; });
  });
  dock.addEventListener("pointermove", (e) => {
    if (!S.interactive || !dockCenters) return;
    [...dock.children].forEach((t, i) => {
      const d = e.clientX - dockCenters[i];
      const s = 1 + 0.55 * Math.exp(-(d * d) / (2 * 85 * 85));
      t.firstElementChild.style.transform = `translateY(${(-(s - 1) * 22).toFixed(1)}px) scale(${s.toFixed(3)})`;
      t.style.margin = `0 ${((s - 1) * 26).toFixed(1)}px`;
      t.style.setProperty("--lift", `${((s - 1) * 64).toFixed(1)}px`);
    });
  });
  dock.addEventListener("pointerleave", () => {
    dockCenters = null;
    dock.classList.add("calm");
    [...dock.children].forEach((t) => {
      t.firstElementChild.style.transform = "";
      t.style.margin = "";
      t.style.removeProperty("--lift");
    });
  });

  // ================================================================ widget layout + edit mode
  const DEFAULT_LAYOUT = {
    cols: { farleft: ["calendar"], left: ["clock", "weather", "media"], right: ["markets", "system", "storage"], farright: ["todo", "chat"] },
    hidden: [],
  };
  const COLS = [...document.querySelectorAll(".col")];
  const colByName = (name) => COLS.find((c) => c.dataset.col === name);
  const WIDGETS = new Map([...document.querySelectorAll("#widgetStore .widget")].map((w) => [w.dataset.widget, w]));

  WIDGETS.forEach((w, id) => {
    const b = document.createElement("button");
    b.className = "remove-w";
    b.setAttribute("aria-label", `Hide ${w.dataset.title}`);
    b.append(svg(GLYPHS.close));
    b.addEventListener("click", (e) => { e.stopPropagation(); hideWidget(id); });
    w.append(b);
  });

  const widgetsIn = (col) => [...col.querySelectorAll(":scope > .widget")];

  function syncCols() {
    COLS.forEach((c) => c.classList.toggle("filled", !!c.querySelector(":scope > .widget, :scope > .placeholder")));
    renderTray();
  }

  function applyLayout(layout) {
    const placed = new Set();
    const hidden = new Set((layout.hidden || []).filter((id) => WIDGETS.has(id)));
    for (const col of COLS) {
      for (const id of (layout.cols && layout.cols[col.dataset.col]) || []) {
        if (!WIDGETS.has(id) || placed.has(id) || hidden.has(id)) continue;
        col.append(WIDGETS.get(id));
        placed.add(id);
      }
    }
    // widgets the saved layout doesn't know about (added in an update) go to their default column
    for (const [id, w] of WIDGETS) {
      if (placed.has(id) || hidden.has(id)) continue;
      const name = Object.keys(DEFAULT_LAYOUT.cols).find((k) => DEFAULT_LAYOUT.cols[k].includes(id)) || "right";
      colByName(name).append(w);
    }
    S.hidden = [...hidden];
    S.hidden.forEach((id) => $("widgetStore").append(WIDGETS.get(id)));
    syncCols();
  }

  function currentLayout() {
    return {
      cols: Object.fromEntries(COLS.map((c) => [c.dataset.col, widgetsIn(c).map((w) => w.dataset.widget)])),
      hidden: S.hidden,
    };
  }

  function saveLayout() { if (BASE) api("/layout", currentLayout()).catch(() => {}); }

  async function loadLayout() {
    applyLayout(DEFAULT_LAYOUT);
    setTimeout(() => WIDGETS.forEach((w) => w.classList.add("settled")), 1200);
    if (!BASE) return;
    try {
      const res = await api("/layout");
      if (res.layout) applyLayout(res.layout);
    } catch { /* keep defaults */ }
  }

  // smoothly animate widgets to their new places after a DOM change
  function flip(mutate) {
    const els = [...document.querySelectorAll(".col > .widget, .col > .placeholder")];
    const first = new Map(els.map((el) => [el, el.getBoundingClientRect()]));
    mutate();
    for (const el of els) {
      if (!el.isConnected || !el.offsetParent) continue;
      const a = first.get(el), b = el.getBoundingClientRect();
      const dx = a.left - b.left, dy = a.top - b.top;
      if (Math.abs(dx) + Math.abs(dy) < 1) continue;
      el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }],
        { duration: 300, easing: "cubic-bezier(.2, .9, .3, 1)" });
    }
  }

  function renderTray() {
    const box = $("trayItems");
    box.textContent = "";
    S.hidden.forEach((id) => {
      const b = document.createElement("button");
      b.className = "pill";
      b.textContent = WIDGETS.get(id).dataset.title;
      b.addEventListener("click", () => showWidget(id));
      box.append(b);
    });
  }

  function hideWidget(id) {
    flip(() => {
      $("widgetStore").append(WIDGETS.get(id));
      S.hidden = [...S.hidden.filter((x) => x !== id), id];
      syncCols();
    });
    saveLayout();
  }

  function showWidget(id) {
    const order = ["left", "right", "farleft", "farright"].map(colByName);
    const target = order.reduce((best, c) => (widgetsIn(c).length < widgetsIn(best).length ? c : best), order[0]);
    const w = WIDGETS.get(id);
    flip(() => {
      target.append(w);
      S.hidden = S.hidden.filter((x) => x !== id);
      syncCols();
    });
    w.animate([{ transform: "scale(.6)", opacity: 0 }, { transform: "none", opacity: 1 }],
      { duration: 450, easing: "cubic-bezier(.2, 1.35, .4, 1)" });
    saveLayout();
    if (id === "weather") refreshWeather();
    if (id === "markets") refreshMarkets();
    if (id === "system" || id === "storage") refreshStats();
    if (id === "calendar") refreshCalendar();
    if (id === "todo") refreshTodos();
  }

  function enterEdit() {
    if (S.editing) return;
    S.editing = true;
    document.body.classList.add("editing");
    $("tray").hidden = false;
    renderTray();
    closeFolder();
  }

  function exitEdit() {
    if (!S.editing) return;
    S.editing = false;
    document.body.classList.remove("editing");
    $("tray").hidden = true;
    syncCols();
    saveLayout();
  }

  $("done").addEventListener("click", exitEdit);

  // long-press a widget to enter edit mode; in edit mode, drag widgets around
  let press = null;
  document.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const w = e.target.closest(".col > .widget");
    if (S.editing) {
      if (w && !e.target.closest(".remove-w")) {
        e.preventDefault();
        press = { w, x: e.clientX, y: e.clientY, dragging: false };
      } else if (!e.target.closest(".tray, .col, .tile, .dock, .remove-w")) {
        exitEdit(); // tap on empty background
      }
      return;
    }
    if (w) {
      press = { w, x: e.clientX, y: e.clientY, dragging: false };
      press.timer = setTimeout(enterEdit, 550);
    }
  });

  window.addEventListener("pointermove", (e) => {
    if (!press) return;
    const dist = Math.hypot(e.clientX - press.x, e.clientY - press.y);
    if (!S.editing) {
      if (dist > 8) { clearTimeout(press.timer); press = null; }
      return;
    }
    if (!press.dragging) {
      if (dist < 6) return;
      beginDrag(e);
    }
    moveDrag(e);
  });

  window.addEventListener("pointerup", () => {
    if (!press) return;
    clearTimeout(press.timer);
    if (press.dragging) endDrag(press);
    press = null;
  });

  function beginDrag(e) {
    const w = press.w, r = w.getBoundingClientRect();
    const ph = document.createElement("div");
    ph.className = "placeholder";
    ph.style.height = `${r.height}px`;
    w.replaceWith(ph);
    document.body.append(w);
    w.classList.add("floating");
    w.style.width = `${r.width}px`;
    w.style.left = `${r.left}px`;
    w.style.top = `${r.top}px`;
    w.animate([{ transform: "scale(1)" }, { transform: "scale(1.04)" }], { duration: 180, fill: "forwards" });
    Object.assign(press, { dragging: true, ph, ox: e.clientX - r.left, oy: e.clientY - r.top });
  }

  function moveDrag(e) {
    const { w, ph } = press;
    w.style.left = `${e.clientX - press.ox}px`;
    w.style.top = `${e.clientY - press.oy}px`;
    // the column whose span is closest to the cursor
    let best = null, bestD = Infinity;
    for (const c of COLS) {
      const r = c.getBoundingClientRect();
      const d = e.clientX < r.left ? r.left - e.clientX : e.clientX > r.right ? e.clientX - r.right : 0;
      if (d < bestD) { bestD = d; best = c; }
    }
    if (!best || bestD > 320) return; // over the app grid: leave the placeholder where it is
    const before = widgetsIn(best).find((it) => {
      const r = it.getBoundingClientRect();
      return e.clientY < r.top + r.height / 2;
    }) || null;
    if (ph.parentNode === best && ph.nextElementSibling === before) return;
    flip(() => { best.insertBefore(ph, before); syncCols(); });
  }

  function endDrag(p) {
    const { w, ph } = p;
    const r = ph.getBoundingClientRect();
    const dx = r.left - parseFloat(w.style.left), dy = r.top - parseFloat(w.style.top);
    w.getAnimations().forEach((a) => a.cancel());
    const settle = w.animate(
      [{ transform: "scale(1.04)" }, { transform: `translate(${dx}px, ${dy}px) scale(1)` }],
      { duration: 240, easing: "cubic-bezier(.2, .9, .3, 1)" });
    settle.finished.catch(() => {}).then(() => {
      w.classList.remove("floating");
      w.style.width = w.style.left = w.style.top = "";
      ph.replaceWith(w);
      syncCols();
      saveLayout();
    });
  }

  // ================================================================ Wallpaper Engine settings + pausing
  const toRgb = (v) => {
    const [r, g, b] = String(v).split(" ").map(Number);
    return `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)})`;
  };

  window.wallpaperPropertyListener = {
    applyUserProperties(p) {
      if (p.accent) document.documentElement.style.setProperty("--accent", toRgb(p.accent.value));
      if (p.clock24) {
        S.clock24 = !!p.clock24.value;
        renderClock();
        if (S.weather) renderWeather(S.weather);
      }
      if (p.animate) document.body.classList.toggle("still", !p.animate.value);
      if (p.interactive) { S.interactive = !!p.interactive.value; if (!S.interactive) resetMotion(); }
      if (p.audioreact) { S.audioReact = !!p.audioreact.value; if (!S.audioReact) M.e = 0; }
      if (p.iconsize) {
        S.iconSize = Number(p.iconsize.value);
        document.documentElement.style.setProperty("--icon", `${S.iconSize}px`);
        if (S.rendered) renderGrid();
      }
      if (p.columns) { S.columns = Number(p.columns.value); if (S.rendered) renderGrid(); }
    },
    setPaused(paused) { S.paused = paused; paused ? stop() : start(); },
  };

  function start() {
    stop();
    scheduleClock();
    refreshItems();
    refreshStats();
    refreshWeather();
    refreshMarkets();
    refreshCalendar();
    refreshTodos();
    refreshChat();
    S.timers.items = setInterval(refreshItems, 4000);
    S.timers.stats = setInterval(refreshStats, 3000);
    S.timers.weather = setInterval(refreshWeather, 5 * 60 * 1000);
    S.timers.markets = setInterval(refreshMarkets, 60 * 1000);
    S.timers.calendar = setInterval(refreshCalendar, 5 * 60 * 1000);
    // to-dos: every 1.5 s right after "+ Add task" was used, otherwise every 6 s
    S.timers.todos = setInterval(() => { if (Date.now() < T.fastUntil || Date.now() % 6000 < 1500) refreshTodos(); }, 1500);
    // chat: ~3x a second while a reply is streaming or right after "Ask", otherwise every 6 s
    S.timers.chat = setInterval(() => { if (Date.now() < CH.fastUntil || (CH.state && CH.state.busy) || Date.now() % 6000 < 300) refreshChat(); }, 300);
  }

  function stop() {
    clearTimeout(S.timers.clock);
    clearTimeout(S.timers.wxRetry);
    clearTimeout(S.timers.mkRetry);
    clearTimeout(S.timers.calRetry);
    ["items", "stats", "weather", "markets", "calendar", "todos", "chat"].forEach((k) => clearInterval(S.timers[k]));
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else if (!S.paused) start();
  });

  loadLayout();
  initMedia();
  initAudio();
  start();
})();
