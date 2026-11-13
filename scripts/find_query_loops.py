"""Find database calls made once per iteration of a loop - N+1 suspects - in app/**.

    python scripts/find_query_loops.py              # every finding, grouped by file (what tests/test_no_n_plus_one.py checks)
    python scripts/find_query_loops.py --check      # exit code 1 when a finding is not allowlisted or must be re-reviewed
    python scripts/find_query_loops.py --json       # machine-readable findings (with fingerprint, fn_sha, allowlist entry)
    python scripts/find_query_loops.py --direct     # only database calls written inside the loop itself
    python scripts/find_query_loops.py --refresh [FINGERPRINT ...]   # AFTER re-reviewing: re-pin allowlisted loops

A finding is a database call that runs once per iteration of a ``for`` / ``async for`` / ``while`` loop (body or
``while`` condition) or of a comprehension:

* ``direct``: ``db.execute / get / scalar / scalars / stream / ...`` on a session-like name (``db``, ``session``, ``s``,
  ``conn``, ``database``, ``self._db`` ...), on a name bound by ``async with session_scope() as x`` /
  ``get_sessionmaker()() as x`` / ``engine.begin() as x``, or on a parameter annotated ``AsyncSession`` /
  ``AsyncConnection`` - awaited in the loop, or wrapped (``wait_for(db.execute(...))``);
* ``indirect`` (unless ``--direct``): an awaited call that reaches the database one call deeper - a helper handed the
  session (``await notify(db, ...)`` per row), or a function / method / closure of app/** that runs SQL itself or opens
  its own session, resolved through the module's functions, the enclosing function's nested defs (closures capture
  the session), class methods (``self.m(...)``, ``x = Repo(db); x.load(...)``) and ``from app... import`` names,
  transitively;
* ``fan-out``: such a call made per row but NOT awaited in the loop - collected and run together:
  ``asyncio.gather(*(f(x) for x in xs))``, ``gather(*map(f, xs))``, ``[f(x) for x in xs]`` gathered later,
  ``tg.create_task(f(x))`` / ``ensure_future(f(x))`` per row (still one statement per row, only concurrent);
* ``async-for``: an ``async for`` over a query helper inside an outer loop (a new query per outer iteration).

Each finding is either a real N+1 (fix it: one set-based statement, an IN list, a join, a bulk INSERT, notify_bulk,
enqueue_many) or allowlisted in tests/n_plus_one_allowlist.json under ``"loops"``::

    "<file>::<function>::<call start>": {"reason": "why it is bounded / not an N+1", "fn_sha": "<12 hex>"}

Fingerprints carry no line numbers, so unrelated edits elsewhere keep them stable. ``fn_sha`` pins the code the reason
was written for: the enclosing top-level function (docstrings dropped), the functions of its module it calls by name
and the module-level constants either of them reads (``SUPERADMINS_NOTIFIED = 20`` ...). When any of that changes, the
bound that justified the entry may be gone, so the entry must be reviewed again: tests/test_no_n_plus_one.py fails
until ``--refresh`` re-pins it (run it only after checking the loop is still bounded). Entries that match no loop any
more fail too (remove them, or let ``--refresh`` drop them).
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
ALLOWLIST = ROOT / "tests" / "n_plus_one_allowlist.json"

# methods of an AsyncSession / AsyncConnection that send SQL (``session.*`` below adds every method of a certain session)
DB_METHODS = frozenset({
    "execute", "get", "get_one", "scalar", "scalar_one", "scalars", "stream", "stream_scalars", "run_sync", "merge",
    "refresh", "flush", "commit", "rollback", "delete", "exec_driver_sql", "execution_options",
})
# names that hold a session in this codebase by convention (``self._db`` counts as ``db``)
SESSION_NAMES = frozenset({"db", "session", "sess", "s", "conn", "connection", "holder", "dbs", "tx", "db_session",
                           "database", "db_conn", "async_session"})
CERTAIN_NAMES = frozenset({"db", "session", "sess", "db_session", "database"})  # every awaited method is a DB call
# context managers / constructors whose value is a session or connection
SESSION_FACTORIES = frozenset({"session_scope", "get_sessionmaker", "sessionmaker", "AsyncSession", "begin", "connect",
                               "begin_nested", "_session", "request_session", "fresh_session", "new_session"})
SESSION_TYPES = frozenset({"AsyncSession", "AsyncConnection", "Session", "Connection"})
OPENS_SESSION = frozenset({"session_scope", "get_sessionmaker"})
# calls that schedule a coroutine to run concurrently (one per row when called in a loop)
SPAWNERS = frozenset({"create_task", "ensure_future", "start_soon", "run_coroutine_threadsafe"})
FINGERPRINT_CALL_CHARS = 72  # characters of the call text in a fingerprint
FN_SHA_CHARS = 12


@dataclass(frozen=True)
class Finding:
    file: str  # relative to the project root
    line: int
    function: str  # qualified name (Class.method / outer.inner)
    loop: str  # the enclosing loop, e.g. "for row in rows (line 120)"
    call: str  # the call, e.g. "db.execute(select(X).where(...))"
    kind: str  # "direct" | "indirect" | "fan-out" | "async-for"
    fingerprint: str
    fn_sha: str = ""  # the reviewed code (see the module docstring)

    def describe(self, reason: str | None = None) -> str:
        verb = {"fan-out": "per row", "async-for": "async for over"}.get(self.kind, "await")
        out = f"{self.file}:{self.line}  {self.function}  [{self.kind}]\n    in   {self.loop}\n    call {verb} {self.call}"
        if reason:
            out += f"\n    ok   {reason}"
        return out


def _short(node: ast.AST, limit: int = 110) -> str:
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover - unparse handles every node of a parsed file
        text = type(node).__name__
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _root_name(node: ast.AST) -> str | None:
    """``db`` of ``db``; ``self.db`` / ``self._db`` / ``ctx.session`` -> the attribute without its leading ``_`` when it
    is a session-like name; else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and node.attr.lstrip("_") in SESSION_NAMES:
        return node.attr.lstrip("_")
    return None


def _call_name(node: ast.AST) -> str | None:
    """``session_scope`` of ``session_scope()``, ``begin`` of ``engine.begin()``, ``get_sessionmaker`` of
    ``get_sessionmaker()()``."""
    while isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _annotation_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {n.id if isinstance(n, ast.Name) else n.attr for n in ast.walk(node) if isinstance(n, (ast.Name, ast.Attribute))}


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("") if path.is_relative_to(ROOT) else path.with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    a = fn.args
    return [*a.posonlyargs, *a.args, *a.kwonlyargs, *([a.vararg] if a.vararg else []), *([a.kwarg] if a.kwarg else [])]


_OWN: dict[int, tuple[ast.AST, list[ast.AST]]] = {}


def _own_nodes(fn: ast.AST) -> list[ast.AST]:
    """Every node of ``fn``'s body, not descending into nested functions, lambdas and classes (their own code)."""
    hit = _OWN.get(id(fn))
    if hit is not None and hit[0] is fn:
        return hit[1]
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    _OWN[id(fn)] = (fn, out)
    return out


def _strip_docstrings(node: ast.AST) -> ast.AST:
    node = copy.deepcopy(node)
    for n in ast.walk(node):
        body = getattr(n, "body", None)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            n.body = body[1:] or [ast.Pass()]
    return node


# ==================================================================================================== the index
@dataclass
class _Fn:
    key: str  # "module:qualname" (qualname: f, Class.m, outer.<locals>.inner)
    node: ast.FunctionDef | ast.AsyncFunctionDef
    module: str
    parent: str | None  # the enclosing function's key (closures)
    cls: str | None  # "module:Class" of a method
    sessions: set[str]  # names that certainly hold a session here (annotated, with-bound, captured from the parent)
    nested: dict[str, str] = field(default_factory=dict)  # nested def name -> key
    callees: set[str] = field(default_factory=set)
    direct: bool = False

    @property
    def is_async(self) -> bool:
        return isinstance(self.node, ast.AsyncFunctionDef)


class _Index:
    """Which functions, methods and closures of the scanned modules touch the database (directly, through a session
    they open, or through another such function) - to resolve calls inside loops one level deeper."""

    def __init__(self) -> None:
        self.fns: dict[str, _Fn] = {}
        self.imports: dict[str, dict[str, str]] = {}  # module -> local name -> "module:name" or "module"
        self.classes: dict[str, dict[str, str]] = {}  # "module:Class" -> method name -> key
        self.bases: dict[str, list[str]] = {}  # "module:Class" -> base names (same-module classes are followed)
        self.toplevel: dict[str, dict[str, ast.AST]] = {}  # module -> top-level name -> def / constant assignment
        self.touching: set[str] = set()
        self._local: dict[tuple[str, str | None, int], dict[str, str]] = {}

    # ------------------------------------------------------------------ building
    def add_module(self, module: str, tree: ast.Module, *, package: bool = False) -> None:
        names: dict[str, str] = {}
        pkg = module if package else (module.rsplit(".", 1)[0] if "." in module else "")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or node.level):
                base = node.module or ""
                if node.level:  # from . import x / from .mod import y
                    parts = pkg.split(".") if pkg else []
                    parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                    base = ".".join([*parts, *([base] if base else [])])
                for a in node.names:
                    names[a.asname or a.name] = f"{base}:{a.name}"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    names[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        top: dict[str, ast.AST] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names[node.name] = f"{module}:{node.name}"
                top[node.name] = node
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    if isinstance(t, ast.Name):
                        top[t.id] = node
        self.imports[module] = names
        self.toplevel[module] = top
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._add_fn(module, node, node.name, None, None, set())
            elif isinstance(node, ast.ClassDef):
                self._add_class(module, node, node.name)

    def _add_class(self, module: str, cls: ast.ClassDef, qual: str) -> None:
        ckey = f"{module}:{qual}"
        self.classes[ckey] = {}
        self.bases[ckey] = [b.id for b in cls.bases if isinstance(b, ast.Name)]
        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.classes[ckey][item.name] = self._add_fn(module, item, f"{qual}.{item.name}", None, ckey, set())
            elif isinstance(item, ast.ClassDef):
                self._add_class(module, item, f"{qual}.{item.name}")

    def _add_fn(self, module: str, node: ast.FunctionDef | ast.AsyncFunctionDef, qual: str, parent: str | None,
                cls: str | None, inherited: set[str]) -> str:
        key = f"{module}:{qual}"
        sessions = set(inherited) | {a.arg for a in _params(node) if _annotation_names(a.annotation) & SESSION_TYPES}
        for n in _own_nodes(node):
            if isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if isinstance(item.optional_vars, ast.Name) and _call_name(item.context_expr) in SESSION_FACTORIES:
                        sessions.add(item.optional_vars.id)
            elif isinstance(n, ast.Assign) and _call_name(n.value) in SESSION_FACTORIES:
                sessions |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        fn = _Fn(key, node, module, parent, cls, sessions)
        self.fns[key] = fn
        for n in _own_nodes(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn.nested[n.name] = self._add_fn(module, n, f"{qual}.<locals>.{n.name}", key, None, sessions)
        return key

    def _is_session(self, fn: _Fn, node: ast.AST) -> bool:
        name = _root_name(node)
        return name is not None and (name in fn.sessions or name in SESSION_NAMES)

    def finish(self) -> set[str]:
        """Resolve every function's calls, then every function that reaches the database, transitively."""
        for fn in self.fns.values():
            local = self.local_types(fn.module, fn.node, fn.key)
            for n in _own_nodes(fn.node):
                if not isinstance(n, ast.Call):
                    continue
                if _call_name(n) in OPENS_SESSION:
                    fn.direct = True
                if isinstance(n.func, ast.Attribute) and n.func.attr in DB_METHODS and self._is_session(fn, n.func.value):
                    fn.direct = True
                target = self.resolve(fn.module, n.func, fn.key, local)
                if target:
                    fn.callees.add(target)
        touching = {k for k, f in self.fns.items() if f.direct}
        changed = True
        while changed:
            changed = False
            for key, fn in self.fns.items():
                if key not in touching and fn.callees & touching:
                    touching.add(key)
                    changed = True
        self.touching = touching
        return touching

    # ------------------------------------------------------------------ resolving
    def local_types(self, module: str, fn: ast.AST, context: str | None) -> dict[str, str]:
        """``x = Class(...)`` in ``fn`` -> {"x": "module:Class"} (classes of the scanned modules only)."""
        cache_key = (module, context, id(fn))
        if cache_key in self._local:
            return self._local[cache_key]
        out: dict[str, str] = {}
        self._local[cache_key] = out
        for n in _own_nodes(fn):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                target = self._resolve_name(module, n.value.func, context)
                if target in self.classes:
                    out.update({t.id: target for t in n.targets if isinstance(t, ast.Name)})
        return out

    def _resolve_name(self, module: str, func: ast.AST, context: str | None) -> str | None:
        if isinstance(func, ast.Name):
            key = context
            while key is not None:  # the enclosing functions' nested defs first (closures), innermost first
                fn = self.fns.get(key)
                if fn is None:
                    break
                if func.id in fn.nested:
                    return fn.nested[func.id]
                key = fn.parent
            target = self.imports.get(module, {}).get(func.id)
            return target if target and ":" in target else None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            base = self.imports.get(module, {}).get(func.value.id)
            if base is None:
                return None
            if base in self.classes:  # Class.method
                return f"{base}.{func.attr}"
            if ":" in base:  # ``from app.services import shop as shop_service`` -> the module app.services.shop
                base = base.replace(":", ".")
            return f"{base}:{func.attr}"
        return None

    def _method(self, ckey: str, name: str, depth: int = 0) -> str | None:
        if name in self.classes.get(ckey, {}):
            return self.classes[ckey][name]
        module = ckey.split(":", 1)[0]
        for base in self.bases.get(ckey, []) if depth < 5 else []:
            found = self._method(f"{module}:{base}", name, depth + 1)
            if found:
                return found
        return None

    def resolve(self, module: str, func: ast.AST, context: str | None = None,
                local: dict[str, str] | None = None) -> str | None:
        """"module:qualname" of ``f`` / ``mod.f`` / ``self.m`` / ``obj.m`` (``obj = Class(...)``) as seen from the
        function ``context`` of ``module`` (None when unknown)."""
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner = None
            if func.value.id in ("self", "cls") and context is not None:
                key: str | None = context
                while key is not None and owner is None:
                    fn = self.fns.get(key)
                    owner = fn.cls if fn else None
                    key = fn.parent if fn else None
            elif local and func.value.id in local:
                owner = local[func.value.id]
            if owner is not None:
                return self._method(owner, func.attr)
        target = self._resolve_name(module, func, context)
        if target and target in self.classes:  # constructing an object is not a database call
            return None
        if target and target not in self.fns:  # Class.method through an import of the class
            mod, _, qual = target.partition(":")
            if "." in qual:
                cls, _, meth = qual.rpartition(".")
                return self._method(f"{mod}:{cls}", meth) or target
        return target

    # ------------------------------------------------------------------ the reviewed code of a finding
    def unit_sha(self, module: str, unit: ast.AST, cls: str | None = None) -> str:
        """Hash of ``unit`` (a top-level function or a method; docstrings dropped), the module's functions it calls by
        name (and ``self.m`` methods of its class) and the module-level constants either of them reads."""
        top = self.toplevel.get(module, {})
        deps: dict[str, ast.AST] = {}

        def names_in(node: ast.AST) -> set[str]:
            return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

        for name in sorted(names_in(unit)):
            dep = top.get(name)
            if dep is not None and dep is not unit and isinstance(dep, (ast.FunctionDef, ast.AsyncFunctionDef)):
                deps[f"def {name}"] = dep
        if cls is not None:
            methods = {n.attr for n in ast.walk(unit) if isinstance(n, ast.Attribute)
                       and isinstance(n.value, ast.Name) and n.value.id == "self"}
            for name in sorted(methods):
                key = self._method(cls, name)
                if key and key in self.fns and self.fns[key].node is not unit:
                    deps[f"method {name}"] = self.fns[key].node
        for node in [unit, *deps.values()]:
            for name in sorted(names_in(node)):
                dep = top.get(name)
                if isinstance(dep, (ast.Assign, ast.AnnAssign)):
                    deps[f"const {name}"] = dep
        text = ast.unparse(_strip_docstrings(unit)) + "".join(
            f"\n# {k}\n" + ast.unparse(_strip_docstrings(v)) for k, v in sorted(deps.items()))
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:FN_SHA_CHARS]


# ==================================================================================================== the scan
class _Scanner(ast.NodeVisitor):
    def __init__(self, rel: str, indirect: bool, module: str = "", index: _Index | None = None) -> None:
        self.rel = rel
        self.module = module
        self.index = index
        self.touching = index.touching if index is not None else set()
        self.indirect = indirect
        self.findings: list[Finding] = []
        self.scope: list[str] = []  # qualified-name parts (fingerprints: Class.method / outer.inner)
        self.keys: list[str] = []  # the index keys of the enclosing functions (innermost last)
        self.classes: list[str] = []  # the index keys of the enclosing classes
        self.loops: list[str] = []  # enclosing loops of the current function
        self.sessions: list[set[str]] = [set()]  # names certainly bound to a session, per function
        self.not_sessions: list[set[str]] = [set()]  # session-like names bound to something else (an HTTP client ...)
        self.local: list[dict[str, str]] = [{}]  # ``x = Class(...)`` per function
        self.unit: tuple[ast.AST, str | None] | None = None  # the current top-level function (or method) and its class
        self._shas: dict[int, str] = {}

    # ------------------------------------------------------------------ scopes
    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        certain = {a.arg for a in _params(node) if _annotation_names(a.annotation) & SESSION_TYPES}
        if self.keys:
            key = f"{self.keys[-1]}.<locals>.{node.name}"
        elif self.classes:
            key = f"{self.classes[-1]}.{node.name}"
        else:
            key = f"{self.module}:{node.name}"
        saved_unit = self.unit
        if not self.keys:  # a top-level function or a method: the reviewed unit (hashed when it has a finding)
            self.unit = (node, self.classes[-1] if self.classes else None)
        inherited = self.sessions[-1] if self.keys else set()  # closures capture the enclosing function's sessions
        self.scope.append(node.name)
        self.keys.append(key)
        saved_loops, self.loops = self.loops, []
        self.sessions.append(certain | inherited)
        self.not_sessions.append(set())
        self.local.append(self.index.local_types(self.module, node, key) if self.index is not None else {})
        for child in node.body:
            self.visit(child)
        self.local.pop()
        self.not_sessions.pop()
        self.sessions.pop()
        self.loops = saved_loops
        self.keys.pop()
        self.scope.pop()
        self.unit = saved_unit

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self.keys:  # a class inside a function: its methods are not indexed - scan them like functions
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()
            return
        qual = f"{self.classes[-1].split(':', 1)[1]}.{node.name}" if self.classes else node.name
        self.scope.append(node.name)
        self.classes.append(f"{self.module}:{qual}")
        self.generic_visit(node)
        self.classes.pop()
        self.scope.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:  # a lambda body cannot await
        return

    def _with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            if isinstance(item.context_expr, ast.Call):
                item.context_expr._nplus_awaited = True  # type: ignore[attr-defined] # entered here, not fanned out
            self.visit(item.context_expr)
            if isinstance(item.optional_vars, ast.Name):
                name = item.optional_vars.id
                if _call_name(item.context_expr) in SESSION_FACTORIES:
                    self.sessions[-1].add(name)
                    self.not_sessions[-1].discard(name)
                elif name in SESSION_NAMES:
                    self.not_sessions[-1].add(name)
        for child in node.body:
            self.visit(child)

    visit_With = _with
    visit_AsyncWith = _with

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        if _call_name(node.value) in SESSION_FACTORIES:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.sessions[-1].add(t.id)
        for t in node.targets:
            self.visit(t)

    # ------------------------------------------------------------------ loops
    def _loop_label(self, node: ast.AST) -> str:
        if isinstance(node, (ast.For, ast.AsyncFor)):
            kw = "async for" if isinstance(node, ast.AsyncFor) else "for"
            return f"{kw} {_short(node.target, 40)} in {_short(node.iter, 70)} (line {node.lineno})"
        if isinstance(node, ast.While):
            return f"while {_short(node.test, 80)} (line {node.lineno})"
        return f"{type(node).__name__} (line {node.lineno})"

    def _for(self, node: ast.For | ast.AsyncFor) -> None:
        if isinstance(node, ast.AsyncFor) and self.loops and isinstance(node.iter, ast.Call) \
                and self._reaches_db(node.iter, wrapped=True):
            node.iter._nplus_awaited = True  # type: ignore[attr-defined] # reported here, as the loop it is
            self._report(node.iter, node.iter, "async-for")
        self.visit(node.iter)  # evaluated once
        self.visit(node.target)
        self.loops.append(self._loop_label(node))
        for child in node.body:
            self.visit(child)
        self.loops.pop()
        for child in node.orelse:  # runs once
            self.visit(child)

    visit_For = _for
    visit_AsyncFor = _for

    def visit_While(self, node: ast.While) -> None:
        self.loops.append(self._loop_label(node))
        self.visit(node.test)  # evaluated on every iteration
        for child in node.body:
            self.visit(child)
        self.loops.pop()
        for child in node.orelse:
            self.visit(child)

    def _comprehension(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> None:
        first, *rest = node.generators
        self.visit(first.iter)  # evaluated once
        label = f"{type(node).__name__} {_short(node, 90)} (line {node.lineno})"
        self.loops.append(label)
        self.visit(first.target)
        for cond in first.ifs:
            self.visit(cond)
        for gen in rest:
            self.visit(gen)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.loops.pop()

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_GeneratorExp = _comprehension
    visit_DictComp = _comprehension

    # ------------------------------------------------------------------ calls
    def _is_session(self, name: str | None, *, certain: bool) -> bool:
        if name is None or name in self.not_sessions[-1]:
            return False
        if name in self.sessions[-1]:
            return True
        return name in (CERTAIN_NAMES if certain else SESSION_NAMES)

    def _target(self, func: ast.AST) -> str | None:
        if self.index is None:
            return None
        return self.index.resolve(self.module, func, self.keys[-1] if self.keys else None, self.local[-1])

    def _session_method(self, call: ast.Call, *, awaited: bool) -> bool:
        """A database call on a session: awaited, any DB method of a session-like name (or any method of a certain
        session); NOT awaited (wrapped / gathered), a DB method of a certain session only (``s.get(k)`` of a dict is
        not one)."""
        if not isinstance(call.func, ast.Attribute):
            return False
        receiver = _root_name(call.func.value)
        if not awaited:
            return call.func.attr in DB_METHODS and self._is_session(receiver, certain=True)
        return (call.func.attr in DB_METHODS and self._is_session(receiver, certain=False)) or \
            self._is_session(receiver, certain=True)

    def _handed_session(self, call: ast.Call) -> bool:
        args = [*call.args, *(k.value for k in call.keywords)]
        return any(self._is_session(_root_name(a.value if isinstance(a, ast.Starred) else a), certain=False) for a in args)

    def _reaches_db(self, call: ast.Call, *, wrapped: bool) -> bool:
        """``call`` sends SQL when it runs: a session method, a function / method / closure that touches the database,
        or (``wrapped``: a coroutine whose callee is unknown) a call handed the session."""
        if self._session_method(call, awaited=False):
            return True
        if not self.indirect:
            return False
        target = self._target(call.func)
        if target is not None and target in self.touching:
            return True
        fn = self.index.fns.get(target) if (self.index is not None and target) else None
        if fn is not None:
            return fn.is_async and self._handed_session(call)
        return wrapped and self._handed_session(call)

    def _report(self, node: ast.AST, call: ast.Call, kind: str, loop: str | None = None) -> None:
        fn = ".".join(self.scope) or "<module>"
        key = _short(call, 10**6)[:FINGERPRINT_CALL_CHARS]  # the call's start: stable across edits further in
        if self.unit is not None and self.index is not None:
            unit, cls = self.unit
            if id(unit) not in self._shas:
                self._shas[id(unit)] = self.index.unit_sha(self.module, unit, cls)
            sha = self._shas[id(unit)]
        else:  # module-level code: the statement itself
            sha = hashlib.sha1(_short(node, 10**6).encode()).hexdigest()[:FN_SHA_CHARS]
        self.findings.append(Finding(self.rel, getattr(node, "lineno", 0), fn, loop or self.loops[-1], _short(call),
                                     kind, f"{self.rel}::{fn}::{key}", sha))

    def visit_Await(self, node: ast.Await) -> None:
        if isinstance(node.value, ast.Call):
            node.value._nplus_awaited = True  # type: ignore[attr-defined]
        if self.loops and isinstance(node.value, ast.Call):
            call = node.value
            kind = None
            if self._session_method(call, awaited=True):
                kind = "direct"
            if kind is None and self.indirect:
                if self._handed_session(call):
                    kind = "indirect"
                elif self._target(call.func) in self.touching:
                    kind = "indirect"
            if kind is not None:
                self._report(node, call, kind)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func) if isinstance(node.func, (ast.Name, ast.Attribute)) else None
        if name in SPAWNERS:  # create_task(f(x)): f(x) runs concurrently - per row inside a loop
            for a in node.args[:1]:
                if isinstance(a, ast.Call):
                    a._nplus_spawned = True  # type: ignore[attr-defined]
        if self.loops and not getattr(node, "_nplus_awaited", False):
            spawned = getattr(node, "_nplus_spawned", False)
            if self._session_method(node, awaited=False) and node.func.attr != "run_sync":  # type: ignore[union-attr]
                self._report(node, node, "direct")  # wrapped: wait_for(db.execute(...)), gather(db.get(...) ...)
            elif self.indirect and self._reaches_db(node, wrapped=spawned):
                self._report(node, node, "fan-out")
        if self.indirect and name == "map" and len(node.args) >= 2:  # gather(*map(f, xs)): f(x) per element
            f = node.args[0]
            target = self._target(f) if isinstance(f, (ast.Name, ast.Attribute)) else None
            if target is not None and target in self.touching:
                self._report(node, node, "fan-out", loop=f"map over {_short(node.args[1], 70)} (line {node.lineno})")
        self.generic_visit(node)


def _number_duplicates(findings: list[Finding]) -> list[Finding]:
    """Identical calls in one function get ``#2``, ``#3`` ... in source order (the fingerprint stays unique)."""
    seen: Counter[str] = Counter()
    out = []
    for f in sorted(findings, key=lambda f: (f.file, f.line, f.kind, f.call)):
        seen[f.fingerprint] += 1
        if seen[f.fingerprint] > 1:
            f = Finding(f.file, f.line, f.function, f.loop, f.call, f.kind, f"{f.fingerprint}#{seen[f.fingerprint]}",
                        f.fn_sha)
        out.append(f)
    return out


def scan(root: Path = APP, *, indirect: bool = True) -> list[Finding]:
    """Every database call made per iteration of a loop under ``root``."""
    _OWN.clear()
    findings: list[Finding] = []
    trees: list[tuple[Path, str, ast.Module]] = []
    index = _Index()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = _module_name(path)
        index.add_module(module, tree, package=path.name == "__init__.py")
        trees.append((path, module, tree))
    if indirect:
        index.finish()
    for path, module, tree in trees:
        rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()
        scanner = _Scanner(rel, indirect, module, index)
        scanner.visit(tree)
        findings += scanner.findings
    return _number_duplicates(findings)


# ==================================================================================================== the allowlist
def load_allowlist(path: Path = ALLOWLIST) -> dict[str, Any]:
    if not path.exists():
        return {}
    return dict(json.loads(path.read_text(encoding="utf-8")).get("loops", {}))


def reason_of(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("reason") or "")
    return ""


def unexplained(findings: Iterable[Finding], allowed: dict[str, Any]) -> list[Finding]:
    """Findings without an allowlist entry (a reason)."""
    return [f for f in findings if not reason_of(allowed.get(f.fingerprint)).strip()]


def changed(findings: Iterable[Finding], allowed: dict[str, Any]) -> list[Finding]:
    """Allowlisted findings whose reviewed code changed (or was never pinned): the reason must be checked again."""
    out = []
    for f in findings:
        entry = allowed.get(f.fingerprint)
        if reason_of(entry).strip() and (not isinstance(entry, dict) or entry.get("fn_sha") != f.fn_sha):
            out.append(f)
    return out


def stale(findings: Iterable[Finding], allowed: dict[str, Any]) -> list[str]:
    """Allowlist entries that match no loop any more."""
    fps = {f.fingerprint for f in findings}
    return sorted(fp for fp in allowed if fp not in fps)


def refresh(findings: list[Finding], path: Path = ALLOWLIST, only: Iterable[str] = ()) -> list[str]:
    """Re-pin allowlisted loops (``fn_sha`` of today's code) and drop entries that match no loop - run it only AFTER
    checking that each changed loop is still bounded. ``only``: these fingerprints (default: every changed one)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    loops = data.get("loops", {})
    wanted = set(only)
    by_fp = {f.fingerprint: f for f in findings}
    done = []
    for fp in sorted(loops):
        if wanted and fp not in wanted:
            continue
        f = by_fp.get(fp)
        if f is None:
            del loops[fp]
            done.append(f"removed (no such loop any more): {fp}")
            continue
        entry = loops[fp]
        new = {"reason": reason_of(entry), "fn_sha": f.fn_sha}
        if entry != new:
            loops[fp] = new
            done.append(f"re-pinned: {fp}")
    data["loops"] = dict(sorted(loops.items()))
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return done


def _group(findings: list[Finding]) -> Iterator[tuple[str, list[Finding]]]:
    by: dict[str, list[Finding]] = {}
    for f in findings:
        by.setdefault(f.file, []).append(f)
    yield from by.items()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 when a finding is not allowlisted or must be re-reviewed")
    ap.add_argument("--json", action="store_true", help="print the findings as JSON")
    ap.add_argument("--direct", action="store_true", help="only database calls written inside the loop itself")
    ap.add_argument("--root", type=Path, default=APP, help="directory to scan (default: app/)")
    ap.add_argument("--refresh", nargs="*", metavar="FINGERPRINT",
                    help="after re-reviewing: re-pin these allowlisted loops (none given: every changed one) and drop "
                         "entries that match no loop")
    args = ap.parse_args(argv)
    findings = scan(args.root, indirect=not args.direct)
    if args.refresh is not None:
        if args.direct or args.root != APP:
            ap.error("--refresh needs the full scan of app/ (without --direct / --root)")
        for line in refresh(findings, only=args.refresh):
            print(line)
        return 0
    allowed = load_allowlist()
    todo = unexplained(findings, allowed)
    again = changed(findings, allowed)
    if args.json:
        print(json.dumps([{**asdict(f), "allowlisted": allowed.get(f.fingerprint)} for f in findings], indent=2))
    else:
        for file, items in _group(findings):
            print(f"\n{file}")
            for f in items:
                print("  " + f.describe(reason_of(allowed.get(f.fingerprint)) or None).replace("\n", "\n  "))
        print(f"\n{len(findings)} finding(s), {len(findings) - len(todo)} allowlisted, {len(todo)} not explained, "
              f"{len(again)} allowlisted but changed since their review")
        for fp in ([] if args.direct else stale(findings, allowed)):  # --direct leaves the indirect ones out
            print(f"stale allowlist entry (no such loop any more): {fp}")
    if args.check:
        bad = todo or again or (not args.direct and stale(findings, allowed))
        if bad and args.json:
            print(f"{len(todo)} finding(s) not allowlisted, {len(again)} changed since their review", file=sys.stderr)
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
