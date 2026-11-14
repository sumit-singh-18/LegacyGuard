# Held-out validation set (chosen 2026-11-14, never used for tuning)

Chosen by the researcher agent and passed by the reviewer agent in round 2 (hashes compared with each
`bug.info` in BugsInPy; patches and import chains read, no project code executed). It shares no project with the
tuning set (youtube-dl, tqdm, thefuck). Validation runs report it separately, and its number is the one for the
README.

Source of every row: `https://raw.githubusercontent.com/soarsmu/BugsInPy/master/projects/<project>/bugs/<n>/`
(`bug.info`, `bug_patch.txt`, `run_test.sh`). "Direct call only" means the BugsInPy test cannot run on Python 3.10,
but the module the guard calls imports cleanly; LegacyGuard calls functions directly, so the case is valid.

| # | Case | buggy | fixed | Changed function | How it is reached |
|---|---|---|---|---|---|
| 1 | tornado 5 (direct call only) | 2d4053daa56c609d642b214399e046671d4a593e | 886643965b5cb782503d8d7b374b7a794ec2077b | `tornado/ioloop.py` `PeriodicCallback._update_next` (state when the clock goes backwards) | `PeriodicCallback(None, 10000)`, set `_next_timeout`, call `_update_next(998)` |
| 2 | tornado 14 | 81ee310adcd905fbdf7c98d9fb6ef0c5a46026c2 | 1d02ed606f1c52636462633d009bdcbaac644331 | `tornado/ioloop.py` `IOLoop.initialize` (make_current check inverted) | `IOLoop(make_current=True)` |
| 3 | black 17 | bbc09a4f013f2a584f143f3f5e3f76f6082367d4 | 7fc6ce990669464f5172b63fafa3724f5f308be3 | `black.py` `decode_bytes`, `lib2to3_parse` (empty input) | `decode_bytes(b"")`, `format_str("", line_length=88)` |
| 4 | black 19 | 337a4199f90ca48a19cf26511e0cec330b13bd4e | 29e97d1d4a7717f1bd0ca35cacf2f2ce6d815b0c | `black.py` `EmptyLineTracker._maybe_empty_lines` (decorator after a comment) | `format_str(src, line_length=88)` |
| 5 | httpie 5 | 16df8848e81eefac830f407e4b985f42b52970da | 90af1f742230831792d74d303d1e7ce56c96d4bd | `httpie/cli.py` `KeyValueType` (escaped separators) | `KeyValueType(*seps)('bob\\:==foo')` |
| 6 | httpie 3 (direct call only; needs `setuptools<82` for `pkg_resources`) | 8c33e5e3d31d3cd6476c4d9bc963a4c529f883d2 | 589887939507ff26d36ec74bd2c045819cfa3d56 | `httpie/sessions.py` `Session.update_headers` (None values) | `Session(path).update_headers({...: None})` |
| 7 | cookiecutter 2 (needs `MarkupSafe==2.0.1`) | d7e7b28811e474e14d1bed747115e47dcdd15ba3 | 90434ff4ea4477941444f1e83313beb414838535 | `cookiecutter/hooks.py` `find_hook`, `run_hook` (list instead of one path) | `find_hook('pre_gen_project', hooks_dir)` |
| 8 | luigi 20 | b958140c2ec838e590a5be02dbac7414d5d0bf17 | c3d685e2b03369aab6f4d86ed1c95169c1c2c217 | `luigi/task.py` `Task.to_str_params` (insignificant params) | a Task subclass with `significant=False`; `.to_str_params()` |
| 9 | luigi 33 | a7c0662eab78fd226fd7ef6b4461d7199336cbb1 | fccb631a14e1d52138d39f06004be14ca8f3337d | `luigi/task.py` `Task.get_param_values` (positional filter) | `Cls.get_param_values(Cls.get_params(), args, {})` |
| 10 | luigi 22 | 9c4c47ae449593c55fb67ce51115d0be1fecb163 | 2db9768958c9665c2bb78f040054a25534205fc4 | `luigi/scheduler.py` `Worker.__init__` default `last_active` (then `prune` stops raising TypeError) | `Worker(123).prune(cfg)` |
| control | PySnooper 3 (not counted) | 6e3d797be3fa0a746fb5b1b7c7fea78eb926c208 | 15555ed760000b049aff8fecc79d29339c1224c3 | `pysnooper/pysnooper.py` `get_write_function` | expected "unchecked": the bug only shows through a file write, which the guard blocks by design |

Rules for scoring:
- luigi 22: the fixed default `time.time()` is evaluated at import. Score on outcome type (raises vs returns), not on
  `prune`'s exact value or on `last_active`. (The guard installs its frozen clock before importing project code, so its
  own runs are deterministic either way.)
- PySnooper 3 is reported on its own line and never in the detection rate.

Rejected (with reasons checked by the reviewer): tornado 9 (`collections.MutableMapping` in `tornado/httputil.py` at
import on 3.10); httpie 1, luigi 3 and 13, scrapy 17 and 29 (removed `collections` ABC imports); httpie 4 (needs
`is_windows`/`is_py26` from `requests.compat`, defined only in requests 2.5.1 and older, which all import
`collections.Mapping`); sanic 1, 3 and 4 (`websockets<9`, `multidict<5` have no Python 3.10 wheels); black 4 (dropped to
keep formatter-only bugs to one of ten).
