---
name: impact
description: Show who depends on a function - callers, routes, tests and co-changed files, each with its source.
argument-hint: <symbol>
arguments: symbol
allowed-tools: Bash(python3 *lg.py*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lg.py" --skill impact "$symbol" --tree 2>&1`

Show the output above to the developer as it is.
