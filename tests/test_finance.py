"""金融网关核心功能单元测试套件 (Finance Gateway Tests)
测试市场代码推导、内置量化指标计算引擎 (MA/MACD/KDJ/BOLL)、交易日历时钟以及网关分发。
"""

import unittest
from mcp_gateway.finance import (
    calculate_indicators,
    get_market_context,
    infer_market_setcode,
    FinanceGateway,
)


class TestFinanceGateway(unittest.TestCase):
    def test_infer_market_setcode(self) -> None:
        """测试不同市场证券代码的自动推导"""
        self.assertEqual(infer_market_setcode("600519"), "1")  # 沪市主板
        self.assertEqual(infer_market_setcode("688318"), "1")  # 科创板
        self.assertEqual(infer_market_setcode("000001"), "0")  # 深市主板
        self.assertEqual(infer_market_setcode("300750"), "0")  # 创业板
        self.assertEqual(infer_market_setcode("835185"), "2")  # 北交所
        self.assertEqual(infer_market_setcode("920001"), "2")  # 北交所920代码
        self.assertEqual(infer_market_setcode("512480"), "1")  # 沪市ETF
        self.assertEqual(infer_market_setcode("159325"), "0")  # 深市ETF

    def test_market_context(self) -> None:
        """测试 A 股交易日历与时钟状态输出"""
        ctx = get_market_context()
        self.assertIn("market_status", ctx)
        self.assertIn("market_status_desc", ctx)
        self.assertIn("is_trading_time", ctx)
        self.assertIn("reference_trading_date", ctx)
        self.assertTrue(len(ctx["reference_trading_date"]) == 10)

    def test_calculate_indicators(self) -> None:
        """测试纯 Python 原生量化指标计算引擎 (MA, MACD, KDJ, BOLL)"""
        # 构造模拟连续 30 根 K 线
        bars = []
        base_price = 10.0
        for i in range(30):
            price = round(base_price + (i % 5) * 0.2 + (i // 10) * 0.5, 2)
            bars.append({
                "time": f"2026-01-{i+1:02d}",
                "open": price - 0.1,
                "high": price + 0.3,
                "low": price - 0.2,
                "close": price,
                "volume": 10000.0,
                "amount": 100000.0,
            })

        enriched = calculate_indicators(bars)
        self.assertEqual(len(enriched), 30)

        # 验证 MA 均线
        self.assertIsNotNone(enriched[4]["ma5"])
        self.assertIsNotNone(enriched[9]["ma10"])
        self.assertIsNotNone(enriched[19]["ma20"])

        # 验证 MACD
        self.assertIn("macd_dif", enriched[-1])
        self.assertIn("macd_dea", enriched[-1])
        self.assertIn("macd_bar", enriched[-1])

        # 验证 KDJ
        self.assertIn("kdj_k", enriched[-1])
        self.assertIn("kdj_d", enriched[-1])
        self.assertIn("kdj_j", enriched[-1])

        # 验证 BOLL
        self.assertIsNotNone(enriched[-1]["boll_mid"])
        self.assertIsNotNone(enriched[-1]["boll_up"])
        self.assertIsNotNone(enriched[-1]["boll_dn"])
        self.assertTrue(enriched[-1]["boll_up"] >= enriched[-1]["boll_dn"])

        # 验证 RSI
        self.assertIn("rsi6", enriched[-1])
        self.assertIn("rsi12", enriched[-1])
        self.assertIsNotNone(enriched[-1]["rsi6"])
        self.assertTrue(0.0 <= enriched[-1]["rsi6"] <= 100.0)

    def test_invalid_symbol_validation(self) -> None:
        """测试对非法或空证券代码的防御性拦截"""
        fin_gw = FinanceGateway()
        with self.assertRaises(ValueError):
            fin_gw.stock_quotes({"symbol": ""})
        with self.assertRaises(ValueError):
            fin_gw.stock_quotes({"symbol": "   "})
        with self.assertRaises(ValueError):
            fin_gw.stock_quotes({"symbol": "INVALID_NO_DIGITS"})

    def test_finance_tools_registration(self) -> None:
        """测试 8 大金融工具定义规范与 Schema 契约完整性"""
        from mcp_gateway.finance import get_finance_tool_specs
        fin_gw = FinanceGateway(tdx_token="test_mock_token")
        specs = get_finance_tool_specs(fin_gw)
        self.assertEqual(len(specs), 8)
        expected_tools = {
            "stock_lookup",
            "stock_quotes",
            "stock_kline",
            "stock_financials",
            "stock_f10",
            "stock_screener",
            "stock_news",
            "market_context",
        }
        self.assertEqual(set(specs.keys()), expected_tools)
        for name, (schema, handler) in specs.items():
            self.assertEqual(schema["name"], name)
            self.assertTrue(callable(handler))
            self.assertIn("description", schema)
            self.assertIn("inputSchema", schema)
            self.assertEqual(schema["inputSchema"]["type"], "object")


if __name__ == "__main__":
    unittest.main()
