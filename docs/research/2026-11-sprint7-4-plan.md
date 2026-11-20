# Sprint 7.4 build spec: A2 throwaway working folder (cwd)

Researcher agent, 2026-11-20, main at 193f183. Revised after reviewer REVISE; the user's answers to the open
questions are folded in as decisions. Self-contained: the implementer should not need the first plan.
**Round-2 corrections (reviewer, 2026-11-20) are folded in and override earlier text:** the launcher drops only
`''`/`.`; no uncapped copy fallback (mount points pruned, per-file copy capped at `MAX_COPY_BYTES`, more prune rules);
Part B split out; one working folder per tree per check across the diff and the affected tests; two more limits notes.
Spec origin: `docs/research/2026-11-sprint7.md` (7.4 row, "A2 strategies") and `docs/ROADMAP.md` 3d
("black-style import with absolute-path write is handled"; a cwd change alone does not stop an absolute-path write).

## 0. Summary

Every process that runs project code for a check today has the **real tree** as its working folder: cold worker,
warm server and its children, and the affected-tests pytest run. 7.4 gives each of them a throwaway working
folder in the system temp folder: a copy-on-write clone of the tree where the file system allows it, a plain copy
for small trees, otherwise the real tree read-only under the OS sandbox (`off`) or an empty folder when there is no
OS sandbox. Phase 1 is **safety only**: imports still come from the real tree (absolute `sys_path` entries), and
in-process statuses do not change, because the working folder is added to `SCRATCH["protect"]` and writes there
during a call stay refused exactly as writes into the tree are today. The chosen strategy is shown in the session
report next to the OS sandbox label. A cwd change does nothing for absolute-path writes: those are stopped by 6.8a's
in-process redirect/refusal and by the OS sandbox; without an OS sandbox the report says so.

## 1. Decisions (user, final)

| # | Decision |
|---|---|
| Q1 | `auto` on a tree too large to clone or copy: **`off`** (the real tree, read-only) when an OS sandbox is active; **`empty`** when there is none. |
| Q2 | Writes into the working folder during a call stay **refused**: the folder goes in `SCRATCH["protect"]` (cold and warm). |
| Q3 | Clone cap **20,000 files**. Cost recorded below (about 2.6 s per tree at 20k on this Mac). Part A clones **both trees per check**; reusing the baseline's clone for the session (Part B) is split out as the first 7.4 follow-up (section 9). |
| Q4 | The empty-mode relative-read rule (mark a relative read in an empty cwd as untested) is a **later follow-up**. |

## 2. Sources and confirmed claims

Every claim was read in the source given. "Inference" marks my own reasoning.

### bubblewrap overlay (not built in 7.4; see section 9)
- `--overlay-src SRC` "does nothing on its own" and must be followed by `--overlay`, `--tmp-overlay` or
  `--ro-overlay`; with `--tmp-overlay` writes go to the tmpfs hosting the sandbox root, "not accessible from either
  the host or the child process", and "not persisted across multiple runs"; no `--overlay-src`/`--overlay` host
  directory may be an ancestor of another. — [bwrap.xml](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)
- Added in **0.11.0 (2024-10-30)**; "not available when bubblewrap is installed setuid". —
  [v0.11.0 release](https://github.com/containers/bubblewrap/releases/tag/v0.11.0),
  [NEWS.md](https://raw.githubusercontent.com/containers/bubblewrap/main/NEWS.md). The v0.11.0 source dies with
  "The --tmp-overlay option is not permitted in setuid mode" (and the same for the other three) —
  [bubblewrap.c @ v0.11.0](https://raw.githubusercontent.com/containers/bubblewrap/v0.11.0/bubblewrap.c) lines 2028–2080.
- **0.12.0 (2026-08-26) removed setuid support entirely**; main dies with "setuid use of bubblewrap is not
  supported". — NEWS.md; [bubblewrap.c main](https://raw.githubusercontent.com/containers/bubblewrap/main/bubblewrap.c) line 803.
  Releases since: 0.11.1 (2026-03-21), 0.11.2 (2026-04-23), 0.12.0, 0.13.0 (2026-09-22), 0.13.1 (2026-10-14)
  (GitHub releases API, `api.github.com/repos/containers/bubblewrap/releases`).
- Unprivileged overlayfs in a user namespace needs Linux **5.11** ("Allow unprivileged mounting in a user
  namespace"). — [overlayfs update for 5.11 (LKML)](https://lkml.iu.edu/2012.2/01416.html)
- Ubuntu packages: 22.04 has 0.6.1, **24.04 (noble, today's `ubuntu-latest`) has 0.9.0**, 26.04 has 0.11.1. —
  [packages.ubuntu.com](https://packages.ubuntu.com/search?keywords=bubblewrap&searchon=names&exact=1&suite=all&section=all)

### macOS clonefile and `cp -c`
- `cp -c`: "copy files using clonefile(2)". If source and target are on different file systems or the target does
  not support cloning, cp "will fallback to using copyfile(2)". —
  [Apple file_cmds cp.1](https://raw.githubusercontent.com/apple-oss-distributions/file_cmds/main/cp/cp.1)
  (the local `man cp` on macOS 27.2.1 says the same).
- The fallback is **silent**: on EXDEV or ENOTSUP the code goes on to the copy path with no warning; only other
  errors warn. — [Apple file_cmds cp/utils.c](https://raw.githubusercontent.com/apple-oss-distributions/file_cmds/main/cp/utils.c) lines 285–294.
  So `cp -c` cannot tell us whether a clone happened. **Use `clonefile(2)` directly.**
- `clonefile(2)`: **EXDEV** "src and dst are not on the same filesystem"; **ENOTSUP** "The underlying filesystem
  does not support this call"; `CLONE_NOFOLLOW` clones a symlink itself; cloning a whole directory hierarchy "is
  strongly discouraged". — local `man 2 clonefile`, macOS 27.2.1 (no Apple-hosted online copy found).
- APFS / same-volume detection: Python has no `statfs`. Not needed: a probe `clonefile` of one real file from the
  tree into the temp folder answers both questions (EXDEV = other volume, ENOTSUP = no cloning) (inference).
  `st_dev` equality is a cheap pre-check. On this Mac the repo, `/private/tmp` and `tempfile.gettempdir()`
  (`/var/folders/...`) share one `st_dev`; `mount` shows `/System/Volumes/Data` as APFS; a clone onto devfs
  returned EXDEV (local observation).

### Linux reflink
- `cp --reflink=always`: "If the copy-on-write operation is not supported then report the failure for each file and
  exit with a failure status"; `auto` (the default) falls back to a normal copy. —
  [GNU coreutils: cp invocation](https://www.gnu.org/software/coreutils/manual/html_node/cp-invocation.html)
- FICLONE ioctl: **EXDEV** "not on the same mounted filesystem", **EOPNOTSUPP** "filesystem does not support
  reflinking", also EINVAL, EISDIR; Linux 4.5+. — [ioctl_ficlone(2)](https://man7.org/linux/man-pages/man2/ioctl_ficlone.2.html)
- `fcntl.FICLONE` exists only from **Python 3.12**. — [Python fcntl docs](https://docs.python.org/3/library/fcntl.html).
  Kernel: `#define FICLONE _IOW(0x94, 9, int)` = 0x40049409 on the generic ioctl encoding (x86_64, aarch64) —
  [include/uapi/linux/fs.h](https://raw.githubusercontent.com/torvalds/linux/master/include/uapi/linux/fs.h) line 319.
  Other architectures (powerpc, mips, sparc, alpha) encode `_IOW` differently (inference): treat clone as unsupported there.

### Python and pytest
- `-c` and `-m` add the current directory to the start of `sys.path`; `-P` / `PYTHONSAFEPATH` were added in 3.11. —
  [Python command line](https://docs.python.org/3/using/cmdline.html). Locally, `sys.path[0]` is `''` under `-c`
  on 3.10 and current Python.
- Without `-c`, pytest takes rootdir/configfile from the common ancestor of the path args (the cwd only when no
  arg is a path); with no config file, the common ancestor becomes rootdir; `--rootdir=path` forces it; nodeids are
  rooted at rootdir; rootdir does not change `sys.path`. — [pytest: Configuration](https://docs.pytest.org/en/stable/reference/customize.html)

### macOS sandbox rule order
- No public Apple documentation for SBPL. Evidence is local: `legacyguard/runner/os_sandbox.py:42` relies on "later
  rules win", and `tests/test_os_sandbox.py:18-25` (a project inside the temp folder stays read-only although the
  temp folder is allowed) passed on 2026-11-20 (4 passed).

### Not confirmed
- Whether an unprivileged bwrap overlay works on GitHub's Ubuntu runners (kernel, AppArmor user-namespace rules),
  and whether the existing bwrap tests run or skip on CI.
- The exact message GNU cp prints on EXDEV; whether tmpfs-to-tmpfs FICLONE gives EOPNOTSUPP or EINVAL (treat any
  OSError from the probe as "no clone").
- That `--rootdir` leaves configfile discovery from the args unchanged (the pytest page implies it; not stated).
- GitHub Linux runners' `/tmp` file system (assumed ext4, so no reflink: CI will pick `copy`).

## 3. Measurements (this Mac, APFS, macOS 27.2.1, best of 3)

Script: `/private/tmp/claude-501/-Users-sumit-Documents-LegacyGuard/e158a863-c5e6-4266-9845-b6d0ff2f4e97/scratchpad/clone_time_n.py`
(`N=<files> python3 clone_time_n.py <parent dir>`). Files of about 0.3–1.5 KB in 40×7 folders.

| Files | clonefile per file | shutil.copy2 per file | `cp -c -R` | clonefile of the directory | os.walk count |
|---|---|---|---|---|---|
| 2,000 | 0.23–0.25 s | 0.39–0.40 s | 0.32–0.34 s | 0.016–0.018 s | 0.008 s |
| 20,000 | **2.56 s** | 3.73 s | 3.03 s | 0.24 s | 0.017 s |

Cost per check at the 20k cap: about 2.6 s per tree, **about 5 s per check if both trees are cloned**, against run
4's median check time of about 1.7 s (reviewer's figure). Part B (split out, section 9) would make the per-check
cost one tree: about 0.25 s at 2k files, 2.6 s at 20k. Directory clonefile is 10× faster but the man
page discourages it and it cannot leave out heavy folders; not used. Copy at the 2k / 50 MB copy limit: about 0.4 s.
The implementer re-measures on the 2k-file test fixture and records both numbers (clone where available, copy) in
`docs/PROGRESS.md`. Measured at build time (2,000 files, 1.3 MB, three runs): clone 0.27 s, copy 0.42 to 0.43 s per
tree.

## 4. Current code (file:line at 193f183)

| Where | What it does today |
|---|---|
| `legacyguard/runner/diff.py:267-298` `_run_worker` | Builds the request (272-276), wraps with `os_sandbox.wrap(..., writable=[tempdir, tmp], readonly=[cfg.root, root])` (284-285), `subprocess.run(cmd, cwd=root)` (287). Called from 1150 (carve), 1403-1405 (second pass), 1558-1560 (main three runs), 1665/1668 (slowdowns). `sys_path` entries are absolute (`paths_for`, 1531-1533), so the cwd does not affect imports. |
| `legacyguard/runner/lg_worker.py:1810-1834` `prepare` | `os.chdir(root)` at **1829**; `SCRATCH["protect"] = [root_real, *protect]` at **1833** (`realpath + os.sep` form). |
| `lg_worker.py:201-218` `_is_scratch` | Writable = under the system temp folder and not under any `SCRATCH["protect"]` entry. A cwd in temp **not** in protect would be writable to both in-process layers. |
| `lg_worker.py:98-123`, `145-174`, `285-308`, `311-381` | Layer "patches" (`GuardedOpen`, os redirects) and the PEP 578 audit hook; both return early when `GUARD_WRITES["allowed"]`. 6.8a redirect of cache-like paths during setup (`_redirect`, `_cache_like` 258-266, judged below the longest protect prefix). |
| `legacyguard/runner/lg_server.py:52-91` `_run` | Fork child (60-79): `use_scratch`, pids, Django, `execute`. Never chdirs: inherits the server's cwd (the tree). Server `prepare` at 106 sets protect once from the init request. |
| `legacyguard/runner/warm.py:107-111` | Starts the server sandboxed with `writable=[tempdir]` (fixed for its life), `Popen(cwd=tree_root)`. |
| `legacyguard/runner/affected_tests.py:82-100` `run` | `python -B -m pytest ... <relative nodeids>`, `cwd=tree_root` (97), sandbox lists as above. Results keyed by `to_nodeid(tree_root, classname)` (29-37, 55-79). |
| `legacyguard/runner/os_sandbox.py:32-43` | macOS: `(deny file-write*)`, allow writable (36), **deny readonly later (42); later rule wins**. Linux 46-53: `--ro-bind / /`, `--bind` writable, then `--ro-bind` readonly (wins). The whole temp folder is writable. `describe()` 94-96 is shown in no report today (only `tools/validate.py:919` and tests). |
| `legacyguard/guardlib/config.py:16-17` | `behavioral_diff` defaults. |
| `legacyguard/scripts/post_edit.py:35-45` | `differ.run`, then ledger lock (38), then `engine.affected_tests`. |
| `legacyguard/guardlib/report.py:145-149` | Report header lines. |
| `legacyguard/guardlib/baseline.py:82-105` | Baseline = `git worktree add --detach HEAD` + dirty files copied in: **it has no git-ignored files**. |
| Out of scope, also tree cwd | `legacyguard/indexer/routes.py:77`, `legacyguard/indexer/coverage_map.py:53`. |

**The working folder must be outside `cfg.root` and the tree.** Both are in `readonly`; on macOS the later deny wins
and on Linux the later `--ro-bind` wins, so a folder under `cfg.root` (for example `.legacyguard/cwd/`, where the
baseline also lives) would be read-only. Put it under `tempfile.gettempdir()` and check by realpath prefix that it is
inside neither; if the temp folder itself is inside the project, use `off` with that reason.

## 5. Build plan

### Part A (ships in 7.4)

#### New `legacyguard/runner/scratch.py` (guard side; stdlib, ctypes, fcntl)

Constants: `MAX_CLONE_FILES = 20_000`; `MAX_COPY_FILES = 2_000`; `MAX_COPY_BYTES = 50 * 2**20`;
`PRUNE_DIRS = {".git", ".hg", ".svn", ".legacyguard", "node_modules", "__pycache__", ".tox", ".nox", ".mypy_cache",
".pytest_cache", ".ruff_cache", ".hypothesis", ".eggs", "htmlcov"}`, plus any `*.egg-info`; `PRUNE_TOP = {"build",
"dist", "_build"}` at the tree's top level only (review round 1: `pkg/build/` is source); a `.git` pointer file is
left out; `LIST_VERSION = 4` (bump when the listing rule changes). Review round 1 also: the request carries
`cwd_root` (the folder root) so a `cwd: <subdir>` setting protects the whole folder; the hook pid in folder names is
base 36 (`lg-cwd-<pid base 36>-*`).

`Cwd` dataclass: `path`, `strategy` (`clone|copy|empty|off`), `label`, `seconds`, `files`, `nbytes`, `owned` (delete
on release), `tree`, `folder` (what release deletes; `path` may be `folder/<subdir>`).

- `_list_tree(tree, limit) -> (files, nbytes, over)`: **one rule for both trees: walk the tree itself**, not
  `git ls-files`. Prune `PRUNE_DIRS` and `*.egg-info`, any folder containing `pyvenv.cfg` (a virtualenv) or
  `conda-meta` (a conda environment), any sub-folder containing a `.git` entry (nested worktree, submodule,
  `.claude/worktrees/...`), and **any sub-folder whose `st_dev` differs from the tree's** (a mount point; test seam
  `_dev(path)`). Keep regular files and
  symlinks (never follow folder symlinks); skip sockets/fifos. Stop counting once over `limit` (`over=True`).
  Consequence, stated in the docstring and CLAUDE.md:
  - **Working tree**: tracked, untracked and **git-ignored** files are all there (`.env`, `settings_local.py`, data
    files, in-place builds), so relative reads behave as today.
  - **Baseline tree**: it never had ignored files (`baseline.py` builds it from HEAD plus dirty files), so relative
    reads of ignored files fail on the old side **as they do today**. 7.4 neither fixes nor worsens this asymmetry.
- `_clone_one(src, dst)` (the test seam):
  - darwin: ctypes `libc.clonefile(src, dst, CLONE_NOFOLLOW=0x0001)`; on -1 raise `OSError(get_errno(), ...)`.
  - linux: symlinks recreated with `os.symlink(os.readlink(src), dst)`; files with
    `fcntl.ioctl(dst_fd, FICLONE, src_fd)` where `FICLONE = getattr(fcntl, "FICLONE", None)`, else `0x40049409`
    only when `platform.machine()` is `x86_64`/`aarch64`/`arm64`, else raise `OSError(errno.EOPNOTSUPP)`.
  - other platforms: raise `OSError(errno.ENOTSUP)`.
  - Never `cp -c` (silent fallback).
- `_copy_one(src, dst)`: `shutil.copy2(src, dst, follow_symlinks=False)`.
- `_probe(cfg, tree) -> (ok, reason)`: clone one listed file into a fresh temp folder. Any OSError is "no clone":
  EXDEV gives "the temp folder is on another file system", ENOTSUP/EOPNOTSUPP "no copy-on-write on this file system",
  others "clone failed (<errno name>)". **Only the probe result is cached** (section "Strategy cache"); file count and
  size are recomputed every check.
- `_location(cfg, tree)`: `tempfile.mkdtemp(prefix=f"lg-cwd-{os.getpid()}-", dir=tempfile.gettempdir())`; if its
  realpath is inside `cfg.root` or the tree, remove it and return None (→ `off`, "the temp folder is inside the project").
- `_sweep()`: on each `prepare_cwd`, delete `lg-cwd-<pid>-*` folders in the temp folder whose pid is no longer
  alive (`os.kill(pid, 0)` raises ProcessLookupError; a PermissionError, i.e. a pid of another user, is treated as
  alive, and the docstring says so). Covers a hook killed mid-check. (Warm servers never own a
  working folder: the hook makes it and passes it in the request, so killing a server leaks nothing new.)
- `prepare_cwd(cfg, tree, sandboxed: bool) -> Cwd`, by `behavioral_diff.cwd_mode`:
  - `off`: the tree; label "off (switched off in config)".
  - `empty`: fresh empty folder.
  - `clone` (explicit opt-in to the cost): clone with no file cap; if the probe fails, copy **within the copy
    limits**, else off/empty as below.
  - `copy`: copy within the copy limits, else off/empty as below.
  - `auto` (default):
    1. probe ok and files ≤ 20,000 → clone;
    2. else files ≤ 2,000 and bytes ≤ 50 MB → copy;
    3. else **`off` if `sandboxed`** ("off: N files, too many to copy; the real tree, read-only under <sandbox>"),
       **else `empty`** ("empty: ...; no OS sandbox, so relative reads of project files fail in this run").
  - Unknown mode → `auto`, label notes "unknown cwd_mode 'x', used auto".
  - **No uncapped copy fallback.** A clone that fails on one file after a good probe copies that file and counts the
    copied bytes; once they would pass `MAX_COPY_BYTES` it gives up, deletes the half-made folder and goes to `off`
    if an OS sandbox is active, else `empty`, with the reason in the label. A partial clone's label says
    "clone (clonefile) of N files, X MB, in s; M copied (Y KB) after the clone failed: <reason>".
  - Labels carry file and byte counts ("clone (clonefile) of 1,840 files, 2.1 MB, in 0.21 s").
  - `behavioral_diff.cwd: <subdir>`: the cwd is `<folder>/<subdir>` (`<tree>/<subdir>` for `off`); a missing
    subdir falls back to the folder root with a label note. In `empty` mode create the subdir.
  - No file list problem exists (the walk needs no git), so "not a git tree" is not a fallback any more.
- `release(cwd)`: `shutil.rmtree` when `owned`.
- `for_trees(cfg, baseline_root, work_root)`: re-entrant context manager; registers both trees in a module dict
  `{realpath(root): entry}`. Each entry prepares its folder **on first use**, under a lock, so OLD-A, OLD-B and NEW
  get one folder and a check that runs nothing clones nothing. A folder is released when the **outermost** block for
  its tree exits. `sandboxed` comes from `os_sandbox.active`.
- `current(cfg, root)`: context manager yielding the registered folder, or, **only when no registry entry exists**
  (direct callers, e.g. a test calling `affected_tests.run`), one prepared for that block and released after it.
- `describe() -> str`: labels of the folders prepared in the last block, e.g. "work: clone (clonefile) of 1,840
  files, 2.1 MB, in 0.21 s; baseline: clone (clonefile) of 1,838 files, 2.1 MB, in 0.20 s".

`sandboxed` comes from `os_sandbox`: add `os_sandbox.active(python, mode) -> bool` (= mode != "off" and
`available(python)`), so the strategy is known before `wrap` runs.

#### Strategy cache

`cfg.state_dir / "cwd_strategy.json"`: `{key: {"ok": bool, "reason": str}}`, key =
`realpath(tree) | st_dev(tree) | st_dev(tempdir) | sys.platform | cwd_mode`. Only the probe result is stored.
A changed device (remount, other temp folder) or mode re-probes. Written with an atomic replace; a read error means
"probe again". Guard-side write under `.legacyguard/` (git-ignored).

#### `legacyguard/runner/diff.py`
- `run` (1452): `with scratch.for_trees(cfg, baseline_root, work_root): return _run(...)` (the old body), so the
  carve, main, second-pass and slowdown phases share it; re-entrant, so an enclosing block's folders are reused.
- `_run_worker` (267-298): `with scratch.current(cfg, root) as cwd:`; `request["cwd"] = cwd.path`;
  `subprocess.run(cmd, cwd=cwd.path, ...)`; `writable=[tempdir, tmp, cwd.path]` (redundant with tempdir but explicit;
  for `off`, cwd is the tree and stays read-only because readonly is applied later).
- **One working folder per tree per check**, shared by OLD-A, OLD-B, carve, second pass and slowdowns. OLD-A and
  OLD-B must see the same path, or old-vs-old disagrees on anything derived from `os.getcwd()`.

#### Callers of `differ.run`: one folder per tree per check, never two clones (correction 4)
- `scripts/post_edit.py`: opens `scratch.for_trees` **once** around both `differ.run` and `engine.affected_tests`,
  so the diff's workers and both pytest runs share the two folders. The cwd goes into `protect` only for worker
  processes; pytest has no in-process guard, so a test's relative writes land in the folder, which is fine.
- `guardlib/audit.py` (Stop): one block around the whole `_behavior_findings` loop (one folder per tree per audit).
- `guardlib/ci.py::run` (and so `tools/validate.py`, which calls `ci.run`): one block around the file loop.
- `guardlib/backtest.py`: one `differ.run` per commit pair; `run`'s own block covers it.

#### `legacyguard/runner/lg_worker.py`
- `prepare` (1829-1833): `os.chdir(request.get("cwd") or root)`; append `os.path.realpath(cwd) + os.sep` to
  `SCRATCH["protect"]` (same form as the root entry). Effect: writes into the working folder are refused during calls,
  and cache-like writes there during setup are redirected, exactly as for the tree today.
- New helper `use_cwd(path)` doing the chdir and the protect append, used by `prepare` and by the warm child.

#### `legacyguard/runner/lg_server.py`
- In the child (`_run`, after `use_scratch` at 65): `if request.get("cwd"): W.use_cwd(request["cwd"])`. The
  protect entry must be in the `realpath + os.sep` form `_is_scratch` compares against; otherwise a relative write
  during a call would be allowed warm and refused cold.
- The server's own cwd stays the tree (`warm.py:110`); in baseline mode it imports project modules there. Known gap,
  documented, unchanged from today.

#### `legacyguard/runner/affected_tests.py` (`run`, 82-100)
- `with scratch.current(cfg, tree_root) as cwd:` the hook's registered folder; prepared on demand (and released
  after the run) only when no registry entry exists.
- Arguments: **absolute** paths (`str(Path(tree_root) / nodeid)`) plus an explicit **`--rootdir=<tree_root>`**.
  Results and `old_test_results` stay keyed by **relative** nodeids (`to_nodeid(tree_root, classname)` is
  unchanged), because the baseline and working trees have different absolute paths and the cache compares them.
  Why `--rootdir`: without an ini file the rootdir is the common ancestor of the args (for example `<tree>/tests`),
  the junit classnames lose their `tests.` prefix, `to_nodeid` returns None and results drop silently. With relative
  args and cwd = tree that already happens today on such trees (inference from the pytest rules); the explicit
  rootdir fixes both.
- **Keep the clone off `sys.path`: a launcher, not `-m pytest`.** `python -m pytest` (and `-c`) put the cwd at the
  start of `sys.path`, so with cwd = clone the tests would import the project from the clone, where in-place builds
  and generated modules may be missing (the clone is a walk with pruning) and which breaks "imports from the real
  tree". `-P`/`PYTHONSAFEPATH` need 3.11, and the project interpreter may be 3.10. Chosen: run
  `[py, "-B", "-c", LAUNCH, *pytest_args]` with
  `LAUNCH = "import sys; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; import pytest; sys.exit(pytest.main(sys.argv[1:]))"`.
  **Drop only `''` and `.`, never `os.getcwd()`** (correction 1): in `off` mode the cwd is the tree root, which
  `PYTHONPATH` carries, and removing it breaks `import pkg` from tests. Tested with an off-mode case.
  `PYTHONPATH` (tree root and source roots, line 91) stays, so imports resolve in the real tree as today. Rejected
  alternative (cwd = tree for affected tests): simpler, but the 7.4 spec names the affected-tests run, and a
  relative write by a C extension would land in the real tree when no OS sandbox is active.

#### Config (`legacyguard/guardlib/config.py:16-17`)
`behavioral_diff`: `"cwd_mode": "auto"`, `"cwd": None`. No schema elsewhere needs changing; `autosetup` writes no
`behavioral_diff` keys (check while implementing).

#### Report label
- `legacyguard/scripts/post_edit.py` and the Stop audit call `engine.record_isolation(data)` inside the ledger lock:
  `data["isolation"] = {"os_sandbox": os_sandbox.describe(), "cwd": scratch.describe()}`, only when a folder was used.
- `legacyguard/guardlib/report.py:145-149`: one header line, e.g.
  "Isolation: OS sandbox **sandbox-exec**; working folder **work: clone (clonefile) of 1,840 files, 2.1 MB, in
  0.21 s; baseline: clone (clonefile) of 1,838 files, 2.1 MB, in 0.20 s**."
  Without an OS layer, append: "**No OS sandbox: absolute-path writes are stopped only by the in-process guards.**"
- CLAUDE.md: replace "The worker has no throwaway cwd yet" with a 7.4 entry (strategies, labels, gaps below).

## 6. What "absolute-path write leaves the tree unchanged" needs

A cwd change catches only **relative** writes. Absolute writes are handled by layers that already exist:

1. In-process (patches and audit hook): refuse writes outside the temp folder and outside protected paths; during
   setup, 6.8a redirects cache-like absolute paths (a black-style import writing a cache under `~/.cache/<tool>/`)
   into the run's shadow folder.
2. OS sandbox: the tree and `cfg.root` are in `readonly`, home is not writable, so an absolute write that gets past
   the in-process layers fails with EPERM. Keep the working folder out of `readonly` (except `off`, where it is the
   tree) and inside the writable temp folder.
3. Without an OS sandbox nothing in phase 1 stops an absolute write that gets past the in-process layers; the report
   line says so. Phase 2 (imports from the clone) would move `__file__`-relative absolute writes into the clone;
   hard-coded absolute paths would still need the OS layer.

## 7. Tests

New file `tests/test_throwaway_cwd.py` (plus small additions to `tests/test_affected_tests.py`, `tests/test_warm.py`).
Generic fixture names only (`pkg/m.py`, `fmtlib`, `data/table.txt`). Test-only knobs are monkeypatches of
`scratch._clone_one`, `scratch._probe`, `scratch.MAX_*`, `tempfile.gettempdir`; no production test switch.

**Unit tests (`scratch`)**
1. `auto` on a small fixture: the chosen strategy equals what `_probe` says for this machine (clone if the probe
   succeeds, else copy); the label names that strategy. Never hard-code "clone" (CI's ext4 picks copy).
2. `_clone_one` raising `OSError(EXDEV)` → copy, label contains "another file system".
3. `_clone_one` raising `OSError(ENOTSUP)` and `OSError(EOPNOTSUPP)` → copy, label "no copy-on-write".
4. `MAX_COPY_FILES = 1` and probe forced to fail: with `sandboxed=True` → `off` (cwd is the tree); with
   `sandboxed=False` → `empty`.
5. `MAX_CLONE_FILES = 1` with a working probe → copy (still under copy limits).
6. Modes `off`, `empty`, `clone` honoured; unknown mode → auto with a note.
7. `cwd: sub` → `<folder>/sub`; missing `sub` → folder root with a note.
8. Temp folder inside the project (`tempfile.gettempdir` monkeypatched) → `off` with that reason.
9. Cache: a second `prepare_cwd` does not call `_probe` (counter); a different mode or `st_dev` re-probes;
   thresholds recomputed each call (add files over `MAX_COPY_FILES` between calls → strategy changes without a probe).
10. Listing: tracked, untracked and **git-ignored** files present (`.env`, `data/local.json` listed in
    `.gitignore`); `.git`, `__pycache__`, a folder with `pyvenv.cfg`, `node_modules` and a nested folder with its
    own `.git` absent; a symlink stays a symlink.
11. The working folder is never under `cfg.root` or the tree; `release` deletes owned folders; `_sweep` deletes a
    `lg-cwd-<dead pid>-*` folder and leaves a live one.
12. FICLONE selection: with `fcntl.FICLONE` absent and `platform.machine()` monkeypatched to `ppc64le`, Linux clone
    is reported unsupported (skip off Linux, or test the selection function directly).

**End to end through `diff.run`, write layers ON** (cold; each of `cwd_mode` clone-or-copy as chosen, `copy`, and `empty` where stated)

13. **Acceptance core** (one test, `os_sandbox: off` so the cwd is the only guard for relative paths):
    - a function reading `data/table.txt` by relative path is checked on both trees (status done, no worker error);
    - a function writing `out.txt` by relative path during the call is `untested` with today's reason
      ("writes files"), same as with `cwd_mode: off`;
    - afterwards the tree and the baseline are byte-identical to before (snapshot of names, sizes, mtimes).
14. Ignored-file read, stated behaviour: a function reading the git-ignored `data/local.json` by relative path gets
    the same result as with `cwd_mode: off` (working tree has it; the baseline has never had it).
15. **Black-style import** (named `test_import_writing_an_absolute_cache_path_runs_with_a_clone_cwd`): module `fmtlib`
    that at import does `os.makedirs(os.path.join(os.path.expanduser("~"), ".cache", "fmtlib", "1.0"), exist_ok=True)`
    and writes a pickle there; HOME points at a folder inside `cfg.root` (so it is read-only to the OS layer and in
    protect); the function is checked (6.8a redirect) with a clone/copy cwd, and the real cache path is not created.
    Run once with `os_sandbox: auto` (skip assertion of the label if unavailable) and once with `off`.
16. OLD-A and OLD-B agree on a function returning `os.path.basename(os.getcwd())` (shared folder per tree; no
    "non-deterministic").
17. Report: with `os_sandbox: off`, the rendered report contains the working-folder label of the strategy actually
    chosen and the "No OS sandbox" sentence; with `auto` on a machine with a sandbox, the sandbox name.

**Warm (in `tests/test_warm.py`, opt-in warm)**

18. The child runs in `request["cwd"]` (function returning `os.path.realpath(os.getcwd())` equals the cwd the hook
    made), and a relative write during a call is refused with the same status and reason as cold (protect entry in
    `realpath + os.sep` form).

**Gets past the in-process layers (the only use of the bypass)**

19. Fixture function sets `(sys.modules.get("lg_worker") or sys.modules["__main__"]).GUARD_WRITES["allowed"] = True`,
    then writes a relative file and an absolute file inside the tree. With `os_sandbox: auto` (skip without one):
    the tree is unchanged; the relative file landed in the working folder (or was refused for `off`). One function
    per run (the flag stays off for the rest of that process).

**Affected tests (`tests/test_affected_tests.py`)**

20. Tree with **no ini file** (no pytest.ini, pyproject or setup.cfg), tests in `tests/`: results are keyed by
    relative nodeids, a passing-then-failing test is found as a regression, nothing drops.
21. A test that reads `tests/data/x.json` by relative path (run from the tree root today) passes on both trees.
22. **Ignored in-place module**: `pkg/_gen.py` is git-ignored (an in-place build or generated module) and exists in
    the real tree; a test does `from pkg._gen import VALUE`. Monkeypatch `scratch._list_tree` to leave `pkg/_gen.py`
    out of the clone, so the clone and the real tree differ. The test must still pass on the working tree: imports
    come from the real tree, not the clone. With `-m pytest` instead of the launcher, `''`/the clone would come first
    on `sys.path`, `pkg` would import from the clone and the test would fail (keep this as the reason in the test's
    docstring).
23. `old_test_results` cache: keys are relative nodeids and stay equal between the baseline and working runs.

**Added by the round-2 corrections**

25. Launcher, off mode: with `cwd_mode: off` (cwd = tree root, carried by `PYTHONPATH`) a test doing `import pkg`
    still passes; and a direct check that the launcher drops `''`/`.` but keeps a `PYTHONPATH` entry equal to the cwd.
26. Forced EXDEV on a large file after a good probe (`MAX_COPY_BYTES` lowered): ends in `off` (sandboxed) or `empty`,
    never a full copy; the half-made folder is gone; the label has the reason and counts. Also a small failing file:
    "clone ... 1 copied".
27. A sub-folder on another device (`_dev` monkeypatched) is left out; `build`, `dist`, `.eggs`, `*.egg-info`,
    `htmlcov`, `_build` and a folder with `conda-meta` are left out.
28. post_edit end to end: one `prepare_cwd` per tree for the diff and both affected-test runs together; the
    isolation labels are stored; folders deleted after.
29. `for_trees` is lazy, re-entrant and releases at the outermost exit.

**Timing**

24. 2,000-file fixture (40 packages × 7 sub-folders, about 1 KB each): `prepare_cwd` for the machine's strategy and
    for forced copy; print the seconds; assert only `< 10 s`; record the numbers in `docs/PROGRESS.md`.

Run suites one at a time (`.venv-dev/bin/python -m pytest -q`, then the 3.10 venv).

## 8. Known limits to document (CLAUDE.md and the docstring)

- Shared OLD-A/OLD-B working folder: a write that gets past the in-process layers (C extension) in OLD-A can be
  seen by OLD-B, which weakens the old-vs-old noise filter for that case; it never hides an old-vs-new difference.
- Path canon gap (existing): `lg_codec.canon` does not mask tree or cwd paths, so a result containing an absolute
  path already differs between the baseline and the working tree today; the working-folder path behaves the same.
- Baseline has no git-ignored files; relative reads of them fail on the old side, as today.
- The warm server's own cwd is still the tree; `indexer/routes.py:77` and `coverage_map.py:53` still run in the tree.
- `empty` mode: relative reads of project files fail, and if old and new raise the same FileNotFoundError the input
  shows no difference (Q4 follow-up adds an untested reason).
- Without an OS sandbox, absolute writes that get past the in-process layers reach the tree (report says so).
- Target code calling `os.chdir` during a call changes the cwd for later inputs (existing).
- A warm baseline server imports project modules with cwd = the tree (its own cwd), while a cold run imports them with
  cwd = the throwaway folder. A module that stores `os.getcwd()` at import therefore reads different paths on a
  warm/cold mix: the result is untested (old-vs-old disagreement) or a difference, never a hidden one.
- The hook's pid is part of the folder name, and `lg_codec.canon` may show it as `<parent pid>` in a cold worker
  (the hook is the worker's parent); OLD-A and OLD-B still see the same text.
- `_sweep` treats a PermissionError from `os.kill(pid, 0)` as alive (a folder of another user's process is kept).

## 9. Split out (not in 7.4)

0. **Part B: baseline clone once per session** (first 7.4 follow-up; split out by the reviewer in round 2). Part A
   clones both trees per check (about 0.55 s per check at 2k files on this Mac, about 5 s at the 20k cap). Tests to
   write with it:
   - staleness keys (each key in `stale_key` below, and the folder gone, re-clones);
   - fingerprint re-clone (a changed entry count, size or max mtime re-clones);
   - `flock` with overlapping hooks (two hooks at once build one folder; a replacement retires the old path, never
     deleting it under another hook);
   - retired cleanup at SessionStart (`drop_session_cache` deletes the cached folder and every retired path).

#### Part B detail (kept for the follow-up)

The baseline never changes during a session, so its clone can be reused; only the working tree is cloned per check.
Applies to the `clone` and `copy` strategies (empty and off cost nothing).

- Location: `tempfile.mkdtemp(prefix="lg-cwd-base-")` (not pid-named, so `_sweep` leaves it).
- Manifest `cfg.state_dir / "cwd_cache.json"`: `{path, strategy, label, stale_key, fingerprint, retired: [paths]}`.
- **Stale when any of these differs** from the stored `stale_key`: `baseline_commit` from `session.json`;
  `warm._generation(cfg)` (inode and mtime_ns of the baseline folder, which change when SessionStart rebuilds it,
  including a rebuild at the same commit with different dirty files); `realpath(baseline)`; `st_dev(baseline)`;
  `st_dev(tempdir)`; `cwd_mode`; `cwd` subdir; `scratch.LIST_VERSION`; or when the folder no longer exists.
- **Kept clean**: it is in `SCRATCH["protect"]` (Q2), so in-process writes during calls are refused and setup
  writes are redirected to the run's shadow folder. Only a write that gets past both in-process layers (a C
  extension) can change it; under an OS sandbox that write is still possible because the folder is in the temp
  folder (writable, like the work clone, for symmetry). So each check also compares a **fingerprint**: (entry count,
  total size, max `st_mtime_ns` over files and folders) from one `os.walk` + `lstat` (about 0.02 s at 2k files;
  roughly 0.1–0.2 s at 20k, inference). A mismatch re-clones. Heuristic: a write that restores mtime through
  `os.utime` would evade it (utime is refused by the audit hook during calls anyway).
- Concurrency: two async PostToolUse hooks can overlap. Build or replace under an exclusive `fcntl.flock` on
  `cfg.state_dir / "cwd_cache.lock"`; a replacement clones into a new folder and swaps the manifest, and the old
  path goes into `retired` (never deleted while another hook may be using it).
- Cleanup: `legacyguard/scripts/session_start.py` (after the baseline is rebuilt, near line 120) calls
  `scratch.drop_session_cache(cfg)`, deleting the cached folder and every `retired` path.


1. **7.4b bwrap overlay strategy** (`--overlay-src <tree> --tmp-overlay <tree>` instead of `--ro-bind`): needs
   bwrap 0.11+ (CI's Ubuntu 24.04 has 0.9.0, so CI cannot test it), Linux 5.11+, a probe; in a long-lived warm server
   overlay writes would persist across children (inference); and it makes writes into the tree succeed silently into
   tmpfs, which changes the readonly rule.
2. (Part B: see 0 above.)
3. Q4: empty-mode relative-read rule.
4. Phase 2: imports from the clone.
5. Masking tree/cwd paths in `lg_codec.canon`.
6. Non-cache home writes at import (run 4's `~/.<tool>/tmp`).
7. Server cwd, `indexer/routes.py` cwd, resetting the cwd before each input.
