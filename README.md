# LegacyGuard

A guardrail for Claude Code on Python backends.

When Claude changes a function, LegacyGuard runs the old version and the new version on the same inputs. If the results differ, it shows Claude the exact input, and Claude goes back and fixes the code. When Claude finishes, you get a short report and a map of what changed.

It runs by itself in the background. You use Claude Code the way you always do.

## What you need

- [Claude Code](https://claude.com/claude-code), signed in with a Pro or Max plan. No API key.
- git
- Python 3.10 or newer, anywhere on the machine. The guard finds it by itself.

## 1. Install (once per computer)

Copy these two lines into your terminal, one at a time:

```bash
claude plugin marketplace add turnback-lab/legacy-guard
```

```bash
claude plugin install legacyguard@turnback-lab
```

Check that it worked:

```bash
claude plugin list
```

You should see `legacyguard@turnback-lab` with status `enabled`.

## 2. Open a Python project

Use your own project, or clone one:

```bash
git clone https://github.com/mahmoud/boltons.git
```

```bash
cd boltons
```

The project must be a git repository with Python code in it.

For the fullest checks, install the project's own libraries in a `.venv` folder inside it, as you normally would. Without them, code that needs those libraries is reported as "not checked". The example project above needs no libraries.

## 3. Start Claude

```bash
claude
```

The first start in a project takes about a minute. You will see one line:

> LegacyGuard set itself up for this project and is watching ./

That line means the guard is on. You do not create any settings file or run any other command.

If it adds that no `.venv` folder was found, that is fine for the example project. For your own project, see step 2.

## 4. Ask for a change

Type your request as usual. To see the guard catch something, try this in the example project:

```
In boltons/strutils.py, apply this exact edit with the Edit tool before anything else: in parse_int_list replace `max(range_limits)+1` with `max(range_limits)`. Then tell me the result.
```

## 5. What you will see

| You see | It means |
| --- | --- |
| Nothing special | The guard checked the edit and found no difference, or the edit did not touch a function |
| `LegacyGuard behavior difference [LG-1] …` followed by a "before" and "after" example | The edit changed what a function returns. Claude gets the example and revises its code |
| A permission prompt labelled LegacyGuard | The edit would break an API field, a database column or a test. You decide. At most three per session |
| An edit refused | The file is on your "never edit" list |

For the example request above you should see a line like:

> parse_int_list(range_string='5-8')  before [5, 6, 7, 8]  after [5, 6, 7]

## 6. Read the result

Inside Claude:

```
/legacyguard:status
```

The report: what you asked, what broke, what fixed it, and what was not checked.

```
/legacyguard:map
```

A map of what changed and what depends on it. Open the file it names in a browser.

Both are also saved in your project under `.legacyguard/reports/`.

## Good to know

**Several projects in one folder.** If you start Claude in a folder that only holds projects, the guard starts in each project inside it (up to five) and tells you which.

**Your git status stays clean.** Everything the guard writes goes in `.legacyguard/`, which git ignores. To share the settings with your team: `git add -f .legacyguard/config.yaml`.

**Protect a file.** Add it to `frozen:` in `.legacyguard/config.yaml`, for example `frozen: [legacy/ledger_v1.py]`. Claude can no longer edit it.

**Accept a change the guard flagged.** `/legacyguard:waive LG-1 "reason"`

**Turn a caught break into a permanent test.** `/legacyguard:pin LG-1`

**Switch the guard off in one project.** Add `enabled: false` to `.legacyguard/config.yaml`.

## Update

```bash
claude plugin marketplace update turnback-lab
```

```bash
claude plugin update legacyguard@turnback-lab
```

Then restart Claude.

## Remove

```bash
claude plugin uninstall legacyguard@turnback-lab
```

```bash
claude plugin marketplace remove turnback-lab
```

## What it does not do

- **Python only.** It cannot check front-end or other languages.
- **"Checked" is not a proof.** It means old and new code gave the same results on the inputs tried, and at least one input ran the changed lines.
- **Some code cannot be run.** Functions that need a database, the network or a logged-in user are reported as "not checked", with the reason.
- **It compares results, not side effects.** Writes to a database or a file are not compared.
- **Claude can out-test it on small functions.** In a session where Claude is allowed to run code, Claude's own comparison can cover more cases. The guard's value is that it always runs, does not depend on Claude deciding to check, and works where Claude cannot run code.

## If something goes wrong

| Message | What to do |
| --- | --- |
| `Unknown command: /legacyguard:status` | The guard is not installed, or Claude was not restarted after installing. Run `claude plugin list` |
| `Cannot add marketplace …: its source doesn't match…` | A copy from another place is installed. Run the two "Remove" commands, then the two "Install" commands |
| You installed before this project moved to turnback-lab | Run `claude plugin uninstall legacyguard@legacyguard` and `claude plugin marketplace remove legacyguard`, then the two "Install" commands |
| `LegacyGuard needs Python 3.10 or newer…` | Install Python 3.12 from python.org, then start Claude again |
| `LegacyGuard could not install its dependencies…` | Check your internet connection and start Claude again |
| "not set up here" from `/legacyguard:status` | The folder is not a git repository with Python code in it (at least three Python files). Start Claude inside your project, or run `git init` and commit your files first |

## Prefer a zip?

On this repository's GitHub page choose Code, then Download ZIP, and unzip it. Open a terminal, type `sh ` with a space after it, drag `install.sh` from the unzipped folder into the window and press Enter. It ends with "LegacyGuard is installed".

## Team

Built by turnback-lab for the University of Southern Mississippi Hatchathon 2026 (Legacy Tech track).

## Licence

MIT. See [LICENSE](LICENSE).
