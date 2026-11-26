"""External contract tools (Sprint 7.6 Part A): find a tool, cache what was found, run it, never raise.

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
TOOLS = ("oasdiff",)
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
    """oasdiff did not give a usable result. kind: skip (new contract file, no note), off (switched off, no note),
    missing, refused, failed, timeout. reason is the note text without the "LegacyGuard: " prefix."""
    reason: str
    kind: str = "refused"


@dataclass
class ToolContext:
    """What check_http_file needs to run a tool: trees = (base tree, revision tree) for sibling `$ref` files; notes
    collects (kind, text) pairs, kind None for notes that are shown every time."""
    cfg: object
    trees: tuple
    timeout: float
    notes: list = field(default_factory=list)
    allow_oasdiff: bool = True
    pre_edit: bool = False


def settings(cfg) -> dict:
    try:
        out = dict((cfg.raw.get("contract_tools") or {}))
    except Exception:  # noqa: BLE001
        out = {}
    out.setdefault("oasdiff", "auto")
    out.setdefault("makemigrations", "auto")
    out.setdefault("openapi_from_app", "auto")  # Sprint 7.6 Part B (D8): auto | false
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
                    res = subprocess.run([e["path"], "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_S)
                    words = (res.stdout or "").strip().splitlines()[:1]
                    e["version"] = words[0].split()[-1] if words and words[0].split() else None
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    e["version"] = None
            data = _load(cfg)
            data[name] = e
            _save(cfg, data)
    except Exception:  # noqa: BLE001
        pass


def pre_edit_allowed(cfg) -> bool:
    """False after SKIP_AFTER_TIMEOUTS pre-edit timeouts in a row this session (with this binary and config value)."""
    try:
        return int(_entry(cfg, "oasdiff").get("pre_edit_timeouts") or 0) < SKIP_AFTER_TIMEOUTS
    except Exception:  # noqa: BLE001
        return True


def _bump_timeouts(cfg, timed_out: bool) -> None:
    data = _load(cfg)
    e = data.get("oasdiff")
    if isinstance(e, dict):
        e["pre_edit_timeouts"] = int(e.get("pre_edit_timeouts") or 0) + 1 if timed_out else 0
        _save(cfg, data)


# ---- running ----

def _run(cmd: list[str], timeout: float, cwd: str | None, env: dict | None) -> tuple[Completed | None, str, bool]:
    started = time.time()
    try:
        from runner import os_sandbox  # kills the whole process group on a timeout (a sandboxed oasdiff too)
        res = os_sandbox.run(cmd, timeout, capture_output=True, text=True, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout} s", True
    except (OSError, ValueError) as exc:
        return None, f"could not start: {type(exc).__name__}: {exc}"[:300], False
    return Completed(res.returncode, res.stdout or "", res.stderr or "", round(time.time() - started, 3)), "", False


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
