# Validation run 1: nine real BugsInPy bugs (2026-11-14)

First run of LegacyGuard on real code (roadmap 3b). Harness: `tools/validate.py`. Manifest:
`tools/validation/manifest.yaml`, patches from BugsInPy in `tools/validation/patches/`. Raw results:
`2026-11-bugsinpy-results.json`. Interpreter: Python 3.10.22 for every case. Produced by the implementer
agent; every number and diagnosis below was reproduced by the reviewer agent by calling the fixed and
buggy code directly.

Method: for each bug, the BugsInPy fixed commit is the baseline (OLD) and the bug is planted back into
the working tree (NEW) by reverse-applying BugsInPy's bug_patch.txt. A case counts as detected only when
a function in the bug's files, or a caller of one, behaves differently. Detection rate is over cases
where something could be compared; cases where nothing could be compared are reported as unchecked,
not as misses or passes (following the coverage / propagation / environment split of misses in
Shamshiri et al., ASE 2015, "Do Automatically Generated Unit Tests Find Real Faults?").

## Headline

| | Detected | Rate |
|---|---|---|
| With the fix commit's regression tests present in the baseline | 5 of 7 checked | 71% |
| **Without them (the realistic case: that test does not exist until the bug is found)** | **2 of 7 checked** | **29%** |
| Unchecked (nothing could be compared) | thefuck-1, thefuck-7 | |

Three of the five detections (youtube-dl 1, 3, 5) used a literal that appears only in the test the fix
added. With the fix's test changes reverted, they become quiet. youtube-dl 11 (`str_to_int(0)` raises
TypeError) and tqdm 9 (`format_sizeof(9.995, 'g')` gives `'10.0g'` vs `'9.99g'`, and `__len__`) are caught
without them. Median time per case 2.8 s, p95 4.0 s. No planted-bug confidence scores ran (see below).

## Per case

| Case | Verdict | Counterexample, or why missed | Miss category |
|---|---|---|---|
| youtube-dl 1 | detected* | `match_str('is_live', {'is_live': False})`: False before, True after | propagation without fix test (no dict with a bool value) |
| youtube-dl 3 | detected* | `unescapeHTML('&a&quot;')`: `'&a"'` vs `'&a&quot;'` | infection without fix test (no `&` before an entity) |
| youtube-dl 5 | detected* | `unified_timestamp('May 16, 2016 11:15 PM')`: 1463440500 vs AttributeError | coverage without fix test (the buggy return line is never reached) |
| youtube-dl 10 | quiet (miss) | no input was a quoted string containing a `\n` or `\u` escape; the fix's test passes its input through a variable | propagation / infection |
| youtube-dl 11 | detected | `str_to_int(523)` / `str_to_int(0)`: value vs TypeError | |
| tqdm 4 | quiet (miss) | `format_meter` itself untested (no input had `total=None` with a numeric `unit_scale`); quiet only because callers were "verified" without being checked against the changed lines | coverage |
| tqdm 9 | detected | `format_sizeof(9.995, 'g')`, `__len__()`, caller `format_meter(1, 9999, 1, unit_scale=True)` | |
| thefuck 1 | unchecked | untyped `command` parameter got strings only, every call raised AttributeError | coverage (could not build a valid argument) |
| thefuck 7 | unchecked | same; `Command('php -t pub -s 0.0.0.0:8080', '')` distinguishes the versions | coverage |

\* detected only with the fix's own regression test present.

## What this shows

1. **A correctness bug in the guard: callers bypass the "must reach the changed lines" rule.** Caller
   targets carry no gate, so a caller can be "verified" on inputs that never reach the edit (tqdm 4).
   Fixing this does not raise detection; it removes false "verified" results.
2. **The gate passes when any changed statement is reached.** youtube-dl 5 was "verified" although the
   buggy return was never reached. A per-statement gate is again an honesty fix.
3. **Inputs are the bottleneck.** Every miss is a coverage or propagation miss, none environmental.
   What would have found them, each checked by direct calls: test arguments passed through variables or
   built as objects (`inp = '...'; js_to_json(inp)`, `Command(...)`), arguments that callers pass into the
   edited function, and values built from the edit itself (a bool into dict values catches youtube-dl 1,
   `'&&#47;'` catches youtube-dl 3; expected yield 2 to 3 of the 5 no-test misses, extending the existing
   literal-based generation rather than replacing it).
4. **Planted-bug scores never ran**: the directly verified functions were a regex-only change
   (js_to_json, no mutable spot) and a comment-only change (tqdm `__init__`, which should not have been
   selected at all).

## Next steps (ranked, for Sprint 5)

1. Gate callers on the callee's changed lines, and gate per changed statement (honesty fixes).
2. Inputs from tests: resolve arguments passed through local variables, and build objects for parameters
   from constructor calls in tests (as `_instance_maker` already does for `self`).
3. Record and reuse the arguments callers pass to the edited function.
4. Edit-derived inputs: splice the edit's literals and character classes into existing examples.
5. String and regex planted bugs; skip comment- and whitespace-only changes; skip callers that cannot be
   called (27 of the 34 untested functions were youtube-dl `_real_extract` extractors).
6. Harness: a "without fix tests" mode built on a synthetic base commit, reported as the headline number,
   and the source (tests, examples, generated) of each deciding input.
