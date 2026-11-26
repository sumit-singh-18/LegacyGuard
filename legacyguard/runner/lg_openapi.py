"""Code-first OpenAPI: the spec the app generates (`app.openapi()`), built inside the worker's set-up (Sprint 7.6 Part B).

Runs in the TARGET project's interpreter: standard library only here; the project (and FastAPI) is imported only at run
time, after `lg_worker.prepare` has put the blocks, stubs, determinism, scratch folder and tree protection in place.
The guard side (`guardlib/app_openapi.py`) starts it under the OS sandbox as `[py, "-B", lg_openapi.py, request.json]`
with `PYTHONHASHSEED=0` and reads the one `LGOAS{...}` line it prints on the real stdout.

Steps: prepare (no Django), `reset_determinism()` (seeds `random`, so a `random.random()` default is the same in every
process), `load_app` under `setup_phase()`, then `app.openapi()` under `setup_phase()`. An app without a callable
`openapi()` (Flask, plain Starlette) is `kind: "none"`, not an error. The spec is written as canonical JSON text
(`sort_keys`, compact separators) after a string-key round trip, so int keys and mixed key types cannot break the sort.
A value JSON cannot hold is passed to `fastapi.encoders.jsonable_encoder` only when FastAPI is already imported; NaN or
anything still unencodable is an `encode` error.

A result is complete when `error` is None, `blocks` is empty and `kind != "none"`.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

RUNNER_DIR = os.path.dirname(os.path.abspath(__file__))
MARK = "LGOAS"
_CLOCK = time.perf_counter  # taken before lg_worker freezes the clocks for the project


def _default(obj):
    enc = sys.modules.get("fastapi.encoders")
    if enc is None and "fastapi" in sys.modules:
        try:
            import fastapi.encoders as enc  # noqa: PLC0415 - FastAPI is already loaded by the app
        except Exception:  # noqa: BLE001
            enc = None
    if enc is not None and hasattr(enc, "jsonable_encoder"):
        out = enc.jsonable_encoder(obj)
        if out is not obj:
            return out
    raise TypeError(f"{type(obj).__name__} is not JSON serialisable")


def canonical(spec) -> str:
    """Canonical JSON text: a first pass turns every value into JSON types (int keys become strings), the second sorts."""
    first = json.dumps(spec, allow_nan=False, default=_default)
    return json.dumps(json.loads(first), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _files(root_real: str) -> list[str]:
    out = set()
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if not isinstance(f, str) or not f:
            continue
        try:
            real = os.path.realpath(f)
        except (OSError, ValueError):
            continue
        if real.startswith(root_real):
            out.add(real)
    return sorted(out)


def _versions() -> dict:
    out = {}
    try:
        from importlib import metadata
    except Exception:  # noqa: BLE001
        return {"fastapi": None, "pydantic": None}
    for name in ("fastapi", "pydantic"):
        try:
            out[name] = metadata.version(name)
        except Exception:  # noqa: BLE001
            out[name] = None
    return out


def main(request_path: str) -> dict:
    started = _CLOCK()
    with open(request_path) as fh:
        request = json.load(fh)
    if RUNNER_DIR not in sys.path:
        sys.path.insert(0, RUNNER_DIR)
    import lg_worker
    out = {"kind": None, "text": None, "sha": None, "bytes": 0, "files": [], "app_file": None, "versions": {},
           "blocks": [], "error": None, "detail": None, "seconds": 0.0}
    sys.stdout = open(os.devnull, "w")  # project prints must not reach the result line
    root_real, _ = lg_worker.prepare(request, with_django=False)
    lg_worker.reset_determinism()
    ref = str(request.get("app") or "")
    try:
        with lg_worker.setup_phase():
            app = lg_worker.load_app(ref)
    except BaseException as exc:  # noqa: BLE001 - the import failed or was refused: not complete
        out.update(error="import", detail=f"{type(exc).__name__}: {exc}"[:400])
        return _finish(out, root_real, ref, started, lg_worker)
    fn = getattr(app, "openapi", None)
    if not callable(fn):
        out["kind"] = "none"
        return _finish(out, root_real, ref, started, lg_worker)
    try:
        with lg_worker.setup_phase():
            spec = fn()
    except BaseException as exc:  # noqa: BLE001
        out.update(error="openapi", detail=f"{type(exc).__name__}: {exc}"[:400])
        return _finish(out, root_real, ref, started, lg_worker)
    if not isinstance(spec, dict) or "openapi" not in spec or "paths" not in spec:
        out.update(kind="none", detail=f"openapi() returned {type(spec).__name__}"
                   + ("" if not isinstance(spec, dict) else " without openapi and paths keys"))
        return _finish(out, root_real, ref, started, lg_worker)
    try:
        with lg_worker.setup_phase():
            text = canonical(spec)
    except (ValueError, TypeError, RecursionError) as exc:
        out.update(error="encode", detail=f"{type(exc).__name__}: {exc}"[:400])
        return _finish(out, root_real, ref, started, lg_worker)
    out.update(kind="fastapi" if "fastapi" in sys.modules else "openapi", text=text,
               sha=hashlib.sha256(text.encode()).hexdigest()[:16], bytes=len(text.encode()))
    return _finish(out, root_real, ref, started, lg_worker)


def _finish(out: dict, root_real: str, ref: str, started: float, lg_worker) -> dict:
    try:
        out["files"] = _files(root_real)
        module = ref.partition(":")[0].strip()
        mod = sys.modules.get(module)
        f = getattr(mod, "__file__", None) if mod is not None else None
        out["app_file"] = os.path.realpath(f) if isinstance(f, str) and f else None
        out["versions"] = _versions()
    except BaseException:  # noqa: BLE001 - the file list only steers when the check runs
        pass
    out["blocks"] = list(lg_worker.BLOCKS)
    out["seconds"] = round(_CLOCK() - started, 3)
    return out


if __name__ == "__main__":
    try:
        result = main(sys.argv[1])
    except BaseException as exc:  # noqa: BLE001 - always print one line the guard can read
        result = {"kind": None, "text": None, "error": "internal", "detail": f"{type(exc).__name__}: {exc}"[:400],
                  "files": [], "blocks": [], "versions": {}}
    sys.__stdout__.write(MARK + json.dumps(result, default=str) + "\n")
    sys.__stdout__.flush()
    try:
        import lg_worker
        lg_worker.drop_scratch()
    except BaseException:  # noqa: BLE001
        pass
