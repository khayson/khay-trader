"""SPEC section 6.4: the risk layer depends on nothing model-, strategy- or broker-driven."""

import ast
from pathlib import Path

import pytest

RISK = Path(__file__).parent.parent / "src" / "trader" / "risk"
FORBIDDEN = ("trader.strategy", "trader.review", "trader.exec", "trader.backtest", "anthropic")


def imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("path", sorted(RISK.glob("*.py")), ids=lambda p: p.name)
def test_risk_imports_nothing_forbidden(path: Path) -> None:
    bad = [m for m in imported_modules(path) if m.startswith(FORBIDDEN)]
    assert bad == []


def test_risk_modules_exist() -> None:
    assert {p.name for p in RISK.glob("*.py")} >= {"limits.py", "killswitch.py"}
