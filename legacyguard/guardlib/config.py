"""Read .legacyguard/config.yaml on every hook call (FR-02, D-026, D-030)."""
from __future__ import annotations

import copy
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MODES = ("advisory", "guarded", "strict")
DEFAULTS = {
    "enabled": True, "scope": ".", "language": "python", "mode": "guarded", "python": None, "app": None, "django_settings": None,
    "source_roots": [], "contracts": {}, "frozen": [],
    "behavioral_diff": {"network": "blocked", "max_inputs": 500, "timeout_s": 60, "self_diff": True, "env": {}},
    "alarm_budget": {"max_prompts_per_session": 3, "may_interrupt": ["frozen", "contract_break", "behavior_difference"],
                     "context_only": ["many_callers", "history_coupling", "untested"]},
}


class ConfigMissing(Exception):
    pass


class ConfigError(Exception):
    pass


class ConfigDisabled(ConfigError):
    """enabled: false. The developer switched the guard off in this repo."""


@dataclass
class Config:
    root: str
    raw: dict = field(default_factory=dict)

    def __getattr__(self, name):
        try:
            return self.__dict__["raw"][name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def contract_path(self, kind: str) -> str | None:
        """Repo-relative path of a contract named in config. Contract paths are relative to scope."""
        value = (self.raw.get("contracts") or {}).get(kind)
        if not value:
            return None
        scope = self.raw["scope"].rstrip("/")
        return os.path.normpath(value if scope in ("", ".") else f"{scope}/{value}").replace("\\", "/")

    @property
    def state_dir(self) -> Path:
        return Path(self.root) / ".legacyguard"

    @property
    def budget(self) -> int:
        return int(self.raw["alarm_budget"].get("max_prompts_per_session", 3))


def config_path(root: str) -> Path:
    return Path(root) / ".legacyguard" / "config.yaml"


def load_config(root: str) -> Config:
    path = config_path(root)
    if not path.exists():
        raise ConfigMissing(str(path))
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        raise ConfigError(" ".join(str(exc).split())) from exc
    if not isinstance(data, dict):
        raise ConfigError("config.yaml must be a mapping")
    merged = copy.deepcopy(DEFAULTS)
    for key, value in data.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key].update(value)
        elif value is not None:
            merged[key] = value
    if merged["enabled"] is False:
        raise ConfigDisabled("switched off by enabled: false in .legacyguard/config.yaml")
    if merged["mode"] not in MODES:
        raise ConfigError(f"mode must be one of {', '.join(MODES)}, got {merged['mode']!r}")
    if not isinstance(merged["scope"], str):
        raise ConfigError("scope must be a path")
    if merged["behavioral_diff"].get("network") != "blocked":
        raise ConfigError("behavioral_diff.network must be blocked")
    return Config(root=root, raw=merged)


def target_python(cfg: Config) -> str | None:
    """Config key python:, then .venv, venv, env, then python3/python on PATH (D-026)."""
    configured = cfg.raw.get("python")
    if configured:
        p = Path(configured)
        return str(p if p.is_absolute() else Path(cfg.root) / p)
    for name in (".venv", "venv", "env"):
        for rel in (("bin", "python"), ("Scripts", "python.exe")):
            cand = Path(cfg.root).joinpath(name, *rel)
            if cand.exists():
                return str(cand)
    return shutil.which("python3") or shutil.which("python")
