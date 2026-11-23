"""Contract and test-integrity checks: pure functions comparing a file before and after (FR-12 step 4, FR-17)."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from guardlib import contracts_parse as cp
from guardlib import route_decorators, symbols
from guardlib.symbol_diff import diff as symbol_diff

DEPENDENCY_DEFAULT = re.compile(r"\b(Depends|Security|BackgroundTasks)\b")


@dataclass
class Break:
    kind: str       # http, db, messages, test
    item: str       # stable identity of what broke
    subject: str    # short description for people
    source: str     # schema, routes, tests
    contract: str   # file that defines the contract

    def text(self) -> str:
        return f"{self.subject} ({self.contract})"


def is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return name.endswith(".py") and ("tests" in parts[:-1] or "test" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py"))


def is_under(rel: str, folder: str | None) -> bool:
    return bool(folder) and (rel == folder.rstrip("/") or rel.startswith(folder.rstrip("/") + "/"))


def _compare(before: dict[str, str], after: dict[str, str]) -> tuple[list[str], list[tuple[str, str, str]]]:
    removed = sorted(set(before) - set(after))
    retyped = sorted((k, before[k], after[k]) for k in set(before) & set(after) if before[k] != after[k])
    return removed, retyped


def _http_breaks(rel: str, a: dict[str, str], b: dict[str, str]) -> list[Break]:
    removed, retyped = _compare(a, b)
    out = []
    for item in removed:
        label, _, rest = item.partition(":")
        word = {"route": "route removed", "param": "parameter removed or renamed", "field": "response field dropped"}[label]
        out.append(Break("http", item, f"{word}: {rest}", "schema", rel))
    out += [Break("http", f"{i}|retype", f"field retyped: {i.split(':', 1)[1]} {x} to {y}", "schema", rel) for i, x, y in retyped if not i.startswith("route:")]
    return out


TOOL_NOTE_KIND = {"missing": "oasdiff", "refused": "oasdiff-refused", "failed": "oasdiff-failed", "timeout": "oasdiff-timeout"}
SLOW_NOTE = ("LegacyGuard: oasdiff took longer than {t} s here twice in a row; the pre-edit check now uses the built-in "
             "parser, and the Stop audit still runs oasdiff.")


def _oasdiff_breaks(rel: str, before: str, after: str, tools, specs) -> list[Break] | None:
    """Breaks from oasdiff, or None to keep the parser's (with a note in tools.notes saying why)."""
    from guardlib import contract_tools
    if not before.strip():
        return None  # a new contract file: nothing to diff, no note
    if not tools.allow_oasdiff:
        tools.notes.append(("oasdiff-slow", SLOW_NOTE.format(t=tools.timeout)))
        return None
    res = contract_tools.oasdiff_breaking(tools.cfg, rel, before, after, tools.trees, tools.timeout, specs=specs, pre_edit=tools.pre_edit)
    if isinstance(res, contract_tools.Refusal):
        if res.kind in TOOL_NOTE_KIND and res.reason:
            tools.notes.append((TOOL_NOTE_KIND[res.kind], "LegacyGuard: " + res.reason))
        return None
    out = []
    for change in res.errs:
        item, subject = contract_tools.break_fields(rel, change)
        out.append(Break("http", item, subject, "oasdiff", rel))
    if res.warns:
        n = len(res.warns)
        texts = "; ".join(w.get("text") or w.get("id") or "?" for w in res.warns[:3])
        tools.notes.append((None, f"LegacyGuard: oasdiff also reports {n} possible breaking change{'s' if n != 1 else ''} it cannot confirm "
                                  f"in {rel}: {texts}."))
    return out


def check_http_file(rel: str, before: str, after: str, tools=None) -> list[Break]:
    """OpenAPI contract changes. tools (contract_tools.ToolContext) lets oasdiff replace the parser's result for this
    file; any failure of the tool path falls back to the parser with a note. tools=None: the parser alone."""
    if tools is None:
        try:
            a, b = cp.http_items(before), cp.http_items(after)
        except Exception:  # noqa: BLE001 - a broken YAML edit: Claude's next step will reveal it; never block on our parse
            return []
        return _http_breaks(rel, a, b)
    try:
        import yaml
        a_spec, b_spec = yaml.safe_load(before) or {}, yaml.safe_load(after) or {}
        a, b = cp.http_items_from(a_spec), cp.http_items_from(b_spec)
    except Exception:  # noqa: BLE001 - as above; the tool is not run on a spec the parser cannot read either
        return []
    parsed = _http_breaks(rel, a, b)
    try:
        found = _oasdiff_breaks(rel, before, after, tools, (a_spec, b_spec))
    except Exception as exc:  # noqa: BLE001 - hooks fail open: the parser's result stands
        tools.notes.append(("oasdiff-failed", f"LegacyGuard: oasdiff step failed: {type(exc).__name__}; the built-in parser was used."))
        return parsed
    return parsed if found is None else found


def check_migration(rel: str, folder_scripts_before: list[str], folder_scripts_after: list[str], before: str, after: str) -> list[Break]:
    out: dict[str, Break] = {}
    new_statements = {k: v for k, v in cp.dangerous_statements(after).items() if k not in cp.dangerous_statements(before)}
    for item, label in new_statements.items():
        out[f"stmt:{item}"] = Break("db", f"stmt:{item}", f"{label}: {item.split(':', 1)[1]} in a migration", "schema", rel)
    a, b = cp.sql_schema(folder_scripts_before), cp.sql_schema(folder_scripts_after)
    if a is not None and b is not None:
        removed, retyped = _compare(a, b)
        for item in removed:
            col = item.split(":", 1)[1]
            if not any(col.split(".")[-1] == k.split(":", 1)[1] for k in new_statements):
                out[item] = Break("db", item, f"column dropped: {col}", "schema", rel)
        for item, x, y in retyped:
            out[f"{item}|retype"] = Break("db", f"{item}|retype", f"column retyped: {item.split(':', 1)[1]} {x} to {y}", "schema", rel)
    return list(out.values())


def check_event_schema(rel: str, before: str, after: str) -> list[Break]:
    stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    removed, retyped = _compare(cp.event_items(stem, before), cp.event_items(stem, after))
    out = [Break("messages", i, f"event field removed: {i.split(':', 1)[1]}", "schema", rel) for i in removed]
    out += [Break("messages", f"{i}|retype", f"event field retyped: {i.split(':', 1)[1]} {x} to {y}", "schema", rel) for i, x, y in retyped]
    return out


def _routes_of(defs) -> dict[str, tuple[str, str]]:
    """{route id: (handler qualname, path)} from decorators."""
    out = {}
    for s in defs:
        for dec in s.decorators:
            for owner, method, path in route_decorators.parse(dec):
                out[f"{owner} {method} {path}"] = (s.qualname, path)
    return out


def _class_fields(sym: symbols.Symbol) -> dict[str, str]:
    """Annotated fields of a class, from its own body."""
    root = symbols.parse(sym.node_text).root_node
    cls = next((c for c in root.children if c.type == "class_definition"), None)
    fields: dict[str, str] = {}
    if cls is None:
        return fields
    body = cls.child_by_field_name("body")
    for stmt in body.children if body is not None else []:
        if stmt.type == "expression_statement" and stmt.named_children and stmt.named_children[0].type == "assignment":
            asg = stmt.named_children[0]
            left, ty = asg.child_by_field_name("left"), asg.child_by_field_name("type")
            if left is not None and left.type == "identifier" and ty is not None:
                fields[symbols.text(left)] = symbols.text(ty)
    return fields


def _dict_keys(source: str) -> set[str]:
    out: set[str] = set()

    def walk(node):
        if node.type == "pair":
            k = node.child_by_field_name("key")
            if k is not None and k.type == "string":
                out.add(symbols.text(k).strip("\"'"))
        for c in node.children:
            walk(c)
    walk(symbols.parse(source).root_node)
    return out


def check_python_source(rel: str, before: str, after: str, http: dict[str, str] | None, contract_file: str | None) -> list[Break]:
    """Route, handler-parameter and response-model changes visible in Python."""
    out: list[Break] = []
    a_defs, _ = symbols.extract(before)
    b_defs, _ = symbols.extract(after)
    a_by = {s.qualname: s for s in a_defs}
    b_by = {s.qualname: s for s in b_defs}
    a_routes, b_routes = _routes_of(a_defs), _routes_of(b_defs)
    contract = contract_file or rel
    for rid, (qual, path) in a_routes.items():
        if rid not in b_routes:
            out.append(Break("http", f"route:{rid}", f"route removed: {rid.split(' ', 1)[1]} (handler {qual})", "routes", contract))
            continue
        new_sym = b_by.get(b_routes[rid][0])
        old_sym = a_by.get(qual)
        if old_sym is None or new_sym is None:
            continue
        old_params = {p["name"] for p in old_sym.params if not (p["default"] and DEPENDENCY_DEFAULT.search(p["default"]))}
        new_params = {p["name"] for p in new_sym.params}
        for gone in sorted(old_params - new_params):
            out.append(Break("http", f"param:{rid}:{gone}", f"parameter removed or renamed: {gone} on {rid.split(' ', 1)[1]}", "routes", contract))
    http = http or {}
    schemas = cp.schema_names(http)
    for q, s in a_by.items():
        if s.kind == "class" and s.name in schemas:
            t = b_by.get(q)
            if t is None:
                out.append(Break("http", f"schema:{s.name}", f"response schema removed: {s.name}", "schema", contract))
                continue
            old_f, new_f = _class_fields(s), _class_fields(t)
            published = {k.split(".", 1)[1] for k in http if k.startswith(f"field:{s.name}.")}
            for field in sorted((set(old_f) - set(new_f)) & published):
                out.append(Break("http", f"field:{s.name}.{field}", f"response field dropped: {s.name}.{field}", "schema", contract))
            for field in sorted((set(old_f) & set(new_f)) & published):
                if old_f[field] != new_f[field]:
                    out.append(Break("http", f"field:{s.name}.{field}|retype", f"response field retyped: {s.name}.{field} {old_f[field]} to {new_f[field]}", "schema", contract))
    if a_routes:  # a module of route handlers: dict keys it returns are response fields
        names = cp.response_field_names(http)
        for q, s in a_by.items():
            t = b_by.get(q)
            if s.kind == "class" or t is None:
                continue
            for key in sorted((_dict_keys(s.node_text) - _dict_keys(t.node_text)) & names):
                out.append(Break("http", f"key:{q}.{key}", f"response key removed: {key} in {q}", "routes", contract))
    return out


_ASSERT_METHOD = re.compile(r"^(self\.assert\w+|pytest\.raises|pytest\.approx|self\.fail)\b")


def _test_functions(source: str) -> dict[str, symbols.Symbol]:
    defs, _ = symbols.extract(source)
    return {d.qualname: d for d in defs if d.kind != "class" and d.name.startswith("test")}


def _assertions(sym: symbols.Symbol) -> list[str]:
    found: list[str] = []

    def walk(node):
        if node.type == "assert_statement":
            found.append(" ".join(symbols.text(node).split()))
            return
        if node.type == "call":
            fn = node.child_by_field_name("function")
            if fn is not None and _ASSERT_METHOD.match(symbols.text(fn)):
                found.append(" ".join(symbols.text(node).split()))
                return
        for c in node.children:
            walk(c)
    walk(symbols.parse(sym.node_text).root_node)
    return found


def _is_strong(assertion: str) -> bool:
    return bool(re.search(r"==|!=|<=|>=|<|>|\bin\b|\bis\b|approx|raises|assert[A-Z]", assertion))


_DISABLED = re.compile(r"pytest\.mark\.(skip|xfail)|unittest\.skip")


def check_test_file(rel: str, before: str | None, after: str | None) -> list[Break]:
    a, b = _test_functions(before or ""), _test_functions(after or "")
    out: list[Break] = []
    for q, s in a.items():
        t = b.get(q)
        if t is None:
            out.append(Break("test", f"deleted:{q}", f"test deleted: {q}", "tests", rel))
            continue
        old, new = Counter(_assertions(s)), Counter(_assertions(t))
        lost = list((old - new).elements())
        gained = list((new - old).elements())
        if not any(_DISABLED.search(d) for d in s.decorators) and any(_DISABLED.search(d) for d in t.decorators):
            out.append(Break("test", f"disabled:{q}", f"test disabled: {q}", "tests", rel))
        if sum(old.values()) > sum(new.values()):
            out.append(Break("test", f"assertion_removed:{q}", f"assertion removed from {q}: {lost[0][:80] if lost else ''}", "tests", rel))
        else:
            strong_lost = [x for x in lost if _is_strong(x)]
            weak_gained = [x for x in gained if not _is_strong(x)]
            if strong_lost and weak_gained:
                out.append(Break("test", f"assertion_weakened:{q}", f"assertion weakened in {q}: {strong_lost[0][:60]} became {weak_gained[0][:40]}", "tests", rel))
    return out


def run_checks(cfg, rel: str, before: str | None, after: str, http: dict[str, str] | None, read_migrations=None,
               trees=None, notes=None, tool_timeout=None, allow_oasdiff=True, pre_edit=False) -> list[Break]:
    """All checks that apply to one file change. trees (base tree, revision tree) turns on contract tools (oasdiff) for
    the OpenAPI contract file; their notes go to `notes` as (kind, text) pairs (contract_tools.take_notes)."""
    before_text = before or ""
    if rel == cfg.contract_path("http"):
        tools = None
        if trees is not None:
            try:
                from guardlib import contract_tools
                timeout = tool_timeout if tool_timeout is not None else contract_tools.settings(cfg)["timeout_s"]
                tools = contract_tools.ToolContext(cfg, tuple(trees), float(timeout), notes if notes is not None else [],
                                                   allow_oasdiff, pre_edit)
            except Exception:  # noqa: BLE001 - e.g. a timeout in the config that is not a number: the parser alone
                tools = None
        return check_http_file(rel, before_text, after, tools)
    db_dir = cfg.contract_path("db")
    if is_under(rel, db_dir) or ("/migrations/" in rel and rel.endswith(".py")):  # Django keeps a migrations folder per app
        scripts_before, scripts_after = read_migrations(rel, before_text, after) if read_migrations else ([], [])
        return check_migration(rel, scripts_before, scripts_after, before_text, after)
    if is_under(rel, cfg.contract_path("messages")) and rel.endswith(".json"):
        return check_event_schema(rel, before_text, after)
    if is_test_path(rel):
        return check_test_file(rel, before, after)
    if rel.endswith(".py"):
        return check_python_source(rel, before_text, after, http, cfg.contract_path("http"))
    return []
