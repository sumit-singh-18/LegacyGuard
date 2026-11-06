"""Build or refresh the index. `python -m indexer <root> [--with-tests]`."""
from __future__ import annotations

import json
import subprocess
import sys

from guardlib import baseline
from guardlib import config as cfgmod
from indexer import contracts, coverage_map, db, history, names, routes


def refresh(cfg, with_tests: bool = False) -> dict:
    """Incremental refresh of names, routes and contracts; history when HEAD moved; tests only on request."""
    baseline.ensure_state_dir(cfg)
    conn = db.connect(cfg.root)
    try:
        report: dict = {"names": names.update(conn, cfg.root, cfg.scope)}
        report["contracts"] = contracts.rebuild(conn, cfg)
        if report["names"]["parsed"] or not conn.execute("SELECT 1 FROM routes LIMIT 1").fetchone():
            report["routes"] = routes.rebuild(conn, cfg)
        head = history.head(cfg.root)
        if head != db.get_meta(conn, "head"):
            report["history"] = history.rebuild(conn, cfg.root)
            db.set_meta(conn, "head", head)
        if with_tests:
            report["tests"] = coverage_map.build(conn, cfg)
        conn.commit()
        return report
    finally:
        conn.close()


def main() -> int:
    root = sys.argv[1]
    print(json.dumps(refresh(cfgmod.load_config(root), "--with-tests" in sys.argv), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
