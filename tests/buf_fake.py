"""A fake buf for the .proto contract tests (Sprint 7.6 Part D). No real binary is needed.

The fake is a Python script at <base>/bin/buf. `--version` is handled first: it appends a line to <base>/versions.log,
prints `1.75.0` (or what the mode says) and exits 0, without touching calls.jsonl (so the refresh() probes at
SessionStart, Stop and ci.run do not count as runs). Every other call appends {"argv", "cwd", "env" (sorted names
starting with BUF_, plus the values of BUF_CACHE_DIR, BUF_CONFIG_DIR, BUF_DATA_DIR and HOME), "listing" (every file
under the cwd, relative), "configs" (text of rev/buf.yaml, base/buf.yaml and lg-buf.yaml, None when absent), "stdin",
"bytes" ({listing path: hex of the bytes} for .proto files)} to <base>/calls.jsonl, then behaves per <base>/mode.

The output shape is pinned to what buf v1.75.0 (buf-Darwin-arm64, sha256 f71b009e...330b) printed when it was run here
(2026-11-26) with `breaking rev#format=dir --against base#format=dir --error-format json --disable-symlinks` in a folder
holding rev/ and base/, each with a v2 buf.yaml: one compact JSON object per line, keys in the order path, start_line,
start_column, end_line, end_column, type, message, `path` omitted when buf has no location; paths printed as `rev/<m>`
(or `base/<m>` for a COMPILE error in the baseline); exit 100 with annotations, no output and exit 0 without.
Shapes seen on the real binary, and copied here:
- FILE_NO_DELETE: no `path`, positions 1/1/1/1, message `Previously present file "<m>" was deleted.`;
- SERVICE_NO_DELETE and MESSAGE_NO_DELETE: `path` of the new file, positions 1/1/1/1;
- FIELD_NO_DELETE: the span of the message (`message X {` to its `}`); FIELD_SAME_TYPE: the type token of the field;
  FIELD_SAME_NAME: the name token, message `Field "<n>" on message "<M>" changed name from "<a>" to "<b>".`;
  RPC_NO_DELETE: the span of the service;
- annotations sorted by path ("" first), then positions;
- a syntax error or a missing import is a COMPILE line on stdout (the import: message `imported file does not
  exist`, positions of the import statement), exit 100, nothing on stderr; an empty side prints
  `Failure: Module "path: \".\"" had no .proto files` on stderr and exits 1; usage errors exit 1.
The patterns are a stand-in, not buf's rules: a file gone, `message <Name> {` gone, a field line `<type> <name> = <n>;`
whose number is gone / whose type or name changed, `rpc <Name>(` gone, `service <Name> {` gone. Under WIRE_JSON the
fake drops MESSAGE_NO_DELETE and FILE_NO_DELETE (stand-in for buf's category sets).

Tokens: COMPILEERR in a rev (or base) file gives only a COMPILE line for it; IMPORTMISSING gives the real COMPILE line
for a missing import (the import line must be in the file); STDERRIMPORT gives the stderr form the plan sourced
(`Failure: rev/<m>: import "third/x.proto": file does not exist`, empty stdout, exit 100); PACKAGEGONE in a base file
adds a path-less PACKAGE_NO_DELETE line.

Modes: breaking (default), garbage, array, rc1, rc0-nonempty, rc100-empty, bad-field, zero-line, abs-path,
foreign-path, plugin, slow, hang-child, probe, old-version, dev-version, garbage-version.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPT = r'''#!{python}
import json, os, re, socket, subprocess, sys, time
BASE = {base!r}
try:
    mode = open(os.path.join(BASE, "mode")).read().strip()
except OSError:
    mode = "breaking"
if sys.argv[1:2] == ["--version"]:
    with open(os.path.join(BASE, "versions.log"), "a") as fh:
        fh.write("version\n")
    print({{"old-version": "1.31.0", "dev-version": "1.75.1-dev", "garbage-version": "buf"}}.get(mode, "1.75.0"))
    sys.exit(0)

args = sys.argv[1:]
stdin = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""


def read(p):
    try:
        with open(p, "r", newline="") as fh:
            return fh.read()
    except OSError:
        return None


listing = []
for d, _, fs in os.walk("."):
    for f in fs:
        listing.append(os.path.relpath(os.path.join(d, f), ".").replace(os.sep, "/"))
listing.sort()
envnames = sorted(k for k in os.environ if k.startswith("BUF_"))
try:
    with open(os.path.join(BASE, "calls.jsonl"), "a") as fh:
        fh.write(json.dumps({{"argv": args, "cwd": os.getcwd(), "env": envnames,
                             "values": {{k: os.environ.get(k) for k in ("BUF_CACHE_DIR", "BUF_CONFIG_DIR", "BUF_DATA_DIR", "HOME")}},
                             "listing": listing,
                             "configs": {{p: read(p) for p in ("rev/buf.yaml", "base/buf.yaml", "lg-buf.yaml")}},
                             "stdin": stdin.decode("latin-1"),
                             "bytes": {{p: open(p, "rb").read().hex() for p in listing if p.endswith(".proto")}}}}) + "\n")
except OSError:
    pass

if args[:4] != ["breaking", "rev#format=dir", "--against", "base#format=dir"]:
    sys.stderr.write("unexpected arguments\n")
    sys.exit(1)
if mode == "probe":
    res = {{}}
    try:
        with open(open(os.path.join(BASE, "probe_target")).read().strip(), "w") as fh:
            fh.write("x")
        res["write"] = "ok"
    except OSError:
        res["write"] = "denied"
    try:
        port = int(open(os.path.join(BASE, "port")).read())
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()
        res["connect"] = "ok"
    except OSError:
        res["connect"] = "denied"
    with open(os.path.join(BASE, "probe.json"), "w") as fh:
        json.dump(res, fh)
if mode == "slow":
    time.sleep(3)
if mode == "hang-child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.path.join(BASE, "hang.pids"), "w") as fh:
        fh.write("%d %d" % (os.getpid(), child.pid))
    time.sleep(60)
if mode == "rc1":
    sys.stderr.write("Failure: something\n")
    sys.exit(1)
if mode == "rc100-empty":
    sys.exit(100)
if mode == "garbage":
    print("not json")
    sys.exit(100)


def side(name):
    out = {{}}
    for p in listing:
        if p.startswith(name + "/") and p.endswith(".proto"):
            with open(p, "rb") as fh:
                out[p[len(name) + 1:]] = fh.read().decode("utf-8", "replace").lstrip("﻿")
    return out


base, rev = side("base"), side("rev")
category = "FILE"
cfg = read("rev/buf.yaml") or ""
m = re.search(r"use:\s*\n\s*-\s*(\w+)", cfg)
if m:
    category = m.group(1)
if not rev or not base:
    sys.stderr.write('Failure: Module "path: \\".\\"" had no .proto files\n')
    sys.exit(1)


def lines(text):
    return re.split(r"\r\n|\r|\n", text)


def ann(path, pos, typ, msg):
    d = {{}}
    if path is not None:
        d["path"] = path
    d.update(start_line=pos[0], start_column=pos[1], end_line=pos[2], end_column=pos[3], type=typ, message=msg)
    return d


for name, files in (("rev", rev), ("base", base)):
    for mod, text in sorted(files.items()):
        ls = lines(text)
        for i, l in enumerate(ls, 1):
            if "COMPILEERR" in l:
                print(json.dumps(ann(name + "/" + mod, (i, 1, i, 2), "COMPILE", "unexpected `}}` after definition"), separators=(",", ":")))
                sys.exit(100)
            if "STDERRIMPORT" in l:
                sys.stderr.write('Failure: %s/%s: import "third/x.proto": file does not exist\n' % (name, mod))
                sys.exit(100)
            if "IMPORTMISSING" in l:
                j = next((k for k, x in enumerate(ls, 1) if x.lstrip().startswith("import ")), i)
                print(json.dumps(ann(name + "/" + mod, (j, 1, j, len(ls[j - 1]) + 1), "COMPILE", "imported file does not exist"), separators=(",", ":")))
                sys.exit(100)


def blocks(text, word):
    """{{name: (start line, end line, body lines with numbers)}} for `<word> <Name> {{` blocks (not nested)."""
    out, ls, i = {{}}, lines(text), 0
    while i < len(ls):
        m = re.match(r"\s*%s\s+(\w+)\s*\{{" % word, ls[i])
        if m:
            depth, j = 0, i
            while j < len(ls):
                depth += ls[j].count("{{") - ls[j].count("}}")
                if depth <= 0:
                    break
                j += 1
            out[m.group(1)] = (i + 1, j + 1, [(k + 1, ls[k]) for k in range(i + 1, j)])
            i = j
        i += 1
    return out


FIELD = re.compile(r"^(\s*)([\w.]+)\s+(\w+)\s*=\s*(\d+)\s*;")
out = []
for mod in sorted(set(base) - set(rev)):
    out.append(ann(None, (1, 1, 1, 1), "FILE_NO_DELETE", "Previously present file \"%s\" was deleted." % mod))
for mod in sorted(set(base) & set(rev)):
    b, r = base[mod], rev[mod]
    path = "rev/" + mod
    if "PACKAGEGONE" in b:
        out.append(ann(None, (1, 1, 1, 1), "PACKAGE_NO_DELETE", "Previously present package \"gone.v1\" was deleted."))
    bm, rm = blocks(b, "message"), blocks(r, "message")
    for name in sorted(set(bm) - set(rm)):
        out.append(ann(path, (1, 1, 1, 1), "MESSAGE_NO_DELETE", "Previously present message \"%s\" was deleted from file." % name))
    for name in sorted(set(bm) & set(rm)):
        old = {{}}
        for _, l in bm[name][2]:
            f = FIELD.match(l)
            if f:
                old[f.group(4)] = (f.group(2), f.group(3))
        new = {{}}
        for n, l in rm[name][2]:
            f = FIELD.match(l)
            if f:
                new[f.group(4)] = (f.group(2), f.group(3), n, l, len(f.group(1)) + 1)
        for num in sorted(set(old) - set(new), key=int):
            out.append(ann(path, (rm[name][0], 1, rm[name][1], 2), "FIELD_NO_DELETE",
                           "Previously present field \"%s\" with name \"%s\" on message \"%s\" was deleted." % (num, old[num][1], name)))
        for num in sorted(set(old) & set(new), key=int):
            (ot, on), (nt, nn, n, l, col) = old[num], new[num]
            if ot != nt:
                out.append(ann(path, (n, col, n, col + len(nt)), "FIELD_SAME_TYPE",
                               "Field \"%s\" with name \"%s\" on message \"%s\" changed type from \"%s\" to \"%s\"." % (num, nn, name, ot, nt)))
            if on != nn:
                c = l.index(nn, col + len(nt) - 1) + 1
                out.append(ann(path, (n, c, n, c + len(nn)), "FIELD_SAME_NAME",
                               "Field \"%s\" on message \"%s\" changed name from \"%s\" to \"%s\"." % (num, name, on, nn)))
    bs, rs = blocks(b, "service"), blocks(r, "service")
    for name in sorted(set(bs) - set(rs)):
        out.append(ann(path, (1, 1, 1, 1), "SERVICE_NO_DELETE", "Previously present service \"%s\" was deleted from file." % name))
    for name in sorted(set(bs) & set(rs)):
        brpc = {{re.match(r"\s*rpc\s+(\w+)", l).group(1) for _, l in bs[name][2] if re.match(r"\s*rpc\s+\w+\s*\(", l)}}
        rrpc = {{re.match(r"\s*rpc\s+(\w+)", l).group(1) for _, l in rs[name][2] if re.match(r"\s*rpc\s+\w+\s*\(", l)}}
        for rpc in sorted(brpc - rrpc):
            out.append(ann(path, (rs[name][0], 1, rs[name][1], 2), "RPC_NO_DELETE",
                           "Previously present RPC \"%s\" on service \"%s\" was deleted." % (rpc, name)))
if category == "WIRE_JSON":
    out = [x for x in out if x["type"] not in ("MESSAGE_NO_DELETE", "FILE_NO_DELETE")]
out.sort(key=lambda x: (x.get("path", ""), x["start_line"], x["start_column"], x["end_line"], x["end_column"], x["type"], x["message"]))

if mode == "array":
    print(json.dumps(out))
    sys.exit(100 if out else 0)
if mode == "bad-field":
    out = [dict(x, start_line="1") for x in out] or [{{"start_line": "1", "type": "X", "message": "m"}}]
if mode == "zero-line":
    out = [dict(x, start_line=0) for x in out] or [{{"start_line": 0, "type": "X", "message": "m"}}]
if mode == "abs-path":
    out = [dict(x, path=os.path.join(os.getcwd(), x["path"])) if "path" in x else x for x in out]
if mode == "foreign-path":
    out = [dict(x, path="other/x.proto") if "path" in x else x for x in out] + [ann("other/x.proto", (2, 1, 2, 2), "FIELD_NO_DELETE", "m")]
if mode == "plugin":
    out = [dict(x, plugin="p") for x in out]
for x in out:
    print(json.dumps(x, separators=(",", ":")))
if mode == "rc0-nonempty":
    if not out:
        print(json.dumps(ann(None, (1, 1, 1, 1), "FILE_NO_DELETE", "m"), separators=(",", ":")))
    sys.exit(0)
sys.exit(100 if out else 0)
'''


def install(base: Path, mode: str = "breaking") -> Path:
    """Write the fake to base/bin/buf and set its mode. Returns the bin folder."""
    bin_dir = base / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "buf"
    path.write_text(SCRIPT.format(python=sys.executable, base=str(base)))
    os.chmod(path, 0o755)
    set_mode(base, mode)
    return bin_dir


def set_mode(base: Path, mode: str) -> None:
    (base / "mode").write_text(mode)


def calls(base: Path) -> list[dict]:
    p = base / "calls.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def version_calls(base: Path) -> int:
    p = base / "versions.log"
    return len(p.read_text().splitlines()) if p.exists() else 0


def strip_prefix(argv: list[str], base: Path) -> list[str]:
    """argv after the fake's own path (a sandbox prefix may come before it)."""
    me = str(base / "bin" / "buf")
    return argv[argv.index(me) + 1:] if me in argv else argv
