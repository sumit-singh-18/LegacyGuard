---
name: index
description: Build or refresh LegacyGuard's index of this repo, including running the test suite once to map tests to functions.
disable-model-invocation: true
allowed-tools: Bash(python3 *lg.py*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lg.py" --skill index 2>&1`

Show the output above to the developer as it is.
