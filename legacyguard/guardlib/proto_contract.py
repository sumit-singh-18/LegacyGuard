"""`.proto` contracts checked with `buf breaking` (Sprint 7.6 Part D). Guard side only: no project code runs.

`contracts.proto` names one folder, the import root of the project's .proto files (relative to scope, like the other
contract keys). At the Stop audit and in CI, the `.proto` files under it in the baseline tree and in the working tree
are copied (bytes only, nothing else from the project) into a temp folder and compared by buf (contract_tools.
buf_breaking). Each annotation becomes a `checks.Break(kind="proto", source="buf")`.

Rules:
- The folder becomes a prefix: "" for the repo root (`.`), else "<folder>/". Every repo path is prefix + module path,
  so the root folder never gives `./x.proto`.
- Item keys: buf:<rule>:<repo path or ->:<message[:80]>:<sha12(rule, path, message)>, message whitespace collapsed;
  no lines or columns, so CRLF and LF texts give the same keys.
- A folder component named buf.yaml, buf.work.yaml or buf.lock, a symlink leading outside the tree, or more than
  MAX_PROTO_FILES files / MAX_PROTO_BYTES bytes on a side refuses the run (no result, findings kept).
- No baseline .proto: nothing can break (skip). Every .proto deleted: under FILE, one FILE_NO_DELETE per file with
  buf's own message (buf refuses an empty input); under other categories not checked.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from guardlib import checks, paths, symbols

MAX_PROTO_FILES = 2000
MAX_PROTO_BYTES = 32 * 2**20
BUF_NAMES = ("buf.yaml", "buf.work.yaml", "buf.lock")
NOTE_KIND = {"missing": "buf", "old": "buf-old", "failed": "buf-failed", "unstartable": "buf-failed",
             "refused": "buf-failed", "too_large": "buf-failed", "timeout": "buf-timeout", "compile": "buf-compile",
             "imports": "buf-imports", "config": "buf-folder", "empty": "buf-empty"}
_DELETED_FILE = re.compile(r'^Previously present file ("(?:[^"\\]|\\.)*") was deleted\.$')
FOLDER_NAME_NOTE = ("LegacyGuard: contracts.proto must name the folder your .proto imports are relative to, e.g. proto/; "
                    ".proto changes were not checked.")
FOLDER_OUTSIDE_REPO_NOTE = ("LegacyGuard: contracts.proto must be a folder inside the repository; .proto changes were "
                            "not checked.")
UNSET_NOTE = ("LegacyGuard: A .proto file changed ({rel}), but contracts.proto is not set in .legacyguard/config.yaml, "
              "so .proto changes were not checked. Set it to the folder your .proto imports are relative to.")


@dataclass
class ProtoOutcome:
    state: str                                   # checked | not_checked | skip
    kind: str | None = None                      # why not checked (a Refusal kind, config, empty)
    breaks: list = field(default_factory=list)   # (ledger file, checks.Break)
    notes: list = field(default_factory=list)    # (note kind, text)
    category: str = "FILE"


def folder_problem(cfg) -> tuple[str, str] | None:
    """(note text, short reason) when contracts.proto is set but cannot be used, else None. Decided from the config
    alone (R2-1): a file name instead of a folder, a folder outside the repository, or one outside scope."""
    value = (cfg.raw.get("contracts") or {}).get("proto")
    if not value:
        return None
    if not isinstance(value, str) or value.rstrip("/").lower().endswith((".proto", ".yaml", ".yml")):
        return FOLDER_NAME_NOTE, "it must name a folder"
    folder = cfg.contract_path("proto")
    if os.path.isabs(value) or folder == ".." or folder.startswith("../") or os.path.isabs(folder):
        return FOLDER_OUTSIDE_REPO_NOTE, "it must be a folder inside the repository"
    if not paths.in_scope(prefix(folder) + "x.proto", cfg.scope):
        return (f"LegacyGuard: contracts.proto ({folder}) is outside scope ({cfg.scope}), so LegacyGuard never sees "
                f"changes there; .proto changes were not checked.", f"{folder} is outside scope {cfg.scope}")
    return None


def prefix(folder: str) -> str:
    return "" if folder in (".", "") else folder.rstrip("/") + "/"


def ledger_folder(folder: str) -> str:
    return "." if folder in (".", "") else folder.rstrip("/")


def applies(cfg, changed) -> bool:
    folder = cfg.contract_path("proto")
    if not folder:
        return False
    pre = prefix(folder)
    return any(rel.startswith(pre) and rel.endswith(".proto") for rel in changed)


def unset_note(cfg, changed) -> tuple | None:
    """R1-5: a changed in-scope .proto with contracts.proto unset gives one note (kind buf-unset)."""
    if cfg.contract_path("proto"):
        return None
    rel = next((r for r in changed if r.endswith(".proto") and not r.startswith(".legacyguard/")), None)
    return ("buf-unset", UNSET_NOTE.format(rel=rel)) if rel else None


def _listing(tree: str) -> list[str] | None:
    try:
        res = subprocess.run(["git", "-C", tree, "ls-files", "-co", "--exclude-standard", "-z"], capture_output=True,
                             timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode:
        return None
    return [x for x in res.stdout.decode("utf-8", "surrogateescape").split("\0") if x]


def collect(tree: str, pre: str, files: list[str] | None = None) -> tuple[dict, tuple | None]:
    """({module path: bytes}, None) for the .proto files under the prefix in one tree, or ({}, (kind, note)) when the
    run must be refused. files: the tree's `git ls-files -co --exclude-standard` listing (read here when None)."""
    names = _listing(tree) if files is None else files
    if names is None:
        return {}, ("failed", "LegacyGuard: buf was not run: git could not list the files of a tree; .proto changes "
                              "were not checked.")
    root = os.path.realpath(tree)
    out: dict = {}
    total = 0
    for name in sorted(set(names)):
        if not (name.startswith(pre) and name.endswith(".proto")) or name.startswith(".legacyguard/"):
            continue
        parts = name.split("/")
        if name.startswith("/") or ".." in parts or "" in parts:
            return {}, ("refused", f"LegacyGuard: buf was not run: {name} is not a plain path inside the repository; "
                                   ".proto changes were not checked.")
        m = name[len(pre):]
        bad = next((p for p in m.split("/")[:-1] if p.lower() in BUF_NAMES), None)
        if bad:
            return {}, ("refused", f"LegacyGuard: buf was not run: {name} has a folder named {bad}, which buf would read "
                                   "as its own config; .proto changes were not checked.")
        path = os.path.join(tree, *parts)
        if os.path.islink(path):
            real = os.path.realpath(path)
            if not (real.startswith(root + os.sep)) or not os.path.isfile(real):
                return {}, ("refused", f"LegacyGuard: buf was not run: {name} is a symlink that leads outside the "
                                       "repository or to no file; .proto changes were not checked.")
        if not os.path.isfile(path):
            continue  # listed but deleted in the working tree
        body = Path(path).read_bytes()
        total += len(body)
        out[m] = body
        if len(out) > MAX_PROTO_FILES or total > MAX_PROTO_BYTES:
            return {}, ("too_large", f"LegacyGuard: buf was not run: more than {MAX_PROTO_FILES} .proto files or "
                                     f"{MAX_PROTO_BYTES // 2**20} MB under contracts.proto; .proto changes were not "
                                     "checked.")
    return out, None


def _sha12(text: str) -> str:
    from guardlib import contract_tools
    return contract_tools._sha12(text)


def item_key(rule: str, rel: str, message: str) -> str:
    m = " ".join(message.split())
    return f"buf:{rule}:{rel}:{m[:80]}:{_sha12(rule + chr(0) + rel + chr(0) + m)}"


def deleted_name(message: str) -> str | None:
    match = _DELETED_FILE.match(" ".join(message.split()))
    if not match:
        return None
    try:
        name = json.loads(match.group(1))
    except ValueError:
        return None
    return name if isinstance(name, str) else None


def breaks_from(lines, folder: str, base_files: dict) -> list:
    """(ledger file, Break) per annotation, deduplicated by item. lines: [(annotation, new-side module path or None)]."""
    pre = prefix(folder)
    out, seen = [], set()
    for x, m in lines:
        rule, message = x["type"], " ".join(x["message"].split())
        line = None
        if m is not None:
            rel = pre + m
            pos = [x.get(k) for k in ("start_line", "start_column", "end_line", "end_column")]
            if x.get("start_line") and pos != [1, 1, 1, 1]:
                line = x["start_line"]
        else:
            name = deleted_name(message) if rule == "FILE_NO_DELETE" else None
            rel = pre + name if name is not None and name in base_files else "-"
        item = item_key(rule, rel, message)
        if item in seen:
            continue
        seen.add(item)
        file = rel if rel != "-" else ledger_folder(folder)
        out.append((file, checks.Break("proto", item, f"{message} (buf {rule})", "buf", file, line=line)))
    return out


def code_hash(root: str, file: str, item: str) -> str:
    """The working-tree file's hash when it exists, else the item's (a deleted file, a package rule)."""
    p = Path(root) / file
    try:
        if file not in (".", "-") and p.is_file():
            return symbols.sha(p.read_bytes())
    except OSError:
        pass
    return symbols.sha(item)


def _refused(kind: str, text: str, category: str) -> ProtoOutcome:
    note = NOTE_KIND.get(kind)
    return ProtoOutcome("not_checked", kind=kind, notes=[(note, text)] if note else [], category=category)


def compute(cfg, base_dir: str, root: str, timeout: float) -> ProtoOutcome:
    """The buf check of the .proto files under contracts.proto, baseline tree against working tree. Never raises."""
    from guardlib import contract_tools
    category = "FILE"
    try:
        problem = folder_problem(cfg)
        if problem:
            return ProtoOutcome("not_checked", kind="config", notes=[("buf-folder", problem[0])])
        folder = cfg.contract_path("proto")
        category, cat_note = contract_tools.buf_category(cfg)
        extra = [cat_note] if cat_note else []
        pre = prefix(folder)
        base_files, refusal = collect(base_dir, pre)
        if refusal is None:
            rev_files, refusal = collect(root, pre)
        if refusal is not None:
            out = _refused(refusal[0], refusal[1], category)
            out.notes = extra + out.notes
            return out
        if not base_files:
            return ProtoOutcome("skip", notes=extra, category=category)
        if base_files == rev_files:
            return ProtoOutcome("checked", notes=extra, category=category)
        tool = contract_tools.buf_tool(cfg)
        if isinstance(tool, contract_tools.Refusal):
            out = _refused(tool.kind, "LegacyGuard: " + tool.reason, category)
            out.notes = extra + out.notes
            return out
        if not rev_files:
            if category != "FILE":
                out = _refused("empty", f"LegacyGuard: every .proto file under contracts.proto was deleted; buf cannot "
                                        f"check an empty module under {category}, so .proto changes were not checked.",
                               category)
                out.notes = extra + out.notes
                return out
            lines = [({"type": "FILE_NO_DELETE", "message": f"Previously present file {json.dumps(m)} was deleted."}, None)
                     for m in sorted(base_files)]
            return ProtoOutcome("checked", breaks=breaks_from(lines, folder, base_files), notes=extra, category=category)
        res = contract_tools.buf_breaking(cfg, base_files, rev_files, category, timeout, pre)
        if isinstance(res, contract_tools.Refusal):
            out = _refused(res.kind, "LegacyGuard: " + res.reason, category)
            out.notes = extra + out.notes
            return out
        return ProtoOutcome("checked", breaks=breaks_from(res.lines, folder, base_files), notes=extra, category=category)
    except Exception as exc:  # noqa: BLE001 - hooks fail open
        return ProtoOutcome("not_checked", kind="failed", category=category,
                            notes=[("buf-failed", f"LegacyGuard: the .proto check failed inside LegacyGuard "
                                                  f"({type(exc).__name__}).")])
