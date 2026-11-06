#!/usr/bin/env python3
"""Start the MCP server under the guard's own venv."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from guardlib import bootstrap  # noqa: E402

bootstrap.reexec_in_venv(ROOT, ensure=True)
from mcp_server.server import main  # noqa: E402

main()
