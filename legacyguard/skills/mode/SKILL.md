---
name: mode
description: Developer sets LegacyGuard's mode to advisory, guarded or strict.
argument-hint: <advisory|guarded|strict>
arguments: mode
disable-model-invocation: true
allowed-tools: Bash(python3 *lg.py*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lg.py" --skill mode "$mode" 2>&1`

Show the output above to the developer as it is.
