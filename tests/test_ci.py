"""CI mode: base commit against the working tree, report, annotations, exit code (roadmap E1)."""
import json
import subprocess

from guardlib import ci

from conftest import TARGET_PY, write_tree

FILES = {
    "shop/__init__.py": "",
    "shop/pricing.py": '''
def discount(price: float, rate: float) -> float:
    """>>> discount(100.0, 0.25)"""
    return round(price * (1 - rate), 2)


def label(price: float) -> str:
    return "sale" if discount(price, 0.1) < 50 else "full"
''',
}


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def repo(tmp_path, new_pricing):
    root = write_tree(tmp_path / "proj", FILES)
    (root / ".gitignore").write_text(".legacyguard/\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    (root / "shop/pricing.py").write_text(new_pricing)
    return root


def test_difference_fails_with_report_and_annotations(tmp_path):
    root = repo(tmp_path, FILES["shop/pricing.py"].lstrip("\n").replace("(1 - rate)", "(1 + rate)"))
    cfg, out = ci.run(str(root), "HEAD", TARGET_PY)
    text = ci.report(cfg, out)
    assert "**2 behave differently**" in text or "behave differently**" in text
    assert "`shop/pricing.py::discount`" in text and "`shop/pricing.py::label`" in text  # the caller too
    notes = ci.annotations(cfg, out)
    assert any(n.startswith("::error file=shop/pricing.py,line=1,title=LegacyGuard: discount") for n in notes)
    assert ci.exit_code(cfg, out, "difference") == 1 and ci.exit_code(cfg, out, "none") == 0


def test_refactor_passes(tmp_path):
    root = repo(tmp_path, FILES["shop/pricing.py"].lstrip("\n").replace("return round(price * (1 - rate), 2)",
                                                                       "kept = 1 - rate\n    return round(price * kept, 2)"))
    cfg, out = ci.run(str(root), "HEAD", TARGET_PY)
    assert ci.exit_code(cfg, out, "difference") == 0
    assert "**0 behave differently**" in ci.report(cfg, out)


def test_committed_waiver_is_honored(tmp_path):
    root = repo(tmp_path, FILES["shop/pricing.py"].lstrip("\n").replace("(1 - rate)", "(1 + rate)"))
    cfg, out = ci.run(str(root), "HEAD", TARGET_PY)
    entries = [{"kind": "behavior_difference", "file": r.file, "item": r.qualname, "code_hash": r.code_hash} for r in out.failing(cfg)]
    (root / ".legacyguard/waivers.json").write_text(json.dumps(entries))
    cfg2, out2 = ci.run(str(root), "HEAD", TARGET_PY)
    assert ci.exit_code(cfg2, out2, "difference") == 0 and "waived" in ci.report(cfg2, out2)


def test_cli_end_to_end(tmp_path):
    root = repo(tmp_path, FILES["shop/pricing.py"].lstrip("\n").replace("(1 - rate)", "(1 + rate)"))
    summary = tmp_path / "summary.md"
    import os
    import sys
    from conftest import PLUGIN
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary), "GITHUB_ACTIONS": "true"}
    res = subprocess.run([sys.executable, str(PLUGIN / "scripts/lg_ci.py"), "--root", str(root), "--base", "HEAD", "--python", TARGET_PY],
                         capture_output=True, text=True, env=env, timeout=300)
    assert res.returncode == 1, res.stderr[-1500:]
    assert "::error file=shop/pricing.py" in res.stdout and "## LegacyGuard" in summary.read_text()
