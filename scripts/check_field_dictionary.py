"""字段字典一致性校验（7.4）：文档/实现里的关键字段是否还在源码里。

字典按市场分表（tests/field_contract.json：a_share / hk 各一份，A 股与港股互不覆盖），
这里只做「字面量存在性」检查——能抓住改名/删字段导致的文档漂移，抓不到数值错误。
用法：python scripts/check_field_dictionary.py   （缺失时退出码 1）
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "tests" / "field_contract.json"


def check(root: Path | str | None = None) -> list[str]:
    base = Path(root) if root else ROOT
    contract = json.loads((base / "tests" / "field_contract.json").read_text(encoding="utf-8"))
    missing: list[str] = []
    for market, files in contract.items():
        if market.startswith("_"):
            continue
        for rel_path, fields in files.items():
            src = base / rel_path
            if not src.exists():
                missing.append(f"[{market}] 源文件缺失: {rel_path}")
                continue
            text = src.read_text(encoding="utf-8", errors="replace")
            for field in fields:
                if field not in text:
                    missing.append(f"[{market}] {rel_path} 缺少字段: {field}")
    return missing


def main() -> int:
    missing = check()
    if missing:
        for item in missing:
            print(f"FAIL {item}")
        print(f"字段字典校验失败：{len(missing)} 项")
        return 1
    print("字段字典校验通过：A 股 / 港股 两份字典字段全部存在于对应源文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
