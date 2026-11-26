"""A fake squawk for the SQL migration tests (Sprint 7.6 Part C). No real binary is needed.

The fake is a Python script at <base>/bin/squawk. `--version` is handled first: it appends a line to
<base>/versions.log, prints `squawk 2.74.0` and exits 0, without touching calls.jsonl (so the refresh() probes at
SessionStart, Stop and ci.run do not count as runs). Every other call appends {"argv", "cwd", "env" (sorted names
starting with SQUAWK or GITHUB), "config" (text of the --config file or None), "listing" (the cwd's entries),
"files" ({name: text})} to
<base>/calls.jsonl, then behaves per <base>/mode.

The output shape is pinned to what squawk-cli 2.74.0 printed when it was run here (2026-11-26), which matches squawk's
own `display_violations_json` and `skip_lint_on_syntax_error` snapshots (reporter.rs at v2.74.0): one flat compact JSON
array over all files, files in path order (after.sql before before.sql), violations by start offset; each element has
the ten fields file, line, column, level, message, note, help, rule_name, column_end, line_end in that order; `level`
"Warning" (lint) or "Error" (syntax-error only); zero-based lines, columns as zero-based UTF-8 byte offsets in the line;
a leading BOM is skipped and `\\n`, `\\r\\n` and a lone `\\r` each end a line; exit 1 with violations, `[]` and exit 0
without; a path that matches nothing prints "Failed to find files for provided patterns" on stderr and exits 1.
The messages are the real ones; the patterns are a stand-in, not squawk's rules: `drop column|view|table|function <x>`,
`alter table <t> drop <x>` (not column, constraint, default, not, identity), `rename column`, `create index` without
`concurrently`, `add column <x> <type> not null` without `default` (adding-required-field), `add column` (prefer-robust-
stmts), `alter table` (require-lock-timeout). `-- squawk-ignore-file` drops that file's violations, as squawk does.

Modes: lint (default), drop-attr, abs-file, garbage, object, rc2, config-error, bad-field, rc0-nonempty, slow,
hang-child. Tokens: FAKE_NEW_RULE2 (rule ban-other-new), FAKE_NEW_RULE (ban-something-new), SYNTAXERR (only a
syntax-error entry for that file).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPT = r'''#!{python}
import json, os, re, subprocess, sys, time
BASE = {base!r}
if sys.argv[1:2] == ["--version"]:
    with open(os.path.join(BASE, "versions.log"), "a") as fh:
        fh.write("version\n")
    print("squawk 2.74.0")
    sys.exit(0)

args = sys.argv[1:]
config, paths, i = None, [], 0
while i < len(args):
    a = args[i]
    if a in ("--reporter", "--config", "-c", "--pg-version"):
        if a in ("--config", "-c"):
            config = args[i + 1]
        i += 2
        continue
    if a.startswith("--"):
        i += 1
        continue
    paths.append(a)
    i += 1


def read(p):
    try:
        with open(p, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None


try:
    with open(os.path.join(BASE, "calls.jsonl"), "a") as fh:
        fh.write(json.dumps({{"argv": args, "cwd": os.getcwd(),
                             "env": sorted(k for k in os.environ if k.startswith(("SQUAWK", "GITHUB"))),
                             "config": read(config) if config else None,
                             "listing": sorted(os.listdir(".")),
                             "files": {{p: read(p) for p in paths}}}}) + "\n")
except OSError:
    pass
try:
    mode = open(os.path.join(BASE, "mode")).read().strip()
except OSError:
    mode = "lint"

if mode == "garbage":
    print("not json"); sys.exit(1)
if mode == "object":
    print("{{}}"); sys.exit(1)
if mode == "rc2":
    sys.stderr.write("error: unexpected argument '--fake' found\n\nUsage: squawk [OPTIONS] [path]... [COMMAND]\n")
    sys.exit(2)
if mode == "config-error":
    sys.stderr.write("Configuration error: invalid type: integer `3`, expected a sequence for key `excluded_rules`\n")
    sys.exit(1)
if mode == "slow":
    time.sleep(3)
if mode == "hang-child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.path.join(BASE, "hang.pids"), "w") as fh:
        fh.write("%d %d" % (os.getpid(), child.pid))
    time.sleep(60)

for p in paths:
    if not os.path.isfile(p):
        sys.stderr.write("Failed to find files for provided patterns: %s\n" % json.dumps([p]))
        sys.exit(1)

NAME = r'"?\w+"?'
NOT_COLUMN = r"(?!(?:column|constraint|default|not|identity)\b)"
# (pattern, group whose span is reported, rule, message, note, help)
RULES = [
    (r"\bdrop\s+column\s+(?:if\s+exists\s+)?" + NAME, 0, "ban-drop-column", "dropping a column may break existing clients", None, None),
    # `alter table <t> drop <x>` without `column` (Postgres accepts it): squawk reports ban-drop-column on `drop <x>`
    (r"\balter\s+table\s+(?:if\s+exists\s+)?" + NAME + r"\s+(drop\s+" + NOT_COLUMN + NAME + ")", 1, "ban-drop-column",
     "dropping a column may break existing clients", None, None),
    (r"\bdrop\s+view\s+(?:if\s+exists\s+)?" + NAME, 0, "ban-drop-view", "dropping a view may break existing clients", None, None),
    (r"\bdrop\s+table\s+(?:if\s+exists\s+)?" + NAME, 0, "ban-drop-table", "dropping a table may break existing clients", None, None),
    (r"\bdrop\s+function\s+(?:if\s+exists\s+)?" + NAME, 0, "ban-drop-function", "dropping a function may break existing clients", None, None),
    (r"\brename\s+column\b", 0, "renaming-column", "renaming a column may break existing clients", None, None),
    (r"\bcreate\s+(?:unique\s+)?index\b(?!\s+concurrently\b)", 0, "require-concurrent-index-creation",
     "during normal index creation, table updates are blocked, but reads are still allowed", None,
     "use `concurrently` to avoid blocking writes"),
    (r"\badd\s+column\s+(?!if\s+not\s+exists\b)" + NAME + r"\s+\w+\s+not\s+null\b(?![^;]*\bdefault\b)", 0, "adding-required-field",
     "adding a new column that is `not null` and has no default value to an existing table effectively makes it required",
     None, "make the field nullable or add a non-volatile default"),
    (r"\badd\s+column\s+(?!if\s+not\s+exists\b)" + NAME + r"\s+\w+(?:\s+not\s+null\b)?", 0, "prefer-robust-stmts",
     "missing `if not exists`, the migration can't be rerun if it fails part way through", None, None),
    (r"\balter\s+table\b", 0, "require-lock-timeout", "missing `set lock_timeout` before potentially slow `access exclusive` lock operations",
     "statement requires: `access exclusive` lock; blocking: reads, writes, schema changes", "configure a `lock_timeout` before this statement"),
]
DROP_ATTR = re.compile(r"\balter\s+type\s+" + NAME + r"\s+(drop\s+attribute\s+" + NAME + ")", re.I)


def positions(text):
    """(line, byte column) for every character offset, with squawk's line breaks: \n, \r\n and a lone \r."""
    out, line, col, i = [], 0, 0, 0
    while i <= len(text):
        out.append((line, col))
        if i == len(text):
            break
        ch = text[i]
        if ch == "\n" or (ch == "\r" and text[i + 1:i + 2] != "\n"):
            line, col = line + 1, 0
        else:
            col += len(ch.encode("utf-8"))
        i += 1
    return out


def violation(name, text, pos, start, end, rule, message, note=None, help=None, level="Warning"):
    (l0, c0), (l1, c1) = pos[start], pos[end]
    return {{"file": name, "line": l0, "column": c0, "level": level, "message": message, "note": note, "help": help,
             "rule_name": rule, "column_end": c1, "line_end": l1}}


def lint(name, text):
    if text.startswith("\ufeff"):
        text = text[1:]
    pos = positions(text)
    if "SYNTAXERR" in text:
        at = text.index("SYNTAXERR")
        return [violation(name, text, pos, at, at, "syntax-error", "expected command, found identifier", level="Error")]
    if re.search(r"^\s*--\s*squawk-ignore-file", text, re.M):
        return []
    found = []
    for pattern, group, rule, message, note, help in RULES:
        for m in re.finditer(pattern, text, re.I):
            found.append(violation(name, text, pos, m.start(group), m.end(group), rule, message, note, help))
    if mode == "drop-attr":
        for m in DROP_ATTR.finditer(text):
            found.append(violation(name, text, pos, m.start(1), m.end(1), "ban-drop-column",
                                   "dropping an attribute may break existing clients"))
    for token, rule in (("FAKE_NEW_RULE2", "ban-other-new"), ("FAKE_NEW_RULE", "ban-something-new")):
        at = text.find(token)
        if at >= 0:
            found.append(violation(name, text, pos, at, at + len(token), rule, "a rule this LegacyGuard does not know"))
            text = text.replace(token, " " * len(token))
    found.sort(key=lambda v: (v["line"], v["column"]))
    return found


out = []
for p in sorted(paths):
    out += lint(p, read(p) or "")
if mode == "abs-file":
    for v in out:
        v["file"] = os.path.join(os.getcwd(), v["file"])
if mode == "bad-field" and out:
    out[0]["line"] = True
if mode == "bad-field" and not out:
    out = [{{"file": "after.sql", "line": True, "column": 0, "level": "Warning", "message": "m", "note": None,
            "help": None, "rule_name": "ban-drop-view", "column_end": 0, "line_end": 0}}]
if mode == "rc0-nonempty":
    if not out:
        out = [{{"file": "after.sql", "line": 0, "column": 0, "level": "Warning", "message": "m", "note": None,
                "help": None, "rule_name": "ban-drop-view", "column_end": 0, "line_end": 0}}]
    print(json.dumps(out, separators=(",", ":")))
    sys.exit(0)
print(json.dumps(out, separators=(",", ":")))
sys.exit(1 if out else 0)
'''


def install(base: Path, mode: str = "lint") -> Path:
    """Write the fake to base/bin/squawk and set its mode. Returns the bin folder."""
    bin_dir = base / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "squawk"
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
