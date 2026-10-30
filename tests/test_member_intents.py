"""Exercise startup's intent configuration without connecting to Discord."""
import ast
from pathlib import Path

import discord


def test_startup_enables_members_for_safe_council_reconciliation():
    path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    assignments = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Name) and target.id == "intents") or (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "intents"
            ):
                assignments.append(node)
    namespace = {"discord": discord}
    module = ast.Module(body=sorted(assignments, key=lambda node: node.lineno), type_ignores=[])
    exec(compile(module, str(path), "exec"), namespace)
    assert namespace["intents"].members is True
    assert namespace["intents"].message_content is True