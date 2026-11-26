"""External contract tools (Sprint 7.6 Parts A and C): find a tool, cache what was found, run it, never raise.

squawk (Part C) lints `.sql` files under `contracts.db` (PostgreSQL only); its breaking rules add to the built-in
regex items, they never replace them (`squawk_lint`, `squawk_items`, `checks._squawk_breaks`).

oasdiff (`oasdiff breaking <base> <revision> -f json`) replaces the built-in OpenAPI parser for the configured
contract file when it is installed. Every public function here catches `Exception` and returns None or a safe value.

Rules:
- Decide from parsed JSON, never from exit codes. We never pass `--fail-on`, so a non-zero exit only *rejects* a run
  (the caller falls back to the parser); it never says anything about breaks. In `-f json` output `level` is an
  integer (3 = ERR, a definite break; 2 = WARN, a possible one); only `level` is always present, every other field
  (`id` included) may be missing.
- oasdiff reads URLs and sibling files named in `$ref` by default. Specs whose refs are only internal (`#/...`) run
  with `--allow-external-refs=false`, so oasdiff reads only the two files. Specs with relative file refs run only
  inside the OS sandbox (no network, tree read-only), with the referenced files copied from the right tree; with no
  sandbox available they are not given to oasdiff at all. Remote, absolute and out-of-tree refs are never given to
  oasdiff.
- oasdiff runs in a temp folder without `OASDIFF_CONFIG`, so a project's `.oasdiff.*` file (severity levels, ignore
  files, `--open`, which uploads to oasdiff.com) is ignored.

Known limits:
- oasdiff runs only when the configured contract file itself is edited (`rel == contracts.http`); an edit to a sibling
  file it `$ref`s is not diffed at PreToolUse.
- Waiver items differ between the parser and oasdiff, so a waiver made under one does not carry to the other; an
  oasdiff fingerprint can change when the base changes (arguments taken from the base, positions, renames).
- Inside the sandbox oasdiff can still *read* files outside the temp folder if the `$ref` scan missed a ref (the
  sandbox blocks the network and writes, not reads); the scan over-approximates (a `$ref` inside an `example` or an
  `x-` extension counts too) to make that unlikely.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_REF_FILES = 50
MAX_REF_BYTES = 5 * 2**20
MAX_DEPTH = 200
SKIP_AFTER_TIMEOUTS = 2
VERSION_TIMEOUT_S = 2
TOOLS = ("oasdiff", "squawk")  # Sprint 7.6 Part C: squawk for .sql files under contracts.db
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_LEVELS = {"ERR": 3, "err": 3, "error": 3, "WARN": 2, "warn": 2, "warning": 2, "INFO": 1, "info": 1}

MISSING_NOTE = ("LegacyGuard: oasdiff was not found, so the OpenAPI check used its built-in parser (routes, parameters and "
                "schema fields only). Install oasdiff or set contract_tools.oasdiff in .legacyguard/config.yaml.")


@dataclass
class Tool:
    name: str
    path: str
    version: str | None
    source: str  # "config" | "path"


@dataclass
class Completed:
    returncode: int
    stdout: str
    stderr: str
    seconds: float


@dataclass
class OasResult:
    errs: list[dict]
    warns: list[dict]
    seconds: float


@dataclass
class Refusal:
    """A tool did not give a usable result. kind: skip (new contract file, no note), off (switched off, no note),
    missing, refused, failed, timeout; squawk also unstartable and parse. reason is the note text without the
    "LegacyGuard: " prefix."""
    reason: str
    kind: str = "refused"


@dataclass
class ToolContext:
    """What check_http_file (oasdiff) and check_migration (squawk) need to run a tool: trees = (base tree, revision
    tree) for sibling `$ref` files; notes collects (kind, text) pairs, kind None for notes that are shown every time.
    state (Sprint 7.6 Part C): one dict per Stop, ci.run or hook call, shared by every file of that run: the files
    squawk did not lint (`squawk_unchecked`, with the reason in `squawk_unchecked_kind`) and the time rule
    (`squawk_stopped`: the file whose timeout or failed start stopped squawk for the rest of the run)."""
    cfg: object
    trees: tuple
    timeout: float
    notes: list = field(default_factory=list)
    allow_oasdiff: bool = True
    pre_edit: bool = False
    allow_squawk: bool = True
    state: dict | None = None


def settings(cfg) -> dict:
    try:
        out = dict((cfg.raw.get("contract_tools") or {}))
    except Exception:  # noqa: BLE001
        out = {}
    out.setdefault("oasdiff", "auto")
    out.setdefault("makemigrations", "auto")
    out.setdefault("openapi_from_app", "auto")  # Sprint 7.6 Part B (D8): auto | false
    out.setdefault("squawk", "auto")  # Sprint 7.6 Part C: auto | false | <path>
    out.setdefault("squawk_pg_version", None)
    out.setdefault("squawk_assume_in_transaction", False)
    out.setdefault("pre_edit_timeout_s", 0.8)
    out.setdefault("timeout_s", 20)
    return out


# ---- tools.json ----

def _state_path(cfg) -> Path:
    return Path(cfg.state_dir) / "tools.json"


def _load(cfg) -> dict:
    try:
        data = json.loads(_state_path(cfg).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(cfg, data: dict) -> None:
    try:
        path = _state_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".tools.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - best effort
        pass


def _update(cfg, name: str, **values) -> None:
    data = _load(cfg)
    entry = data.get(name)
    if isinstance(entry, dict):
        entry.update(values)
        _save(cfg, data)


def _st(path: str) -> list[int] | None:
    try:
        s = os.stat(path)
        return [s.st_mtime_ns, s.st_size, s.st_ino]
    except OSError:
        return None


def _config_value(cfg, name: str):
    return settings(cfg).get(name, "auto")


def _resolve(cfg, name: str) -> tuple[str | None, str, str]:
    """(path, source, reason). Never spawns a process."""
    value = _config_value(cfg, name)
    if value is False or (isinstance(value, str) and value.lower() in ("false", "off", "no")):
        return None, "config", "switched off"
    if isinstance(value, str) and value and value.lower() != "auto":
        p = Path(value)
        p = p if p.is_absolute() else Path(cfg.root) / p
        if p.is_file() and os.access(p, os.X_OK):
            return str(p), "config", ""
        return None, "config", f"configured path {value} not found or not executable"
    found = shutil.which(name)
    return (found, "path", "") if found else (None, "path", "not found")


def _entry(cfg, name: str, rebuild: bool = False) -> dict:
    data = _load(cfg)
    value = _config_value(cfg, name)
    cached = data.get(name)
    if not rebuild and isinstance(cached, dict) and cached.get("config_value") == value and cached.get("path"):
        if _st(cached["path"]) == cached.get("st"):
            return cached
    path, source, reason = _resolve(cfg, name)
    st = _st(path) if path else None
    same = (isinstance(cached, dict) and cached.get("config_value") == value and cached.get("path") == path
            and cached.get("st") == st and path is not None)
    entry = {"path": path, "source": source, "config_value": value, "version": cached.get("version") if same else None,
             "st": st, "checked": time.time(), "reason": reason,
             "last_seconds": cached.get("last_seconds") if same else None,
             "last_error": cached.get("last_error") if same else None,
             "pre_edit_timeouts": int(cached.get("pre_edit_timeouts") or 0) if same else 0}
    data[name] = entry
    _save(cfg, data)
    return entry


def find(name: str, cfg) -> Tool | None:
    """The tool, from the config value (false: never; a path: it must exist and be executable), else PATH. The result
    is cached in .legacyguard/tools.json and reused while the config value and the binary's stat are unchanged."""
    try:
        e = _entry(cfg, name)
        if not e.get("path"):
            return None
        return Tool(name, e["path"], e.get("version"), e.get("source") or "path")
    except Exception:  # noqa: BLE001
        return None


def find_reason(name: str, cfg) -> str:
    try:
        return _entry(cfg, name).get("reason") or ""
    except Exception:  # noqa: BLE001
        return "not found"


def refresh(cfg, reset_counters: bool) -> None:
    """Re-resolve every tool and read its version (`<path> --version`, 2 s). SessionStart passes reset_counters=True,
    the only call that sets the pre-edit timeout counter back to 0, and always re-probes. The Stop audit and ci.run
    pass False: they keep the counter, and keep the cached entry as it is (no process started) unless the config
    value, the resolved path or the binary's stat changed, so a Stop does not run `--version` under the ledger lock."""
    try:
        for name in TOOLS:
            if not reset_counters:
                cached = _load(cfg).get(name)
                path, _, _ = _resolve(cfg, name)
                if (isinstance(cached, dict) and cached.get("config_value") == _config_value(cfg, name)
                        and cached.get("path") == path and cached.get("st") == (_st(path) if path else None)
                        and "version" in cached):
                    continue
            e = _entry(cfg, name, rebuild=True)
            if reset_counters:
                e["pre_edit_timeouts"] = 0
            if e.get("path"):
                try:
                    from runner import os_sandbox  # the probe's whole process group is killed on a timeout
                    res = os_sandbox.run([e["path"], "--version"], VERSION_TIMEOUT_S, capture_output=True, text=True)
                    words = (res.stdout or "").strip().splitlines()[:1]
                    e["version"] = words[0].split()[-1] if words and words[0].split() else None
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    e["version"] = None
            data = _load(cfg)
            data[name] = e
            _save(cfg, data)
    except Exception:  # noqa: BLE001
        pass


def pre_edit_allowed(cfg, name: str = "oasdiff") -> bool:
    """False after SKIP_AFTER_TIMEOUTS pre-edit timeouts in a row this session (with this binary and config value)."""
    try:
        return int(_entry(cfg, name).get("pre_edit_timeouts") or 0) < SKIP_AFTER_TIMEOUTS
    except Exception:  # noqa: BLE001
        return True


def _bump_timeouts(cfg, timed_out: bool, name: str = "oasdiff") -> None:
    data = _load(cfg)
    e = data.get(name)
    if isinstance(e, dict):
        e["pre_edit_timeouts"] = int(e.get("pre_edit_timeouts") or 0) + 1 if timed_out else 0
        _save(cfg, data)


# ---- running ----

def _run(cmd: list[str], timeout: float, cwd: str | None, env: dict | None, input=None,
         text: bool = True) -> tuple[Completed | None, str, bool]:
    """input None: the child inherits stdin. text=False: output is read as bytes and decoded here as UTF-8 with
    errors="replace", so Completed.stdout/stderr are always str and a stray byte never raises."""
    started = time.time()
    try:
        from runner import os_sandbox  # kills the whole process group on a timeout (a sandboxed oasdiff too)
        res = os_sandbox.run(cmd, timeout, input=input, capture_output=True, text=text, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout} s", True
    except (OSError, ValueError) as exc:
        return None, f"could not start: {type(exc).__name__}: {exc}"[:300], False
    out, err = res.stdout or "", res.stderr or ""
    if not text:
        out = out.decode("utf-8", errors="replace") if isinstance(out, bytes) else out
        err = err.decode("utf-8", errors="replace") if isinstance(err, bytes) else err
    return Completed(res.returncode, out, err, round(time.time() - started, 3)), "", False


def run(cmd: list[str], timeout: float, cwd: str | None, env: dict | None = None, cfg=None, name: str | None = None) -> Completed | None:
    """Run a tool. None on a timeout or a start failure, with the reason kept in tools.json `last_error`."""
    try:
        done, reason, timed_out = _run(cmd, timeout, cwd, env)
        if cfg is not None and name:
            _update(cfg, name, last_seconds=done.seconds if done else None,
                    last_error=None if done else ("timeout: " if timed_out else "") + reason)
        return done
    except Exception:  # noqa: BLE001
        return None


def _level(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        return _LEVELS.get(v.strip())
    return None


def parse_changes(stdout: str) -> tuple[list[dict], list[dict]] | None:
    """(errs, warns) from `oasdiff breaking -f json` output, or None when the output is not the expected shape."""
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        return None
    errs, warns = [], []
    for x in data:
        level = _level(x.get("level"))
        if level is None:
            return None
        item = {k: str(x.get(k) or "") for k in ("id", "text", "operation", "path", "fingerprint")}
        if not item["id"] and not item["text"]:
            item["text"] = "a change oasdiff did not describe"
        (errs if level == 3 else warns if level == 2 else []).append(item)
    return errs, warns


def _parse_yaml(text: str):
    import yaml
    return yaml.safe_load(text)


class _Refused(Exception):
    pass


def _refs(node, depth: int = 0) -> list[str]:
    if depth > MAX_DEPTH:
        raise _Refused("the spec is nested too deeply to scan for $ref")
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                out.append(v)
            else:
                out += _refs(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            out += _refs(v, depth + 1)
    return out


def _inside(path: str, root: str) -> bool:
    root = os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _materialise(rel: str, text: str, spec, tree: str, dest: str, budget: dict) -> bool:
    """Write the spec and every file it refers to under dest, scanning each. True when some ref names a file."""
    target = os.path.join(dest, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as fh:
        fh.write(text)
    todo = [(rel, spec)]
    seen = {os.path.normpath(rel)}
    file_refs = False
    while todo:
        referrer, parsed = todo.pop()
        for ref in _refs(parsed):
            location = ref.split("#", 1)[0]
            if not location:
                continue
            if _SCHEME.match(location):
                raise _Refused(f"the spec refers to {ref}; LegacyGuard never lets a tool read URLs")
            if os.path.isabs(location) or location.startswith(("/", "\\")):
                raise _Refused(f"the spec has an absolute $ref {ref}")
            file_refs = True
            joined = os.path.normpath(os.path.join(os.path.dirname(referrer), location))
            if joined.startswith("..") or os.path.isabs(joined):
                raise _Refused(f"the $ref {ref} leaves the project")
            if joined in seen:
                continue
            seen.add(joined)
            real = os.path.realpath(os.path.join(tree, joined))
            if not _inside(real, tree):
                raise _Refused(f"the $ref {ref} leaves the project")
            budget["files"] += 1
            if budget["files"] > MAX_REF_FILES:
                raise _Refused(f"the spec refers to more than {MAX_REF_FILES} files")
            try:
                with open(real, "rb") as fh:
                    raw = fh.read(MAX_REF_BYTES + 1)
                budget["bytes"] += len(raw)
                if budget["bytes"] > MAX_REF_BYTES:
                    raise _Refused(f"the files the spec refers to are larger than {MAX_REF_BYTES // 2**20} MB")
                body = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise _Refused(f"could not read {joined} ({type(exc).__name__})") from exc
            try:
                sub = _parse_yaml(body)
            except Exception as exc:  # noqa: BLE001
                raise _Refused(f"could not read {joined}") from exc
            out = os.path.join(dest, joined)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w") as fh:
                fh.write(body)
            todo.append((joined, sub))
    return file_refs


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def oasdiff_breaking(cfg, rel: str, before: str | None, after: str, trees, timeout: float, specs=None,
                     pre_edit: bool = False, internal_only: bool = False, label: str | None = None):
    """OasResult, or a Refusal (with the note text). Never raises. specs: the already-parsed (before, after) dicts.
    pre_edit: a timeout adds 1 to the pre-edit counter, a run that returns in time sets it to 0.
    internal_only (Sprint 7.6 Part B, the app's generated spec): any `$ref` that names a file or URL is refused before
    anything is copied or run, so `--allow-external-refs=false` is always passed and `trees` is unused.
    label: the name used for rel in every refusal text (the generated spec's temp file name is never shown)."""
    tmp = None
    name = label or rel
    try:
        if not before or not str(before).strip():
            return Refusal("", "skip")
        tool = find("oasdiff", cfg)
        if tool is None:
            reason = find_reason("oasdiff", cfg)
            if reason == "switched off":
                return Refusal("", "off")
            if reason.startswith("configured path"):
                return Refusal(f"oasdiff {reason}, so the OpenAPI check used its built-in parser.", "missing")
            return Refusal(MISSING_NOTE[len("LegacyGuard: "):], "missing")
        a_spec, b_spec = specs if specs is not None else (_parse_yaml(before), _parse_yaml(after))
        if internal_only:
            try:
                external = next((r for spec in (a_spec, b_spec) for r in _refs(spec) if r.split("#", 1)[0]), None)
            except _Refused as exc:
                return Refusal(f"oasdiff was not run on {name}: {exc}, so the built-in parser was used.", "refused")
            if external is not None:
                return Refusal(f"oasdiff was not run: {name} refers to {external}; LegacyGuard gives oasdiff only specs with "
                               "internal references, so the built-in parser was used.", "refused")
        tmp = tempfile.mkdtemp(prefix="lg-oas-")
        budget = {"files": 0, "bytes": 0}
        try:
            refs_a = _materialise(rel, before, a_spec, str(trees[0]), os.path.join(tmp, "base"), budget)
            refs_b = _materialise(rel, after, b_spec, str(trees[1]), os.path.join(tmp, "rev"), budget)
        except _Refused as exc:
            return Refusal(f"oasdiff was not run on {name}: {exc}, so the built-in parser was used.", "refused")
        cmd = [tool.path, "breaking", os.path.join(tmp, "base", rel), os.path.join(tmp, "rev", rel), "-f", "json"]
        if not (refs_a or refs_b):
            cmd.append("--allow-external-refs=false")
        else:
            from runner import os_sandbox
            mode = str((cfg.raw.get("behavioral_diff") or {}).get("os_sandbox", "auto"))
            if not os_sandbox.active(sys.executable, mode):
                return Refusal(f"the spec {name} refers to other files and no OS sandbox is available here, so oasdiff "
                               "was not run and the built-in parser was used.", "refused")
            cmd = os_sandbox.wrap(cmd, sys.executable, writable=[tmp], readonly=[str(t) for t in trees], mode=mode)
        env = {k: v for k, v in os.environ.items() if k != "OASDIFF_CONFIG"}
        done, reason, timed_out = _run(cmd, timeout, tmp, env)
        _update(cfg, "oasdiff", last_seconds=done.seconds if done else None,
                last_error=None if done else ("timeout: " if timed_out else "") + reason)
        if pre_edit and (done is not None or timed_out):
            _bump_timeouts(cfg, timed_out)
        if done is None:
            if timed_out:
                return Refusal(f"oasdiff took longer than {(f'{timeout:.0f}' if label else timeout)} s on {name}, so the built-in parser was used.", "timeout")
            return Refusal(f"oasdiff could not run ({reason}); the built-in parser was used.", "failed")
        parsed = parse_changes(done.stdout)
        if parsed is None:
            first = (done.stderr or "").strip().splitlines()[:1]
            if done.returncode and first:
                msg = first[0]
                if label:  # oasdiff names the files it read; those are temp paths here
                    for side in ("base", "rev"):
                        msg = msg.replace(os.path.join(tmp, side, rel), label)
                return Refusal(f"oasdiff could not load the spec: {msg[:200]}; the built-in parser was used.", "failed")
            return Refusal("oasdiff printed output LegacyGuard could not read; the built-in parser was used.", "failed")
        if done.returncode:  # we never pass --fail-on, so a non-zero exit is a failure even with readable output
            return Refusal(f"oasdiff exited with code {done.returncode}; the built-in parser was used.", "failed")
        return OasResult(parsed[0], parsed[1], done.seconds)
    except Exception as exc:  # noqa: BLE001
        return Refusal(f"oasdiff step failed (internal error: {type(exc).__name__}); the built-in parser was used.", "failed")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def break_fields(rel: str, change: dict) -> tuple[str, str]:
    """(item, subject) for one oasdiff ERR change."""
    cid, op, path, text, fp = (change.get(k, "") for k in ("id", "operation", "path", "text", "fingerprint"))
    item = f"oasdiff:{cid or 'change'}:{op} {path}:{fp or _sha12(cid + op + path + text)}"
    where = f"{op} {path}".strip()
    subject = f"{where}: {text}" if where else text
    return item, subject


# ---- squawk (Sprint 7.6 Part C) ----

SQUAWK_MISSING_NOTE = ("LegacyGuard: squawk was not found, so the SQL migration check used its built-in patterns. Install "
                       "squawk (`pip install squawk-cli`) or set contract_tools.squawk in .legacyguard/config.yaml.")
SQUAWK_CONFIG_NOTE = ('LegacyGuard: contract_tools.squawk_pg_version must be a quoted version like "13" or "13.4"; squawk '
                      "used its default, Postgres 15.")
SQUAWK_FILES = ("before.sql", "after.sql")
MAX_UNKNOWN_RULES = 20
_PG_VERSION = re.compile(r"^[0-9]{1,4}(\.[0-9]{1,4}){0,2}$")
_IGNORE_COMMENT = re.compile(r"squawk-(ignore|disable-assume-in-transaction)", re.I)
_SQL_COMMENT = re.compile(rb"--[^\n]*|/\*.*?(?:\*/|\Z)", re.S)

# Classes from squawk's own tags at v2.74.0 (docs/src/pages/index.js), D3: break = the default-on "backwards
# compatibility" rules plus ban-drop-column; risk = locking and run-time rules; ignored = style, timeouts and any name
# not listed here (a newer squawk's rule, counted in tools.json squawk.unknown_rules).
_BREAK_RULES = ("ban-drop-column", "renaming-column", "renaming-table", "ban-drop-table", "changing-column-type",
                "adding-required-field", "ban-drop-database", "ban-drop-not-null", "ban-truncate-cascade", "ban-drop-view",
                "ban-drop-function", "ban-drop-type", "ban-drop-default", "ban-drop-schema", "ban-drop-sequence",
                "ban-drop-domain", "ban-drop-constraint", "ban-drop-generated-expression", "renaming-object",
                "ban-set-schema", "ban-alter-identity", "ban-drop-extension")
_RISK_RULES = ("adding-field-with-default", "adding-foreign-key-constraint", "adding-not-nullable-field",
               "adding-serial-primary-key-field", "constraint-missing-not-valid", "disallowed-unique-constraint",
               "require-concurrent-index-creation", "require-concurrent-index-deletion",
               "require-concurrent-partition-detach", "require-concurrent-reindex", "transaction-nesting",
               "ban-concurrent-index-creation-in-transaction", "ban-uncommitted-transaction",
               "ban-create-domain-with-constraint", "ban-alter-domain-with-add-constraint", "identifier-too-long",
               "ban-duplicate-column-assignments")
_IGNORED_RULES = ("prefer-robust-stmts", "prefer-repack", "prefer-text-field", "require-lock-timeout",
                  "require-statement-timeout", "prefer-bigint-over-int", "prefer-bigint-over-smallint", "prefer-identity",
                  "prefer-timestamp-tz", "prefer-timestamptz", "ban-char-field", "require-enum-value-ordering",
                  "unused-ignore", "syntax-error")
SQUAWK_CLASSES = {**{r: "break" for r in _BREAK_RULES}, **{r: "risk" for r in _RISK_RULES},
                  **{r: "ignored" for r in _IGNORED_RULES}}


@dataclass
class SquawkResult:
    before: list[dict]   # violations in the old text (empty for a new file)
    after: list[dict]
    seconds: float


@dataclass
class SquawkItem:
    key: str        # squawk:<rule>:<window[:80]>:<sha12(window)>
    rule: str
    cls: str        # break | risk | ignored
    line: int       # 1-based, in the prepared text
    message: str
    window: str     # the normalised enclosing statement
    span: tuple     # (start, end) character offsets of that statement in the prepared after text


def prepare(text: str | None) -> str:
    """The text squawk sees, and every offset is computed on: one leading BOM stripped, `\\r\\n` and a lone `\\r` turned
    into `\\n` (squawk's line index breaks lines at all three), and `squawk-ignore...` /
    `squawk-disable-assume-in-transaction` comments neutralised by a same-length rewrite (`-` to `_`)."""
    s = text or ""
    if s.startswith("\ufeff"):
        s = s[1:]
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return _IGNORE_COMMENT.sub(lambda m: m.group(0).replace("-", "_"), s)


def squawk_pg_version(cfg) -> tuple[str | None, str | None]:
    """(value to pass as --pg-version or None, note text or None). A YAML float (13.4, 13.10) is refused: it may no longer
    be what the user wrote. A string must be ASCII digits, at most 4 per part, at most three parts."""
    value = settings(cfg).get("squawk_pg_version")
    if value is None:
        return None, None
    if isinstance(value, bool) or isinstance(value, float):
        return None, SQUAWK_CONFIG_NOTE
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str) and _PG_VERSION.match(value.strip()):
        return value.strip(), None
    return None, SQUAWK_CONFIG_NOTE


def _assume_in_transaction(cfg) -> bool:
    return settings(cfg).get("squawk_assume_in_transaction") is True


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def parse_squawk(stdout: str) -> list[dict] | None:
    """The violations from `squawk --reporter json` (one flat array over all files), or None when any element is not the
    v2.74.0 shape. Positions are zero-based (line; column as a UTF-8 byte offset in that line)."""
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, list):
        return None
    out = []
    for x in data:
        if not isinstance(x, dict):
            return None
        if not (isinstance(x.get("file"), str) and _int(x.get("line")) and _int(x.get("column"))
                and x.get("level") in ("Warning", "Error") and isinstance(x.get("rule_name"), str)
                and isinstance(x.get("message"), str)):
            return None
        line_end = x.get("line_end", x["line"])
        column_end = x.get("column_end", x["column"])
        if not (_int(line_end) and _int(column_end)):
            return None
        if any(x.get(k) is not None and not isinstance(x.get(k), str) for k in ("note", "help")):
            return None
        out.append({"file": x["file"], "line": x["line"], "column": x["column"], "level": x["level"],
                    "message": x["message"], "note": x.get("note"), "help": x.get("help"), "rule_name": x["rule_name"],
                    "line_end": line_end, "column_end": column_end})
    return out


def squawk_lint(cfg, rel: str, before: str | None, after: str, timeout: float, pre_edit: bool = False):
    """SquawkResult or Refusal (kinds off, missing, unstartable, timeout, failed, parse). Never raises.
    squawk runs once, in a fresh temp folder that is its cwd, on the relative names before.sql (only when the old text
    has content) and after.sql, with our own --config file (so no .squawk.toml above the folder is read), stdin empty,
    and no GITHUB_ACTIONS or SQUAWK_* variables. pre_edit: a timeout adds 1 to squawk's pre-edit counter, a run in time
    sets it to 0."""
    tmp = None
    try:
        tool = find("squawk", cfg)
        if tool is None:
            reason = find_reason("squawk", cfg)
            if reason == "switched off":
                return Refusal("", "off")
            if reason.startswith("configured path"):
                return Refusal(f"squawk {reason}, so the SQL migration check used its built-in patterns.", "missing")
            return Refusal(SQUAWK_MISSING_NOTE[len("LegacyGuard: "):], "missing")
        try:
            after_bytes = prepare(after).encode("utf-8")
            before_bytes = prepare(before).encode("utf-8") if before and before.strip() else None
        except UnicodeError:
            return Refusal(f"squawk was not run on {rel}: the text is not valid UTF-8; the built-in patterns were used.",
                           "failed")
        tmp = tempfile.mkdtemp(prefix="lg-squawk-")
        with open(os.path.join(tmp, "after.sql"), "wb") as fh:
            fh.write(after_bytes)
        if before_bytes is not None:
            with open(os.path.join(tmp, "before.sql"), "wb") as fh:
                fh.write(before_bytes)
        assume = _assume_in_transaction(cfg)
        with open(os.path.join(tmp, "squawk.toml"), "w") as fh:
            fh.write(f"assume_in_transaction = {'true' if assume else 'false'}\n")
        cmd = [tool.path, "--reporter", "json", "--config", "squawk.toml"]
        version, _ = squawk_pg_version(cfg)
        if version:
            cmd += ["--pg-version", version]
        if assume:
            cmd.append("--assume-in-transaction")
        cmd += (["before.sql"] if before_bytes is not None else []) + ["after.sql"]
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS" and not k.startswith("SQUAWK_")}
        env["SQUAWK_DISABLE_GITHUB_ANNOTATIONS"] = "1"
        done, reason, timed_out = _run(cmd, timeout, tmp, env, input=b"", text=False)
        _update(cfg, "squawk", last_seconds=done.seconds if done else None,
                last_error=None if done else ("timeout: " if timed_out else "") + reason)
        if pre_edit and (done is not None or timed_out):
            _bump_timeouts(cfg, timed_out, "squawk")
        if done is None:
            if timed_out:
                return Refusal(f"squawk took longer than {timeout:g} s on {rel}; the built-in patterns were used.", "timeout")
            return Refusal(f"squawk could not run ({reason}); the built-in patterns were used.", "unstartable")
        found = parse_squawk(done.stdout)
        if found is None:
            first = (done.stderr or "").strip().splitlines()[:1]
            if done.returncode and first:
                return Refusal(f"squawk could not check {rel}: {first[0][:200]}; the built-in patterns were used.", "failed")
            return Refusal("squawk printed output LegacyGuard could not read; the built-in patterns were used.", "failed")
        if done.returncode not in (0, 1) or (done.returncode == 0) != (not found):
            return Refusal(f"squawk exited with code {done.returncode}; the built-in patterns were used.", "failed")
        sides = {"before.sql": [], "after.sql": []}
        for v in found:
            if v["file"] not in sides:  # squawk prints each path as given; anything else is not our file
                return Refusal("squawk reported a file LegacyGuard did not give it; the built-in patterns were used.", "failed")
            sides[v["file"]].append(v)
        for name, side in (("after.sql", "the new text"), ("before.sql", "the old text")):
            err = next((v for v in sides[name] if v["level"] == "Error"), None)
            if err is not None:
                return Refusal(f"squawk could not parse {side} of {rel} as PostgreSQL (line {err['line'] + 1}: "
                               f"{err['message'][:120]}); the built-in patterns were used.", "parse")
        return SquawkResult(sides["before.sql"], sides["after.sql"], done.seconds)
    except Exception as exc:  # noqa: BLE001
        return Refusal(f"squawk step failed (internal error: {type(exc).__name__}); the built-in patterns were used.", "failed")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _blank_comments(b: bytes) -> bytes:
    """b with every `--` comment and `/* */` comment (not nested; an unclosed one runs to the end) overwritten with
    spaces of the same byte length, newlines kept."""
    return _SQL_COMMENT.sub(lambda m: re.sub(rb"[^\n]", b" ", m.group()), b)


def _windows(prepared: str, violations: list[dict]) -> list[tuple[dict, str, tuple]]:
    """(violation, normalised enclosing statement, (start, end) character span of that statement) per violation."""
    b = prepared.encode("utf-8")
    blank = _blank_comments(b)
    starts = [0] + [i + 1 for i, ch in enumerate(b) if ch == 0x0A]
    out = []

    def offset(line, column):
        return min(starts[line] + column, len(b)) if line < len(starts) else len(b)

    def chars(n):
        return len(b[:n].decode("utf-8", errors="ignore"))
    for v in violations:
        start = offset(v["line"], v["column"])
        end = max(start, offset(v["line_end"], v["column_end"]))
        lo = blank.rfind(b";", 0, start) + 1
        hi = blank.find(b";", max(start, end - 1))
        hi = len(blank) if hi < 0 else hi
        window = " ".join(blank[lo:hi].decode("utf-8", errors="replace").replace('"', "").replace("`", "").split()).lower()
        out.append((v, window or f"line-{v['line'] + 1}", (chars(lo), chars(hi))))
    return out


def squawk_items(before: str | None, after: str, result: SquawkResult) -> tuple[list[SquawkItem], list[str]]:
    """(the items new in after, in file order, one per key; unknown rule names seen in after). Item key =
    squawk:<rule>:<first 80 characters of the normalised statement>:<sha12 of the whole statement>."""
    old = {f"squawk:{v['rule_name']}:{w[:80]}:{_sha12(w)}" for v, w, _ in _windows(prepare(before), result.before)}
    items: dict[str, SquawkItem] = {}
    unknown: list[str] = []
    for v, w, span in _windows(prepare(after), result.after):
        rule = v["rule_name"]
        cls = SQUAWK_CLASSES.get(rule)
        if cls is None:
            unknown.append(rule)
            cls = "ignored"
        key = f"squawk:{rule}:{w[:80]}:{_sha12(w)}"
        if key in old or key in items:
            continue
        items[key] = SquawkItem(key, rule, cls, v["line"] + 1, v["message"], w, span)
    return list(items.values()), sorted(set(unknown))


def record_unknown_rules(cfg, names: list[str]) -> None:
    """Add rule names squawk reported that SQUAWK_CLASSES does not know to tools.json (capped), for the report."""
    if not names:
        return
    data = _load(cfg)
    e = data.get("squawk")
    if isinstance(e, dict):
        known = list(e.get("unknown_rules") or [])
        e["unknown_rules"] = (known + [n for n in names if n not in known])[:MAX_UNKNOWN_RULES]
        _save(cfg, data)


def take_notes(data: dict, notes: list) -> list[str]:
    """The note texts to show now: kind None always; any other kind once per session (session.json tool_notes)."""
    out = []
    try:
        sent = data.setdefault("tool_notes", [])
        for kind, text in notes:
            if kind is None:
                out.append(text)
            elif kind not in sent:
                sent.append(kind)
                out.append(text)
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(out))


def _app_openapi_part(data: dict) -> str:
    """Sprint 7.6 Part B: the code-first OpenAPI part of the "Contract tools" line, or ""."""
    oas = (data.get("contract_tools") or {}).get("app_openapi") or {}
    state = data.get("app_openapi") or {}
    failed = state.get("failed") or {}
    if failed.get("paused"):
        return f"app OpenAPI: paused ({failed.get('detail') or 'unknown'})"
    if oas.get("kind") == "none" or state.get("kind") == "none":
        return "app OpenAPI: not available for this app (no openapi() method)"
    runs = int(oas.get("runs") or 0)
    if not runs:
        return ""
    count = f"{runs} run{'s' if runs != 1 else ''}"
    if oas.get("kind") == "fastapi":
        return f"app OpenAPI: FastAPI {(oas.get('versions') or {}).get('fastapi') or '?'}, {count}"
    return f"app OpenAPI: {count}"


def _squawk_part(cfg) -> str:
    """Sprint 7.6 Part C: the squawk part of the "Contract tools" line, only when contracts.db is set (D10)."""
    if not cfg.contract_path("db"):
        return ""
    e = _load(cfg).get("squawk") or {}
    if e.get("path"):
        text = f"squawk {e.get('version') or '(version unknown)'} ({e['path']})"
        if e.get("last_seconds") is not None:
            text += f", last run {e['last_seconds']:.2f} s"
        elif e.get("last_error"):
            text += f", last run failed ({e['last_error'][:80]})"
    elif e.get("reason") == "switched off" or str(_config_value(cfg, "squawk")).lower() in ("false", "off", "no"):
        text = "squawk switched off"
    else:
        text = "squawk not found"
    n = len(e.get("unknown_rules") or [])
    if n:
        text += f", {n} unknown rule{'s' if n != 1 else ''} ignored"
    return text


def report_line(cfg, data: dict) -> str:
    """The "Contract tools" line of the session report."""
    try:
        parts = []
        e = _load(cfg).get("oasdiff") or {}
        if e.get("path"):
            text = f"oasdiff {e.get('version') or '(version unknown)'} ({e['path']})"
            if e.get("last_seconds") is not None:
                text += f", last run {e['last_seconds']:.2f} s"
            elif e.get("last_error"):
                text += f", last run failed ({e['last_error'][:80]})"
            parts.append(text)
        elif e.get("reason") == "switched off" or str(_config_value(cfg, "oasdiff")).lower() in ("false", "off", "no"):
            parts.append("oasdiff switched off")
        else:
            parts.append("oasdiff not found")
        squawk = _squawk_part(cfg)
        if squawk:
            parts.append(squawk)
        mig = (data.get("contract_tools") or {}).get("makemigrations") or {}
        if mig.get("runs"):
            parts.append(f"makemigrations --check: Django {mig.get('django') or '?'}, {mig['runs']} run{'s' if mig['runs'] != 1 else ''}")
        app = _app_openapi_part(data)
        if app:
            parts.append(app)
        line = "Contract tools: " + "; ".join(parts) + "."
        notes = (data.get("contract_tools") or {}).get("notes") or []
        if notes:
            line += " " + " ".join(notes[:4])
        return line
    except Exception:  # noqa: BLE001
        return ""
