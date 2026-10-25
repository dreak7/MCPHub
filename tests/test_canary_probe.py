# -*- coding: utf-8 -*-
"""
单元测试模块：28 核心标的金丝雀巡检机制测试套件 (Canary Probe Test Suite)

检验 A 股与港股标的池的物理隔离规则、跨市场逆向拒信断言以及四象限金融框架属性定义。
"""

from __future__ import annotations
import unittest
from typing import Dict, Any

from scripts.canary_probe import A_SHARE_CANARY_POOL, HK_SHARE_CANARY_POOL
from mcp_gateway.engines.a_stock import AStockEngine
from mcp_gateway.engines.hk_stock import HKStockEngine


# ==============================================================================
# 功能块：金丝雀巡检机制单元测试用例类
# 【输入】：测试套件初始化环境
# 【功能】：1. 校验 A 股金丝雀池 16 只标的规格（6位代码、行业全覆盖、金融/非金融标签）；
#          2. 校验 港股金丝雀池 14 只标的规格（5位代码、行业全覆盖、金融/非金融标签）；
#          3. 验证 A 股引擎对港股标的的严格物理隔离拒信断言；
#          4. 验证 港股引擎对 A 股标的的严格物理隔离拒信断言。
# 【输出】：测试通过 (OK)
# ==============================================================================
class TestCanaryProbeSuite(unittest.TestCase):
    """金丝雀巡检与隔离规则测试套件。"""

    def setUp(self) -> None:
        """初始化测试引擎实例。"""
        self.a_engine = AStockEngine()
        self.hk_engine = HKStockEngine()

    def test_a_share_pool_specifications(self) -> None:
        """验证 A 股金丝雀池 16 只标的代码格式与金融/工商业准则覆盖"""
        self.assertEqual(len(A_SHARE_CANARY_POOL), 16, "A 股金丝雀池标的数必须为 16 只")
        for stock in A_SHARE_CANARY_POOL:
            sym = stock["symbol"]
            self.assertTrue(sym.isdigit() and len(sym) == 6, f"A 股代码格式不合规: {sym}")
            self.assertIn("is_financial", stock, f"标的 {sym} 缺失 is_financial 标记")
            self.assertIn("sector", stock, f"标的 {sym} 缺失 sector 行业属性")

        fin_stocks = [s for s in A_SHARE_CANARY_POOL if s["is_financial"]]
        ind_stocks = [s for s in A_SHARE_CANARY_POOL if not s["is_financial"]]
        self.assertEqual(len(fin_stocks), 3, "A 股金融标的必须包含 3 只（邮储银行/中信证券/新华保险）")
        self.assertEqual(len(ind_stocks), 13, "A 股工商业标的必须包含 13 只")

    def test_hk_share_pool_specifications(self) -> None:
        """验证 港股金丝雀池 14 只标的代码格式与金融/工商业准则覆盖"""
        self.assertEqual(len(HK_SHARE_CANARY_POOL), 14, "港股金丝雀池标的数必须为 14 只")
        for stock in HK_SHARE_CANARY_POOL:
            sym = stock["symbol"]
            self.assertTrue(sym.isdigit() and len(sym) == 5, f"港股代码格式不合规: {sym}")
            self.assertIn("is_financial", stock, f"标的 {sym} 缺失 is_financial 标记")
            self.assertIn("sector", stock, f"标的 {sym} 缺失 sector 行业属性")

        fin_stocks = [s for s in HK_SHARE_CANARY_POOL if s["is_financial"]]
        ind_stocks = [s for s in HK_SHARE_CANARY_POOL if not s["is_financial"]]
        self.assertEqual(len(fin_stocks), 4, "港股金融标的必须包含 4 只（中国人寿/中金公司/建行/中行）")
        self.assertEqual(len(ind_stocks), 10, "港股工商业标的必须包含 10 只")

    def test_a_engine_rejects_hk_stock(self) -> None:
        """验证 A 股引擎对港股标的的严格物理隔离逆向拒信断言"""
        hk_symbol = "00700"
        with self.assertRaises(ValueError) as cm:
            self.a_engine.get_financials(hk_symbol, report_type="balance")
        self.assertIn("港股", str(cm.exception), "A 股引擎未正确拦截港股代码")

    def test_hk_engine_rejects_a_stock(self) -> None:
        """验证 港股引擎对 A 股标的的严格物理隔离逆向拒信断言"""
        a_symbol = "600519"
        with self.assertRaises(ValueError) as cm:
            self.hk_engine.get_quotes(a_symbol)
        self.assertTrue(
            "港股" in str(cm.exception) or "未找到" in str(cm.exception),
            "港股引擎未正确拦截 A 股代码",
        )


if __name__ == "__main__":
    unittest.main()
