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
        self.assertEqual(infer_market_setcode("00700"), "31")   # 港股主板（5位数字）
        self.assertEqual(infer_market_setcode("HK09988"), "31") # 港股带前缀

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

    def test_hk_stock_quotes_and_kline_dispatch(self) -> None:
        """测试港股代码识别与腾讯行情/K线接口解析分发"""
        from unittest.mock import patch, MagicMock
        fin_gw = FinanceGateway()

        # 1. 模拟腾讯港股实时行情响应
        mock_resp_quotes = MagicMock()
        mock_resp_quotes.read.return_value = 'v_r_hk00700="100~腾讯控股~00700~410.40~417.80~413.20~22840013~0~0~410.40~1000~0~0~0~0~0~0~0~0~410.60~2000~0~0~0~0~0~0~0~0~0~2026/10/20 16:08:24~-7.40~-1.77~415.60~406.40~00700/22840013/9371898153~9371898153~0.24~14.99~~415.60~406.40~2.20~37316.21~37316.21~0.00~0.00~658.70~406.40~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~0.00~HKD";\n'.encode("gb18030")
        mock_resp_quotes.__enter__.return_value = mock_resp_quotes

        with patch("mcp_gateway.finance._SECURE_OPENER.open", return_value=mock_resp_quotes):
            q_res = fin_gw.stock_quotes({"symbol": "00700"})
            self.assertEqual(q_res["symbol"], "00700")
            self.assertEqual(q_res["name"], "腾讯控股")
            self.assertEqual(q_res["setcode"], "31")
            self.assertEqual(q_res["盘口核心行情"]["现价"], 410.4)
            self.assertEqual(q_res["盘口核心行情"]["涨跌幅_pct"], -1.77)
            self.assertEqual(q_res["盘口核心行情"]["结算货币"], "HKD")
            self.assertEqual(q_res["五档深度盘口"]["买一"]["价格"], 410.4)
            self.assertEqual(q_res["五档深度盘口"]["卖一"]["价格"], 410.6)

        # 2. 模拟腾讯港股多周期K线响应
        mock_kline_json = (
            'kline_dayqfq={"code":0,"msg":"","data":{"hk00700":{"qt":{"hk00700":["100","腾讯控股"]},'
            '"qfqday":[["2026-10-17","410.0","412.0","415.0","408.0","100000"],'
            '["2026-10-20","413.2","410.4","415.6","406.4","200000"]]}}}'
        )
        mock_resp_kline = MagicMock()
        mock_resp_kline.read.return_value = mock_kline_json.encode("utf-8")
        mock_resp_kline.__enter__.return_value = mock_resp_kline

        with patch("mcp_gateway.finance._SECURE_OPENER.open", return_value=mock_resp_kline):
            k_res = fin_gw.stock_kline({"symbol": "HK00700", "count": 2})
            self.assertEqual(k_res["symbol"], "00700")
            self.assertEqual(k_res["name"], "腾讯控股")
            self.assertEqual(k_res["setcode"], "31")
            self.assertEqual(k_res["total_bars"], 2)
            self.assertEqual(k_res["klines"][-1]["close"], 410.4)
            self.assertIn("ma5", k_res["klines"][-1])

    def test_f10_summary_enrichment_loss_and_profit(self) -> None:
        """测试 F10 摘要表防幻觉增强：隔日风格标签时效警示、破净动态复核与亏损公司同比变动方向判定"""
        fin_gw = FinanceGateway()

        # 1. 模拟亏损标的 (如 600271: 归母净利润 < 0, 同比增减率 -24.78%)
        loss_tables = {
            "table3": [{"T060": None, "T063": "破净股"}],
            "table4": [{
                "mgsy": -0.2,
                "mgjzc": 6.9427,
                "gmjlr": -376941479.96,
                "gmjlrtbzzl": -24.7834,
                "kfjlrtbzzl": -15.5923,
            }],
            "table6": [{"sjl": 0.9694, "sylttm": -15.2}],
            "table8": [{"name": "航天信息", "gxrq": "2026-10-20"}],
        }
        tdx_res_loss = {"symbol": "600271", "category": "summary", "formatted_tables": loss_tables}
        fin_gw._enrich_f10_summary_tables(loss_tables, tdx_res_loss)

        # 验证亏损标的同比方向判定
        t4_loss = loss_tables["table4"][0]
        self.assertEqual(t4_loss["归母净利润_元"], -376941479.96)
        self.assertEqual(t4_loss["每股净资产_BPS"], 6.9427)
        self.assertIn("亏损扩大 24.78%", t4_loss["归母净利润变动方向判定"])
        self.assertIn("亏损标的同比解读警示", t4_loss)
        self.assertIn("两负数相除切勿误读为盈利增长", t4_loss["亏损标的同比解读警示"])

        # 验证隔日风格标签与破净动态复核指引
        t3_loss = loss_tables["table3"][0]
        self.assertEqual(t3_loss["有效风格标签"], ["破净股"])
        self.assertIn("隔日状态与时效警示", t3_loss)
        self.assertIn("破净动态复核指引", t3_loss)
        self.assertIn("6.9427", t3_loss["破净动态复核指引"])

        # 验证估值表与根级风险提示
        self.assertIn("估值时效提示", loss_tables["table6"][0])
        self.assertIn("f10_audit_notice", tdx_res_loss)

        # 2. 模拟盈利标的 (如 300371: 归母净利润 > 0, 同比增减率 -61.98%)
        profit_tables = {
            "table3": [{"T060": None, "T063": None}],
            "table4": [{
                "mgsy": 0.079,
                "mgjzc": 5.7525,
                "gmjlr": 15799658.14,
                "gmjlrtbzzl": -61.9816,
                "kfjlrtbzzl": -68.3834,
            }],
            "table6": [{"sjl": 1.6549, "sylttm": 47.3551}],
            "table8": [{"name": "汇中股份", "gxrq": "2026-10-20"}],
        }
        tdx_res_profit = {"symbol": "300371", "category": "summary", "formatted_tables": profit_tables}
        fin_gw._enrich_f10_summary_tables(profit_tables, tdx_res_profit)

        # 验证盈利标的正常判定为“下滑”，且不触发亏损警示
        t4_profit = profit_tables["table4"][0]
        self.assertIn("盈利同比下滑 61.98%", t4_profit["归母净利润变动方向判定"])
        self.assertNotIn("亏损标的同比解读警示", t4_profit)

    def test_stock_lookup_pure_code_pinning(self) -> None:
        """测试纯数字证券代码正主保全与首位置顶逻辑（解决基金/债券代码碰撞挤占股票正主缺陷）"""
        fin_gw = FinanceGateway()

        # 1. 模拟 002698：RAG 仅返回基金“博时裕利纯债债券A”，A股正主“博实股份”被漏掉
        mock_rag_response_002698 = {
            "retrieved_entities": [
                {"entity_code": "002698", "entity_name": "博时裕利纯债债券A", "entity_type": "基金代码", "entity_setcode": 33},
                {"entity_code": "002667", "entity_name": "前海沪港深创新C", "entity_type": "基金代码", "entity_setcode": 33},
            ]
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_rag_response_002698):
            with unittest.mock.patch.object(
                fin_gw.tdx_client,
                "_resolve_exact_security_entity",
                return_value={
                    "entity_code": "002698",
                    "entity_name": "博实股份",
                    "entity_type": "A股代码",
                    "entity_setcode": 0,
                    "aliases": ["博实股份"],
                },
            ):
                res = fin_gw.stock_lookup({"query": "002698"})
                entities = res.get("entities", [])
                self.assertGreaterEqual(len(entities), 2)
                # 验证正主“博实股份”被精确补齐并置于第 0 位
                self.assertEqual(entities[0]["entity_code"], "002698")
                self.assertEqual(entities[0]["entity_name"], "博实股份")
                self.assertEqual(entities[0]["entity_type"], "A股代码")
                # 验证原基金实体保留在第 1 位
                self.assertEqual(entities[1]["entity_name"], "博时裕利纯债债券A")

        # 2. 模拟 002594：RAG 返回比亚迪，但排在同代码基金后面（第 1 位）
        mock_rag_response_002594 = {
            "retrieved_entities": [
                {"entity_code": "002594", "entity_name": "工银现代服务业混合A", "entity_type": "基金代码", "entity_setcode": 33},
                {"entity_code": "002594", "entity_name": "比亚迪", "entity_type": "A股代码", "entity_setcode": 0},
            ]
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_rag_response_002594):
            with unittest.mock.patch.object(
                fin_gw.tdx_client,
                "_resolve_exact_security_entity",
                return_value={
                    "entity_code": "002594",
                    "entity_name": "比亚迪",
                    "entity_type": "A股代码",
                    "entity_setcode": 0,
                    "aliases": ["比亚迪"],
                },
            ):
                res_byd = fin_gw.stock_lookup({"query": "002594"})
                entities_byd = res_byd.get("entities", [])
                # 验证“比亚迪”被自动上浮并置于第 0 位
                self.assertEqual(entities_byd[0]["entity_name"], "比亚迪")
                self.assertEqual(entities_byd[0]["entity_type"], "A股代码")
                self.assertEqual(entities_byd[1]["entity_name"], "工银现代服务业混合A")

    def test_stock_quotes_pettm(self) -> None:
        """测试 stock_quotes 从 ProInfo 中提取真实 PETTM 与 PEStatic"""
        fin_gw = FinanceGateway()
        mock_raw = {
            "HQInfo": {"Now": 1309.69, "Close": 1331.88, "MaxP": 1331.98, "MinP": 1302.36},
            "ExtInfo": {"SYL": 18.3887463, "MGJZC": 200.99, "MGSY": 35.57},
            "ProInfo": {"PETTM": 20.1048641, "PEStatic": 19.8884583},
            "BspInfo": [{"BuyP": 1309.69, "BuyV": "10"}, {}, {}, {}, {}],
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_raw):
            res = fin_gw.stock_quotes({"symbol": "600519"})
            ext = res.get("估值与基本盘面", {})
            self.assertEqual(ext.get("滚动市盈率_PETTM"), 20.10)
            self.assertEqual(ext.get("静态市盈率_PEStatic"), 19.89)
            self.assertEqual(ext.get("动态市盈率_PE"), 18.39)
            self.assertIn("滚动市盈率_PETTM", ext.get("估值口径说明", ""))
            self.assertIn("盘口深度说明", res)

    def test_f10_margin_and_flow_column_mapping(self) -> None:
        """测试 F10 margin_trading、capital_flow 及 events 的列名汉化与排序"""
        fin_gw = FinanceGateway()
        # 1. 模拟 margin_trading
        mock_margin = {
            "ResultSets": [{
                "ResultSetKey": "table0",
                "ColName": ["T001", "T003", "T004", "ClosePrice"],
                "Content": [["20261020", "16634514788", "266624827", 1331.88]]
            }]
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "get_f10", return_value={"data": mock_margin}):
            res_m = fin_gw.stock_f10({"symbol": "600519", "category": "margin_trading"})
            row0 = res_m.get("formatted_tables", {}).get("table0", [])[0]
            self.assertIn("交易日期", row0)
            self.assertIn("融资余额_元", row0)
            self.assertIn("融资买入额_元", row0)
            self.assertIn("收盘价", row0)

        # 2. 模拟 capital_flow
        mock_flow = {
            "ResultSets": [{
                "ResultSetKey": "table0",
                "ColName": ["rq", "N001", "N002", "N015"],
                "Content": [["2026-10-20", 29867648, 0.66, 1331.88]]
            }]
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "get_f10", return_value={"data": mock_flow}):
            res_cf = fin_gw.stock_f10({"symbol": "600519", "category": "capital_flow"})
            row0_cf = res_cf.get("formatted_tables", {}).get("table0", [])[0]
            self.assertIn("交易日期", row0_cf)
            self.assertIn("主力净流入_元", row0_cf)
            self.assertIn("主力净占比_pct", row0_cf)

        # 3. 模拟 events
        mock_events = {
            "ResultSets": [{
                "ResultSetKey": "table0",
                "ColName": ["cjrq", "sjmc", "sjxz", "cjrzdf"],
                "Content": [
                    ["20160413", "历史旧事件", "利好", -1.09],
                    ["20260901", "最新里程碑", "中性", 0.003],
                ]
            }]
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "get_f10", return_value={"data": mock_events}):
            res_ev = fin_gw.stock_f10({"symbol": "600519", "category": "events"})
            rows_ev = res_ev.get("formatted_tables", {}).get("table0", [])
            # 验证倒序：最新事件在第 0 行
            self.assertEqual(rows_ev[0]["事件名称"], "最新里程碑")
            self.assertEqual(rows_ev[1]["事件名称"], "历史旧事件")
            self.assertIn("事件库属性说明", res_ev)

    def test_balance_sheet_field_alignment(self) -> None:
        """测试资产负债表科目的精准映射（应付票据与预收款项无重叠错位）"""
        from mcp_gateway.finance import BALANCE_FIELDS_NON_FINANCE
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N043"], "应付票据及应付账款合计")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N044"], "应付票据")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N045"], "应付账款")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N046"], "预收款项")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N047"], "合同负债")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N057"], "流动负债合计")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N066"], "非流动负债合计")


if __name__ == "__main__":
    unittest.main()
