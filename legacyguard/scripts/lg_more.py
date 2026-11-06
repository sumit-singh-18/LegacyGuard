"""Developer controls: waive, pin, mode, status (FR-19, FR-22 to FR-25)."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def _cfg():
    from guardlib import config as cfgmod
    from guardlib.paths import find_root
    root = find_root(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    try:
        return cfgmod.load_config(root)
    except cfgmod.ConfigMissing:
        sys.exit("LegacyGuard is not set up here: no .legacyguard/config.yaml.")
    except cfgmod.ConfigError as exc:
        sys.exit(f"LegacyGuard config is invalid: {exc}")


def cmd_status(args) -> int:
    from guardlib import ledger, report
    cfg = _cfg()
    print(report.render(ledger.load(cfg.state_dir), cfg))
    return 0


def cmd_waive(args) -> int:
    from guardlib import ledger, waivers
    cfg = _cfg()
    reason = (args.reason or "").strip()
    if not reason:
        sys.exit('A waiver needs a reason: /legacyguard:waive <id> "<reason>"')
    with ledger.locked(cfg.state_dir) as data:
        f = ledger.find(data, args.id)
        if f is None:
            sys.exit(f"{args.id}: no such finding in this session. /legacyguard:status lists them.")
        if f["kind"] not in ("behavior_difference", "contract_break"):
            sys.exit(f"{f['id']} is {f['status']}; only behavior differences and contract breaks can be waived.")
        waivers.add(cfg, f, reason)
        f["state"] = "waived"
    print(f"Waived {f['id']} ({f['subject']}). It stays quiet until this code changes. Recorded in .legacyguard/waivers.json.")
    return 0


def cmd_mode(args) -> int:
    from guardlib import config as cfgmod
    cfg = _cfg()
    if args.mode not in cfgmod.MODES:
        sys.exit(f"Mode must be one of {', '.join(cfgmod.MODES)}.")
    path = cfgmod.config_path(cfg.root)
    text = path.read_text()
    new, n = re.subn(r"^mode:.*$", f"mode: {args.mode}", text, flags=re.M)
    path.write_text(new if n else f"mode: {args.mode}\n{text}")
    print(f"Mode is now {args.mode}. It applies to the next edit.")
    return 0


def _literal(enc) -> str | None:
    """Python source for a tagged value, or None if it has no simple literal form."""
    t, v = enc.get("t"), enc.get("v")
    if t == "v":
        return repr(v)
    if t == "bigint":
        return v
    if t == "float":
        return f'float("{v}")' if v in ("nan", "inf", "-inf") else v
    if t == "Decimal":
        return f'Decimal("{v}")'
    if t == "date":
        return f'date.fromisoformat("{v}")'
    if t == "datetime":
        return f'datetime.fromisoformat("{v}")'
    if t in ("list", "tuple", "set"):
        items = [_literal(x) for x in v]
        if any(i is None for i in items):
            return None
        joined = ", ".join(items)
        return {"list": f"[{joined}]", "tuple": f"({joined}{',' if len(items) == 1 else ''})", "set": f"{{{joined}}}" if items else "set()"}[t]
    if t == "dict":
        pairs = [(_literal(k), _literal(x)) for k, x in v]
        if any(a is None or b is None for a, b in pairs):
            return None
        return "{" + ", ".join(f"{a}: {b}" for a, b in pairs) + "}"
    return None


def build_pinned_test(finding: dict, cfg) -> str:
    from runner import diff as differ
    ce = finding["counterexamples"][0]
    module, _ = differ.module_for(finding["file"], cfg.root)
    qual = finding["item"]
    parts = qual.split(".")
    pos, kw = [], []
    for a in ce["args"]:
        if a["name"] == "self":
            raise ValueError("the counterexample needs an object recorded from a test, which has no literal form")
        lit = _literal(a["value"])
        if lit is None:
            raise ValueError(f"argument {a['name']} has no literal form")
        if a.get("kind") == "kw":
            kw.append(f"{a['name']}={lit}")
        elif a.get("kind") == "varargs":
            pos.append("*" + lit)
        elif a.get("kind") == "varkw":
            kw.append("**" + lit)
        else:
            pos.append(lit)
    target = parts[0] if len(parts) == 1 else f"{parts[0]}().{parts[1]}"
    call = f"{target}({', '.join(pos + kw)})"
    before = ce["before_raw"]
    if before["k"] == "return":
        expected = _literal(before["v"])
        if expected is None:
            raise ValueError("the expected result has no literal form")
        body = f"    assert {call} == {expected}\n"
    else:
        body = (f"    with pytest.raises(Exception) as caught:\n        {call}\n"
                f"    assert type(caught.value).__name__ == {before['type']!r}\n    assert str(caught.value) == {before['message']!r}\n")
    safe = finding["id"].replace("-", "_")
    return (f'"""Pinned by /legacyguard:pin {finding["id"]}: {ce["call"]} returned {ce["before"]} at the session baseline, and an edit changed it to {ce["after"]}."""\n'
            "import sys\nfrom datetime import date, datetime\nfrom decimal import Decimal\nfrom pathlib import Path\n\nimport pytest\n\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[2]))\n"
            f"from {module} import {parts[0]}  # noqa: E402\n\n\ndef test_pinned_{safe}():\n{body}")


def cmd_pin(args) -> int:
    """The only guard write outside .legacyguard/, and only on this explicit developer command (D-028)."""
    from guardlib import ledger
    cfg = _cfg()
    data = ledger.load(cfg.state_dir)
    f = ledger.find(data, args.id)
    if f is None:
        sys.exit(f"{args.id}: no such finding in this session.")
    if f["kind"] != "behavior_difference" or not f.get("counterexamples"):
        sys.exit(f"{f['id']} has no counterexample to pin.")
    try:
        source = build_pinned_test(f, cfg)
    except ValueError as exc:
        sys.exit(f"{f['id']} cannot be pinned: {exc}.")
    out = Path(cfg.root) / "tests" / "legacyguard" / f"test_pinned_{f['id'].replace('-', '_')}.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(source)
    print(f"Wrote {out.relative_to(cfg.root)}. It replays {f['counterexamples'][0]['call']} and expects {f['counterexamples'][0]['before']}.")
    return 0


def cmd_backtest(args) -> int:
    """No Claude involved: replays real commits and planted bugs through the guard's checks (FR-28)."""
    import json
    from guardlib import backtest
    from indexer.coverage_map import build, load_harvest
    from indexer import db
    cfg = _cfg()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.harvest:
        os.environ["LG_HARVEST_MAX"] = "10"
        from indexer.coverage_map import run_suite
        code, tail, _ = run_suite(cfg, timeout=args.harvest_timeout, extra_args=args.harvest)
        print(f"harvest: tests exit {code}; {tail}")
    harvest = load_harvest(cfg)
    print(f"harvest has {len(harvest)} functions, {sum(len(v['inputs']) for v in harvest.values())} calls")
    if args.bugs:
        r = backtest.planted(cfg, Path(args.bugs), harvest, out / "planted.json")
        print(json.dumps({k: v for k, v in r.items() if k != "bugs"}))
    if args.commits:
        r = backtest.replay(cfg, args.commits, tuple(args.prefix), harvest, out / "replay.json")
        print(json.dumps(r["summary"], indent=1))
    return 0


def cmd_map(args) -> int:
    from guardlib import ledger, sessionmap
    cfg = _cfg()
    path = sessionmap.write(cfg, ledger.load(cfg.state_dir))
    print(f"Wrote {path.relative_to(cfg.root)}. Open it in a browser: {path}")
    if args.open:
        import webbrowser
        webbrowser.open(path.as_uri())
    return 0


def register(sub) -> None:
    p = sub.add_parser("map", help="write this session's blast radius map")
    p.add_argument("--open", action="store_true", help="also open it in the default browser")
    p.set_defaults(fn=cmd_map)
    p = sub.add_parser("backtest", help="replay real commits and planted bugs (no Claude)")
    p.add_argument("--commits", type=int, default=0)
    p.add_argument("--prefix", nargs="*", default=["src/", "tests/"])
    p.add_argument("--bugs")
    p.add_argument("--out", default="backtest-results")
    p.add_argument("--harvest", nargs="*")
    p.add_argument("--harvest-timeout", type=int, default=1500)
    p.set_defaults(fn=cmd_backtest)
    p = sub.add_parser("status", help="findings, mode and prompts used")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("waive", help="accept a finding, with a reason")
    p.add_argument("id")
    p.add_argument("reason", nargs="?")
    p.set_defaults(fn=cmd_waive)
    p = sub.add_parser("mode", help="advisory, guarded or strict")
    p.add_argument("mode")
    p.set_defaults(fn=cmd_mode)
    p = sub.add_parser("pin", help="write a regression test from a counterexample")
    p.add_argument("id")
    p.set_defaults(fn=cmd_pin)
