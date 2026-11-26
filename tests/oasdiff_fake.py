"""A fake oasdiff for the contract-tool tests (Sprint 7.6). No real binary is needed or downloaded.

The fake is a Python script at <base>/bin/oasdiff. It appends its argv, cwd and the OASDIFF_* variables it sees to
<base>/calls.jsonl (best effort: inside the OS sandbox that file is not writable), then behaves per <base>/mode.
The pinned sample has the fields and types of `formatters.Change` at oasdiff v1.39.0 (`level` an integer); the ids
are illustrative and no test depends on real rule ids.

Part B modes (the two spec files are read as JSON):
- `diff` mirrors oasdiff's levels as read in checker/rules.go at v1.39.0: for every schema present in both
  `components.schemas`, a removed property is level 3 ("property '<p>' removed from <schema>", at the first operation
  that refers to the schema); a changed `default` is level 1 (INFO, as ResponsePropertyDefaultValueChangedId); a
  changed `operationId` is level 1 (as APIOperationIdRemovedId/AddId). `breaking` prints WARN and up, so only levels
  >= 2 are printed: an all-INFO diff prints `[]`.
- `diff-loud`: the same, but a changed `default` is level 2 at the first operation whose request body refers to the
  schema. A stand-in to exercise the hedge path; real oasdiff gives INFO for a default change.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SAMPLE = [
    {"id": "api-path-removed-without-deprecation", "text": "api path removed without deprecation", "level": 3,
     "operation": "GET", "path": "/pets", "section": "paths", "fingerprint": "80a8c624dc40"},
    {"id": "response-property-became-optional", "text": "the response property 'name' became optional", "level": 2,
     "operation": "GET", "path": "/pets/{id}", "section": "paths", "fingerprint": "f8cd9af6b117"},
    {"id": "endpoint-added", "text": "endpoint added", "level": 1, "operation": "POST", "path": "/pets", "section": "paths"},
]

SCRIPT = r'''#!{python}
import json, os, sys, time
BASE = {base!r}
SAMPLE = {sample!r}
if sys.argv[1:2] == ["--version"]:
    with open(os.path.join(BASE, "versions.log"), "a") as fh:
        fh.write("version\n")
    print("oasdiff version 1.39.0")
    sys.exit(0)
try:
    with open(os.path.join(BASE, "calls.jsonl"), "a") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
                             "env": sorted(k for k in os.environ if k.startswith("OASDIFF"))}}) + "\n")
except OSError:
    pass
try:
    mode = open(os.path.join(BASE, "mode")).read().strip()
except OSError:
    mode = "ok"
sample = json.loads(SAMPLE)
if mode == "ok":
    print(json.dumps(sample))
elif mode == "fail":
    sys.stderr.write("Error: failed to load revision spec from x.yaml: bad\n")
    sys.exit(102)
elif mode == "garbage":
    print("not json")
elif mode == "object":
    print("{{}}")
elif mode == "string-levels":
    for x in sample:
        x["level"] = {{3: "error", 2: "warning", 1: "info"}}[x["level"]]
    print(json.dumps(sample))
elif mode == "no-id":
    del sample[0]["id"]
    print(json.dumps(sample))
elif mode == "slow":
    time.sleep(3)
    print(json.dumps(sample))
'''

DIFF_CODE = r'''
if mode in ("diff", "diff-loud"):
    with open(sys.argv[2]) as fh:
        old = json.load(fh)
    with open(sys.argv[3]) as fh:
        new = json.load(fh)

    def where(spec, name, body_only):
        ref = json.dumps("#/components/schemas/" + name)
        for path in sorted(spec.get("paths") or {}):
            for method in sorted(spec["paths"][path] or {}):
                op = spec["paths"][path][method]
                part = op.get("requestBody") if body_only else op
                if part is not None and ref in json.dumps(part):
                    return method.upper(), path
        return "", ""

    out = []
    sa = (old.get("components") or {}).get("schemas") or {}
    sb = (new.get("components") or {}).get("schemas") or {}
    for name in sorted(set(sa) & set(sb)):
        pa, pb = sa[name].get("properties") or {}, sb[name].get("properties") or {}
        for p in sorted(set(pa) - set(pb)):
            op, path = where(old, name, False)
            out.append({"id": "fake-property-removed", "text": "property '%s' removed from %s" % (p, name), "level": 3,
                        "operation": op, "path": path, "section": "components"})
        for p in sorted(set(pa) & set(pb)):
            if (pa[p] or {}).get("default") != (pb[p] or {}).get("default"):
                loud = mode == "diff-loud"
                op, path = where(old, name, True) if loud else where(old, name, False)
                out.append({"id": "fake-default-changed", "text": "default of '%s' changed in %s" % (p, name),
                            "level": 2 if loud else 1, "operation": op, "path": path, "section": "components"})
    for path in sorted(set(old.get("paths") or {}) & set(new.get("paths") or {})):
        for method in sorted(set(old["paths"][path] or {}) & set(new["paths"][path] or {})):
            a, b = old["paths"][path][method].get("operationId"), new["paths"][path][method].get("operationId")
            if a != b:
                out.append({"id": "fake-operation-id-changed", "text": "operationId changed", "level": 1,
                            "operation": method.upper(), "path": path, "section": "paths"})
    print(json.dumps([x for x in out if x["level"] >= 2]))
'''


def install(base: Path, mode: str = "ok") -> Path:
    """Write the fake to base/bin/oasdiff and set its mode. Returns the bin folder."""
    bin_dir = base / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "oasdiff"
    path.write_text(SCRIPT.format(python=sys.executable, base=str(base), sample=json.dumps(SAMPLE)) + DIFF_CODE)
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
