#!/usr/bin/env python3
"""Minimal Electron asar patcher (no node needed).

asar layout: [u32 4][u32 headerPickleLen][u32 payloadLen][u32 jsonLen] json pad* data...
Data offsets in the header JSON are relative to the end of the (padded) header.

Commands:
  patch  <app.asar> <loader.js> [extra...]
                                  inject loader as <main dir>/claudify.js (plus each extra
                                  file as <main dir>/claudify-<basename>) and prepend a
                                  require() to the main entry (idempotent)
  status <app.asar>               print "patched" / "clean"
"""
import hashlib, json, os, struct, sys

MARK = '/*claudify*/require("./claudify.js");\n'
LOADER_NAME = "claudify.js"
BLOCK = 4 * 1024 * 1024


def read_header(f):
    f.seek(0)
    _, pickle_len, _, json_len = struct.unpack("<IIII", f.read(16))
    hdr = json.loads(f.read(json_len))
    return hdr, 8 + pickle_len  # data base


def walk(node, pre=""):
    for k, v in node.get("files", {}).items():
        if "files" in v:
            yield from walk(v, pre + k + "/")
        else:
            yield pre + k, v


def lookup(hdr, path):
    node = hdr
    for part in path.split("/"):
        node = node["files"][part]
    return node


def integrity(data):
    blocks = [hashlib.sha256(data[i:i + BLOCK]).hexdigest() for i in range(0, max(len(data), 1), BLOCK)]
    return {"algorithm": "SHA256", "hash": hashlib.sha256(data).hexdigest(),
            "blockSize": BLOCK, "blocks": blocks}


def patch(asar_path, loader_path, extras=()):
    with open(asar_path, "rb") as f:
        hdr, base = read_header(f)
        main = lookup(hdr, "package.json")
        f.seek(base + int(main["offset"])); pkg = json.loads(f.read(main["size"]))
        main_path = pkg["main"]
        main_dir = main_path.rsplit("/", 1)[0]
        # collect blobs, sharing by (offset,size)
        blobs, shared = {}, {}
        size = os.fstat(f.fileno()).st_size
        for path, ent in walk(hdr):
            if "offset" not in ent:
                continue
            off, n = int(ent["offset"]), ent["size"]
            if base + off + n > size:
                sys.exit(f"corrupt entry {path}: {off}+{n} beyond EOF")
            f.seek(base + off); shared.setdefault((off, n), f.read(n))
            blobs[path] = (off, n)
    main_data = shared[blobs[main_path]]
    if main_data.startswith(MARK.encode()):
        print("already patched"); return
    new_main = MARK.encode() + main_data
    added = {LOADER_NAME: open(loader_path, "rb").read()}
    for x in extras:
        added["claudify-" + os.path.basename(x)] = open(x, "rb").read()
    # rebuild: unique data per path (dedupe by source key)
    out_hdr = hdr
    ent_main = lookup(out_hdr, main_path)
    ent_main["integrity"] = integrity(new_main)
    d = lookup(out_hdr, main_dir)["files"]
    for name, data in added.items():
        d[name] = {"size": len(data), "offset": "0", "integrity": integrity(data)}
    new_paths = {f"{main_dir}/{n}": data for n, data in added.items()}
    # assign data per path
    data_by_path = {}
    for path, ent in walk(out_hdr):
        if "offset" not in ent:
            continue
        if path == main_path: data_by_path[path] = new_main
        elif path in new_paths: data_by_path[path] = new_paths[path]
        else: data_by_path[path] = shared[blobs[path]]
    # lay out, deduping identical source blobs
    pos, placed = 0, {}
    for path, ent in walk(out_hdr):
        if "offset" not in ent:
            continue
        key = path if path == main_path or path in new_paths else blobs[path]
        if key in placed:
            ent["offset"] = str(placed[key]); continue
        placed[key] = pos; ent["offset"] = str(pos); ent["size"] = len(data_by_path[path]); pos += ent["size"]
    j = json.dumps(out_hdr, separators=(",", ":")).encode()
    pad = (4 - len(j) % 4) % 4
    tmp = asar_path + ".tmp"
    with open(tmp, "wb") as out:
        # Chromium Pickle: payload size counts the alignment padding
        out.write(struct.pack("<IIII", 4, 8 + len(j) + pad, 4 + len(j) + pad, len(j)))
        out.write(j); out.write(b"\0" * pad)
        done = set()
        for path, ent in walk(out_hdr):
            if "offset" not in ent or ent["offset"] in done:
                continue
            done.add(ent["offset"]); out.write(data_by_path[path])
    os.replace(tmp, asar_path)
    print(f"patched {asar_path}: main={main_path}, added {', '.join(sorted(new_paths))}, {pos} data bytes")


def status(asar_path):
    with open(asar_path, "rb") as f:
        hdr, base = read_header(f)
        main = lookup(hdr, "package.json")
        f.seek(base + int(main["offset"])); pkg = json.loads(f.read(main["size"]))
        ent = lookup(hdr, pkg["main"])
        f.seek(base + int(ent["offset"]))
        print("patched" if f.read(len(MARK)) == MARK.encode() else "clean")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "patch": patch(sys.argv[2], sys.argv[3], sys.argv[4:])
    elif len(sys.argv) == 3 and sys.argv[1] == "status": status(sys.argv[2])
    else: sys.exit(__doc__)
