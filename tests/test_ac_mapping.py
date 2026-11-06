"""acceptance.md의 AC와 테스트 함수가 1:1인지 확인한다. AC 자체는 아니고 대응 관계를 지키는 검사다."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROW = re.compile(r"^\| AC-([A-Z]+)-(\d+) \| \S+ \| .+ \| (단위|배포|평가) \| `(test_\w+)` \|$", re.M)


def test_every_ac_has_exactly_one_test():
    rows = ROW.findall((ROOT / "design" / "acceptance.md").read_text(encoding="utf-8"))
    assert len(rows) == 62

    expected = {}
    for area, _number, _kind, name in rows:
        expected.setdefault(f"test_ac_{area.lower()}.py", []).append(name)

    actual = {}
    for path in sorted((ROOT / "tests").glob("test_ac_*.py")):
        if path.name == "test_ac_mapping.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        actual[path.name] = [
            node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]

    assert {name: sorted(tests) for name, tests in actual.items()} == {
        name: sorted(tests) for name, tests in expected.items()
    }
