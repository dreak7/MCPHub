"""结构指纹回归对比：重构/修复前后，检测字段与表格结构的漂移。

只比结构（键、列、是否存在），不比数值；行数变化记为 WARN 而不是失败。
用法：
  python scripts/golden_check.py --baseline tests/golden/fingerprints.json --jsonl <dir> [<dir> ...]
  python scripts/golden_check.py --baseline tests/golden/fingerprints.json --current <new.json>
退出码：结构漂移=1，仅有行数变化或全一致=0。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_golden_fingerprints import collect  # noqa: E402


def diff_shape(base: Any, cur: Any, path: str, errors: list[str], warns: list[str]) -> None:
    if isinstance(base, dict) and isinstance(cur, dict):
        base_keys, cur_keys = set(base), set(cur)
        for k in sorted(base_keys - cur_keys):
            errors.append(f"{path}.{k}: 基線存在、当前缺失")
        for k in sorted(cur_keys - base_keys):
            errors.append(f"{path}.{k}: 当前新增、基线未覆盖（确认是否预期）")
        for k in sorted(base_keys & cur_keys):
            diff_shape(base[k], cur[k], f"{path}.{k}", errors, warns)
        return

    if isinstance(base, dict) and "list_of_dict" in base and isinstance(cur, dict) and "list_of_dict" in cur:
        b_cols, c_cols = set(base.get("columns") or []), set(cur.get("columns") or [])
        for c in sorted(b_cols - c_cols):
            errors.append(f"{path}: 列缺失 {c}")
        for c in sorted(c_cols - b_cols):
            errors.append(f"{path}: 新增列 {c}（确认兼容性）")
        if base.get("list_of_dict") != cur.get("list_of_dict"):
            warns.append(f"{path}: 行数 {base.get('list_of_dict')} -> {cur.get('list_of_dict')}")
        return

    if type(base) is not type(cur):
        errors.append(f"{path}: 类型 {type(base).__name__} -> {type(cur).__name__}")


def load_cases(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("cases", data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--jsonl", nargs="+", default=None)
    ap.add_argument("--current", default=None)
    args = ap.parse_args()

    baseline = load_cases(Path(args.baseline))
    if args.current:
        current = load_cases(Path(args.current))
    elif args.jsonl:
        current = collect(args.jsonl)
    else:
        print("需要 --jsonl 或 --current 之一")
        return 2

    errors: list[str] = []
    warns: list[str] = []
    missing = sorted(set(baseline) - set(current))
    extra = sorted(set(current) - set(baseline))
    for k in missing:
        errors.append(f"用例缺失: {k}")
    for k in extra:
        errors.append(f"用例新增: {k}")
    for k in sorted(set(baseline) & set(current)):
        diff_shape(baseline[k].get("shape"), current[k].get("shape"), k.split("|")[0], errors, warns)

    print(f"cases: baseline={len(baseline)} current={len(current)}")
    for w in warns[:40]:
        print(f"  WARN {w}")
    for e in errors[:80]:
        print(f"  FAIL {e}")
    print(f"结构漂移 {len(errors)} 项，行数变化 {len(warns)} 项")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
