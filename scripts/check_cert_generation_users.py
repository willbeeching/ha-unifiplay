#!/usr/bin/env python3
"""Verify nothing configures mTLS outside the certificate-generation fallback.

Why this exists
---------------
v1.3.5 added a per-generation fallback to ``UnifiPlayMqttClient.connect`` after
PowerAmp firmware 1.0.41 rotated the MQTT certificate authority (#20). It
missed ``discovery.py``, which builds its own paho client for the
identification probe and hardcoded the 2023 pair.

The consequence was worse than the original bug. An Audio Port on firmware
1.1.12 or later - the Port platform crossed the same threshold that 1.0.41
crossed for amps - could not be set up **at all**: discovery failed before a
device existed, so the working fallback in the coordinator was never reached
(#24). And the failure is silent in the same way: under TLS 1.3 the device
verifies the client certificate after the handshake, so the connect succeeds,
no exception is raised, and the only symptom is a probe that sees nothing.

So: assert statically that every ``tls_set`` call in the package sits in a
function that also consults the bundled generations. A new call site that
hardcodes one pair fails here rather than in a user's setup dialog.
"""

from __future__ import annotations

import ast
import pathlib
import sys

PKG = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "unifi_play"
)
#: A function calling tls_set must reference one of these, so that the
#: credentials it offers come from the generation list rather than a constant.
GENERATION_NAMES = {"bundled_generations", "CERT_GENERATIONS", "generation"}


def _enclosing_function(tree: ast.Module, target: ast.AST) -> ast.FunctionDef | None:
    """The innermost function definition containing target."""
    best: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(child is target for child in ast.walk(node)):
                if best is None or node.lineno > best.lineno:
                    best = node  # type: ignore[assignment]
    return best


def main() -> int:
    failures: list[str] = []
    checked = 0

    for path in sorted(PKG.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as err:
            print(f"{path}: could not parse: {err}", file=sys.stderr)
            return 2

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Attribute) and node.func.attr == "tls_set"
            ):
                continue
            checked += 1
            fn = _enclosing_function(tree, node)
            if fn is None:
                failures.append(f"{path.name}:{node.lineno} tls_set at module level")
                continue
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            names |= {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
            names |= {a.arg for a in fn.args.args}
            if not (names & GENERATION_NAMES):
                failures.append(
                    f"{path.name}:{node.lineno} {fn.name}() calls tls_set without "
                    "consulting the bundled certificate generations"
                )

    if not checked:
        print("error: found no tls_set calls - has the guard drifted?", file=sys.stderr)
        return 2

    if failures:
        for line in failures:
            print(f"error: {line}", file=sys.stderr)
        print(
            "       a hardcoded certificate pair cannot reach a device whose CA\n"
            "       has rotated, and the failure is silent under TLS 1.3.\n"
            "       see mqtt_client.bundled_generations and issues #20, #24",
            file=sys.stderr,
        )
        return 1

    print(f"ok: all {checked} tls_set call site(s) use the bundled generations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
