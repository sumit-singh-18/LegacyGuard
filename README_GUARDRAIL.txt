LegacyGuard for Claude Code
===========================

What it does
  While Claude Code edits a Python backend, LegacyGuard runs the old and the new version of each changed
  function on the same inputs. If the results differ, it shows Claude the exact input and Claude goes back
  and fixes the code. When Claude stops, you get a short report and a map of what changed.

What you need
  - Claude Code, signed in with a Pro or Max plan. No API key.
  - git, and Python 3.10 or newer somewhere on the machine (the guard finds it by itself).

Install (once)
  1. Unzip this folder anywhere.
  2. Open Terminal, type "sh " (with the space), drag install.sh into the window, press Enter.

Use
  1. Open Terminal in your Python project (it must be a git repository).
  2. Type: claude
  3. Ask for a change as you normally would.

  The first start in a project takes about a minute. The guard writes its own settings to
  .legacyguard/config.yaml and reads the codebase. You do not create any file or run any other command.

  For the fullest checks, the project's own libraries should be installed in a .venv folder inside the project.
  Without that, code that needs those libraries is reported "Untested".

(From GitHub, without a zip: see README.md for the two-line install.)

See what happened (type inside Claude)
  /legacyguard:status    the report: what you asked, what broke, what fixed it, what was not checked
  /legacyguard:map       the map: what changed and what depends on it

Switch it off in one project
  Add this line to .legacyguard/config.yaml in that project:   enabled: false

Update
  Unzip the new version and run install.sh again.

Remove
  claude plugin uninstall legacyguard@turnback-lab
