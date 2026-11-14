"""Validation harness: run the guard on real repositories and measure it (roadmap section 3b).

Each case in a manifest names a repository and two commits:

- kind "bug" (BugsInPy style): `fixed` is checked out as the baseline and the buggy versions of the changed source
  files are put into the working tree. The guard should report a difference (expected: detected).
- kind "change": `head` is checked out and compared against `base`. `expect` says "quiet" or "detected".

The guard runs through guardlib.ci.run (base commit against the working tree, mutants on). Per case the harness
records the verdict, whether it matches the expectation, the functions found different with their first
counterexample, the verified and untested ones, mutation scores and wall time. It writes results.json and
results.md to --out and prints the metrics. A failing case is recorded as "error" and the run goes on.

Usage:
    python tools/validate.py MANIFEST.yaml [--workdir DIR] [--out DIR] [--only NAME ...]

Repositories are cloned (or, for a local path, cloned from that path) into --workdir and reused between cases.
`setup` commands run once per repository with cwd at the clone and LG_VENV set to a cached folder for a venv;
if LG_VENV/bin/python exists afterwards and the case gives no `python`, that interpreter runs the target code.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "legacyguard"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

from guardlib import baseline, checks, ci, config as cfgmod  # noqa: E402
from runner import diff  # noqa: E402

DETECTED, QUIET, UNCHECKED, ERROR = "detected", "quiet", "unchecked", "error"
SETUP_TIMEOUT = 1800


# ---------------------------------------------------------------- manifest

def load_manifest(path: Path) -> list[dict]:
    """The cases of a YAML or JSON manifest: a list, or a mapping with a `cases` list."""
    text = path.read_text()
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        import yaml
        data = yaml.safe_load(text)
    if isinstance(data, dict):
        data = data.get("cases")
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list of cases or a mapping with 'cases'")
    return [c for c in data if isinstance(c, dict)]


def expected(case: dict) -> str:
    if case.get("kind") == "bug":
        return DETECTED
    exp = str(case.get("expect") or "").strip().lower()
    if exp not in (DETECTED, QUIET):
        raise ValueError("a change case needs expect: quiet or detected")
    return exp


# ---------------------------------------------------------------- repositories

def _git(root: Path | str, *args: str, timeout: int = 600) -> str:
    res = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout)
    if res.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(res.stderr or res.stdout).strip()[:500]}")
    return res.stdout.strip()


def _slug(repo: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", repo.rstrip("/").split("/")[-1].removesuffix(".git")) or "repo"
    return f"{base}-{hashlib.sha1(repo.encode()).hexdigest()[:8]}"


def resolve_repo(repo: str, manifest_dir: Path) -> str:
    """A local path (absolute, or relative to the manifest) if one exists, else the string as given (a git URL)."""
    if "://" in repo or repo.startswith("git@"):
        return repo
    path = Path(repo).expanduser()
    if not path.is_absolute():
        path = manifest_dir / path
    return str(path.resolve()) if path.exists() else repo


def ensure_clone(repo: str, workdir: Path) -> Path:
    """The clone of repo under workdir/repos, made once and reused."""
    dest = workdir / "repos" / _slug(repo)
    if (dest / ".git").exists():
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", repo, str(dest)], capture_output=True, text=True, timeout=1800)
    if res.returncode:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"git clone {repo}: {res.stderr.strip()[:500]}")
    exclude = dest / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as fh:
        fh.write("\n.legacyguard/\n")
    return dest


def reset_clone(clone: Path, commit: str) -> None:
    """Drop the previous case's guard state and edits, then check out commit (detached)."""
    state = clone / ".legacyguard"
    if state.exists():
        with contextlib.suppress(Exception):
            _git(clone, "worktree", "remove", "--force", str(state / "baseline"))
        shutil.rmtree(state, ignore_errors=True)
    _git(clone, "worktree", "prune")
    _git(clone, "reset", "--hard", "--quiet")
    _git(clone, "clean", "-ffdxq")
    _git(clone, "checkout", "--quiet", "--force", "--detach", commit)


def ensure_setup(case: dict, clone: Path, workdir: Path) -> str | None:
    """Run the case's setup commands once per repository (cached by their text); the venv's python if it made one."""
    setup = case.get("setup")
    if not setup:
        return None
    commands = setup if isinstance(setup, list) else [setup]
    env_dir = workdir / "envs" / clone.name
    marker = env_dir.with_name(env_dir.name + ".setup-done")
    key = hashlib.sha1("\n".join(map(str, commands)).encode()).hexdigest()
    if not (marker.exists() and marker.read_text() == key):
        env_dir.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "LG_VENV": str(env_dir)}
        for cmd in commands:
            res = subprocess.run(str(cmd), shell=True, cwd=clone, env=env, capture_output=True, text=True, timeout=SETUP_TIMEOUT)
            if res.returncode:
                raise RuntimeError(f"setup command failed ({cmd}): {(res.stderr or res.stdout).strip()[-500:]}")
        marker.write_text(key)
    for rel in (("bin", "python"), ("Scripts", "python.exe")):
        candidate = env_dir.joinpath(*rel)
        if candidate.exists():
            return str(candidate)
    return None


def resolve_python(case: dict, clone: Path, setup_python: str | None) -> str | None:
    py = case.get("python")
    if not py:
        return setup_python
    path = Path(str(py)).expanduser()
    if not path.is_absolute() and (clone / path).exists():
        return str(clone / path)
    return str(path) if path.is_absolute() else (shutil.which(str(py)) or str(py))


def _source_files_between(clone: Path, a: str, b: str) -> list[str]:
    names = _git(clone, "diff", "--name-only", a, b, "--", "*.py").splitlines()
    return [f for f in names if f and not checks.is_test_path(f)]


def _patch_files(patch: str) -> list[str]:
    """Source files a unified diff touches (new name, or old name for a deletion)."""
    out = []
    for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", patch, re.M):
        rel = m.group(2)
        if rel.endswith(".py") and not checks.is_test_path(rel) and rel not in out:
            out.append(rel)
    return out


def _whole_files(clone: Path, buggy: str, files: list[str]) -> None:
    for rel in files:
        target = clone / rel
        res = subprocess.run(["git", "-C", str(clone), "show", f"{buggy}:{rel}"], capture_output=True, timeout=120)
        if res.returncode:
            if b"does not exist" in res.stderr or b"exists on disk, but not in" in res.stderr:
                target.unlink(missing_ok=True)
                continue
            raise RuntimeError(f"git show {buggy}:{rel}: {res.stderr.decode(errors='replace').strip()[:300]}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(res.stdout)


def plant_bug(clone: Path, fixed: str, buggy: str, files: list[str] | None, patch: str | None = None) -> tuple[list[str], list[str]]:
    """Turn the fixed checkout into the buggy version of the bug's files: (the bug's files, warnings).

    The fix is reverse-applied: `patch` if the case gives one (BugsInPy's bug_patch.txt, which keeps other changes
    in the same files untouched), else `git diff buggy fixed -- files`. If that does not apply, the buggy files
    replace the fixed ones whole, with a warning. Files that exist only in the buggy version are marked
    intent-to-add so `git diff <base>` (and so ci.run) sees them.
    """
    warnings = []
    if not files and patch:
        files = _patch_files(patch)
    elif files and patch:
        extra = [f for f in _patch_files(patch) if f not in files]
        if extra:
            warnings.append(f"the patch also changes {', '.join(extra)}, outside the bug's files; differences there are not credited")
    if not files:
        files = _source_files_between(clone, fixed, buggy)
        if len(files) > 1:
            warnings.append(f"no files given and {len(files)} source files differ between fixed and buggy "
                            f"({', '.join(files)}); all were planted and all count as the bug's files. "
                            "Take files from BugsInPy's bug_patch.txt.")
    if not files:
        raise RuntimeError("no Python source files differ between fixed and buggy")
    if patch is None:
        patch = subprocess.run(["git", "-C", str(clone), "diff", "--binary", buggy, fixed, "--", *files],
                               capture_output=True, text=True, timeout=120).stdout
        if not patch.strip():
            raise RuntimeError(f"the bug's files do not differ between fixed and buggy: {', '.join(files)}")
    res = subprocess.run(["git", "-C", str(clone), "apply", "-R", "--whitespace=nowarn", "-"], input=patch,
                         capture_output=True, text=True, timeout=120)
    if res.returncode:
        _whole_files(clone, buggy, files)
        warnings.append("the fix did not reverse-apply (" + " ".join(res.stderr.split())[:200]
                        + "); the buggy files replaced the fixed ones whole, so other changes in them were reverted too")
    for rel in files:
        if (clone / rel).exists() and subprocess.run(["git", "-C", str(clone), "ls-files", "--error-unmatch", "--", rel],
                                                     capture_output=True).returncode:
            add = subprocess.run(["git", "-C", str(clone), "add", "-N", "--", rel], capture_output=True, text=True)
            if add.returncode:
                warnings.append(f"{rel} exists only in the buggy version and could not be marked for git diff, so the guard skips it")
    return list(files), warnings


def preflight(clone: Path, python: str | None) -> tuple[str, str]:
    """(interpreter, version) the guard will run target code with; raises if it cannot run or is older than 3.7."""
    resolved = cfgmod.target_python(ci.load_cfg(str(clone), python))
    if not resolved:
        raise RuntimeError("no Python interpreter found for the target code")
    try:
        res = subprocess.run([resolved, "-c", "import sys; print('%d.%d.%d' % tuple(sys.version_info[:3]))"],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"interpreter {resolved} cannot run: {exc}") from exc
    if res.returncode or not re.fullmatch(r"\d+\.\d+\.\d+", res.stdout.strip()):
        raise RuntimeError(f"interpreter {resolved} cannot run: {(res.stderr or res.stdout).strip()[:300]}")
    return resolved, res.stdout.strip()


# ---------------------------------------------------------------- running the guard

def overrides_for(case: dict) -> dict:
    """Config the case adds on top of the project's: app, source_roots, and mutants always on."""
    out = {key: case[key] for key in ("app", "source_roots") if case.get(key)}
    out["behavioral_diff"] = {"mutants": cfgmod.DEFAULTS["behavioral_diff"]["mutants"] or 8}
    return out


def _function(r) -> dict:
    out = {"file": r.file, "qualname": r.qualname, "status": r.status, "reason": r.reason,
           "inputs_compared": r.inputs_compared, "mutants_killed": r.mutants_killed, "mutants_total": r.mutants_total}
    if r.via:
        out["via"], out["via_file"] = r.via, r.via_file
    if r.differences:
        d = r.differences[0]
        out["counterexample"] = {"call": d.get("call"), "before": d.get("before"), "after": d.get("after")}
    return out


def classify(kind: str, functions: list[dict], bug_files: list[str]) -> dict:
    """Verdict and the function lists behind it.

    For a bug case only functions in the bug's files, or callers of them (via), count: a difference elsewhere goes
    to other_different. The verdict is "unchecked" when no counted function was verified, different or broken
    (nothing was really compared), so it is not mistaken for quiet.
    """
    if kind == "bug":
        files = set(bug_files)
        relevant = [f for f in functions if f["file"] in files or f.get("via_file") in files]
    else:
        relevant = list(functions)
    bad = (diff.DIFFERENT, diff.BROKEN)
    different = [f for f in relevant if f["status"] in bad]
    other = [f for f in functions if f["status"] in bad and f not in relevant]
    checked = any(f["status"] in (diff.DONE, *bad) for f in relevant)
    verdict = DETECTED if different else QUIET if checked else UNCHECKED
    return {"verdict": verdict, "different": different, "other_different": other}


def run_case(case: dict, workdir: Path, manifest_dir: Path) -> dict:
    rec = {"name": case.get("name") or "?", "kind": case.get("kind"), "repo": case.get("repo"), "expect": None,
           "verdict": ERROR, "match": False, "error": "", "warnings": [], "seconds": 0.0, "prep_seconds": 0.0,
           "python": None, "python_version": None, "files": [], "changed_files": [],
           "functions": [], "different": [], "other_different": [], "verified": [], "untested": [], "contract_breaks": 0,
           "mutants_killed": 0, "mutants_total": 0, "mutation_score": None}
    start = time.monotonic()
    try:
        if any("<FILL ME" in str(v) for v in case.values()):
            raise ValueError("placeholder values left in the case (<FILL ME>)")
        kind = case.get("kind")
        if kind not in ("bug", "change"):
            raise ValueError(f"unknown kind {kind!r} (bug or change)")
        rec["expect"] = expected(case)
        if not case.get("repo"):
            raise ValueError("no repo")
        clone = ensure_clone(resolve_repo(str(case["repo"]), manifest_dir), workdir)
        if kind == "bug":
            fixed, buggy = str(case.get("fixed") or ""), str(case.get("buggy") or "")
            if not fixed or not buggy:
                raise ValueError("a bug case needs fixed and buggy commits")
            reset_clone(clone, fixed)
            base = _git(clone, "rev-parse", "--verify", f"{fixed}^{{commit}}")
            buggy = _git(clone, "rev-parse", "--verify", f"{buggy}^{{commit}}")
            files = case.get("files")
            patch = None
            if case.get("patch"):
                patch_path = Path(str(case["patch"])).expanduser()
                patch = (patch_path if patch_path.is_absolute() else manifest_dir / patch_path).read_text()
            rec["files"], rec["warnings"] = plant_bug(clone, base, buggy, [files] if isinstance(files, str) else files, patch)
        else:
            base_ref, head = str(case.get("base") or ""), str(case.get("head") or "")
            if not base_ref or not head:
                raise ValueError("a change case needs base and head commits")
            reset_clone(clone, head)
            base = _git(clone, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
        python = resolve_python(case, clone, ensure_setup(case, clone, workdir))
        rec["python"], rec["python_version"] = preflight(clone, python)
        if tuple(int(n) for n in rec["python_version"].split(".")[:2]) < (3, 7):
            raise RuntimeError(f"interpreter {rec['python']} is Python {rec['python_version']}; the guard's worker needs 3.7 or newer")
        rec["prep_seconds"] = round(time.monotonic() - start, 3)
        t0 = time.monotonic()
        cfg = None
        try:
            cfg, out = ci.run(str(clone), base, rec["python"], overrides=overrides_for(case))
        finally:
            rec["seconds"] = round(time.monotonic() - t0, 3)
            if cfg is not None:
                with contextlib.suppress(Exception):
                    baseline.remove(cfg)
        rec["changed_files"] = list(out.files)
        if kind == "change":
            rec["files"] = list(out.files)
        rec["functions"] = [_function(r) for r in out.results]
        rec.update(classify(kind, rec["functions"], rec["files"]))
        rec["verified"] = [f for f in rec["functions"] if f["status"] == diff.DONE]
        rec["untested"] = [f for f in rec["functions"] if f["status"] == diff.UNTESTED]
        rec["contract_breaks"] = len(out.breaks)
        rec["mutants_killed"] = sum(r.mutants_killed for r in out.results)
        rec["mutants_total"] = sum(r.mutants_total for r in out.results)
        if rec["mutants_total"]:
            rec["mutation_score"] = round(rec["mutants_killed"] / rec["mutants_total"], 3)
        rec["match"] = rec["verdict"] == rec["expect"]
    except Exception as exc:  # noqa: BLE001 - one case never stops the run
        rec["verdict"], rec["match"] = ERROR, False
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["traceback"] = traceback.format_exc()[-3000:]
    rec["wall_seconds"] = round(time.monotonic() - start, 3)
    return rec


# ---------------------------------------------------------------- metrics and output

def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]  # nearest rank


def metrics(records: list[dict]) -> dict:
    """Rates over cases that were checked: errors and "unchecked" cases are counted and listed separately,
    never as misses, passes or alarms."""
    ran = [r for r in records if r["verdict"] != ERROR]
    checked = [r for r in ran if r["verdict"] != UNCHECKED]
    want = [r for r in checked if r["expect"] == DETECTED]
    calm = [r for r in checked if r["kind"] == "change" and r["expect"] == QUIET]
    functions = [f for r in ran for f in r["functions"]]
    untested = [f for f in functions if f["status"] == diff.UNTESTED]
    times = [r["seconds"] for r in ran]
    killed, total = sum(r["mutants_killed"] for r in ran), sum(r["mutants_total"] for r in ran)
    found = sum(1 for r in want if r["verdict"] == DETECTED)
    alarms = sum(1 for r in calm if r["verdict"] == DETECTED)
    return {
        "cases": len(records), "ran": len(ran), "checked": len(checked),
        "errors": len(records) - len(ran), "unchecked": len(ran) - len(checked),
        "error_cases": [r["name"] for r in records if r["verdict"] == ERROR],
        "unchecked_cases": [r["name"] for r in ran if r["verdict"] == UNCHECKED],
        "matched": sum(1 for r in checked if r["match"]),
        "detection": {"detected": found, "total": len(want), "rate": _rate(found, len(want))},
        "false_alarm": {"alarms": alarms, "total": len(calm), "rate": _rate(alarms, len(calm))},
        "untested": {"functions": len(untested), "total": len(functions), "share": _rate(len(untested), len(functions))},
        "mutation": {"killed": killed, "total": total, "score": _rate(killed, total)},
        "seconds": {"median": round(statistics.median(times), 3) if times else None, "p95": _p95(times)},
    }


def _pct(rate) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def metrics_text(m: dict) -> str:
    d, f, u, s = m["detection"], m["false_alarm"], m["untested"], m["seconds"]
    lines = [
        f"Cases: {m['cases']} ({m['checked']} checked, {m['unchecked']} unchecked, {m['errors']} errors; "
        f"{m['matched']} matched the expectation)",
        f"Detection rate: {_pct(d['rate'])} ({d['detected']} of {d['total']} checked cases expected to be detected)",
        f"False-alarm rate: {_pct(f['rate'])} ({f['alarms']} of {f['total']} checked change cases expected quiet)",
        f"Untested share: {_pct(u['share'])} ({u['functions']} of {u['total']} functions)",
        f"Mutation score: {_pct(m['mutation']['score'])} ({m['mutation']['killed']} of {m['mutation']['total']} planted bugs caught)",
        f"Time per case: median {s['median']} s, p95 {s['p95']} s",
    ]
    if m["unchecked_cases"]:
        lines.append(f"Unchecked (nothing compared, left out of the rates): {', '.join(m['unchecked_cases'])}")
    if m["error_cases"]:
        lines.append(f"Errors (left out of the rates): {', '.join(m['error_cases'])}")
    return "\n".join(lines)


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _diff_line(label: str, f: dict) -> str:
    ce = f.get("counterexample")
    text = f"`{ce['call']}` before `{_cell(ce['before'])}` after `{_cell(ce['after'])}`" if ce else _cell(f["reason"])
    via = f" (not edited; it {f['via']}, edited in `{f['via_file']}`)" if f.get("via") else ""
    return f"- {label}: `{f['file']}::{f['qualname']}`{via}: {text}"


def markdown(records: list[dict], m: dict, manifest: str) -> str:
    lines = ["# LegacyGuard validation", "", f"Manifest: `{manifest}`", "", "## Metrics", "", "```", metrics_text(m), "```", "",
             "Rates count only checked cases. A case is \"unchecked\" when no function in it was verified, different or "
             "broken (nothing was really compared); it and errors are listed separately. "
             "Agreement is evidence on the inputs tried, not a proof.", "",
             "## Cases", "", "| Case | Kind | Expected | Verdict | Match | Different | Verified | Untested | Mutants | Python | Seconds |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        mut = f"{r['mutants_killed']}/{r['mutants_total']}" if r["mutants_total"] else "-"
        lines.append(f"| {_cell(r['name'])} | {r['kind']} | {r['expect'] or '-'} | {r['verdict']} | {'yes' if r['match'] else 'no'} | "
                     f"{len(r['different'])} | {len(r['verified'])} | {len(r['untested'])} | {mut} | {r.get('python_version') or '-'} | {r['seconds']} |")
    lines.append("")
    for r in records:
        if not (r["different"] or r["other_different"] or r["untested"] or r["error"] or r["warnings"] or r["verdict"] == UNCHECKED):
            continue
        lines += [f"### {r['name']}", ""]
        if r["error"]:
            lines.append(f"- Error: {_cell(r['error'])}")
        if r["verdict"] == UNCHECKED:
            lines.append("- Unchecked: no function in this case was verified, different or broken.")
        for w in r["warnings"]:
            lines.append(f"- Warning: {_cell(w)}")
        lines += [_diff_line("Different", f) for f in r["different"]]
        lines += [_diff_line("Different outside the bug's files (not credited)", f) for f in r["other_different"]]
        for f in r["untested"]:
            lines.append(f"- Untested: `{f['file']}::{f['qualname']}`: {_cell(f['reason'])}")
        lines.append("")
    return "\n".join(lines)


def run(manifest: Path, workdir: Path, out_dir: Path, only: list[str] | None = None, echo=print) -> dict:
    cases = load_manifest(manifest)
    if only:
        cases = [c for c in cases if c.get("name") in only]
    workdir.mkdir(parents=True, exist_ok=True)
    records = []
    for case in cases:
        echo(f"[{len(records) + 1}/{len(cases)}] {case.get('name')} ...")
        rec = run_case(case, workdir, manifest.parent)
        echo(f"    {rec['verdict']}" + (f" (expected {rec['expect']})" if rec["expect"] else "")
             + (f": {rec['error']}" if rec["error"] else f" in {rec['seconds']} s"))
        for w in rec["warnings"]:
            echo(f"    warning: {w}")
        records.append(rec)
    m = metrics(records)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"manifest": str(manifest), "workdir": str(workdir), "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "metrics": m, "cases": records}
    (out_dir / "results.json").write_text(json.dumps(result, indent=2, default=str))
    (out_dir / "results.md").write_text(markdown(records, m, str(manifest)))
    echo("")
    echo(metrics_text(m))
    echo(f"Wrote {out_dir / 'results.json'} and {out_dir / 'results.md'}")
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="validate", description="Run LegacyGuard on the cases of a manifest and measure it.")
    p.add_argument("manifest", type=Path)
    p.add_argument("--workdir", type=Path, default=Path(tempfile.gettempdir()) / "legacyguard-validate",
                   help="where repositories are cloned and venvs cached (reused between runs)")
    p.add_argument("--out", type=Path, default=Path("validation-results"))
    p.add_argument("--only", nargs="*", help="run only the cases with these names")
    a = p.parse_args(argv)
    try:
        run(a.manifest.resolve(), a.workdir.resolve(), a.out.resolve(), a.only)
    except (OSError, ValueError) as exc:
        print(f"validate: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
