"""Session report (FR-19): one line per finding, exactly one status each, readable in ten seconds."""
from __future__ import annotations

import re
from pathlib import Path

from guardlib import ledger

ORDER = {ledger.FAILURE: 0, ledger.DIFFERENCE: 1, ledger.SLOWDOWN: 2, ledger.UNTESTED: 3, ledger.RISK: 4, ledger.VERIFIED: 5}
HIDDEN_STATES = {"superseded"}
STATE_NOTE = {"open": "open", "applied": "edit went through", "resolved": "turned back: re-run matches baseline", "waived": "waived by developer",
              "not_applied": "edit did not land", "blocked": "denied", "recorded": ""}
FOOTER = ("Limits: the behavioral diff compares return values and exception type and message, not side effects such as database or file writes (D-029). "
          "Edits made through shell commands are only seen at this audit. Untested items were not checked. "
          "This report states what was checked; it makes no claim about what was not.")


def visible(data: dict) -> list[dict]:
    items = [f for f in data["findings"] if f["state"] not in HIDDEN_STATES]
    return sorted(items, key=lambda f: (ORDER.get(f["status"], 9), int(f["id"].split("-")[1])))


def counts(data: dict) -> dict:
    c = {s: 0 for s in ledger.STATUSES}
    for f in visible(data):
        c[f["status"]] += 1
    return c


def pinnable(data: dict) -> list[dict]:
    return [f for f in visible(data) if f["kind"] == "behavior_difference" and f.get("counterexamples")]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).replace("|", "\\|")


def outcome(f: dict) -> str:
    if f.get("last_decision") == "over_budget":
        return "not asked: prompt budget used up"
    return STATE_NOTE.get(f["state"], f["state"])


def _line(f: dict) -> str:
    note = outcome(f)
    return f"| {f['id']} | {f['status']} | {_clean(f['subject'])} | {_clean(f['evidence'])[:220]} | {note} |"


def _first_example(f: dict) -> str:
    ce = (f.get("counterexamples") or [{}])[0]
    if ce.get("call"):
        return f"`{ce['call']}` gave `{ce['before']}` before the edit and `{ce['after']}` after it"
    return _clean(f["evidence"])[:200]


def story(data: dict) -> list[dict]:
    """What was asked, what changed, what broke and what fixed it, in order and in plain words (FR-31).

    Each step is {kind, text, id}. Kinds: asked, broke, fixed, open, accepted, checked, unchecked, asked_you, blocked.
    """
    steps = [{"kind": "asked", "text": _clean(p)[:400], "id": None} for p in data.get("prompt_text") or [] if str(p).strip()]
    findings = sorted(data["findings"], key=lambda f: int(f["id"].split("-")[1]))
    verified = {(f["file"], f["item"]): f for f in findings if f["kind"] == "verified" and f["state"] != "superseded"}
    had_difference = {(f["file"], f["item"]) for f in findings if f["kind"] == "behavior_difference"}
    for f in findings:
        key, name = (f["file"], f["item"]), f"`{f['item']}` in {f['file']}"
        if f["kind"] == "behavior_difference":
            if f["status"] == ledger.FAILURE:
                steps.append({"kind": "broke", "id": f["id"], "text": f"The edit to {name} broke it: {_clean(f['evidence'])[:200]}."})
            else:
                if f.get("via"):
                    steps.append({"kind": "broke", "id": f["id"], "text": f"{name} was not edited, but it {f['via']} (edited in {f['via_file']}) and now returns something else: {_first_example(f)}."})
                else:
                    steps.append({"kind": "broke", "id": f["id"], "text": f"The edit to {name} changed what it returns: {_first_example(f)}."})
            if f["state"] == "resolved":
                ok = verified.get(key)
                proof = f" The guard ran it again: {ok['evidence']}, all matching the original." if ok else ""
                steps.append({"kind": "fixed", "id": f["id"], "text": f"Claude saw that example and revised {name}.{proof}"})
            elif f["state"] == "superseded":
                steps.append({"kind": "fixed", "id": f["id"], "text": f"Claude changed {name} again, and the guard re-checked the new version."})
            elif f["state"] == "waived":
                steps.append({"kind": "accepted", "id": f["id"], "text": f"You accepted the change to {name} with /legacyguard:waive."})
            elif f["state"] == "intended":
                steps.append({"kind": "accepted", "id": f["id"], "text": f"Claude declared this change ({_clean(f.get('intent', ''))[:200]}); "
                              f"its declared examples hold. Check that it is what you asked for, or turn it back."})
            elif f["state"] == "open":
                steps.append({"kind": "open", "id": f["id"], "text": f"{name} still behaves differently from the original. If you asked for that, accept it with /legacyguard:waive {f['id']} \"<reason>\"."})
        elif f["kind"] == "verified" and f["state"] != "superseded" and key not in had_difference:
            if f.get("via"):
                steps.append({"kind": "checked", "id": f["id"], "text": f"{name} was not edited; it {f['via']}, so it was run too, and old and new code agree: {f['evidence']}."})
            else:
                steps.append({"kind": "checked", "id": f["id"], "text": f"{name} changed, and old and new code agree: {f['evidence']}."})
        elif f["kind"] == "slowdown" and f["state"] != "superseded":
            # Task 6.6: an input that timed out only in the new code. Evidence, never a wrong result.
            how = {"resolved": " Claude revised it, and the guard re-ran it without the slowdown.",
                   "waived": " You accepted it with /legacyguard:waive."}.get(f["state"], "")
            s = (f.get("slowdowns") or [{}])[0]
            what = (f"the new code ran past {s['new_limit_s']:g} s on `{_clean(s['call'])[:160]}` while the old code finished in "
                    f"{max(s['old_seconds'], 0.001):.3f} s") if s.get("call") else _clean(f["evidence"])[:260]
            steps.append({"kind": "slowdown", "id": f["id"],
                          "text": f"{name} ran slower after the edit: {what}. This is a slowdown, not a proof of a wrong result.{how}"})
        elif f["kind"] == "untested" and f["state"] != "superseded":
            why = _clean(f["evidence"]).replace("untested because: ", "").replace("untested because it ", "it ")[:160]
            steps.append({"kind": "unchecked", "id": f["id"], "text": f"{name} changed but could not be checked by running it: {why}."})
        elif f["kind"] == "contract_break":
            how = {"not_applied": "You were asked first, and the edit did not happen.", "applied": "You were asked first and approved it.",
                   "waived": "You accepted it with /legacyguard:waive.", "undone": "It was later undone.", "open": "It is in the code now and needs your decision."}.get(f["state"], "")
            if f.get("last_decision") == "over_budget":
                how = "You were not asked, because this session's prompt budget was used up."
            steps.append({"kind": "asked_you" if f["state"] in ("not_applied", "applied") else "open" if f["state"] == "open" else "accepted", "id": f["id"],
                          "text": f"An edit to {f['file']} would break something others rely on: {_clean(f['subject'])}. {how}".strip()})
        elif f["kind"] == "test_regression" and f["state"] != "superseded":
            label = {"open": "open", "resolved": "fixed", "waived": "accepted"}.get(f["state"], "broke")
            text = {"open": f"The project's test {f['subject']} passed before this session and fails now: {_clean(f['evidence'])[:200]}.",
                    "resolved": f"The project's test {f['subject']} failed after an edit; Claude revised the code and it passes again.",
                    "waived": f"You accepted that {f['subject']} now fails, with /legacyguard:waive."}.get(f["state"], f"{f['subject']} failed.")
            steps.append({"kind": label, "id": f["id"], "text": text})
        elif f["kind"] == "frozen":
            steps.append({"kind": "blocked" if f["state"] == "blocked" else "open" if f["state"] == "open" else "fixed", "id": f["id"],
                          "text": f"{_clean(f['subject'])}. {outcome(f).capitalize()}."})
    steps += _skipped_steps(data)
    return steps


def _skipped_steps(data: dict) -> list[dict]:
    """Callers of edited code left out of a check (engine.targets_for, roadmap 6.5), one step per reason, so the
    report says they were not run instead of leaving them out."""
    by_reason: dict[str, list[str]] = {}
    for key, s in (data.get("callers_skipped") or {}).items():
        by_reason.setdefault(_clean(s.get("reason") or "no reason recorded"), []).append(f"`{key.split('::', 1)[-1]}` in {key.split('::', 1)[0]}")
    out = []
    for reason, names in by_reason.items():
        n = len(names)
        shown = ", ".join(names[:5]) + (f", +{n - 5} more" if n > 5 else "")
        out.append({"kind": "unchecked", "id": None,
                    "text": f"{n} caller{'s' if n != 1 else ''} of edited code {'were' if n != 1 else 'was'} not run: {reason} ({shown}). "
                            f"Nothing is known about {'them' if n != 1 else 'it'} from execution."})
    return out


STORY_LABEL = {"asked": "You asked", "broke": "What broke", "fixed": "What fixed it", "open": "Needs you", "accepted": "Accepted", "checked": "Checked",
               "unchecked": "Not checked", "asked_you": "Asked you", "blocked": "Blocked", "slowdown": "Slower"}


OS_SANDBOXES = ("sandbox-exec", "bubblewrap")


def isolation_line(data: dict) -> str:
    """What isolated the processes that ran project code in the last check (Task 7.4), or "" when none ran."""
    iso = data.get("isolation") or {}
    if not iso:
        return ""
    sandbox = iso.get("os_sandbox") or "not used"
    line = f"Isolation: OS sandbox **{sandbox}**; working folder **{iso.get('cwd') or 'not used'}**."
    if sandbox not in OS_SANDBOXES:
        line += " **No OS sandbox: absolute-path writes are stopped only by the in-process guards.**"
    return line


def tools_line(data: dict, cfg) -> str:
    """Sprint 7.6: which contract tools ran (oasdiff, makemigrations --check), or "" when none applies here."""
    try:
        from guardlib import contract_tools
        if not (cfg.contract_path("http") or (data.get("contract_tools") or {}).get("makemigrations")
                or (data.get("contract_tools") or {}).get("notes") or (data.get("contract_tools") or {}).get("app_openapi")
                or data.get("app_openapi")):
            return ""
        return contract_tools.report_line(cfg, data)
    except Exception:  # noqa: BLE001 - the report never fails on this line
        return ""


def render(data: dict, cfg) -> str:
    c = counts(data)
    lines = [f"# LegacyGuard session report", "",
             f"Session `{data.get('session_id')}`. Mode **{cfg.mode}**. Baseline `{(data.get('baseline_commit') or 'none')[:10]}`.",
             f"Prompts used **{data['prompts_used']} of {cfg.budget}**. Claude wake-ups **{data['wakeups']}**. Edits seen {data.get('edits', 0)}."]
    iso = isolation_line(data)
    tools = tools_line(data, cfg)
    lines += ([iso] if iso else []) + ([tools] if tools else []) + [""]
    steps = story(data)
    if steps:
        lines += ["## What happened", ""] + [f"- **{STORY_LABEL[st['kind']]}:** {st['text']}" for st in steps] + ["", "## Findings", ""]
    items = visible(data)
    if items:
        lines += ["| ID | Status | Subject | Evidence | Outcome |", "| --- | --- | --- | --- | --- |"]
        lines += [_line(f) for f in items]
    else:
        lines.append("No changes to guarded code were found this session.")
    pins = pinnable(data)
    if pins:
        lines += ["", "Pin a counterexample as a regression test:"]
        lines += [f"- `/legacyguard:pin {f['id']}` ({_clean(f['subject'])})" for f in pins]
    lines += ["", FOOTER, ""]
    return "\n".join(lines)


def summary(data: dict, cfg, path: Path | None = None) -> str:
    c = counts(data)
    turned_back = sum(1 for f in visible(data) if f["state"] == "resolved")
    open_diff = sum(1 for f in visible(data) if f["status"] == ledger.DIFFERENCE and f["state"] == "open")
    parts = [f"{c[ledger.VERIFIED]} verified pass", f"{c[ledger.FAILURE]} confirmed failure", f"{open_diff} open behavior difference", f"{turned_back} turned back",
             f"{c[ledger.UNTESTED]} untested", f"{c[ledger.RISK]} potential risk"]
    if c[ledger.SLOWDOWN]:
        parts.append(f"{c[ledger.SLOWDOWN]} slowdown{'s' if c[ledger.SLOWDOWN] != 1 else ''} (not a proof of a wrong result)")
    text = "LegacyGuard: " + ", ".join(parts) + f". Prompts {data['prompts_used']} of {cfg.budget}, wake-ups {data['wakeups']}."
    return text + (f" Report: {path}" if path else "")


def write(data: dict, cfg) -> Path:
    folder = cfg.state_dir / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_-]", "_", str(data.get("session_id") or "session"))
    path = folder / f"{name}.md"
    path.write_text(render(data, cfg))
    return path
