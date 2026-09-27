#!/usr/bin/env python3
"""Stdio MCP server that lets Claude manage claudify plugins. Standard library only.

It speaks newline-delimited JSON-RPC 2.0 on stdin/stdout and logs to stderr. The loader
inside Claude Desktop applies the plugins; this server only reads and writes the files it
watches, under R = dirname($CLAUDIFY_DIR) or ${XDG_CONFIG_HOME:-~/.config}/claudify:

    R/css/<name>.css, R/js/<name>.js    plugins (a .off suffix means disabled)
    R/settings.json                     {"<plugin file>": {"<key>": value}}
    R/files/                            images and fonts picked for file settings
    R/loader.log                        the loader's log
    R/open-plugins                      touch it to open the plugins window
    R/query.json, R/query-result.json   polling RPC for inspecting the live DOM
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import traceback
import uuid



def get_root() -> str:
    claudify_dir = os.environ.get("CLAUDIFY_DIR")
    if claudify_dir:
        return os.path.dirname(os.path.normpath(claudify_dir)) or "/"
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg, "claudify")


ROOT = get_root()
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "logo.svg")
try:
    with open(LOGO_PATH, "rb") as f:
        ICONS = [{"src": "data:image/svg+xml;base64," + base64.b64encode(f.read()).decode(),
                  "mimeType": "image/svg+xml", "sizes": ["any"]}]
except OSError:
    ICONS = []
CSS_DIR = os.path.join(ROOT, "css")
JS_DIR = os.path.join(ROOT, "js")
SETTINGS_PATH = os.path.join(ROOT, "settings.json")
LOADER_LOG_PATH = os.path.join(ROOT, "loader.log")
QUERY_PATH = os.path.join(ROOT, "query.json")
QUERY_RESULT_PATH = os.path.join(ROOT, "query-result.json")
FILES_DIR = os.path.join(ROOT, "files")
FILE_MAX_MB = 32
OPEN_PLUGINS_PATH = os.path.join(ROOT, "open-plugins")

DIR_FOR_KIND = {"css": CSS_DIR, "js": JS_DIR}

FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.(css|js)$")
KEY_RE = re.compile(r"^[A-Za-z_]\w*$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

HEADER_RE = re.compile(
    r"^\s*(?:\*|//|/\*)?\s*@(name|description|author|version|match|setting)\s+(.*?)\s*(?:\*/)?\s*$"
)

SETTING_TYPES = ("color", "range", "number", "select", "toggle", "text", "file")


def log(msg: str) -> None:
    print(f"[claudify_mcp] {msg}", file=sys.stderr, flush=True)




def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError, ValueError):
        return default


def write_json_atomic(path: str, obj) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-claudify-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def listdir_safe(d: str):
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


class ClaudifyError(Exception):
    """A user-facing tool error (reported as isError, not a crash)."""




def kind_of(full_name: str) -> str:
    return "js" if full_name.endswith(".js") else "css"


def validate_full_name(full_name: str) -> None:
    if not FULL_NAME_RE.match(full_name):
        raise ClaudifyError(
            f"Invalid plugin name {full_name!r}: must match {FULL_NAME_RE.pattern} "
            "(no path separators or traversal, extension .css or .js)."
        )


def variant_paths(full_name: str):
    d = DIR_FOR_KIND[kind_of(full_name)]
    return os.path.join(d, full_name), os.path.join(d, full_name + ".off")


def find_installed(full_name: str):
    """Return (enabled_path_or_off_path, enabled: bool) if the plugin exists, else None."""
    on_path, off_path = variant_paths(full_name)
    if os.path.exists(on_path):
        return on_path, True
    if os.path.exists(off_path):
        return off_path, False
    return None


def resolve_existing_name(name: str) -> str:
    """Resolve a name (extension and .off optional) to an installed plugin's full name, or raise."""
    name = name.strip()
    if name.endswith(".off"):
        name = name[: -len(".off")]
    if FULL_NAME_RE.match(name):
        if find_installed(name) is None:
            raise ClaudifyError(f"No installed plugin named {name!r}.")
        return name
    # bare name: prefer an existing js, else css
    for ext in ("js", "css"):
        candidate = f"{name}.{ext}"
        if FULL_NAME_RE.match(candidate) and find_installed(candidate) is not None:
            return candidate
    raise ClaudifyError(
        f"No installed plugin named {name!r} (checked {name}.js and {name}.css)."
    )




def parse_setting_line(raw: str) -> dict:
    # same tokenizer as the loader: bare words, double-quoted strings (backslash escapes), key="quoted values"
    tokens = [k + re.sub(r"\\(.)", r"\1", q) if q or w == "" else w
              for k, q, w in re.findall(r'([\w-]+=)?"((?:[^"\\]|\\.)*)"|(\S+)', raw)]
    if len(tokens) < 3:
        raise ClaudifyError(f"Malformed @setting line (need key type default): {raw!r}")
    key, type_, default_raw = tokens[0], tokens[1], tokens[2]
    if not KEY_RE.match(key):
        raise ClaudifyError(f"Invalid @setting key {key!r} in: {raw!r}")
    if type_ not in SETTING_TYPES:
        raise ClaudifyError(f"Invalid @setting type {type_!r} in: {raw!r} (one of {SETTING_TYPES})")
    rest = tokens[3:]
    label = key
    attrs: dict = {}
    if rest:
        label = rest[0]
        for tok in rest[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                attrs[k] = v

    if type_ == "toggle":
        default = default_raw.strip().lower() in ("true", "1", "yes", "on")
    elif type_ in ("range", "number"):
        try:
            fval = float(default_raw)
        except ValueError:
            fval = 0.0
        default = int(fval) if fval.is_integer() else fval
        for a in ("min", "max", "step"):
            if a in attrs:
                try:
                    av = float(attrs[a])
                    attrs[a] = int(av) if av.is_integer() else av
                except ValueError:
                    pass
    else:
        default = default_raw

    if type_ == "select" and "options" in attrs:
        option_values = []
        for part in attrs["options"].split("|"):
            option_values.append(part.split(":", 1)[0])
        attrs["_option_values"] = option_values

    return {"key": key, "type": type_, "default": default, "label": label, "attrs": attrs}


def parse_header(source: str) -> dict:
    meta = {"name": None, "description": None, "author": None, "version": None, "match": [], "settings": []}
    for line in source.splitlines():
        m = HEADER_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if key == "match":
            meta["match"].append(val)
        elif key == "setting":
            try:
                meta["settings"].append(parse_setting_line(val))
            except ClaudifyError as e:
                # a malformed @setting line shows up as an inert entry instead of failing the listing
                meta["settings"].append({"key": None, "error": str(e), "raw": val})
        elif meta.get(key) is None:
            meta[key] = val
    return meta


def read_plugin_source(full_name: str) -> str:
    found = find_installed(full_name)
    if found is None:
        raise ClaudifyError(f"No installed plugin named {full_name!r}.")
    path, _enabled = found
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()




def load_settings() -> dict:
    data = read_json(SETTINGS_PATH, {})
    return data if isinstance(data, dict) else {}


def save_settings(data: dict) -> None:
    write_json_atomic(SETTINGS_PATH, data)


def effective_values(full_name: str, schema: list, settings: dict | None = None) -> dict:
    settings = settings if settings is not None else load_settings()
    stored = settings.get(full_name, {}) if isinstance(settings.get(full_name, {}), dict) else {}
    out = {}
    for s in schema:
        if s.get("key") is None:
            continue
        k = s["key"]
        out[k] = stored[k] if k in stored else s["default"]
    return out




def validate_setting_value(setting: dict, value):
    t = setting["type"]
    attrs = setting.get("attrs", {})
    if t == "color":
        if not isinstance(value, str) or not COLOR_RE.match(value):
            raise ClaudifyError(f"{setting['key']}: expected a #rrggbb color string, got {value!r}.")
        return value
    if t in ("range", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ClaudifyError(f"{setting['key']}: expected a number, got {value!r}.")
        try:
            fval = float(value)
        except (TypeError, ValueError):
            raise ClaudifyError(f"{setting['key']}: expected a number, got {value!r}.")
        if "min" in attrs:
            fval = max(fval, float(attrs["min"]))
        if "max" in attrs:
            fval = min(fval, float(attrs["max"]))
        return int(fval) if fval.is_integer() else fval
    if t == "select":
        options = attrs.get("_option_values")
        if not options:
            raise ClaudifyError(f"{setting['key']}: this select has no options defined.")
        if value not in options:
            raise ClaudifyError(f"{setting['key']}: {value!r} is not one of {options}.")
        return value
    if t == "toggle":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        raise ClaudifyError(f"{setting['key']}: expected true/false, got {value!r}.")
    if t == "text":
        if not isinstance(value, str):
            raise ClaudifyError(f"{setting['key']}: expected a string, got {value!r}.")
        cleaned = value.replace("\n", "").replace("\r", "")
        cleaned = re.sub(r"[{}<>]", "", cleaned)
        if len(cleaned) > 200:
            cleaned = cleaned[:200]
        return cleaned
    raise ClaudifyError(f"{setting['key']}: unknown setting type {t!r}.")


def store_file_setting(full: str, setting: dict, value) -> str:
    """A file setting takes "" (clear) or an absolute path on this computer, copied into
    R/files/<plugin>--<key>-<sha8>-<name>.<ext> exactly like the plugins window does."""
    key, attrs = setting["key"], setting.get("attrs", {})
    prefix = f"{full}--{key}-"
    if not isinstance(value, str):
        raise ClaudifyError(f"{key}: expected \"\" or an absolute file path, got {value!r}.")
    stored = ""
    if value:
        src = os.path.expanduser(value)
        if not os.path.isabs(src) or not os.path.isfile(src):
            raise ClaudifyError(f"{key}: {value!r} is not an existing absolute file path.")
        try:
            max_mb = min(FILE_MAX_MB, float(attrs.get("max") or 8))
        except ValueError:
            max_mb = 8
        if os.path.getsize(src) > max_mb * 1048576:
            raise ClaudifyError(f"{key}: file is over {max_mb:g} MB.")
        name = os.path.basename(src)
        stem, ext = os.path.splitext(name)
        ext = ext[1:].lower()
        accept = [a.strip().lower() for a in str(attrs.get("accept", "")).split(",") if a.strip()]
        if accept and not any(a == "." + ext if a.startswith(".") else
                              (MIME.get(ext, "").startswith(a[:-1]) if a.endswith("/*") else MIME.get(ext) == a)
                              for a in accept):
            raise ClaudifyError(f"{key}: {name} is not an accepted type ({attrs.get('accept')}).")
        if not re.fullmatch(r"[a-z0-9]{1,8}", ext):
            ext = "bin"
        with open(src, "rb") as f:
            data = f.read()
        # Chromium drops URLs over 2 MB and a data: URL is 4/3 the file, so a bigger image never paints in a style
        if full.endswith(".css") and MIME.get(ext, "").startswith("image/") and len(data) > 1.5 * 1048576:
            raise ClaudifyError(f"{key}: {name} is too big for Claude to draw (1.5 MB at most); shrink it first.")
        base = re.sub(r"[^\w-]+", "-", stem)[:60] or "file"
        stored = f"{prefix}{hashlib.sha256(data).hexdigest()[:8]}-{base}.{ext}"
        os.makedirs(FILES_DIR, exist_ok=True)
        tmp = os.path.join(FILES_DIR, f".{stored}.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(FILES_DIR, stored))
    for f in os.listdir(FILES_DIR) if os.path.isdir(FILES_DIR) else []:
        if f.startswith(prefix) and f != stored:
            os.remove(os.path.join(FILES_DIR, f))
    return stored


MIME = {"gif": "image/gif", "png": "image/png", "apng": "image/apng", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "avif": "image/avif", "svg": "image/svg+xml", "bmp": "image/bmp", "ico": "image/x-icon",
        "woff": "font/woff", "woff2": "font/woff2", "ttf": "font/ttf", "otf": "font/otf", "mp3": "audio/mpeg",
        "ogg": "audio/ogg", "wav": "audio/wav", "mp4": "video/mp4", "webm": "video/webm"}




def tool_list_plugins(args: dict):
    settings = load_settings()
    installed = []
    seen = set()
    for kind, d in (("css", CSS_DIR), ("js", JS_DIR)):
        for fname in listdir_safe(d):
            is_off = fname.endswith(".off")
            base = fname[: -len(".off")] if is_off else fname
            if not FULL_NAME_RE.match(base) or kind_of(base) != kind:
                continue
            if base in seen:
                continue  # an enabled variant was already recorded; skip its .off leftover
            enabled_path = os.path.join(d, base)
            enabled = os.path.exists(enabled_path)
            if not enabled and not is_off:
                continue
            seen.add(base)
            try:
                src = read_plugin_source(base)
            except ClaudifyError:
                continue
            meta = parse_header(src)
            entry = {
                "name": base,
                "kind": kind,
                "enabled": enabled,
                "name_meta": meta.get("name"),
                "description": meta.get("description"),
                "author": meta.get("author"),
                "version": meta.get("version"),
                "settings": [
                    {
                        "key": s["key"],
                        "type": s["type"],
                        "label": s.get("label"),
                        "default": s.get("default"),
                        "attrs": {k: v for k, v in s.get("attrs", {}).items() if not k.startswith("_")},
                        "value": effective_values(base, [s], settings)[s["key"]],
                    }
                    for s in meta["settings"]
                    if s.get("key")
                ],
            }
            installed.append(entry)
    installed.sort(key=lambda e: e["name"])

    return json.dumps({"installed": installed}, indent=2), False


def tool_read_plugin(args: dict):
    name = args.get("name")
    if not isinstance(name, str) or not name:
        raise ClaudifyError("`name` is required.")
    full = resolve_existing_name(name)
    return read_plugin_source(full), False


def tool_write_plugin(args: dict):
    name = args.get("name")
    code = args.get("code")
    kind = args.get("kind")
    if not isinstance(name, str) or not name:
        raise ClaudifyError("`name` is required.")
    if not isinstance(code, str):
        raise ClaudifyError("`code` (string) is required.")
    stripped = name.strip()
    if stripped.endswith(".off"):
        stripped = stripped[: -len(".off")]

    if FULL_NAME_RE.match(stripped):
        full = stripped
    else:
        if kind is None:
            try:
                full = resolve_existing_name(stripped)
            except ClaudifyError:
                raise ClaudifyError(
                    f"{name!r} has no extension and no matching plugin exists yet; "
                    "pass kind=\"css\" or kind=\"js\", or include the extension in `name`."
                )
        elif kind in ("css", "js"):
            full = f"{stripped}.{kind}"
        else:
            raise ClaudifyError("`kind` must be \"css\" or \"js\" when `name` has no extension.")
    validate_full_name(full)

    k = kind_of(full)
    d = DIR_FOR_KIND[k]
    on_path, off_path = variant_paths(full)
    os.makedirs(d, exist_ok=True)
    was_off = os.path.exists(off_path) and not os.path.exists(on_path)
    target = off_path if was_off else on_path
    with open(target, "w", encoding="utf-8") as f:
        f.write(code)

    note = f"Wrote {full}: live immediately in every Claude window" + (
        " (plugin is currently disabled, so it stays off until re-enabled)." if was_off else "."
    )
    return note, False


def tool_set_plugin_enabled(args: dict):
    name = args.get("name")
    enabled = args.get("enabled")
    if not isinstance(name, str) or not name:
        raise ClaudifyError("`name` is required.")
    if not isinstance(enabled, bool):
        raise ClaudifyError("`enabled` (boolean) is required.")
    full = resolve_existing_name(name)
    on_path, off_path = variant_paths(full)
    if enabled:
        if os.path.exists(off_path) and not os.path.exists(on_path):
            os.replace(off_path, on_path)
        elif not os.path.exists(on_path):
            raise ClaudifyError(f"{full} is missing on disk.")
        return f"{full} enabled.", False
    else:
        if os.path.exists(on_path):
            os.replace(on_path, off_path)
        elif not os.path.exists(off_path):
            raise ClaudifyError(f"{full} is missing on disk.")
        return f"{full} disabled.", False


def tool_delete_plugin(args: dict):
    name = args.get("name")
    if not isinstance(name, str) or not name:
        raise ClaudifyError("`name` is required.")
    full = resolve_existing_name(name)
    on_path, off_path = variant_paths(full)
    removed = False
    for p in (on_path, off_path):
        if os.path.exists(p):
            os.remove(p)
            removed = True
    settings = load_settings()
    if full in settings:
        del settings[full]
        save_settings(settings)
    for f in os.listdir(FILES_DIR) if os.path.isdir(FILES_DIR) else []:
        if f.startswith(f"{full}--"):
            os.remove(os.path.join(FILES_DIR, f))
    if not removed:
        raise ClaudifyError(f"{full} was already gone.")
    return f"Deleted {full} (settings entry dropped too).", False


def tool_configure_plugin(args: dict):
    name = args.get("name")
    values = args.get("values")
    if not isinstance(name, str) or not name:
        raise ClaudifyError("`name` is required.")
    if not isinstance(values, dict):
        raise ClaudifyError("`values` (object) is required.")
    full = resolve_existing_name(name)
    src = read_plugin_source(full)
    meta = parse_header(src)
    schema = {s["key"]: s for s in meta["settings"] if s.get("key")}
    if not schema:
        raise ClaudifyError(f"{full} declares no @setting entries.")

    validated = {}
    for key, value in values.items():
        if key not in schema:
            raise ClaudifyError(f"Unknown setting {key!r} for {full} (known: {sorted(schema)}).")
        if schema[key]["type"] == "file":
            validated[key] = store_file_setting(full, schema[key], value)
        else:
            validated[key] = validate_setting_value(schema[key], value)

    settings = load_settings()
    current = settings.get(full, {})
    current = dict(current) if isinstance(current, dict) else {}
    current.update(validated)
    settings[full] = current
    save_settings(settings)

    effective = effective_values(full, list(schema.values()), settings)
    return json.dumps({"name": full, "values": effective}, indent=2), False


def tool_query_dom(args: dict):
    selector = args.get("selector")
    if not isinstance(selector, str) or not selector:
        raise ClaudifyError("`selector` is required.")
    props = args.get("props") or []
    variables = args.get("vars") or []
    limit = args.get("limit", 20)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(100, limit))
    target = args.get("target", "all")
    if target not in ("chat", "shell", "all"):
        raise ClaudifyError("`target` must be one of \"chat\", \"shell\", \"all\".")
    if not isinstance(props, list) or not all(isinstance(p, str) for p in props):
        raise ClaudifyError("`props` must be an array of strings.")
    if not isinstance(variables, list) or not all(isinstance(v, str) for v in variables):
        raise ClaudifyError("`vars` must be an array of strings.")

    qid = str(uuid.uuid4())
    payload = {
        "id": qid,
        "selector": selector,
        "props": props,
        "vars": variables,
        "limit": limit,
        "target": target,
    }
    write_json_atomic(QUERY_PATH, payload)

    try:
        timeout = float(os.environ.get("CLAUDIFY_QUERY_TIMEOUT", "6"))
    except ValueError:
        timeout = 6.0
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = read_json(QUERY_RESULT_PATH, None)
        if isinstance(data, dict) and data.get("id") == qid:
            if "error" in data:
                raise ClaudifyError(f"query_dom: {data['error']}")
            text = json.dumps(data, indent=2)
            if len(text) > 20000:
                text = text[:20000] + "\n... [truncated]"
            return text, False
        time.sleep(0.1)
    raise ClaudifyError(
        "Timed out waiting for a query-result.json response. Claude Desktop may not be "
        "running, or may not have the claudify loader patch installed to answer query.json."
    )


def tool_open_plugins_window(args: dict):
    os.makedirs(ROOT, exist_ok=True)
    with open(OPEN_PLUGINS_PATH, "w", encoding="utf-8") as f:
        f.write("")
    return "Touched open-plugins; the running Claude Desktop app should open its plugins window.", False


def tool_read_log(args: dict):
    lines = args.get("lines", 40)
    try:
        lines = int(lines)
    except (TypeError, ValueError):
        lines = 40
    lines = max(1, min(2000, lines))
    try:
        with open(LOADER_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            content = f.readlines()
    except FileNotFoundError:
        return "(loader.log does not exist yet)", False
    tail = "".join(content[-lines:])
    return tail if tail else "(loader.log is empty)", False


TOOL_IMPLS = {
    "list_plugins": tool_list_plugins,
    "read_plugin": tool_read_plugin,
    "write_plugin": tool_write_plugin,
    "set_plugin_enabled": tool_set_plugin_enabled,
    "delete_plugin": tool_delete_plugin,
    "configure_plugin": tool_configure_plugin,
    "query_dom": tool_query_dom,
    "open_plugins_window": tool_open_plugins_window,
    "read_log": tool_read_log,
}


SAFETY_NOTE = (
    "Prefer CSS over JS whenever a purely visual change will do. JS plugins run inside "
    "claude.ai with the user's own logged-in session, so NEVER write a script that reads or "
    "sends chat content, cookies or tokens, or makes any network request. A JS plugin file is "
    "the BODY of a function that receives one argument, `settings`; it must end by returning a "
    "cleanup function (`return () => { ... }`) that undoes every effect the script had, so it "
    "can be hot-swapped or disabled cleanly."
)

TOOLS = [
    {
        "name": "list_plugins",
        "description": (
            "List every installed CSS/JS plugin (name, kind, enabled state, "
            "@name/@description metadata, and its @setting schema with current effective values). "
            + SAFETY_NOTE
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_plugin",
        "description": (
            "Read the full source of an installed plugin by name (with or without its .css/.js "
            "extension; a bare name prefers an existing .js plugin, else .css)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Plugin file name, e.g. 'theme.css' or 'mymod'."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_plugin",
        "description": (
            "Create or overwrite a plugin file. If `name` has no .css/.js extension, either pass "
            "`kind` ('css' or 'js') or rely on an existing plugin of that bare name to infer it. "
            "If the plugin is currently disabled (.off), it stays disabled after writing. "
            "CSS and JS changes apply live immediately in every Claude window. " + SAFETY_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Plugin file name, with or without extension."},
                "code": {"type": "string", "description": "Full file contents to write."},
                "kind": {"type": "string", "enum": ["css", "js"], "description": "Required if `name` has no extension and no matching plugin already exists."},
            },
            "required": ["name", "code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_plugin_enabled",
        "description": "Enable or disable an installed plugin by renaming it to/from its .off variant.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Installed plugin name, with or without extension."},
                "enabled": {"type": "boolean"},
            },
            "required": ["name", "enabled"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_plugin",
        "description": "Permanently remove an installed plugin (both its enabled and disabled variants) and drop its settings.json entry. ",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Installed plugin name, with or without extension."}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "configure_plugin",
        "description": (
            "Set one or more @setting values for an installed plugin, validated against the "
            "plugin's own @setting declarations (color as #rrggbb, range/number clamped to "
            "min/max, select restricted to its options, toggle as boolean, text sanitized and "
            "capped at 200 chars, file as \"\" or an absolute path that is copied in), merged atomically into settings.json. Returns the new "
            "effective values for every declared setting of the plugin. In CSS, declared "
            "settings are consumed as {{key}}, {{key|hsl}}, {{key|hsl:+4}}, {{key|hex:-8}}, "
            "{{key|hsl:+lift*1.5}}, {{key|hsl:~4}} (away from the color's own brightness), {{key|ink:otherkey}} (readable on another color; filters chain) and /* @if key */ ... /* @endif */ or /* @if key=value */ "
            "blocks; a JS plugin receives all of them as its `settings` argument."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Installed plugin name, with or without extension."},
                "values": {"type": "object", "description": "Map of setting key to new value."},
            },
            "required": ["name", "values"],
            "additionalProperties": False,
        },
    },
    {
        "name": "query_dom",
        "description": (
            "Ask the running Claude Desktop app (via query.json/query-result.json) for elements "
            "matching a CSS selector: their computed-style `props`, CSS custom-property `vars` "
            "(e.g. '--cds-hsl-gray-8xx', '--cds-clay', '--cds-text-primary'), up to `limit` "
            "matches (default 20, max 100), scoped to target 'chat', 'shell', or 'all'. "
            "ALWAYS inspect with this before writing CSS/JS that styles or reads specific "
            "elements. Polls for a few seconds and reports a clear error if nothing answers "
            "(the app may not be running or not patched with the claudify loader)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "props": {"type": "array", "items": {"type": "string"}, "description": "Computed-style property names to report."},
                "vars": {"type": "array", "items": {"type": "string"}, "description": "CSS custom property names to report."},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                "target": {"type": "string", "enum": ["chat", "shell", "all"], "default": "all"},
            },
            "required": ["selector"],
            "additionalProperties": False,
        },
    },
    {
        "name": "open_plugins_window",
        "description": "Ask the running Claude Desktop app to open its plugins window (the puzzle-piece button in Claude's top bar), where the user can review plugins and change their settings.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_log",
        "description": "Tail the loader's log file (R/loader.log). Useful when a plugin isn't applying.",
        "inputSchema": {
            "type": "object",
            "properties": {"lines": {"type": "integer", "default": 40, "minimum": 1, "maximum": 2000}},
            "additionalProperties": False,
        },
    },
]


INSTRUCTIONS = (
    "claudify lets you reshape Claude Desktop's own UI with plain CSS/JS files that a loader "
    "inside the app hot-applies. Rules: (1) Prefer CSS; only reach for JS when no CSS trick can "
    "do it. (2) " + SAFETY_NOTE + " (3) Before styling anything, use query_dom to find the real "
    "selectors and current computed values/custom properties. Claude's palette comes from "
    "tokens such as --cds-hsl-gray-8xx (an 'H S% L%' triplet, used as hsl(var(--cds-hsl-gray-8xx))), "
    "--cds-clay, and --cds-text-primary, not hardcoded colors. (4) Declare any user-tunable value "
    "with an @setting header line (`@setting <key> <color|range|number|select|toggle|text|file> "
    "<default> \"<label>\" [min=.. max=.. step=.. unit=.. options=a|b|c accept=image/*,.woff2 max=<MB>]`; "
    "`@group \"<label>\"` titles the settings after it; a file setting reaches CSS/JS as a data: URL, "
    "\"\" when unset, and configure_plugin sets it from an absolute path) and reference it in CSS "
    "as {{key}} (or {{key|hsl}}, {{key|hsl:+4}}, {{key|hex:-8}}, {{key|hsl:+lift*1.5}} to shift a "
    "token), and gate blocks with /* @if key */ ... /* @endif */ or /* @if key=value */; JS "
    "plugins receive the same values as their `settings` argument. A stylesheet can carry a "
    "script in a /* @script ... @endscript */ comment (no `*/` inside it); that script runs like a JS "
    "plugin. (5) Every write is live immediately. (6) write_plugin/read_plugin/etc. accept plugin "
    "names with or without their .css/.js extension."
)




def rpc_result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def rpc_error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def dispatch_tool(name: str, arguments: dict):
    impl = TOOL_IMPLS.get(name)
    if impl is None:
        return f"Unknown tool: {name}", True
    try:
        text, is_error = impl(arguments or {})
        return text, is_error
    except ClaudifyError as e:
        return str(e), True
    except Exception as e:  # a bad tool call must not take the server down
        log(f"tool {name} raised: {traceback.format_exc()}")
        return f"Internal error in {name}: {e}", True


def handle_request(req: dict):
    method = req.get("method")
    id_ = req.get("id")
    params = req.get("params") or {}
    has_id = "id" in req

    if method == "initialize":
        client_version = params.get("protocolVersion", "2024-11-05")
        result = {
            "protocolVersion": client_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "Claudify", "title": "Claudify", "version": "1.0.0", "icons": ICONS},
            "instructions": INSTRUCTIONS,
        }
        return rpc_result(id_, result) if has_id else None

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return rpc_result(id_, {}) if has_id else None

    if method == "tools/list":
        return rpc_result(id_, {"tools": TOOLS}) if has_id else None

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        text, is_error = dispatch_tool(tool_name, arguments)
        result = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["isError"] = True
        return rpc_result(id_, result) if has_id else None

    if has_id:
        return rpc_error(id_, -32601, f"Method not found: {method}")
    return None


def main() -> None:
    os.makedirs(ROOT, exist_ok=True)
    log(f"claudify_mcp starting, root={ROOT}")
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            log(f"bad JSON on stdin: {e}: {line[:200]!r}")
            continue
        if not isinstance(req, dict):
            log(f"ignoring non-object message: {line[:200]!r}")
            continue
        try:
            resp = handle_request(req)
        except Exception:
            log(f"unhandled exception handling request: {traceback.format_exc()}")
            resp = rpc_error(req.get("id"), -32603, "Internal error") if "id" in req else None
        if resp is not None:
            try:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
            except Exception:
                log(f"failed writing response: {traceback.format_exc()}")
    log("stdin closed, exiting")


if __name__ == "__main__":
    main()
