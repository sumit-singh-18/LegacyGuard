#!/usr/bin/env python3
"""SessionStart: bootstrap, baseline, session reset, API-key warning, context summary (FR-03, FR-10)."""
import os

import _common
from _common import PLUGIN_ROOT, hook_output, log_error, run_hook

API_KEY = ("LegacyGuard: ANTHROPIC_API_KEY is set, so Claude Code bills that key per token instead of your plan. "
           "The guard never uses it. Unset it to stay on your plan.")


def summary(cfg) -> str:
    c = cfg.raw.get("contracts") or {}
    contracts = ", ".join(f"{k}={v}" for k, v in c.items()) or "none"
    frozen = ", ".join(cfg.raw.get("frozen") or []) or "none"
    return (f"LegacyGuard is active. Scope: {cfg.scope}. Mode: {cfg.mode}. Contract files: {contracts}. Frozen files: {frozen}. "
            "After each edit a background check runs old and new code on the same inputs; differences arrive as counterexamples. "
            "Available guard tools: impact, contract, diff_report (MCP server guard).")


def handle(event: dict):
    from guardlib import bootstrap
    messages = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        messages.append(API_KEY)
    if _common.BOOT_ERROR:
        if "needs Python" in str(_common.BOOT_ERROR):
            messages.append(f"{_common.BOOT_ERROR}. Until then the guard is inactive.")
        else:
            messages.append(f"LegacyGuard could not install its dependencies ({_common.BOOT_ERROR}); the guard is inactive. "
                            f"Install by hand: {bootstrap.manual_command(PLUGIN_ROOT)}")
        return hook_output("SessionStart", system_message=" ".join(messages))

    from guardlib import config as cfgmod
    from guardlib.paths import find_root
    root = find_root(event.get("cwd") or os.getcwd())
    try:
        try:
            cfg = cfgmod.load_config(root)
        except cfgmod.ConfigMissing:
            # First use here: a Python git repo gets its config written from what the repo says (FR-32).
            # Any other folder is left alone and gets no message, because the plugin is installed for every session.
            from guardlib import autosetup
            found = autosetup.setup(root)
            if found is None:
                return hook_output("SessionStart", system_message=" ".join(messages) or None)
            messages.append(autosetup.notice(found))
            cfg = cfgmod.load_config(root)
    except cfgmod.ConfigDisabled:
        return hook_output("SessionStart", system_message=" ".join(messages) or None)
    except cfgmod.ConfigError as exc:
        messages.append(f"LegacyGuard is inactive: .legacyguard/config.yaml is invalid ({exc}).")
        return hook_output("SessionStart", system_message=" ".join(messages))

    try:
        notice = start_session(cfg, event)
        if notice:
            messages.append(notice)
    except Exception as exc:  # noqa: BLE001 - a failed baseline must not take the summary down with it
        log_error(root, f"session_start: {exc!r}")
    return hook_output("SessionStart", context=summary(cfg), system_message=" ".join(messages) or None)


def start_session(cfg, event):
    """New session: new baseline and ledger. Resume or compact of the same session keeps both (D-035, DL log)."""
    from guardlib import baseline, ledger
    from indexer.__main__ import refresh
    session_id = event.get("session_id")
    source = event.get("source", "startup")
    baseline.ensure_state_dir(cfg)
    current = ledger.load(cfg.state_dir)
    if source in ("resume", "compact") and current.get("session_id") == session_id and baseline.baseline_dir(cfg).exists():
        return None
    notice = None
    fresh = ledger.fresh(session_id)
    try:
        info = baseline.create(cfg)
        fresh.update(baseline_commit=info["commit"], dirty_files=info["dirty_files"], baseline_ok=True)
    except baseline.BaselineError as exc:
        notice = f"LegacyGuard: no baseline ({exc}), so behavioral checks are off this session. Contract checks still run."
    with ledger.locked(cfg.state_dir) as data:
        data.clear()
        data.update(fresh)
    if fresh["baseline_ok"]:
        refresh(cfg)
    return notice


if __name__ == "__main__":
    run_hook(handle, install=True, needs_deps=False)
