#!/usr/bin/env python3
"""Verify every call made on a UnifiPlayMqttClient resolves to a real member.

Why this exists
---------------
v1.2.0 shipped unusable. ``coordinator._start_mqtt`` called
``client.request_equalizer()`` and ``client.request_sub_audio()`` after both
methods had been removed in an unrelated refactor, so connecting raised
AttributeError, the error path discarded the client, and every device was left
permanently without state (#13). Nothing in the pipeline could have caught it:
flake8's ``E9,F63,F7,F82`` selection finds undefined *names*, never a missing
*attribute* on an object it cannot type.

This is the guard invited on #13 - a static check that the client's callers and
the client itself cannot drift apart.

How it decides what is a client
-------------------------------
Name heuristics are not good enough. ``client`` is also an aiohttp session in
api.py and the paho module in discovery.py, and matching on the name alone
reports both as errors.

Instead the producers are discovered from their own return annotations: any
function or method annotated ``-> UnifiPlayMqttClient`` (optionally Optional)
produces one. None is named here, so a new accessor is covered the moment it
is annotated; the run prints the set it found, and an accessor left
unannotated is the one gap - this docstring is the warning for it.

A local variable is treated as a client when it is assigned from such a call or
straight from the ``UnifiPlayMqttClient`` constructor. Calls chained directly
onto a producer (``self._require_mqtt().set_volume(5)``) are checked too.

Exits non-zero and lists every offending call site.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

CLASS_NAME = "UnifiPlayMqttClient"
PACKAGE = Path(__file__).resolve().parent.parent / "custom_components" / "unifi_play"

if sys.version_info < (3, 12):
    # __init__.py uses the 3.12+ `type X = Y` statement. Without this the run
    # dies on a SyntaxError pointing at a perfectly valid file, which reads as
    # "the integration is broken" rather than "your interpreter is old".
    sys.exit(
        f"needs Python 3.12+ to parse the package (running "
        f"{sys.version_info.major}.{sys.version_info.minor})"
    )


def _annotation_names(node: ast.AST | None) -> set[str]:
    """Every bare name mentioned in an annotation expression."""
    if node is None:
        return set()
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def collect_members(tree: ast.Module) -> set[str]:
    """Public surface of the client: its methods, properties and attributes."""
    members: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == CLASS_NAME):
            continue
        for item in ast.walk(node):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                members.add(item.name)
            # Attributes set on self, so client.some_attr is not a false hit.
            elif isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Store):
                if isinstance(item.value, ast.Name) and item.value.id == "self":
                    members.add(item.attr)
    return members


def collect_producers(trees: dict[Path, ast.Module]) -> set[str]:
    """Names of functions whose return annotation is a client."""
    producers: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if CLASS_NAME in _annotation_names(node.returns):
                    producers.add(node.name)
    return producers


def _is_producer_call(node: ast.AST, producers: set[str]) -> bool:
    """True for ``producer(...)``, ``obj.producer(...)`` or ``Client(...)``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in producers or func.id == CLASS_NAME
    if isinstance(func, ast.Attribute):
        return func.attr in producers
    return False


def check_file(
    path: Path, tree: ast.Module, members: set[str], producers: set[str]
) -> list[tuple[int, str, str]]:
    """Return (line, variable, attribute) for each unresolved client call."""
    bad: list[tuple[int, str, str]] = []

    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            continue

        # Variables in this scope that hold a client.
        client_vars: set[str] = set()
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign) and _is_producer_call(
                node.value, producers
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        client_vars.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if CLASS_NAME in _annotation_names(node.annotation):
                    client_vars.add(node.target.id)
            # Walrus, as used by "if client := self._mqtt():"
            elif isinstance(node, ast.NamedExpr) and _is_producer_call(
                node.value, producers
            ):
                if isinstance(node.target, ast.Name):
                    client_vars.add(node.target.id)

        for node in ast.walk(scope):
            if not isinstance(node, ast.Attribute):
                continue
            owner = node.value
            # client.foo
            if isinstance(owner, ast.Name) and owner.id in client_vars:
                label = owner.id
            # self._require_mqtt().foo
            elif _is_producer_call(owner, producers):
                label = ast.unparse(owner)
            else:
                continue
            if node.attr not in members:
                bad.append((node.lineno, label, node.attr))

    return sorted(set(bad))


def main() -> int:
    trees: dict[Path, ast.Module] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        try:
            trees[path] = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as err:
            print(f"{path}: could not parse: {err}", file=sys.stderr)
            return 2

    client_module = PACKAGE / "mqtt_client.py"
    if client_module not in trees:
        print(f"{client_module} not found", file=sys.stderr)
        return 2

    members = collect_members(trees[client_module])
    if not members:
        print(f"no {CLASS_NAME} class found in {client_module}", file=sys.stderr)
        return 2
    producers = collect_producers(trees)

    failures: list[str] = []
    checked = 0
    for path, tree in trees.items():
        if path == client_module:
            continue
        for lineno, var, attr in check_file(path, tree, members, producers):
            failures.append(
                f"{path.relative_to(PACKAGE.parent.parent)}:{lineno}: "
                f"{var}.{attr} is not a member of {CLASS_NAME}"
            )
        checked += 1

    # Flushed, so the summary precedes any failures written to stderr.
    print(
        f"{CLASS_NAME}: {len(members)} members; "
        f"{len(producers)} accessors ({', '.join(sorted(producers))}); "
        f"{checked} modules checked",
        flush=True,
    )
    if failures:
        print("\nUnresolved client calls:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        print(
            f"\n{len(failures)} unresolved call(s). This is the v1.2.0 failure "
            "mode: the device connects, raises AttributeError, and is left "
            "with no client at all.",
            file=sys.stderr,
        )
        return 1
    print("All client calls resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
