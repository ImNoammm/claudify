/* claudify loader. It runs in Claude Desktop's main process (packed into app.asar as
 * .vite/build/claudify.js and required by the main entry) and is also the preload of the
 * plugins window. Plugins are files under the config root: css/*.css is inserted into every
 * window, js/*.js runs in claude.ai's page. Their header comments declare metadata and
 * settings; docs/plugins.md describes the format. Nothing here may throw into Claude. */
"use strict";
(() => {
  if (process.env.CLAUDIFY_DISABLE === "1") return;

  if (process.type === "renderer") { // plugins-window preload (sandboxed): a single invoke channel
    try {
      const { contextBridge, ipcRenderer } = require("electron");
      contextBridge.exposeInMainWorld("claudify", {
        call: (op, ...args) => ipcRenderer.invoke("claudify", op, ...args),
        onChange: (fn) => ipcRenderer.on("claudify:changed", () => fn()),
      });
    } catch {}
    return;
  }

  let electron, fs, path, os, crypto;
  try {
    electron = require("electron"); fs = require("fs"); path = require("path");
    os = require("os"); crypto = require("crypto");
  } catch { return; }
  const { app, BrowserWindow, ipcMain, nativeTheme } = electron;
  if (!app) return;

  const cfgHome = process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config");
  const cssDir = process.env.CLAUDIFY_DIR || path.join(cfgHome, "claudify", "css");
  const root = path.dirname(cssDir);
  const jsDir = path.join(root, "js");
  const settingsFile = path.join(root, "settings.json");
  const logFile = path.join(root, "loader.log");
  const DIRS = { css: cssDir, js: jsDir };
  const filesDir = path.join(root, "files");

  const log = (msg) => {
    try { fs.appendFileSync(logFile, `${new Date().toISOString()} ${msg}\n`); } catch {}
  };
  const sha = (s) => crypto.createHash("sha256").update(s).digest("hex");
  const readJson = (f) => { try { return JSON.parse(fs.readFileSync(f, "utf8")); } catch { return {}; } };
  const writeJson = (f, v) => { const t = `${f}.${process.pid}.tmp`; fs.writeFileSync(t, JSON.stringify(v, null, 2) + "\n"); fs.renameSync(t, f); };
  const ls = (d) => { try { return fs.readdirSync(d).sort(); } catch { return []; } };
  const read = (f) => { try { return fs.readFileSync(f, "utf8"); } catch { return ""; } };

  // metadata and settings
  const META = /^\s*(?:\*|\/\/|\/\*)?\s*@(name|description|author|version|match|setting|group)\s+(.*?)\s*(?:\*\/)?\s*$/gm;
  // words, "quoted strings" (backslash escapes) and key="quoted values"
  const tokens = (s) => [...s.matchAll(/([\w-]+=)?"((?:[^"\\]|\\.)*)"|(\S+)/g)].map((m) => m[2] !== undefined ? (m[1] || "") + m[2].replace(/\\(.)/g, "$1") : m[3]);
  const parseMeta = (src) => {
    const meta = { settings: [] };
    let group, groupToggle;
    for (const [, k, v] of src.matchAll(META)) {
      if (k === "group") { const [label, ...rest] = tokens(v); group = label; groupToggle = (rest.find((t) => t.startsWith("toggle=")) || "").slice(7) || undefined; continue; }
      if (k !== "setting") { meta[k] ??= v; continue; }
      const [key, type, def = "", label = "", ...rest] = tokens(v);
      if (!/^[A-Za-z_][\w-]*$/.test(key || "") || !TYPES.has(type)) continue;
      const s = { key, type, label: label || key };
      if (group) s.group = group;
      if (groupToggle) s.groupToggle = groupToggle;
      for (const kv of rest) { const i = kv.indexOf("="); if (i > 0) s[kv.slice(0, i)] = kv.slice(i + 1); }
      for (const n of ["min", "max", "step"]) if (s[n] !== undefined) s[n] = Number(s[n]);
      if (type === "file") s.max = Math.min(32, s.max > 0 ? s.max : 8);
      if (type === "select") s.options = String(s.options || def).split("|").map((o) => { const [value, ...l] = o.split(":"); return { value, label: l.join(":") || value }; });
      s.default = type === "toggle" ? def === "true" : type === "range" || type === "number" ? Number(def) : def;
      s.default = validate(s, s.default, true);
      meta.settings.push(s);
    }
    return meta;
  };
  const IMAGE_MAX = 1.5 * 1048576;
  const TYPES = new Set(["color", "range", "number", "select", "toggle", "text", "file"]);
  const validate = (s, v, isDefault) => {
    const bad = () => { if (isDefault) return s.type === "color" ? "#000000" : s.type === "toggle" ? false : s.type === "select" ? s.options[0]?.value : s.type === "text" || s.type === "file" ? "" : 0; throw new Error(`invalid value for ${s.key}`); };
    switch (s.type) {
      case "color": return typeof v === "string" && /^#[0-9a-fA-F]{6}$/.test(v) ? v.toLowerCase() : bad();
      case "toggle": return typeof v === "boolean" ? v : bad();
      case "select": return s.options.some((o) => o.value === v) ? v : bad();
      case "text": return typeof v === "string" ? v.replace(/[{}<>;\n\r]/g, "").slice(0, 200) : bad();
      case "file": return v === "" || (typeof v === "string" && /^[\w-]+(\.[\w-]+)*$/.test(v)) ? v : bad();
      default: {
        const n = Number(v);
        if (typeof v === "boolean" || v === "" || !Number.isFinite(n)) return bad();
        return Math.min(s.max ?? Infinity, Math.max(s.min ?? -Infinity, n));
      }
    }
  };
  const valuesFor = (file, meta, all = readJson(settingsFile)) => {
    const saved = all[file] || {}, out = {};
    for (const s of meta.settings) {
      try { out[s.key] = s.key in saved ? validate(s, saved[s.key]) : s.default; } catch { out[s.key] = s.default; }
    }
    return out;
  };

  // file settings: <root>/files/<plugin>--<key>-<sha8>-<original name>.<ext>, served as data: URLs
  const MIME = { gif: "image/gif", png: "image/png", apng: "image/apng", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp",
    avif: "image/avif", svg: "image/svg+xml", bmp: "image/bmp", ico: "image/x-icon", woff: "font/woff", woff2: "font/woff2",
    ttf: "font/ttf", otf: "font/otf", mp3: "audio/mpeg", ogg: "audio/ogg", wav: "audio/wav", mp4: "video/mp4", webm: "video/webm" };
  const mimeOf = (name) => MIME[path.extname(name).slice(1).toLowerCase()] || "application/octet-stream";
  const accepts = (s, name) => {
    if (!s.accept) return true;
    const ext = path.extname(name).toLowerCase(), mime = mimeOf(name);
    return s.accept.split(",").map((a) => a.trim().toLowerCase()).some((a) =>
      a.startsWith(".") ? a === ext : a.endsWith("/*") ? mime.startsWith(a.slice(0, -1)) : a === mime);
  };
  const urlCache = new Map(); // stored name -> data: URL (names carry a content hash, so never stale)
  const dataUrl = (name) => {
    if (!name) return "";
    if (!urlCache.has(name)) {
      try { urlCache.set(name, `data:${mimeOf(name)};base64,${fs.readFileSync(path.join(filesDir, name)).toString("base64")}`); }
      catch { return ""; }
    }
    return urlCache.get(name);
  };
  const resolved = (p) => { // values as plugins see them: file settings become data: URLs
    const out = { ...p.values };
    for (const s of p.meta.settings) if (s.type === "file") out[s.key] = dataUrl(out[s.key]);
    return out;
  };
  const pruneFiles = (file, key, keep) => { // drop stored files of a plugin (or of one of its settings) except `keep`
    const pre = key ? `${file}--${key}-` : `${file}--`;
    for (const f of ls(filesDir)) if (f.startsWith(pre) && f !== keep) { fs.rmSync(path.join(filesDir, f), { force: true }); urlCache.delete(f); }
  };

  // colour helpers for {{key|hsl:±n}} / {{key|hex:±n}}
  const hexToHsl = (hex) => {
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
    const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2, d = max - min;
    let h = 0, s = 0;
    if (d) {
      s = d / (1 - Math.abs(2 * l - 1));
      h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
      h = (h * 60 + 360) % 360;
    }
    return [h, s * 100, l * 100];
  };
  const hslToHex = (h, s, l) => {
    s /= 100; l /= 100;
    const k = (n) => (n + h / 30) % 12, a = s * Math.min(l, 1 - l);
    const f = (n) => Math.round(255 * (l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1))));
    return "#" + [0, 8, 4].map((n) => f(n).toString(16).padStart(2, "0")).join("");
  };
  const r2 = (n) => Math.round(n * 100) / 100;
  const isLight = (hex) => /^#[0-9a-f]{6}$/i.test(hex) && hexToHsl(hex)[2] > 60;
  // "+4", "-lift", "+lift*1.5"; "~lift" moves away from the color's own brightness (lighter on dark, darker on light)
  const delta = (expr, vals, light) => {
    const m = /^\s*([+~-])?\s*([\w.-]+)\s*(?:\*\s*([\d.]+))?\s*$/.exec(expr || "0");
    if (!m) return 0;
    const base = /^[\d.]+$/.test(m[2]) ? Number(m[2]) : Number(vals[m[2]]) || 0;
    const sign = m[1] === "-" || (m[1] === "~" && light) ? -1 : 1;
    return sign * base * (m[3] ? Number(m[3]) : 1);
  };
  // `@if` blocks may nest: each pass resolves the innermost ones (bodies with no further @if)
  const IF = /\/\*\s*@if\s+([\w-]+)\s*(?:(!?=)\s*([^\s*]+))?\s*\*\/((?:(?!\/\*\s*@if\b)[\s\S])*?)\/\*\s*@endif\s*\*\//g;
  const resolveIfs = (src, vals) => {
    for (let prev; prev !== src;) {
      prev = src;
      src = src.replace(IF, (_, k, op, want, body) => {
        const v = vals[k];
        const hit = op ? (String(v) === want) === (op === "=") : v !== false && v !== "" && v !== 0 && v !== undefined;
        return hit ? body : "";
      });
    }
    return src;
  };
  const lum = (hex) => {
    const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };
  const contrast = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m); return (x + 0.05) / (y + 0.05); };
  // `ink:<key>`: this color, moved lighter or darker until it reads on the color in <key>
  const ink = (fg, bg) => {
    if (!/^#[0-9a-f]{6}$/i.test(bg)) return fg;
    const [h, s, l] = hexToHsl(fg), step = isLight(bg) ? -2 : 2;
    let out = fg;
    for (let l2 = l; contrast(out, bg) < 4.5 && l2 >= 0 && l2 <= 100; l2 += step) out = hslToHex(h, s, Math.min(100, Math.max(0, l2)));
    return out;
  };
  // {{key}}, with filters chained by |: ink:<key> and hex[:shift] give a color, hsl[:shift] gives an HSL triplet
  const renderCss = (src, vals) => resolveIfs(src, vals)
    .replace(/\{\{\s*([\w-]+)\s*((?:\|[^|}]+)*)\}\}/g, (_, k, chain) => {
      let v = vals[k];
      if (v === undefined) return "";
      for (const part of chain.split("|").slice(1)) {
        const [filter, arg] = part.split(/:(.*)/s).map((x) => x && x.trim());
        if (!/^#[0-9a-f]{6}$/i.test(v)) return "";
        if (filter === "ink") { v = ink(v, vals[arg]); continue; }
        if (filter !== "hsl" && filter !== "hex") return "";
        const [h, s, l] = hexToHsl(v), l2 = Math.min(100, Math.max(0, l + delta(arg, vals, l > 60)));
        v = filter === "hsl" ? `${r2(h)} ${r2(s)}% ${r2(l2)}%` : hslToHex(h, s, l2);
      }
      return String(v);
    });

  // plugins on disk
  const DEFAULT_MATCH = "^https://claude\\.ai/";
  const SCRIPT = /\/\*\s*@script\b([\s\S]*?)@endscript\s*\*\//;
  const readPlugins = (kind) => {
    const all = readJson(settingsFile);
    return ls(DIRS[kind]).filter((f) => f.endsWith(`.${kind}`) || f.endsWith(`.${kind}.off`)).map((f) => {
      const on = !f.endsWith(".off"), file = on ? f : f.slice(0, -4);
      const src = read(path.join(DIRS[kind], f)), hash = sha(src), meta = parseMeta(src);
      const script = kind === "js" ? src : (SCRIPT.exec(src) || [])[1] || null;
      return { file, kind, on, src, script, hash, meta, values: valuesFor(file, meta, all), match: meta.match || DEFAULT_MATCH };
    });
  };
  const matches = (p, url) => { try { return new RegExp(p.match).test(url); } catch { return false; } };

  // styles
  const buildCss = () => {
    const on = readPlugins("css").filter((p) => p.on);
    return { files: on.map((p) => p.file), css: on.map((p) => `/* claudify: ${p.file} */\n${renderCss(p.src.replace(SCRIPT, ""), resolved(p))}\n`).join("\n") };
  };

  // scripts
  const RUNTIME = `window.__claudify = window.__claudify || { mods: {}, stop(n) {
    if (!(n in this.mods)) return true; const f = this.mods[n]; delete this.mods[n];
    if (typeof f !== "function") return false; try { f(); } catch (e) {} return true; } };`;
  const wrap = (p) => `(() => { ${RUNTIME} const C = window.__claudify, N = ${JSON.stringify(p.file)};
    C.stop(N); const r = (function (settings) {\n${p.script}\n}).call(window, ${JSON.stringify(resolved(p))});
    C.mods[N] = typeof r === "function" ? r : null; return typeof r === "function"; })()`;

  const running = new Map(); // webContents -> Map(file -> hash:settings) live in its current document
  const errors = new Map();  // file -> last error message
  const stale = new Set();   // "wcid:file" (stopped scripts without cleanup, need a reload)
  const runKey = (p) => `${p.hash}:${sha(JSON.stringify(p.values))}`;

  const queues = new WeakMap(); // one sync at a time per window (dom-ready and watchers can overlap)
  const syncMods = (wc) => {
    const next = (queues.get(wc) || Promise.resolve()).then(() => syncNow(wc)).catch((e) => log(`sync failed: ${e.message}`));
    queues.set(wc, next);
    return next;
  };
  const syncNow = async (wc) => {
    if (wc.isDestroyed()) return;
    const url = wc.getURL();
    const want = new Map([...readPlugins("js"), ...readPlugins("css")].filter((p) => p.script && p.on && matches(p, url)).map((p) => [p.file, p]));
    const have = running.get(wc) || new Map();
    running.set(wc, have);
    for (const [file, key] of [...have]) {
      const p = want.get(file);
      if (p && runKey(p) === key) continue;
      have.delete(file);
      if (p) continue; // changed: the re-run below calls its cleanup first
      const clean = await wc.executeJavaScript(`window.__claudify ? window.__claudify.stop(${JSON.stringify(file)}) : true`, true).catch(() => true);
      if (!clean) { stale.add(`${wc.id}:${file}`); log(`script ${file} has no cleanup -> reload wc#${wc.id} to fully remove it`); }
      else log(`script ${file} stopped -> wc#${wc.id}`);
    }
    for (const [file, p] of want) {
      const key = runKey(p);
      if (have.get(file) === key) continue;
      have.set(file, key);
      try {
        const hot = await wc.executeJavaScript(wrap(p), true);
        errors.delete(file); stale.delete(`${wc.id}:${file}`);
        log(`script ${file} ran${hot ? "" : " (no cleanup)"} sha256=${p.hash.slice(0, 12)} -> wc#${wc.id} ${url}`);
      } catch (e) {
        errors.set(file, String(e && e.message || e));
        log(`script ${file} failed -> wc#${wc.id}: ${errors.get(file)}`);
      }
    }
  };

  // top bar button that opens the plugins window
  const OPEN_MSG = "__claudify:open-plugins__";
  const BUTTON = `(() => {
    if (window.__claudifyButton) return; window.__claudifyButton = true;
    const ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3.5a2 2 0 0 1 4 0V5h3a1 1 0 0 1 1 1v3h1.5a2 2 0 0 1 0 4H17v3a1 1 0 0 1-1 1h-3v-1.5a2 2 0 0 0-4 0V17H6a1 1 0 0 1-1-1v-3h1.5a2 2 0 0 0 0-4H5V6a1 1 0 0 1 1-1h3z"/></svg>';
    const place = () => {
      const lead = document.querySelector(".dframe-chrome-lead");
      if (!lead || lead.querySelector("[data-claudify-button]")) return;
      const fwd = lead.querySelector('button[aria-label="Forward"]');
      const b = document.createElement("button");
      b.type = "button"; b.dataset.claudifyButton = ""; b.setAttribute("aria-label", "claudify plugins"); b.title = "claudify plugins (Ctrl+Alt+K)";
      b.className = (fwd && fwd.className) || ""; b.innerHTML = ICON;
      b.style.cssText = "-webkit-app-region:no-drag;display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px";
      b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); console.debug(${JSON.stringify(OPEN_MSG)}); });
      const group = fwd && fwd.parentElement;
      if (group && group.parentElement) group.parentElement.insertBefore(b, group.nextSibling); else lead.appendChild(b);
    };
    place(); new MutationObserver(place).observe(document.body, { childList: true, subtree: true });
  })()`;

  // a short accent-colored edge glow when a style changes live
  const GLOW = `(() => {
    let g = document.getElementById("claudify-glow");
    if (!g) {
      g = document.createElement("div"); g.id = "claudify-glow";
      g.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647;opacity:0;" +
        "box-shadow:inset 0 0 0 1px var(--cds-clay,#d97757),inset 0 0 48px -8px var(--cds-clay,#d97757)";
      document.documentElement.appendChild(g);
    }
    g.getAnimations().forEach((a) => a.cancel());
    g.animate([{ opacity: 0 }, { opacity: 0.9, offset: 0.25 }, { opacity: 0 }], { duration: 900, easing: "ease-out" });
  })()`;

  // windows
  const keys = new WeakMap(); // webContents -> inserted CSS key
  const live = new Set();
  let win = null;
  const isPluginsWin = (wc) => win && !win.isDestroyed() && wc === win.webContents;

  const apply = async (wc) => {
    if (wc.isDestroyed() || isPluginsWin(wc)) return;
    const url = wc.getURL();
    if (url.startsWith("devtools://")) return;
    try {
      const { css, files } = buildCss();
      // insert the new sheet before dropping the old one so the page never renders unstyled in between
      const old = keys.get(wc);
      keys.delete(wc);
      if (css) keys.set(wc, await wc.insertCSS(css));
      if (old) {
        await wc.removeInsertedCSS(old).catch(() => {});
        wc.executeJavaScript(GLOW, true).catch(() => {});
      }
      log(`applied ${files.length} style(s) [${files.join(", ")}] -> wc#${wc.id} ${url || "(no url)"}`);
      paintControls(wc); setTimeout(() => paintControls(wc), 2500); // again once the page has drawn its frame
    } catch (e) { log(`apply failed for ${url}: ${e.message}`); }
  };

  // The minimize/maximize/close buttons are Electron's native title bar overlay, which CSS
  // can't reach. Paint it with whatever the page shows under the top-right corner.
  const CORNER = `(() => {
    const ctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
    const hex = (c) => {
      ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = "#000"; ctx.fillStyle = c; ctx.fillRect(0, 0, 1, 1);
      const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
      return a ? "#" + [r, g, b].map((n) => n.toString(16).padStart(2, "0")).join("") : null;
    };
    const under = document.elementsFromPoint(innerWidth - 40, 10);
    const bg = under.map((e) => hex(getComputedStyle(e).backgroundColor)).find(Boolean) || hex(getComputedStyle(document.body).backgroundColor);
    return bg && { color: bg, symbolColor: hex(getComputedStyle(document.body).color) || "#c2c0b6" };
  })()`;
  const hostOf = (wc) => BrowserWindow.fromWebContents(wc)
    || BrowserWindow.getAllWindows().find((w) => w.contentView?.children?.some((v) => v.webContents === wc));
  const paintControls = async (wc) => {
    if (wc.isDestroyed() || !/^https:\/\/claude\.ai\//.test(wc.getURL())) return;
    const host = hostOf(wc);
    if (!host || host === win || !host.setTitleBarOverlay) return;
    try {
      const colors = await wc.executeJavaScript(CORNER, true);
      if (colors) host.setTitleBarOverlay(colors);
    } catch {}
  };
  nativeTheme.on("updated", () => setTimeout(() => { for (const wc of live) paintControls(wc); }, 500));

  const notify = () => { if (win && !win.isDestroyed()) win.webContents.send("claudify:changed"); };

  app.on("web-contents-created", (_e, wc) => {
    live.add(wc);
    wc.on("destroyed", () => { live.delete(wc); running.delete(wc); });
    wc.on("dom-ready", () => { // fresh document: old CSS keys and scripts are gone
      if (isPluginsWin(wc)) return;
      keys.delete(wc); running.delete(wc);
      for (const k of [...stale]) if (k.startsWith(`${wc.id}:`)) stale.delete(k);
      apply(wc); syncMods(wc);
      if (/^https:\/\/claude\.ai\//.test(wc.getURL())) wc.executeJavaScript(BUTTON, true).catch((e) => log(`button failed: ${e.message}`));
    });
    wc.on("console-message", (...a) => {
      const msg = a[0] && typeof a[0].message === "string" ? a[0].message : a[2];
      if (msg === OPEN_MSG) openPlugins(`button in wc#${wc.id}`);
    });
    wc.on("before-input-event", (e, i) => {
      if (i.type === "keyDown" && i.control && i.alt && !i.shift && i.key.toLowerCase() === "k") {
        e.preventDefault(); openPlugins(`Ctrl+Alt+K in wc#${wc.id}`);
      }
    });
  });

  // plugins window
  const safeName = (kind, name) => {
    if (!DIRS[kind] || typeof name !== "string") throw new Error("bad kind/name");
    name = name.trim().replace(/\.off$/, "");
    if (!name.endsWith(`.${kind}`)) name += `.${kind}`;
    if (!/^[A-Za-z0-9_][\w.-]*$/.test(name)) throw new Error(`bad name: ${name}`);
    return name;
  };
  const fileOf = (kind, name) => {
    const p = path.join(DIRS[kind], name);
    return fs.existsSync(p) ? p : fs.existsSync(p + ".off") ? p + ".off" : null;
  };
  const summary = (p) => ({
    file: p.file, kind: p.kind, on: p.on, hash: p.hash.slice(0, 12), match: p.kind === "js" ? p.match : null,
    name: p.meta.name || p.file.replace(/\.(css|js)$/, ""), description: p.meta.description || "",
    author: p.meta.author || "", version: p.meta.version || "",
    script: !!p.script,
    settings: p.meta.settings.map((s) => ({ ...s, value: p.values[s.key],
      ...(s.type === "file" && p.values[s.key] && { preview: /^image\//.test(mimeOf(p.values[s.key])) ? dataUrl(p.values[s.key]) : "" }) })),
    error: errors.get(p.file) || null, needsReload: [...stale].some((k) => k.endsWith(`:${p.file}`)),
  });

  const ops = {
    state() {
      const plugins = [...readPlugins("css"), ...readPlugins("js")].map(summary);
      return { plugins, log: read(logFile).split("\n").slice(-80).join("\n") };
    },
    // the Claudify Theme's values, so this window matches it (null while the theme is off)
    theme() {
      const p = readPlugins("css").find((x) => x.file === "theme.css" && x.on);
      return p ? resolved(p) : null;
    },
    // Claude's current brand color, used as this window's accent
    async accent() {
      for (const wc of live) {
        if (wc.isDestroyed() || isPluginsWin(wc) || !/^https:\/\/claude\.ai\//.test(wc.getURL())) continue;
        const c = await wc.executeJavaScript(`getComputedStyle(document.documentElement).getPropertyValue("--cds-clay").trim()`, true).catch(() => "");
        if (/^#[0-9a-f]{3,8}$|^rgb/i.test(c)) return c;
      }
      return "#d97757";
    },
    read(kind, name) {
      const p = fileOf(kind, safeName(kind, name));
      return p ? read(p) : "";
    },
    save(kind, name, src) {
      name = safeName(kind, name);
      fs.mkdirSync(DIRS[kind], { recursive: true });
      fs.writeFileSync(fileOf(kind, name) || path.join(DIRS[kind], name), src);
      return name;
    },
    toggle(kind, name, on) {
      name = safeName(kind, name);
      const p = path.join(DIRS[kind], name);
      if (on && fs.existsSync(p + ".off")) fs.renameSync(p + ".off", p);
      if (!on && fs.existsSync(p)) fs.renameSync(p, p + ".off");
    },
    remove(kind, name) {
      name = safeName(kind, name);
      const p = path.join(DIRS[kind], name);
      for (const f of [p, p + ".off"]) fs.rmSync(f, { force: true });
      const all = readJson(settingsFile);
      if (name in all) { delete all[name]; writeJson(settingsFile, all); }
      pruneFiles(name);
    },
    // a file picked in the Import dialog
    import(name, src) {
      if (typeof src !== "string" || src.length > 1 << 20) throw new Error("not a text file under 1 MB");
      const kind = /\.js$/i.test(name) ? "js" : /\.css$/i.test(name) ? "css" : null;
      if (!kind) throw new Error("only .css and .js files can be imported");
      name = safeName(kind, path.basename(String(name)).replace(/\.(css|js)$/i, `.${kind}`).replace(/[^\w.-]/g, "-"));
      fs.mkdirSync(DIRS[kind], { recursive: true });
      fs.writeFileSync(fileOf(kind, name) || path.join(DIRS[kind], name), src);
      log(`imported ${name}`);
      return { kind, file: name };
    },
    exists(kind, name) { return !!fileOf(kind, safeName(kind, name)); },
    // the plugin's source with its current settings written in as the defaults, saved where the user picks.
    // File settings (fonts, images) stay empty, since they live outside the plugin file
    async export(kind, name) {
      name = safeName(kind, name);
      const p = fileOf(kind, name);
      if (!p) throw new Error(`no plugin ${name}`);
      const src = read(p), meta = parseMeta(src), values = valuesFor(name, meta);
      const quote = (v) => /^[^\s"]+$/.test(v) ? v : `"${v.replace(/["\\]/g, "\\$&")}"`;
      const out = src.replace(META, (line, k, v) => {
        const s = k === "setting" && meta.settings.find((x) => x.key === tokens(v)[0]);
        if (!s || s.type === "file" || tokens(v).length < 3 || (s.type === "select" && !/\boptions=/.test(v))) return line;
        const next = v.replace(/^(\S+\s+\S+\s+)("(?:[^"\\]|\\.)*"|\S+)/, (_m, head) => head + quote(String(values[s.key])));
        return line.replace(v, () => next);
      });
      const { canceled, filePath } = await electron.dialog.showSaveDialog(win, {
        title: `Export ${name}`, defaultPath: path.join(app.getPath("downloads"), name),
        filters: [{ name: kind === "js" ? "Script" : "Stylesheet", extensions: [kind] }],
      });
      if (canceled || !filePath) return null;
      fs.writeFileSync(filePath, out);
      log(`exported ${name} to ${filePath}`);
      return filePath;
    },
    setSetting(kind, name, key, value) {
      name = safeName(kind, name);
      const p = fileOf(kind, name);
      if (!p) throw new Error(`no plugin ${name}`);
      const s = parseMeta(read(p)).settings.find((x) => x.key === key);
      if (!s) throw new Error(`no setting ${key}`);
      if (s.type === "file" && value !== "") throw new Error("pick files with setFile");
      const all = readJson(settingsFile);
      (all[name] ||= {})[key] = validate(s, value);
      writeJson(settingsFile, all);
      if (s.type === "file") pruneFiles(name, key);
    },
    // a file chosen in a file setting's picker, sent as a data: URL; stored under <root>/files
    setFile(kind, name, key, fileName, url) {
      name = safeName(kind, name);
      const p = fileOf(kind, name);
      if (!p) throw new Error(`no plugin ${name}`);
      const s = parseMeta(read(p)).settings.find((x) => x.key === key && x.type === "file");
      if (!s) throw new Error(`no file setting ${key}`);
      fileName = path.basename(String(fileName));
      if (!accepts(s, fileName)) throw new Error(`${fileName} is not an accepted type (${s.accept})`);
      const m = /^data:[^,]*;base64,(.*)$/s.exec(String(url));
      if (!m) throw new Error("unreadable file");
      const buf = Buffer.from(m[1], "base64");
      if (buf.length > s.max * 1048576) throw new Error(`${fileName} is over ${s.max} MB`);
      // Chromium drops URLs over 2 MB, and a data: URL is 4/3 the file, so a bigger image never paints in a style
      if (kind === "css" && mimeOf(fileName).startsWith("image/") && buf.length > IMAGE_MAX) throw new Error(`${fileName} is too big for Claude to draw (1.5 MB at most)`);
      const ext = (path.extname(fileName).slice(1).toLowerCase().match(/^[a-z0-9]{1,8}$/) || ["bin"])[0];
      const base = path.basename(fileName, path.extname(fileName)).replace(/[^\w-]+/g, "-").slice(0, 60) || "file";
      const stored = `${name}--${key}-${sha(buf).slice(0, 8)}-${base}.${ext}`;
      fs.mkdirSync(filesDir, { recursive: true });
      fs.writeFileSync(path.join(filesDir, stored), buf);
      const all = readJson(settingsFile);
      (all[name] ||= {})[key] = stored;
      writeJson(settingsFile, all);
      pruneFiles(name, key, stored);
      log(`file ${fileName} -> ${name} ${key} (${buf.length} bytes)`);
      return stored;
    },
    resetSettings(kind, name) {
      name = safeName(kind, name);
      const all = readJson(settingsFile);
      if (name in all) { delete all[name]; writeJson(settingsFile, all); }
      pruneFiles(name);
    },
    reloadClaude() {
      for (const wc of live) if (!wc.isDestroyed() && !isPluginsWin(wc) && /^(https?|file):/.test(wc.getURL())) wc.reload();
    },
  };

  ipcMain.handle("claudify", async (e, op, ...args) => {
    if (!isPluginsWin(e.sender) || !Object.hasOwn(ops, op)) throw new Error("denied");
    return ops[op](...args);
  });

  const openPlugins = (why) => {
    try {
      if (win && !win.isDestroyed()) { win.show(); win.focus(); return; }
      win = new BrowserWindow({
        width: 1100, height: 760, minWidth: 760, minHeight: 480, title: "claudify plugins",
        backgroundColor: "#000000", autoHideMenuBar: true,
        webPreferences: { preload: __filename, contextIsolation: true, sandbox: true, nodeIntegration: false },
      });
      const wc = win.webContents;
      wc.setWindowOpenHandler(() => ({ action: "deny" }));
      wc.on("will-navigate", (e) => e.preventDefault());
      wc.on("did-fail-load", (_e, code, desc, url) => log(`plugins window load failed ${code} ${desc} ${url}`));
      wc.on("console-message", (...a) => {
        const d = a[0] && a[0].message !== undefined ? a[0] : { level: a[1], message: a[2], lineNumber: a[3] };
        if (d.level === "error" || d.level === 3) log(`plugins window console: ${d.message} (line ${d.lineNumber})`);
      });
      wc.on("render-process-gone", (_e, d) => log(`plugins window renderer gone: ${d.reason}`));
      win.on("closed", () => { win = null; });
      win.loadFile(path.join(__dirname, "claudify-plugins.html"))
        .then(() => log(`plugins window opened (${why})`), (e) => log(`plugins window load error: ${e.message}`));
    } catch (e) { log(`plugins window failed: ${e.message}`); }
  };

  // structured DOM queries for the MCP server (a fixed query, never arbitrary code)
  const QUERY = function (a) {
    let els;
    try { els = document.querySelectorAll(a.selector || "html"); } catch (e) { return { error: "bad selector: " + e.message }; }
    const out = { count: els.length, matches: [] };
    for (const el of Array.from(els).slice(0, a.limit)) {
      const r = el.getBoundingClientRect(), cs = getComputedStyle(el), attrs = {};
      for (const at of el.attributes) if (/^(aria-|data-|role$|type$|href$|title$|dir$|contenteditable$|placeholder$)/.test(at.name)) attrs[at.name] = at.value.slice(0, 120);
      out.matches.push({ tag: el.tagName.toLowerCase(), id: el.id || undefined, classes: (el.getAttribute("class") || "").slice(0, 240),
        attrs, text: (el.innerText || "").trim().slice(0, 120), rect: [r.x | 0, r.y | 0, r.width | 0, r.height | 0],
        children: el.children.length, styles: Object.fromEntries((a.props || []).map((p) => [p, cs.getPropertyValue(p)])) });
    }
    const rs = getComputedStyle(document.documentElement);
    out.vars = Object.fromEntries((a.vars || []).map((v) => [v, rs.getPropertyValue(v).trim()]));
    return out;
  };
  const queryFile = path.join(root, "query.json"), resultFile = path.join(root, "query-result.json");
  const runQuery = async () => {
    const q = readJson(queryFile);
    if (!q.id) return;
    fs.rmSync(queryFile, { force: true });
    const args = {
      selector: String(q.selector || "html"), limit: Math.min(100, Math.max(1, Number(q.limit) || 20)),
      props: (Array.isArray(q.props) ? q.props : []).map(String).slice(0, 40),
      vars: (Array.isArray(q.vars) ? q.vars : []).map(String).filter((v) => v.startsWith("--")).slice(0, 80),
    };
    const target = ["chat", "shell", "all"].includes(q.target) ? q.target : "chat";
    const results = [];
    for (const wc of live) {
      if (wc.isDestroyed() || isPluginsWin(wc)) continue;
      const url = wc.getURL();
      const kind = /^https:\/\/claude\.ai\//.test(url) ? "chat" : url.startsWith("file:") ? "shell" : null;
      if (!kind || (target !== "all" && target !== kind)) continue;
      try { results.push({ window: kind, url, ...(await wc.executeJavaScript(`(${QUERY})(${JSON.stringify(args)})`, true)) }); }
      catch (e) { results.push({ window: kind, url, error: e.message }); }
    }
    writeJson(resultFile, { id: q.id, results });
    log(`query ${JSON.stringify(args.selector)} target=${target} -> ${results.length} window(s)`);
  };

  // watchers
  const debounce = (fn, ms = 150) => { let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; };
  const reloadCss = debounce(() => Promise.all([...live].map(apply)).then(notify));
  const reloadJs = debounce(() => { for (const wc of live) if (!isPluginsWin(wc) && !wc.isDestroyed()) syncMods(wc); setTimeout(notify, 300); });

  // `claudify inspect`: when <root>/inspect.js changes, run it in every webContents and log the result
  const inspectFile = path.join(root, "inspect.js");
  const runInspect = debounce(() => {
    const src = read(inspectFile);
    if (!src) return;
    for (const wc of live) {
      if (wc.isDestroyed() || isPluginsWin(wc)) continue;
      wc.executeJavaScript(src, true)
        .then((r) => log(`inspect wc#${wc.id} ${wc.getURL()} => ${JSON.stringify(r)}`))
        .catch((e) => log(`inspect wc#${wc.id} failed: ${e.message}`));
    }
  });
  const trigger = path.join(root, "open-plugins");
  const onQuery = debounce(() => runQuery().catch((e) => log(`query failed: ${e.message}`)), 50);
  const onRoot = (_ev, name) => {
    if (name === "inspect.js") runInspect();
    else if (name === "settings.json") { reloadCss(); reloadJs(); }
    else if (name === "query.json") onQuery();
    else if (name === "open-plugins" && fs.existsSync(trigger)) { fs.rmSync(trigger, { force: true }); openPlugins("open-plugins trigger"); }
  };

  app.whenReady().then(() => {
    for (const [d, fn] of [[cssDir, reloadCss], [jsDir, reloadJs], [root, onRoot]]) {
      try { fs.mkdirSync(d, { recursive: true }); fs.watch(d, { persistent: false }, fn); }
      catch (e) { log(`watch ${d} failed: ${e.message}`); }
    }
    log(`watching ${root}`);
  }).catch(() => {});
})();
