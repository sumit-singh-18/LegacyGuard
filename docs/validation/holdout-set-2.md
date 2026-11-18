# Held-out validation set 2 (chosen 2026-11-18, never used for tuning)

Chosen by the researcher agent and passed by the reviewer agent in round 3. The user's decisions on the
two open questions are recorded under "Decisions". Chosen before any LegacyGuard run on these bugs
(task 6.8f, step 1). Hashes were checked against each `bug.info` and patches and import chains were read. No project
code was executed. The set shares no project with the tuning set (youtube-dl, tqdm, thefuck) or with held-out set 1
(tornado, black, httpie, cookiecutter, luigi, PySnooper). Validation runs report it separately from both.

**The set has 9 counted bugs, not 10.** Under the criteria below, only three of the eight remaining BugsInPy projects
qualify (ansible, fastapi, scrapy), and the cap of 3 bugs per project allows at most 9. The user decided to keep 9 and
not waive the cap (see "Decisions").

Source of every row: `https://raw.githubusercontent.com/soarsmu/BugsInPy/master/projects/<project>/bugs/<n>/`
(`bug.info`, `bug_patch.txt`, `run_test.sh`). The BugsInPy snapshot read was commit
`11c5f1eea954a42132cfd06bf257766a7963e0fd` (2025-10-09). "Direct call only" has the same meaning as in set 1. No row
in this set needs it, because the guard does not run the BugsInPy tests.

## Criteria (fixed before the walk)

- **C1 Hashes.** Applying `bug_patch.txt` to the changed files at `buggy_commit_id` gives exactly the files at
  `fixed_commit_id` (both fetched from the upstream repository).
- **C2 Imports on Python 3.10.** Every module that the changed module imports at load time (traced statically from the
  checkout, package `__init__` files included) exists on 3.10, and every third-party dependency in that chain is
  installable on 3.10 **without compiling**: a cp310, abi3 or `none-any` wheel, or a pure-Python sdist (no
  `ext_modules`). Accepting pure-Python sdists was decided by the user on 2026-11-18 (see "Decisions"). It matters for
  fastapi 4 and the fastapi 5 reserve (starlette 0.12.9 has no wheel).
- **C3 Compiled projects.** pandas, matplotlib, keras and similar qualify only if a wheel of the project from the buggy
  commit's era installs on 3.10 and the checkout is pure Python for the changed module's import chain.
- **C4 Reach.** The changed lines can be reached by a direct call whose arguments are built in memory. That means no
  network, no pre-existing files or directories, no subprocess and no running server. ("Prefer pure-ish, directly
  callable" from set 1, made into a yes/no test.)
- **C5 Shared call.** At least one changed function exists in both versions, and the fixed behaviour can be reached
  through a call that both versions accept. A fix that only adds new functions, new parameters or a module constant
  fails this test.
- Whether LegacyGuard would detect the bug was never considered.

## Method (reproducible)

1. Projects: the BugsInPy `projects/` list minus the nine already used. That leaves ansible, fastapi, keras,
   matplotlib, pandas, sanic, scrapy and spacy.
2. Rounds: in each round, visit the projects in alphabetical order. For each project, take the next bug in numeric
   order that meets C1 to C5. Stop a project after 3 picks, and stop the whole walk at 10 picks.
3. Every bug visited before a pick is recorded below with the first criterion it fails.
4. **Replacement rule** (fixed now, for use at setup time): if `--check-setup` shows that a picked row cannot import, the
   row is replaced by the next qualifying bug of the same project in the same walk. The reserves are listed below.

## Set

| # | Case | buggy | fixed | Changed function | How it is reached |
|---|---|---|---|---|---|
| 1 | ansible 2 | de59b17c7f69d5cfb72479b71776cc8b97e29a6b | 5b9418c06ca6d51507468124250bb58046886be6 | `lib/ansible/utils/version.py` `_Alpha.__gt__`, `_Numeric.__gt__` (equal values compare as greater: buggy `not lt`, fixed `not le`). The `__ge__` edits are equivalent rewrites (buggy `(not lt) or eq`, fixed `not lt`; lt and eq are never both true) | `_Alpha('a') > _Alpha('a')`, `_Numeric(1) > _Numeric(1)`, `_Alpha('a') > 'a'`, `_Numeric(1) > 1` (buggy True, fixed False) |
| 2 | ansible 3 | 70219df9056ffb1e2766f572fbe71f7a1800c9f5 | 9d48884e36fb4fd9551f000b87d264383de74e75 | `lib/ansible/module_utils/facts/system/distribution.py` `DistributionFiles.parse_distribution_file_Debian` (Kali in `/etc/os-release`) | `DistributionFiles(None).parse_distribution_file_Debian('Debian', 'Kali\nDISTRIB_RELEASE=2020.1', '/etc/os-release', {})` (`data` and `path` are plain strings; `module` is used only by other methods) |
| 3 | ansible 4 | d91658ec0c8434c82c3ef98bfe9eb4e1027a43a3 | 18a66e291dad71128a32d662aa808213acefe0e9 | `lib/ansible/playbook/collectionsearch.py` `CollectionSearch._load_collections` (warn on a templated collection name); class attribute `_collections` gains `always_post_validate`, `static` | `CollectionSearch()._load_collections('collections', ['ns.coll', '{{ x }}'])`. The fixed version's only change in behaviour is a `Display().warning` on stderr; the return value is the same |
| 4 | fastapi 2 | 210af1fd3dc0f612a08fa02a0cb3f5adb81e5bfb | 02441ff0313d5b471b662293244c53e712f1243f | `fastapi/routing.py` `APIRouter.add_api_websocket_route` (does not pass `dependency_overrides_provider`) | `r = APIRouter(dependency_overrides_provider=p); r.add_api_websocket_route('/ws', endpoint)`, where `endpoint` takes a `Depends(dep)` and `p.dependency_overrides = {dep: other}`. `APIWebSocketRoute.__init__` does not store the provider as an attribute; it only passes it into the `get_websocket_app` closure inside `self.app` (buggy routing.py 195-233: `get_websocket_app` 195-211, `APIWebSocketRoute.__init__` 214-233 at 210af1f). So the difference shows only when the route's ASGI app is called in memory: `await r.routes[-1].app(scope, receive, send)` with a websocket scope and in-memory `receive`/`send` (buggy runs `dep`, fixed runs `other`). C4 judgement: passes. The changed lines run on the direct call, and the follow-up ASGI call needs no network, server, file or subprocess (in-process, like Starlette's TestClient). |
| 5 | fastapi 3 | 869c7389e22dc9ad659940fa271da76c4f3ba3b1 | aea04ee32ee1942e6e1a904527bb8da6ba76abd9 | `fastapi/routing.py` `serialize_response` (async), plus new helper `_prepare_response_content`. The versions differ only when `exclude_unset=True`: buggy turns only a top-level `BaseModel` into a dict (not lists or dicts of models) and drops `by_alias` | `M` has `name: str = Field(..., alias='aliased_name')` and `price: float = None`. Single model: `await serialize_response(field=create_response_field('r', M), response_content=M(aliased_name='x'), exclude_unset=True)` (buggy `.dict(exclude_unset=True)` without `by_alias` gives `{'name': 'x'}`, which fails validation on the required alias; fixed validates). List and dict: `await serialize_response(field=create_cloned_field(create_response_field('r', List[M])), response_content=[M(aliased_name='x')], exclude_unset=True)`, and the same with `Dict[str, M]` and `{'k': M(aliased_name='x')}`. Both helpers come from `fastapi.utils` (lines 96 and 134 at the buggy commit, in both versions). The field must be cloned, as `APIRoute` does it (buggy routing.py line 273: `create_cloned_field(self.response_field)`, which is passed to `serialize_response`). On an uncloned field, pydantic copies each `M` instance with its fields-set intact, so both versions give `[{'aliased_name': 'x'}]`. On a cloned field the model class is a subclass of `M` (`create_model(..., __base__=M)`), so an `M` instance is not an instance of it: buggy goes through `dict(value)` with field names and fails on the required alias, while fixed validates. (Reviewer's inference from pydantic v1.9.0 main.py 666-671, not run.) Mirrors the upstream tests `test_valid_exclude_unset`, `test_validlist_exclude_unset`, `test_validdict_exclude_unset`, which go through `APIRoute` |
| 6 | fastapi 4 (pure-Python sdist dependency, accepted by the user's C2 decision) | 7ccd81f70653857bd8f3a15ee946aa3fb0edc2cb | 74c4d1c1dbe6bfdb05d6e4fc767ffe062398f0a3 | `fastapi/openapi/utils.py` `get_openapi_path` (duplicate parameters when a path parameter is also declared in a dependency) | an app with `@app.get('/{id}')` whose dependency also takes `id`; `get_openapi(title='t', version='1', routes=app.routes)` or `get_openapi_path(route=app.routes[-1], model_name_map={})` |
| 7 | scrapy 1 | c57512fa669e6f6b1b766a7639206a380f0d10ce | 9d9dea0d69709ef0f7aef67ddba1bd7bda25d273 | `scrapy/spidermiddlewares/offsite.py` `OffsiteMiddleware.get_host_regex` (URL entries in `allowed_domains` are warned about but still used) | `OffsiteMiddleware(None).get_host_regex(spider)` with two inputs. `allowed_domains = ['a.com', 'http://b.com']`: buggy keeps the URL entry in the regex, fixed drops it. Adding `None` to that list: buggy raises TypeError from `url_pattern.match(None)` before the regex is built, fixed skips it |
| 8 | scrapy 2 | f02c3d1dcf3e4880388d19e961e7911be5dc54ff | 439a3e59b8e858441f8d97dbc32f398db392330d | `scrapy/utils/datatypes.py` `LocalCache.__setitem__` (`limit=None` raises TypeError) | `c = LocalCache(); c['k'] = 1` |
| 9 | scrapy 3 | be2e910dd06ba4904e7b10eb5a7e3251e8dab099 | 66cbceeb0a9104fc0fa238898e38d0d9ce9cbcf6 | `scrapy/downloadermiddlewares/redirect.py` `RedirectMiddleware.process_response` (a `Location` with three leading slashes, `///host/path`; the two-slash form gives the same URL in both versions) | `RedirectMiddleware(Settings()).process_response(Request('https://a.com/x'), Response('https://a.com/x', status=302, headers={'Location': '///b.com/y'}), Spider('s'))`: buggy redirects to `https://a.com/b.com/y`, fixed to `https://b.com/y` (as in the upstream test at the fixed commit, tests/test_downloadermiddleware_redirect.py:110-112) |
| control | none | | | | No natural control meets C2 to C4. The candidates that would serve as one (sanic 5, a constant-only change; fastapi 1, behaviour reachable only through new parameters) fail C2 or need an equivalence argument over a 4-file patch, which was not made. |

Reserves (next in the walk; used only under the replacement rule): **ansible 5**
(`2af76f16be8cf2239daaec4c2f31c3dcb4e3469e` -> `3c3ffc09c203d1b2262f6a319cceadd727749761`,
`lib/ansible/module_utils/common/validation.py` `check_required_arguments`: the missing names are not sorted, reached
by `check_required_arguments({'b': {'required': True}, 'a': {'required': True}}, {})`), **fastapi 5**
(`7cea84b74ca3106a7f861b774e9d215e5228728f` -> `75a07f24bf01a31225ee687f3e2b3fc1981b67ab`, `fastapi/utils.py`
`create_cloned_field`: sub-model fields are not cloned; starlette 0.12.9 sdist), **scrapy 36**
(`cb8140a42a27ede87b0880372024f2f1804618b8` -> `cf9be5344a89dd8e14f8241ec69de9c984ec1e05`, `scrapy/utils/misc.py`
`create_instance`: a `from_settings` that returns None is not caught). Each reserve passes C1 (patch check done) and
C2 (import chain read).

Source root: ansible imports from `lib/` (BugsInPy's `pythonpath="ansible/build/lib/"` is the result of
`setup.py install`; the guard imports the checkout). fastapi and scrapy import from the repository root.

## Environment (Python 3.10; versions chosen for C2, to be pinned at setup like set 1)

- **ansible 2, 3, 5**: no third-party imports in the chain (stdlib, plus vendored `six` 1.12 and `distro`; `selinux` is
  an optional import). On 3.10, `distutils` (ansible 2) gives a DeprecationWarning, and vendored six 1.12's meta-path
  importer has no `find_spec`, so it gives an ImportWarning (fallback to `find_module` still works on 3.10).
- **ansible 4**: `Jinja2==3.0.3` (py3 wheel; it has `jinja2.utils.concat`, which `ansible/template` imports and which
  3.1 removes), `MarkupSafe==2.0.1` (cp310 wheels), `PyYAML==6.0` (cp310 wheels; BugsInPy's 5.3.1 has none; the
  config loader passes `Loader=SafeLoader` explicitly). `cryptography` is an optional import. Load-time side effects in
  the chain: `ansible.constants` reads config files (ansible.cfg in cwd, `~/.ansible.cfg`, `/etc/ansible/ansible.cfg`).
  `ansible.utils.display.Display()` runs `cowsay -l` through `subprocess.Popen` if a cowsay binary exists at one of four
  fixed paths (display.py lines 104 to 130). The fixed version of `collectionsearch.py` adds a module-level
  `display = Display()`, so on the fixed side that probe runs at import time.
- **fastapi 2, 3**: `starlette==0.13.2` (none-any wheel); **fastapi 4, reserve 5**: `starlette==0.12.9` (exact pin
  `>=0.12.9,<=0.12.9` in pyproject; sdist only, `setup.py` has no `ext_modules`). All: `pydantic==1.9.0`, the first
  release whose changelog says "add Python 3.10 support, #2885" (BugsInPy pins 1.5.1, which predates it). Every
  pydantic name the chain imports (`SHAPE_*`, `FieldInfo`, `ModelField`, `Required`, `get_annotation_from_field_info`,
  `evaluate_forwardref`, `field_schema`, `get_model_name_map`, `get_flat_models_from_fields`, `model_process_schema`,
  `ErrorList`, `ENCODERS_BY_TYPE`, `lenient_issubclass`, `MissingError`, `Validator`) exists in 1.9.0. `email_validator`
  and `orjson` are optional imports; `async_generator` and `async_exit_stack` are fallbacks that 3.7+ does not use.
- **scrapy 1, 2, 3, reserve 36** (scrapy 2.0.0, 1.8.0, 1.8.0, 2.1.0): `Twisted==22.1.0` (first release whose NEWS says
  "Python 3.10 is now a supported platform"; BugsInPy's 20.3.0 is sdist only), `lxml==4.6.4` (cp310; BugsInPy's 4.5.1
  has none), `parsel==1.6.0`, `w3lib==1.22.0`, `cssselect==1.1.0`, `queuelib==1.5.0`, `PyDispatcher==2.0.7`,
  `Protego==0.2.1`, `zope.interface==5.5.0`, `six==1.15.0`, `service-identity==18.1.0`. pyOpenSSL and cryptography are
  install requirements but are not in the import chain. Python-2 names in the chain (`cStringIO`, `urlparse`,
  `collections.Mapping`, `inspect.getargspec`) sit only in `six.PY2` or `try` branches.

## Rejected (walk order, first failed criterion)

- **ansible 1** (C4): `verify_collections` reaches the changed line only when a `<search_path>/<namespace>/<name>`
  directory exists on disk, and it needs Galaxy API objects. ansible 5 onwards: not visited (cap reached; ansible 5 is
  the reserve).
- **fastapi 1** (C5): the fix is an API change (`include_none` removed; `exclude_none`, `exclude_defaults` and
  `response_model_exclude_*` added across 4 modules plus a test). The fixed behaviour is reachable only through the new
  parameters. fastapi 5 onwards: not visited (cap reached; fastapi 5 is the reserve). For the record, fastapi 8 to 16
  pin starlette <=0.12.8 and pydantic <=0.32.2, which are older still.
- **keras 1 to 45** (C2/C3): BugsInPy pins `tensorflow` 1.13.2 (11 bugs), 1.14.0 (3) or 1.15.0 (31). No TensorFlow
  1.x wheel exists for 3.10 (1.15.0 checked). `import keras` loads its backend, and the other backends are sdist-only
  (Theano 1.0.5) or unavailable (CNTK). Moving to TensorFlow 2.x would be an environment change I could not confirm by
  reading.
- **matplotlib 1 to 30** (C3): buggy commits date from 2019-05 to 2020-06 (matplotlib 3.1 to 3.3). matplotlib 3.3.4 has
  no cp310 wheel (3.5.0 is the first I checked that has one). The checkout is not pure Python:
  `lib/matplotlib/__init__.py` calls `_check_versions()` at import, which imports the compiled `ft2font`.
- **pandas** (all bugs) (C3): 169 of 170 BugsInPy `setup.sh` files run `setup.py build_ext --inplace`.
  `pandas/__init__.py` imports `pandas._libs` (compiled). pandas 1.1.5 has no cp310 wheel (1.3.4 does).
- **sanic 1 to 5** (C2): `setup.py` requires `multidict>=4.0,<5.0` and `websockets>=7.0,<9.0` (bug 2) or `<7.0`
  (bug 5). multidict 4.7.6, websockets 8.1 and 6.0 have no 3.10 wheels and ship C extensions. Bugs 1, 3 and 4 were
  rejected the same way in set 1. In addition, bug 2 only adds new methods (C5), and bug 5 changes a module-level dict
  (C5).
- **scrapy**: picks 1, 2, 3 (cap reached). For the reserve walk, bugs 4 to 35 and 38 to 40 fail C2 (unguarded import):
  scrapy 1.1 to 1.5 (and 0.25) has `from collections import MutableMapping` unguarded in `scrapy/item.py`, which
  `scrapy/__init__.py` imports. 36 is the reserve; 37 (scrapy 1.7.0, guarded `six.PY2` form at item.py lines 20-22) was
  not visited. Set 1 had already rejected 17 and 29 for this reason.
- **spacy 1 to 10** (C2/C3): every BugsInPy `setup.sh` runs `setup.py build_ext --inplace` (Cython). spacy 2.2.x has no
  cp310 wheel (2.2.4 checked; 2.3.9 has one but is a later minor line), and neither does `thinc==7.4.0`.
  `spacy/__init__.py` imports `.pipeline` and other Cython modules. Bug 3 changes `bin/wiki_entity_linking/`, which
  imports `spacy.gold` (Cython).

## Decisions (made by the user on 2026-11-18, before any LegacyGuard run on these bugs)

1. The set stays at 9 counted bugs. The per-project cap is not waived, and ansible 5 stays a reserve only.
2. C2 accepts pure-Python sdists (no `ext_modules`). This keeps fastapi 4 and makes fastapi 5 a valid reserve.

Rules for scoring (fixed now):
- ansible 4: the only difference in behaviour is a warning on stderr plus class-attribute metadata. Score it like any
  other row. Do not reclassify it after the run.
- fastapi 2: the difference shows only when the route's ASGI app (`r.routes[-1].app`) is called in memory with a
  websocket scope and a dependency override; it is not visible in the return value of `add_api_websocket_route` or in
  a route attribute. Score it like any other row.
