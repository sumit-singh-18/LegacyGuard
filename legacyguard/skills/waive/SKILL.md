---
name: waive
description: Developer accepts a LegacyGuard finding, with a reason recorded in waivers.json.
argument-hint: <id> "<reason>"
arguments: id reason
disable-model-invocation: true
allowed-tools: Bash(python3 *lg.py*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/lg.py" --skill waive "$id" "$reason" 2>&1`

Show the output above to the developer as it is.
