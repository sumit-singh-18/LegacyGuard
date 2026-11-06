"""Session report (FR-19): one line per finding, exactly one status each, readable in ten seconds."""
from __future__ import annotations

import re
from pathlib import Path

from guardlib import ledger

ORDER = {ledger.FAILURE: 0, ledger.DIFFERENCE: 1, ledger.UNTESTED: 2, ledger.RISK: 3, ledger.VERIFIED: 4}
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
                steps.append({"kind": "broke", "id": f["id"], "text": f"The edit to {name} changed what it returns: {_first_example(f)}."})
            if f["state"] == "resolved":
                ok = verified.get(key)
                proof = f" The guard ran it again: {ok['evidence']}, all matching the original." if ok else ""
                steps.append({"kind": "fixed", "id": f["id"], "text": f"Claude saw that example and revised {name}.{proof}"})
            elif f["state"] == "superseded":
                steps.append({"kind": "fixed", "id": f["id"], "text": f"Claude changed {name} again, and the guard re-checked the new version."})
            elif f["state"] == "waived":
                steps.append({"kind": "accepted", "id": f["id"], "text": f"You accepted the change to {name} with /legacyguard:waive."})
            elif f["state"] == "open":
                steps.append({"kind": "open", "id": f["id"], "text": f"{name} still behaves differently from the original. If you asked for that, accept it with /legacyguard:waive {f['id']} \"<reason>\"."})
        elif f["kind"] == "verified" and f["state"] != "superseded" and key not in had_difference:
            steps.append({"kind": "checked", "id": f["id"], "text": f"{name} changed, and old and new code agree: {f['evidence']}."})
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
        elif f["kind"] == "frozen":
            steps.append({"kind": "blocked" if f["state"] == "blocked" else "open" if f["state"] == "open" else "fixed", "id": f["id"],
                          "text": f"{_clean(f['subject'])}. {outcome(f).capitalize()}."})
    return steps


STORY_LABEL = {"asked": "You asked", "broke": "What broke", "fixed": "What fixed it", "open": "Needs you", "accepted": "Accepted", "checked": "Checked",
               "unchecked": "Not checked", "asked_you": "Asked you", "blocked": "Blocked"}


def render(data: dict, cfg) -> str:
    c = counts(data)
    lines = [f"# LegacyGuard session report", "",
             f"Session `{data.get('session_id')}`. Mode **{cfg.mode}**. Baseline `{(data.get('baseline_commit') or 'none')[:10]}`.",
             f"Prompts used **{data['prompts_used']} of {cfg.budget}**. Claude wake-ups **{data['wakeups']}**. Edits seen {data.get('edits', 0)}.", ""]
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
    text = "LegacyGuard: " + ", ".join(parts) + f". Prompts {data['prompts_used']} of {cfg.budget}, wake-ups {data['wakeups']}."
    return text + (f" Report: {path}" if path else "")


def write(data: dict, cfg) -> Path:
    folder = cfg.state_dir / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_-]", "_", str(data.get("session_id") or "session"))
    path = folder / f"{name}.md"
    path.write_text(render(data, cfg))
    return path
