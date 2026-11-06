"""First-use setup for a repo: write .legacyguard/config.yaml from what the repo itself says (FR-32, D-042).

Standard library only. Nothing here asks a model or guesses beyond what a file in the repo states. An existing
config is never overwritten, and a repo with `enabled: false` in its config stays off.
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path

MIN_PY_FILES = 3
SETTINGS_FILES = ("pytest.ini", "setup.cfg", "tox.ini", "pyproject.toml")
PYTEST_SETTING = re.compile(r"""^\s*DJANGO_SETTINGS_MODULE\s*=\s*["']?([A-Za-z_][\w.]*)""", re.M)
PYTEST_DS_FLAG = re.compile(r"""--ds[= ]["']?([A-Za-z_][\w.]*)""")
# A source folder: src/ or lib/ holding a package, at the top or one level down (a sub-project of a monorepo).
SOURCE_FOLDER = re.compile(r"((?:[^/]+/)?(?:src|lib))/[^/]+/__init__\.py")
MAX_SOURCE_ROOTS = 6
MANAGE_SETTING = re.compile(r"""["']DJANGO_SETTINGS_MODULE["']\s*,\s*["']([A-Za-z_][\w.]*)["']""")
OPENAPI_NAMES = ("openapi.yaml", "openapi.yml", "openapi.json")
VENV_NAMES = (".venv", "venv", "env")


def _tracked_python(root: Path) -> list[str]:
    try:
        res = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", "*.py"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [f for f in res.stdout.split("\0") if f] if res.returncode == 0 else []


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _module_exists(bases: list[Path], module: str) -> bool:
    rel = Path(*module.split("."))
    return any((b / rel).with_suffix(".py").exists() or (b / rel / "__init__.py").exists() for b in bases)


def _django_settings(bases: list[Path]) -> str | None:
    """Test settings named in the pytest config come first: they are the ones made to run without real services."""
    found = []
    for base in bases:
        for name in SETTINGS_FILES:
            text = _read(base / name)
            found += PYTEST_SETTING.findall(text) + PYTEST_DS_FLAG.findall(text)
    for base in bases:
        found += MANAGE_SETTING.findall(_read(base / "manage.py"))
    for module in found:
        if _module_exists(bases, module):
            return module
    return None


def detect(root: str | Path) -> dict | None:
    """What to write for this repo, or None when it is not a Python git repo."""
    root = Path(root)
    if not (root / ".git").exists() or root == Path.home():
        return None
    files = _tracked_python(root)
    if len(files) < MIN_PY_FILES:
        return None
    source_roots = list(dict.fromkeys(m.group(1) for m in map(SOURCE_FOLDER.fullmatch, files) if m))
    # A Django project kept one folder down (netbox/manage.py): that folder is where its imports start.
    source_roots += [f.split("/")[0] for f in files if re.fullmatch(r"[^/]+/manage\.py", f) and f.split("/")[0] not in source_roots]
    source_roots = sorted(source_roots, key=lambda r: (r.count("/"), r))[:MAX_SOURCE_ROOTS]
    scope = "src/" if source_roots == ["src"] else "./"
    bases = [root / r for r in source_roots] + [root]
    scope_dir = root / scope
    openapi = next((n for n in OPENAPI_NAMES if (scope_dir / n).exists()), None)
    return {"scope": scope, "source_roots": source_roots, "django_settings": _django_settings(bases), "openapi": openapi,
            "has_venv": any((root / v).is_dir() for v in VENV_NAMES), "python_files": len(files)}


def render(found: dict, today: str | None = None) -> str:
    today = today or dt.date.today().isoformat()
    lines = [f"# Written by LegacyGuard on first use in this repo ({today}), from what the repo itself says.",
             "# Edit freely: it is never overwritten. To switch the guard off in this repo, add the line: enabled: false",
             f"scope: {found['scope']}", "language: python", "mode: guarded            # advisory | guarded | strict"]
    if found["source_roots"]:
        lines.append(f"source_roots: [{', '.join(found['source_roots'])}]")
    if found["django_settings"]:
        lines.append(f"django_settings: {found['django_settings']}")
    if found["openapi"]:
        lines += ["contracts:", f"  http: {found['openapi']}"]
    lines.append("frozen: []                # files Claude must never edit, for example legacy/ledger_v1.py")
    return "\n".join(lines) + "\n"


def setup(root: str | Path) -> dict | None:
    """Write the config if this is a Python git repo without one. Returns what was found, or None if nothing was written."""
    root = Path(root)
    target = root / ".legacyguard" / "config.yaml"
    if target.exists():
        return None
    found = detect(root)
    if found is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(found))
    return found


def notice(found: dict) -> str:
    parts = [f"LegacyGuard set itself up for this project and is watching {found['scope']}"]
    if found["django_settings"]:
        parts[0] += f" (Django settings: {found['django_settings']})"
    parts[0] += ". Its settings are in .legacyguard/config.yaml."
    if not found["has_venv"]:
        parts.append("No .venv folder was found here, so code that needs the project's libraries will be reported Untested.")
    return " ".join(parts)
