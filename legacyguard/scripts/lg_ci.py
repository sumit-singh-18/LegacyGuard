#!/usr/bin/env python3
"""LegacyGuard on the changes since a base commit, for CI and pull requests. See guardlib/ci.py.

python legacyguard/scripts/lg_ci.py [--base REF] [--python PATH] [--fail-on difference|any|none]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guardlib import ci  # noqa: E402

if __name__ == "__main__":
    sys.exit(ci.main())
