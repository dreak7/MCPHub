"""从实测 JSONL 生成「结构指纹」基线，用于重构前后的字段/表格回归对比。

只提取结构（键集合、表格列集合、行数），不保存数值，也不联网。
用法：
  python scripts/build_golden_fingerprints.py --jsonl <dir> [<dir> ...] --out tests/golden/fingerprints.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def shape(value: Any, depth: int = 2) -> Any:
    """把任意 JSON 值压缩成结构描述：字典取键、字典列表取列集合与行数。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in sorted(value.items()):
            if isinstance(v, list):
                if v and isinstance(v[0], dict):
                    cols = sorted({c for row in v if isinstance(row, dict) for c in row})
                    out[k] = {"list_of_dict": len(v), "columns": cols}
                else:
                    out[k] = {"list": len(v)}
            elif isinstance(v, dict):
                out[k] = shape(v, depth - 1) if depth > 0 else {"dict_keys": sorted(v)}
            else:
                out[k] = type(v).__name__
        return out
    return type(value).__name__


def case_key(tool: str, args: dict[str, Any]) -> str:
    return f"{tool}|{json.dumps(args, ensure_ascii=False, sort_keys=True)}"


def collect(jsonl_dirs: list[str]) -> dict[str, Any]:
    cases: dict[str, Any] = {}
    for d in jsonl_dirs:
        for f in sorted(Path(d).glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not rec.get("ok"):
                    continue
                tool = rec.get("tool") or ""
                args = rec.get("args") or {}
                key = case_key(tool, args)
                if key in cases:
                    continue
                cases[key] = {
                    "tool": tool,
                    "args": args,
                    "shape": shape(rec.get("payload")),
                }
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", nargs="+", required=True, help="探测结果目录（含 *.jsonl）")
    ap.add_argument("--out", default="tests/golden/fingerprints.json")
    args = ap.parse_args()

    cases = collect(args.jsonl)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"count": len(cases), "source": args.jsonl, "cases": cases}, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    print(f"fingerprints -> {out}  ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
