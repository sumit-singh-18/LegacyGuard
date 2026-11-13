"""Who calls a function, resolved through imports instead of matched by name (roadmap B7).

The names table says only that some code calls "something named foo". That gives false callers when two functions
share a name, and misses `from pkg.mod import foo as bar; bar()`. Here each candidate file's imports are read, and a
call counts only when it resolves to the edited definition:

- `foo()` where foo is defined in the same module, or imported from the edited module (any alias, relative or not)
- `m.foo()` where m is the edited module (`import pkg.mod as m`, `from pkg import mod`)
- `self.foo()` / `cls.foo()` inside the edited method's own class, and `Klass.foo()` for the edited class
- a bare name match only when exactly one function in the project has that name ("name" edges, kept separate)

In the spirit of PyCG (precision first), but scoped to the question the guard asks after an edit.
"""
from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_CANDIDATE_FILES = 200


@dataclass(frozen=True)
class Edge:
    file: str
    qualname: str     # the caller
    callee: str       # the edited function's qualname
    how: str          # "resolved" or "name"
    hops: int = 1


def module_name(rel: str, root: str) -> str:
    from runner.diff import module_for
    return module_for(rel, root)[0]


def _candidates(root: str, name: str) -> list[str]:
    """Python files that mention the name as a word. git grep is fast and respects .gitignore."""
    try:
        res = subprocess.run(["git", "-C", root, "grep", "-l", "-w", "-e", name, "--", "*.py"], capture_output=True, text=True, timeout=20)
        files = res.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        files = []
    if not files:  # not a git repo, or untracked files only
        files = [p.relative_to(root).as_posix() for p in Path(root).rglob("*.py")
                 if ".legacyguard" not in p.parts and ".venv" not in p.parts and name in p.read_text(errors="replace")]
    return files[:MAX_CANDIDATE_FILES]


def _resolve_relative(module: str, is_package: bool, level: int, target: str | None) -> str:
    parts = module.split(".")
    base = parts if is_package else parts[:-1]
    base = base[: len(base) - (level - 1)] if level > 1 else base
    return ".".join([*base, *([target] if target else [])])


def _imports(tree: ast.AST, this_module: str, is_package: bool) -> tuple[dict, dict]:
    """(names, modules): local name -> (module, attribute) for `from m import a as b`; local name -> module for
    `import a.b as c` and `from pkg import mod`. Modules are absolute dotted names."""
    names: dict[str, tuple[str, str]] = {}
    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    modules[a.asname] = a.name
                else:
                    modules[a.name.split(".")[0]] = a.name.split(".")[0]
                    modules[a.name] = a.name  # `import pkg.mod` then pkg.mod.foo(): matched on the full dotted text
        elif isinstance(node, ast.ImportFrom):
            source = _resolve_relative(this_module, is_package, node.level, node.module) if node.level else (node.module or "")
            for a in node.names:
                local = a.asname or a.name
                names[local] = (source, a.name)
                modules[local] = f"{source}.{a.name}" if source else a.name  # `from pkg import mod` makes mod a module
    return names, modules


def _dotted(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else None
    return None


class _Scope(ast.NodeVisitor):
    """Calls inside each function or method, with the qualname and class of the function they are in."""

    def __init__(self):
        self.stack: list[tuple[str, str]] = []  # (kind, name)
        self.calls: list[tuple[str, str | None, ast.Call]] = []

    def _qual(self) -> str:
        return ".".join(n for _, n in self.stack)

    def _cls(self) -> str | None:
        classes = [n for k, n in self.stack if k == "class"]
        return ".".join(n for _, n in self.stack[: max(i for i, (k, _) in enumerate(self.stack) if k == "class") + 1]) if classes else None

    def visit_ClassDef(self, node):
        self.stack.append(("class", node.name))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self.stack.append(("def", node.name))
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _runnable(self) -> str:
        """Classes plus the first function: a call in a nested function is a call by the function the guard can run."""
        out = []
        for k, n in self.stack:
            out.append(n)
            if k == "def":
                break
        return ".".join(out)

    def visit_Call(self, node):
        if any(k == "def" for k, _ in self.stack):
            self.calls.append((self._runnable(), self._cls(), node))
        self.generic_visit(node)


def callers_of(root: str, file: str, qualname: str, unique_names: set[str] | None = None) -> list[Edge]:
    """Direct callers of the definition `qualname` in `file`, one hop."""
    name = qualname.rsplit(".", 1)[-1]
    target_cls = qualname.rsplit(".", 1)[0] if "." in qualname else None
    target_cls_name = target_cls.rsplit(".", 1)[-1] if target_cls else None
    target_module = module_name(file, root)
    out: set[Edge] = set()
    for rel in _candidates(root, name):
        try:
            tree = ast.parse((Path(root) / rel).read_text(errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        this_module = module_name(rel, root)
        names, modules = _imports(tree, this_module, rel.endswith("__init__.py"))
        scope = _Scope()
        scope.visit(tree)
        for caller, caller_cls, call in scope.calls:
            if rel == file and caller == qualname:
                continue  # recursion is not a caller
            f = call.func
            how = None
            if isinstance(f, ast.Name) and f.id in (name, *[k for k, v in names.items() if v == (target_module, name)]):
                if target_cls is None and rel == file and f.id == name:
                    how = "resolved"                        # same module, plain function
                elif target_cls is None and names.get(f.id) == (target_module, name):
                    how = "resolved"                        # from the edited module, possibly aliased
            elif isinstance(f, ast.Attribute) and f.attr == name:
                head = _dotted(f.value)
                if head in ("self", "cls") and target_cls is not None and rel == file and caller_cls == target_cls:
                    how = "resolved"                        # self.method() in the method's own class
                elif head is not None and target_cls is None and modules.get(head) == target_module:
                    how = "resolved"                        # module.function()
                elif head is not None and head == target_module:
                    how = "resolved"                        # pkg.mod.function() after `import pkg.mod`
                elif head is not None and target_cls_name is not None and (
                        (rel == file and head == target_cls) or names.get(head) == (target_module, target_cls_name)):
                    how = "resolved"                        # Klass.method()
            if how is None and unique_names is not None and name in unique_names:
                if (isinstance(f, ast.Name) and f.id == name) or (isinstance(f, ast.Attribute) and f.attr == name):
                    how = "name"
            if how:
                out.add(Edge(rel, caller, qualname, how))
    return sorted(out, key=lambda e: (e.how != "resolved", e.file != file, e.file, e.qualname))


def unique_function_names(conn) -> set[str]:
    """Function names defined exactly once in the project, from the index: a bare name match on those is safe enough."""
    if conn is None:
        return set()
    try:
        rows = conn.execute("SELECT name FROM symbols WHERE kind != 'class' GROUP BY name HAVING COUNT(*) = 1").fetchall()
    except Exception:  # noqa: BLE001
        return set()
    return {r[0] for r in rows}


def callers(root: str, file: str, qualname: str, conn=None, hops: int = 2, limit: int = 30) -> list[Edge]:
    """Callers up to `hops` steps away, nearest first. A caller of a caller can return something different too."""
    unique = unique_function_names(conn)
    seen = {(file, qualname)}
    frontier = [(file, qualname)]
    out: list[Edge] = []
    for hop in range(1, hops + 1):
        nxt = []
        for f, q in frontier:
            for e in callers_of(root, f, q, unique):
                if (e.file, e.qualname) in seen:
                    continue
                seen.add((e.file, e.qualname))
                out.append(Edge(e.file, e.qualname, e.callee, e.how, hop))
                nxt.append((e.file, e.qualname))
                if len(out) >= limit:
                    return out
        frontier = nxt
    return out
