---
name: pin
description: Developer turns a LegacyGuard counterexample into a regression test under tests/legacyguard/.
argument-hint: <id>
arguments: id
disable-model-invocation: true
allowed-tools: Bash(python3 *lg.py*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lg.py" --skill pin "$id" 2>&1`

Show the output above to the developer as it is.
