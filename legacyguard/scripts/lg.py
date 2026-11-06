#!/usr/bin/env python3
"""Developer commands behind the /legacyguard:* skills. Prints plain text for the developer."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, PLUGIN_ROOT)
from guardlib import bootstrap  # noqa: E402


def _cfg():
    from guardlib import config as cfgmod
    from guardlib import projects
    try:
        cfg, note = projects.for_command(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    except cfgmod.ConfigMissing:
        sys.exit("LegacyGuard is not set up here: this folder is not a Python git project, and no project inside it is guarded.")
    except cfgmod.ConfigError as exc:
        sys.exit(f"LegacyGuard config is invalid: {exc}")
    if note:
        print(note + "\n")
    return cfg


def cmd_index(args) -> int:
    from indexer.__main__ import refresh
    r = refresh(_cfg(), with_tests=not args.no_tests)
    print(f"Indexed: {r['names']['files']} files, {r['names']['parsed']} parsed, {r['names']['removed']} removed.")
    if "routes" in r:
        print(f"Routes: {r['routes']['routes']} from {r['routes']['source']}.")
    print(f"Contract items: {r['contracts']}.")
    if "history" in r:
        print(f"Co-change pairs: {r['history']['pairs']}.")
    if "tests" in r:
        print(f"Test map: {r['tests']['rows']} test-function pairs; suite status: {r['tests']['status']}.")
    return 0


def cmd_impact(args) -> int:
    from guardlib import impact
    from indexer import db
    cfg = _cfg()
    conn = db.connect(cfg.root)
    print(impact.impact_text(conn, cfg, args.symbol, args.depth))
    if args.tree:
        imp = impact.compute(conn, cfg, args.symbol, args.depth)
        if imp and imp.callers:
            print("\n" + impact.render_tree(imp))
    return 0


def main() -> int:
    bootstrap.reexec_in_venv(PLUGIN_ROOT, ensure=True)
    if "--skill" in sys.argv:  # a skill shows stdout; a non-zero exit would replace our message with a shell error
        sys.argv.remove("--skill")
        try:
            return _run()
        except SystemExit as exc:
            if isinstance(exc.code, str):
                print(exc.code)
            return 0
    return _run()


def _run() -> int:
    ap = argparse.ArgumentParser(prog="legacyguard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index", help="build or refresh the index")
    p.add_argument("--no-tests", action="store_true")
    p.set_defaults(fn=cmd_index)
    p = sub.add_parser("impact", help="who depends on a function")
    p.add_argument("symbol")
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--tree", action="store_true", help="also print callers as a tree")
    p.set_defaults(fn=cmd_impact)
    try:
        import lg_more
        lg_more.register(sub)
    except ImportError:
        pass
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
