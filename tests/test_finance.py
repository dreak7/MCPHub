"""金融网关核心功能单元测试套件 (Finance Gateway Tests)
测试市场代码推导、内置技术指标计算引擎 (MA/MACD/KDJ/BOLL)、交易日历时钟以及网关分发。
"""

import datetime
import json
import re
import unittest
import unittest.mock
from mcp_gateway.finance import (
    calculate_indicators,
    get_market_context,
    infer_market_setcode,
    FinanceGateway,
)


class TestFinanceGateway(unittest.TestCase):
    def setUp(self) -> None:
        self.gw = FinanceGateway()

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
        """测试纯 Python 原生技术指标计算引擎 (MA, MACD, KDJ, BOLL)"""
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
        """测试资产负债表科目的精准映射（流动负债明细与所有者权益无重叠错位）"""
        from mcp_gateway.finance import BALANCE_FIELDS_NON_FINANCE
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N042"], "衍生金融负债")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N043"], "应付票据及应付账款合计")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N044"], "应付票据")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N045"], "应付账款")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N046"], "预收款项")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N047"], "应付职工薪酬")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N048"], "应交税费")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N051"], "其他应付款合计")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N052"], "一年内到期的非流动负债")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N055"], "合同负债")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N056"], "其他流动负债")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N057"], "流动负债合计")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N058"], "长期借款")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N066"], "非流动负债合计")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N067"], "负债合计")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N068"], "实收资本_股本")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N069"], "资本公积")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N070"], "盈余公积")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N076"], "未分配利润")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N077"], "所有者权益合计")
        self.assertEqual(BALANCE_FIELDS_NON_FINANCE["N080"], "负债和所有者权益总计")

    def test_hk_quotes_market_cap_alignment(self) -> None:
        """测试港股行情总市值与流通市值的正确映射（parts[45]为总市值，parts[44]为流通市值）"""
        fin_gw = FinanceGateway()
        # 模拟腾讯港股原始报文 (02357 中航科工: 总市值244.77亿，流通市值190.67亿)
        raw_parts = [""] * 80
        raw_parts[0] = 'v_r_hk02357="100'
        raw_parts[1] = "中航科工"
        raw_parts[2] = "02357"
        raw_parts[3] = "3.070"
        raw_parts[44] = "190.6673"  # 流通市值 (H股市值)
        raw_parts[45] = "244.7666"  # 总市值
        mock_raw = "~".join(raw_parts) + '";'

        with unittest.mock.patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.read.return_value = mock_raw.encode("gbk")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            res = fin_gw.stock_quotes({"symbol": "02357"})
            valuation = res.get("估值与基本盘面", {})
            self.assertEqual(valuation.get("总市值_亿港元"), 244.77)
            self.assertEqual(valuation.get("流通市值_亿港元"), 190.67)
            self.assertGreaterEqual(valuation.get("总市值_亿港元"), valuation.get("流通市值_亿港元"))

    def test_hk_financials_and_f10_supported(self) -> None:
        """测试港股代码调用 financials 与 f10 成功接入新浪主力与东财备份数据源"""
        fin_gw = FinanceGateway()
        # 1. 港股财报三张表 (支持利润表、资产负债表等)
        res_fin = fin_gw.stock_financials({"symbol": "02357", "report_type": "income", "limit_periods": 2})
        self.assertEqual(res_fin.get("market"), "香港交易所 (HKEX)")
        self.assertEqual(res_fin.get("symbol"), "02357")
        self.assertIn("source", res_fin)
        self.assertIsInstance(res_fin.get("statements"), list)

        # 2. 港股 F10 (公司概况与工商)
        res_f10 = fin_gw.stock_f10({"symbol": "02357", "category": "summary"})
        self.assertEqual(res_f10.get("market"), "香港交易所 (HKEX)")
        self.assertEqual(res_f10.get("symbol"), "02357")
        self.assertIn("source", res_f10)

    def test_hk_kline_unadjusted_support(self) -> None:
        """测试港股 K 线支持 adjust='none' 不复权模式并正确路由"""
        fin_gw = FinanceGateway()
        mock_json = json.dumps({
            "code": 0,
            "data": {
                "hk02357": {
                    "day": [
                        ["2026-10-20", "3.000", "3.050", "3.100", "2.980", "5000000.0"],
                        ["2026-10-21", "3.080", "3.070", "3.125", "3.045", "10314000.0"],
                    ]
                }
            }
        })
        mock_raw = f"kline_day={mock_json}"
        with unittest.mock.patch("urllib.request.OpenerDirector.open") as mock_open:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.read.return_value = mock_raw.encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            res = fin_gw.stock_kline({"symbol": "02357", "period": "day", "adjust": "none", "count": 2})
            self.assertEqual(res.get("adjust"), "none")
            self.assertEqual(res.get("total_bars"), 2)
            self.assertEqual(res.get("klines")[-1]["close"], 3.07)

    def test_stock_news_noise_filtering(self) -> None:
        """测试个股新闻资讯智能过滤双色球/彩票等非金融噪音"""
        fin_gw = FinanceGateway()
        mock_sina_json = json.dumps({
            "data": {
                "list": [
                    {"title": "120期双色球预测奖号：02357命中红球", "intro": "体彩开奖预测...", "media": "彩票网", "dataTime": "2026-10-20"},
                    {"title": "新华保险增持中航科工(02357)3100万股", "intro": "新华保险完成增持...", "media": "新浪财经", "dataTime": "2026-10-21"},
                ]
            }
        })

        def fake_urlopen(req, *args, **kwargs):
            url_str = req.full_url if hasattr(req, "full_url") else str(req)
            m = unittest.mock.MagicMock()
            if "gtimg" in url_str:
                m.read.return_value = 'v_r_hk02357="100~中航科工~02357~";'.encode("gbk")
            else:
                m.read.return_value = mock_sina_json.encode("utf-8")
            m.__enter__.return_value = m
            return m

        with unittest.mock.patch("mcp_gateway.finance.urlopen", side_effect=fake_urlopen):
            res = fin_gw.stock_news({"symbol": "02357", "limit": 5})
            news_items = res.get("news", [])
            # 验证噪音已过滤，仅保留新华保险增持
            self.assertEqual(len(news_items), 1)
            self.assertIn("新华保险增持中航科工", news_items[0]["title"])

    def test_market_context_post_trading_reference_date(self) -> None:
        """测试交易日收盘后 (15:00后) reference_trading_date 准确指向当日，而非前一日"""
        import datetime
        from mcp_gateway.finance import get_market_context
        # 模拟工作日周三 17:00
        mock_cst = datetime.datetime(2026, 10, 21, 17, 0, 0, tzinfo=datetime.timezone.utc)
        with unittest.mock.patch("datetime.datetime") as mock_dt:
            mock_dt.now.return_value = mock_cst - datetime.timedelta(hours=8)
            mock_dt.timezone = datetime.timezone
            mock_dt.timedelta = datetime.timedelta
            ctx = get_market_context()
            self.assertEqual(ctx["market_status"], "closed_today")
            self.assertEqual(ctx["reference_trading_date"], "2026-10-21")

    def test_financials_type_consistency_and_n_isolation(self) -> None:
        """测试财报字段数值类型一致性（防负数转str与int漂移）及现金流量表补充资料物理隔离"""
        fin_gw = FinanceGateway()
        mock_raw_cw = {
            "ErrorCode": 0,
            "ResultSets": [
                {"ColName": ["isjr"], "Content": [[0]]},
                {
                    "ColName": ["N001", "N011", "N035", "N038", "N054", "N063", "Tdate"],
                    "Content": [
                        ["2026中报", "70690750119.06", "-375742600", "46033330566.78", "70690750119.06", "58385486034.9", "2026-06-30"]
                    ],
                },
            ],
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_raw_cw):
            res = fin_gw.stock_financials({"symbol": "600519", "report_type": "cashflow", "limit_periods": 1})
            stmt = res["statements"][0]
            # 验证负数字符串已统一转为 float，无类型漂移
            self.assertIsInstance(stmt["现金及现金等价物净增加额"], float)
            self.assertEqual(stmt["现金及现金等价物净增加额"], -375742600.0)
            # 验证主表中无任何未映射的 N 码泄露
            raw_n_keys = [k for k in stmt.keys() if k.startswith("N0")]
            self.assertEqual(len(raw_n_keys), 0)
            # 验证补充资料已独立收纳于二级字典，主表中不包含 N054 等重复科目，杜绝双计
            self.assertIn("补充资料_间接法", stmt)
            self.assertEqual(stmt["补充资料_间接法"]["补充资料_经营活动现金流量净额"], 70690750119.06)

    def test_kline_float32_price_normalization(self) -> None:
        """测试 K 线价格 float32 舍入归一化（彻底消除 1309.689941 等精度漂移）"""
        fin_gw = FinanceGateway()
        mock_pbfxt = {
            "AttachInfo": {"Name": "贵州茅台"},
            "ListItem": [
                {"Item": ["20261021", "1500", "1331.880005", "1331.979980", "1302.359985", "1309.689941", "3500000000.12", "20000", "20000"]},
            ],
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_pbfxt):
            res = fin_gw.stock_kline({"symbol": "600519", "period": "day", "count": 1, "with_indicators": False})
            bar = res["klines"][0]
            self.assertEqual(bar["open"], 1331.88)
            self.assertEqual(bar["high"], 1331.98)
            self.assertEqual(bar["low"], 1302.36)
            self.assertEqual(bar["close"], 1309.69)

    def test_limit_up_empty_records_annotation(self) -> None:
        """测试涨停分析 limit_up 无记录时保留官方数据源并注入明确的无记录说明"""
        fin_gw = FinanceGateway()
        mock_empty_limit_up = {
            "ErrorCode": 0,
            "ResultSets": [
                {
                    "Count": 0,
                    "ResultSetKey": "table0",
                    "ColName": ["currq", "lx", "sT002", "T011", "fdje", "zt", "yy", "N005"],
                    "Content": [],
                }
            ],
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_empty_limit_up):
            res = fin_gw.stock_f10({"symbol": "600519", "category": "limit_up"})
            self.assertEqual(res["source"], "tdx_official_vip (通达信官方授权专线)")
            self.assertIn("无记录说明", res)
            self.assertIn("无涨停或异动记录", res["无记录说明"])

    def test_hk_stock_financials_dual_source(self) -> None:
        """测试港股财务报表双源获取：新浪主力解析与东财备份切换"""
        fin_gw = FinanceGateway()
        # 1. 模拟新浪港股利润表 HTML 响应
        mock_sina_html = """
        <html><body>
        <table class="tab05"><tr><td>指标</td></tr></table>
        <table class="tab05"><tr><td>资产负债</td></tr></table>
        <table class="tab05"><tr><td>现金流</td></tr></table>
        <table class="tab05">
            <tr><td>截止日</td><td>2026-06-30</td><td>2025-12-31</td></tr>
            <tr><td>报告期</td><td>中报</td><td>年报</td></tr>
            <tr><td>营业额</td><td>58,911.89</td><td>106,955.10</td></tr>
            <tr><td>除税后溢利</td><td>17,232.52</td><td>32,694.20</td></tr>
        </table>
        </body></html>
        """
        with unittest.mock.patch("mcp_gateway.finance._SECURE_OPENER.open") as mock_open:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.read.return_value = mock_sina_html.encode("gbk")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            res = fin_gw.stock_financials({"symbol": "00700", "report_type": "income", "limit_periods": 2})
            self.assertEqual(res["market"], "香港交易所 (HKEX)")
            self.assertIn("sina_hk_f10", res["source"])
            self.assertEqual(res["period_count"], 2)
            self.assertEqual(res["statements"][0]["截止日期"], "2026-06-30")
            self.assertEqual(res["statements"][0]["营业额"], 58911.89)

    def test_hk_stock_f10_profile(self) -> None:
        """测试港股 F10 公司概况新浪主力专线获取"""
        fin_gw = FinanceGateway()
        mock_info_html = """
        <html><body>
        <table>
            <tr><td>证券代码</td><td>00700</td></tr>
            <tr><td>公司名称(中文)</td><td>腾讯控股有限公司</td></tr>
            <tr><td>主席</td><td>马化腾</td></tr>
            <tr><td>公司业务</td><td>提供增值服务与社交平台</td></tr>
        </table>
        </body></html>
        """
        with unittest.mock.patch("mcp_gateway.finance._SECURE_OPENER.open") as mock_open:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.read.return_value = mock_info_html.encode("gbk")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            res = fin_gw.stock_f10({"symbol": "00700", "category": "summary"})
            self.assertEqual(res["market"], "香港交易所 (HKEX)")
            self.assertIn("sina_hk_f10", res["source"])
            self.assertIn("腾讯控股有限公司", str(res["data"]))


    def test_quote_total_shares_and_loss_eps(self) -> None:
        """测试盘口中禁用 Yield 假股本、基于万股计算总股本_股，以及亏损标的负市盈率安全计算 EPS 且无 None 泄漏"""
        fin_gw = FinanceGateway()
        mock_raw = {
            "HQInfo": {"Now": 3.12, "Close": 3.10, "Yield": 17.02},
            "ExtInfo": {"SYL": -84.25, "ZGB": 165765.375, "LTGB": 165765.375, "MGSY": -0.02, "MGJZC": 1.715},
            "ProInfo": {"PETTM": -20.67},
            "BspInfo": [],
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_raw):
            res = fin_gw.stock_quotes({"symbol": "600217"})
            hq = res["盘口核心行情"]
            ext = res["估值与基本盘面"]
            # 1. 核心行情中彻底移除以 Yield 映射的假股本
            self.assertNotIn("总股本_股", hq)
            # 2. 估值盘面由 万股*10000 安全导出真实股本
            self.assertEqual(ext["总股本_股"], 1657653750)
            self.assertEqual(ext["流通股本_股"], 1657653750)
            # 3. 亏损标的负PE安全计算出负数年化EPS，且口径说明无 None 泄漏
            self.assertEqual(ext["动态市盈率_PE"], -84.25)
            self.assertEqual(ext["年化每股收益_EPS"], -0.037)
            self.assertNotIn("None", ext["估值口径说明"])
            self.assertIn("-0.037", ext["估值口径说明"])

    def test_kline_adjust_and_minute_time(self) -> None:
        """测试 K 线复权参数 TQFlag 映射 (qfq=1, hfq=2, none=0) 及分钟线时分格式化"""
        fin_gw = FinanceGateway()
        captured_body = {}

        def mock_post(url, req_body):
            captured_body.update(req_body)
            return {
                "ListItem": [
                    {"Item": ["20261021", "53400", "25.12", "25.15", "25.08", "25.10", "1000", "1000", "400"]},
                    {"Item": ["20261021", "53700", "25.12", "25.23", "25.10", "25.20", "2000", "2000", "800"]},
                ],
                "AttachInfo": {"Name": "博实股份"},
            }

        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", side_effect=mock_post):
            # 1. 测试前复权 TQFlag 应为 1
            res_qfq = fin_gw.stock_kline({"symbol": "002223", "period": "5m", "adjust": "qfq"})
            self.assertEqual(captured_body["TQFlag"], 1)
            # 2. 测试后复权 TQFlag 应为 2
            fin_gw.stock_kline({"symbol": "002223", "period": "5m", "adjust": "hfq"})
            self.assertEqual(captured_body["TQFlag"], 2)
            # 3. 测试不复权 TQFlag 应为 0
            fin_gw.stock_kline({"symbol": "002223", "period": "5m", "adjust": "none"})
            self.assertEqual(captured_body["TQFlag"], 0)
            # 4. 验证 5m 分钟线包含具体时分
            self.assertEqual(res_qfq["klines"][0]["time"], "2026-10-21 14:50")
            self.assertEqual(res_qfq["klines"][1]["time"], "2026-10-21 14:55")

    def test_screener_matched_count_and_f10_enhancements(self) -> None:
        """测试智能选股全量命中数提取、F10 无关表净化、十大流通股东比例补齐及限售解禁表头汉化"""
        fin_gw = FinanceGateway()
        # 1. 测试选股 matched_count 来自 raw[0][2]
        mock_screener_raw = [
            [0, "", 31, "session_id", 1],
            ["POS", "market", "sec_code", "sec_name", "now_price", "chg"],
            ["", "", "", "", "", ""],
            [1, "0", "002223", "博实股份", "25.15", "-1.5"],
        ]
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_screener_raw):
            s_res = fin_gw.stock_screener({"message": "储能", "limit": 1})
            self.assertEqual(s_res["matched_count"], 31)
            self.assertEqual(s_res["returned_count"], 1)

        # 2. 测试 F10 过滤无关复权因子表并补齐十大股东持股比例
        mock_f10_raw = {
            "ErrorCode": 0,
            "ResultSets": [
                {
                    "ResultSetKey": "table0",
                    "ColName": ["rq", "T004", "T013", "T016", "T019"],
                    "Content": [["2026-09-16", 100000000, None, None, 100000000]],
                },
                {
                    "ResultSetKey": "table1",
                    "ColName": ["rq", "gd", "cgs", "pos"],
                    "Content": [["2026-09-16", "华泰医疗ETF", 1939000, None]],
                },
                {
                    "ResultSetKey": "table2",
                    "ColName": ["EndDate", "AdjustingFactor", "AdjustingConst"],
                    "Content": [["20260901", 8.23, 46.7]],
                },
            ],
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_f10_raw):
            f_res = fin_gw.stock_f10({"symbol": "002223", "category": "top_holders"})
            tables = f_res["formatted_tables"]
            self.assertIn("table0", tables)
            self.assertIn("table1", tables)
            # 无关复权因子 table2 已被彻底剔除
            self.assertNotIn("table2", tables)
            # table1 中为 null 的 pos 自动计算为 1.939%
            self.assertEqual(tables["table1"][0]["持股比例_pct"], 1.939)
            self.assertEqual(tables["table1"][0]["股东名称"], "华泰医疗ETF")

    def test_financial_statements_discipline_and_no_raw_n_codes(self) -> None:
        """测试利润表完全消除原始 N 码泄露、过滤 N027 净利润双计，以及注入加总纪律说明"""
        fin_gw = FinanceGateway()
        mock_income_raw = {
            "ResultSets": [
                {"ColName": ["isjr"], "Content": [[0]]},
                {
                    "ColName": ["N001", "N002", "N003", "N019", "N024", "N027", "N035", "Tdate"],
                    "Content": [["2026中报", "92278072083.21", "90703260964.48", "61411291686.27", "46033330566.78", "46033330566.78", "44464207646.01", "2026-06-30"]],
                },
            ]
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_income_raw):
            res = fin_gw.stock_financials({"symbol": "600519", "report_type": "income", "limit_periods": 1})
            p_row = res["statements"][0]
            # 验证无任何原始 N 编码泄露
            self.assertEqual([k for k in p_row.keys() if k.startswith("N0")], [])
            # 验证 N027 已被过滤，防止与 N024 净利润双计
            self.assertIn("净利润", p_row)
            self.assertNotIn("重复核对_净利润", p_row)
            # 验证利润表与加总说明存在
            self.assertIn("利润表加总纪律说明", p_row)

    def test_invalid_symbol_explicit_rejection_all_tools(self) -> None:
        """测试对不存在或非上市标的代码在 quotes/kline/financials/f10/news 五大工具中均显式报错，杜绝伪装空壳与无关新闻"""
        fin_gw = FinanceGateway()
        bad_code = "123456"

        # 1. 验证 stock_quotes 显式报错（不再返回 ErrorCode:0 与静默空核心盘口）
        with self.assertRaises(ValueError) as cm_q:
            fin_gw.stock_quotes({"symbol": bad_code})
        self.assertIn("未查询到证券代码 '123456' 的有效盘口行情", str(cm_q.exception))

        # 2. 验证 stock_kline 显式报错（不再返回 total_bars: 0 与静默空）
        with self.assertRaises(ValueError) as cm_k:
            fin_gw.stock_kline({"symbol": bad_code})
        self.assertIn("未查询到证券代码 '123456' 的历史 K 线数据", str(cm_k.exception))

        # 3. 验证 stock_financials 显式报错（修复 P0：无论 report_type 与 period_type 为何种组合，无效代码均必须前置拦截，杜绝服务端游标泄露）
        for rt in ("balance", "income", "cashflow"):
            for pt in ("report", "quarter"):
                with self.assertRaises(ValueError) as cm_f:
                    fin_gw.stock_financials({"symbol": bad_code, "report_type": rt, "period_type": pt})
                self.assertIn("未查询到证券代码 '123456' 的财务报表数据", str(cm_f.exception))

        # 4. 验证 stock_f10 显式报错（不再返回 ErrorCode: 0 假成功空表）
        with self.assertRaises(ValueError) as cm_f10:
            fin_gw.stock_f10({"symbol": bad_code})
        self.assertIn("未查询到证券代码 '123456' 的 F10 基本面资料", str(cm_f10.exception))

        # 5. 验证 stock_news 拦截模糊搜索（不再返回巴菲特减持比亚迪等无关他股新闻）
        with self.assertRaises(ValueError) as cm_n:
            fin_gw.stock_news({"symbol": bad_code})
        self.assertIn("已拦截泛化模糊检索以避免返回无关误导资讯", str(cm_n.exception))

    def test_screener_pagination_no_gap_and_st_accounting(self) -> None:
        """测试智能选股分页严格对齐、无跨页跳过断裂，并清晰披露 ST 过滤数量与说明"""
        fin_gw = FinanceGateway()
        mock_screener_page1 = [
            [0, "", 3, "session_1", 2],
            ["POS", "market", "sec_code", "sec_name", "now_price", "chg"],
            ["", "", "", "", "", ""],
            [1, "0", "002223", "博实股份", "25.15", "-1.5"],
            [2, "0", "002514", "*ST宝实", "5.10", "2.1"],
        ]
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_screener_page1):
            res = fin_gw.stock_screener({"message": "储能", "limit": 2, "page": 1, "filter_st": True})
            self.assertEqual(res["page"], 1)
            self.assertEqual(res["limit"], 2)
            self.assertEqual(res["total_pages"], 2)
            self.assertEqual(res["matched_count"], 3)
            self.assertEqual(res["returned_count"], 1)
            self.assertEqual(res["filtered_st_count"], 1)
            self.assertIn("默认已过滤 ST/*ST 股", res["filter_st_note"])
            self.assertEqual(res["stocks"][0]["code"], "002223")

    def test_kline_week_month_volume_alignment(self) -> None:
        """测试周线与月线成交量统一乘以 100 换算为【股】，消除跨周期 0.0100 漂移"""
        fin_gw = FinanceGateway()
        mock_pbfxt_week = {
            "ListItem": [
                {
                    "Item": [
                        "20261021", "0", "1322.5", "1348.6", "1302.36", "1309.69",
                        "14238156800.0", "14238156800.0", "107378.0", "0.0", "0.0", "0.0"
                    ]
                }
            ],
            "AttachInfo": {"Name": "贵州茅台"},
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_pbfxt_week):
            res = fin_gw.stock_kline({"symbol": "600519", "period": "week", "limit": 1})
            bar = res["klines"][0]
            # 原始 107378.0 手已自动换算为 10737800.0 股
            self.assertEqual(bar["volume"], 10737800.0)

    def test_quotes_ashare_top_level_name_and_market(self) -> None:
        """测试 A 股盘口 quotes 返回顶层与港股对齐的 name 与 market 字段"""
        fin_gw = FinanceGateway()
        mock_pbhq = {
            "HQInfo": {
                "HQDate": "20261021", "HQTime": "150000", "Now": 1309.69,
                "Close": 1331.88, "Open": 1331.88, "MaxP": 1331.98, "MinP": 1302.36,
                "Volume": 33514, "Amount": 4403823104.0
            },
            "ExtInfo": {"ZGB": 125008.16, "LTGB": 125008.16, "ZSZ": 1637219372013.0, "SYL": 20.1},
            "BaseInfo": {"Name": "贵州茅台", "Code": "600519", "Setcode": 1},
            "BspInfo": [{"BuyP": 1309.59, "SellP": 1309.69, "BuyV": "2", "SellV": "46"}],
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_pbhq):
            res = fin_gw.stock_quotes({"symbol": "600519"})
            self.assertEqual(res["name"], "贵州茅台")
            self.assertIn("上海证券交易所", res["market"])
            self.assertIn("股票名称", res["盘口核心行情"])
            self.assertEqual(res["盘口核心行情"]["股票名称"], "贵州茅台")

    def test_f10_raw_t_codes_mapped_and_empty_forecast_note(self) -> None:
        """测试 F10 中 shareholders, forecast, summary, margin_trading 原始 T 码全归一，空预测注入说明"""
        fin_gw = FinanceGateway()
        # 1. 测试 shareholders 汉化
        mock_sh_raw = {
            "ErrorCode": 0,
            "ResultSets": [
                {
                    "ResultSetKey": "table0",
                    "ColName": ["T002", "T003", "T004", "T005", "T006", "T007", "T012"],
                    "Content": [["2026-06-30", 296404, 4217.49, 21.89, -18.1, 1185.49, 53245]],
                }
            ],
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_sh_raw):
            res = fin_gw.stock_f10({"symbol": "600519", "category": "shareholders"})
            row = res["formatted_tables"]["table0"][0]
            self.assertIn("截止日期", row)
            self.assertIn("股东户数_户", row)
            self.assertIn("户均持股数_股", row)
            self.assertNotIn("T002", row)

        # 2. 测试 forecast 空表返回无记录说明
        mock_fc_empty = {
            "ErrorCode": 0,
            "ResultSets": [
                {"ResultSetKey": "table0", "ColName": ["defdate"], "Content": []},
                {"ResultSetKey": "table1", "ColName": ["T026"], "Content": []},
            ],
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_fc_empty):
            res_fc = fin_gw.stock_f10({"symbol": "002252", "category": "forecast"})
            self.assertIn("无记录说明", res_fc)
            self.assertIn("近期无卖方机构发布盈利预测报告", res_fc["无记录说明"])

    def test_pe_discipline_and_after_hours_orderbook_warning(self) -> None:
        """测试 quotes 中 PE 必须双轨分开的纪律说明与收盘后五档残量防误导警示"""
        fin_gw = FinanceGateway()
        mock_pbhq = {
            "HQInfo": {
                "HQDate": "20261021", "HQTime": "150000", "Now": 1309.69,
                "Close": 1331.88, "Open": 1331.88, "MaxP": 1331.98, "MinP": 1302.36,
                "Volume": 33514, "Amount": 4403823104.0
            },
            "ExtInfo": {
                "ZGB": 125008.16, "LTGB": 125008.16, "ZSZ": 1637219372013.0,
                "SYL": 20.1, "SYL_TTM": 19.8, "JLY": 3000000000.0, "MGSY": 5.2
            },
            "BaseInfo": {"Name": "贵州茅台", "Code": "600519", "Setcode": 1},
            "BspInfo": [
                {"BuyP": 1309.59, "SellP": 1309.69, "BuyV": "2", "SellV": "46"},
                {"BuyP": 1309.50, "SellP": 1309.80, "BuyV": "10", "SellV": "26"},
            ],
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_pbhq):
            res = fin_gw.stock_quotes({"symbol": "600519"})
            # 1. 验证 PE 双轨与纪律说明
            val_info = res.get("估值与基本盘面", {})
            self.assertIn("动态市盈率_PE", val_info)
            self.assertIn("滚动市盈率_PETTM", val_info)
            self.assertIn("PE使用纪律说明", val_info)
            self.assertIn("【PE口径防误导铁律】", val_info["PE使用纪律说明"])
            self.assertIn("滚动市盈率_PETTM", val_info["PE使用纪律说明"])
            self.assertIn("动态市盈率_PE", val_info["PE使用纪律说明"])

            # 2. 验证收盘后五档残量防误导警示
            self.assertIn("盘口深度说明", res)
            self.assertIn("【收盘残量防误导警示】", res["盘口深度说明"])
            self.assertIn("严禁将休市后的微量残单当作真实买卖盘口深度", res["盘口深度说明"])

    def test_operating_profit_residual_and_discipline_notice(self) -> None:
        """测试利润表中营业利润不可加总差额（信用减值/资产减值损失）的自动核算与自陈纪律说明"""
        fin_gw = FinanceGateway()
        mock_income_raw = {
            "ErrorCode": 0,
            "ResultSets": [
                {
                    "ResultSetKey": "report",
                    "ColName": [
                        "N001", "N002", "N004", "N015", "N018", "N014",
                        "N017", "N016", "N019", "N022", "N024"
                    ],
                    "Content": [
                        [
                            "2026-06-30",
                            83447957386.95,  # 营业总收入
                            7852378939.99,   # 营业总成本
                            489839.29,       # 投资收益
                            33148152.02,     # 其他收益
                            0.0,             # 公允价值变动
                            0.0,             # 汇兑收益
                            -263654.49,      # 资产处置收益
                            75661448632.82,  # 营业利润 (官方披露值)
                            75591340578.47,  # 利润总额
                            56708779901.81,  # 净利润
                        ]
                    ],
                }
            ],
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_income_raw):
            res = fin_gw.stock_financials({"symbol": "600519", "report_type": "income", "limit_periods": 1})
            period = res["statements"][0]
            self.assertIn("营业利润不可加总差额_信用减值与资产减值损失及其他", period)
            # 复算理论差额: 75661448632.82 - (83447957386.95 - 7852378939.99 + 489839.29 + 33148152.02 - 263654.49) = 32495849.04
            self.assertEqual(period["营业利润不可加总差额_信用减值与资产减值损失及其他"], 32495849.04)
            self.assertIn("利润表加总纪律说明", period)
            self.assertIn("【营业利润不可直接复算自陈】", period["利润表加总纪律说明"])

    def test_top_holders_ah_share_capital_notice(self) -> None:
        """测试十大流通股东表针对 A+H 股本口径差异的警示说明"""
        fin_gw = FinanceGateway()
        mock_th_raw = {
            "ErrorCode": 0,
            "ResultSets": [
                {
                    "ResultSetKey": "table0",
                    "ColName": ["ltg", "qtltg", "b1_zgb", "b2_zgb"],
                    "Content": [[7170468722, 1000000000, 7170468722, 1000000000]],
                },
                {
                    "ResultSetKey": "table1",
                    "ColName": ["T002", "gdmc", "cgs", "cgbl", "cglx", "gddm"],
                    "Content": [["2026-06-30", "王传福", 511000000, None, "自然人", "01"]],
                },
            ],
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_th_raw):
            res = fin_gw.stock_f10({"symbol": "002594", "category": "top_holders"})
            self.assertIn("股本口径说明", res)
            self.assertIn("【A+H及跨市场股本口径警示】", res["股本口径说明"])
            self.assertIn("比亚迪", res["股本口径说明"])
            self.assertIn("实时法定总股本", res["股本口径说明"])

    def test_hk_cashflow_statement_not_balance(self) -> None:
        """测试港股现金流量表返回真实现金流而非资产负债表 (P0 H1)"""
        fin_gw = FinanceGateway()
        mock_em_cf = [
            {"截止日期": "2026-06-30", "报告期": "中报", "经营业务现金净额": 154061000000.0, "期末现金": 206930000000.0, "币种": "CNY"}
        ]
        with unittest.mock.patch.object(fin_gw.tdx_client, "_get_eastmoney_hk_financials", return_value=mock_em_cf):
            res = fin_gw.stock_financials({"symbol": "00700", "report_type": "cashflow", "limit_periods": 1})
            self.assertIn("现金流量表", res["statement_type"])
            self.assertEqual(res["period_count"], 1)
            stmt = res["statements"][0]
            self.assertIn("经营业务现金净额", stmt)
            self.assertIn("期末现金", stmt)

    def test_hk_f10_category_routing(self) -> None:
        """测试港股 F10 分类多路路由（主要股东、回购、分红、概念、龙虎榜等） (P0 H2)"""
        fin_gw = FinanceGateway()
        # 1. 测试 top_holders
        with unittest.mock.patch.object(fin_gw.tdx_client, "_get_sina_hk_table", return_value=[{"主要股东": "Naspers", "持股比例": 28.86}]):
            res_th = fin_gw.stock_f10({"symbol": "00700", "category": "top_holders"})
            self.assertEqual(res_th["category"], "top_holders")
            self.assertIn("top_holders", res_th["formatted_tables"])
            self.assertNotIn("major_shareholders", res_th["formatted_tables"])

        # 2. 测试 repurchases
        with unittest.mock.patch.object(fin_gw.tdx_client, "_get_sina_hk_table", return_value=[{"回购日期": "2026-10-15", "回购股数": 240000}]):
            res_rep = fin_gw.stock_f10({"symbol": "00700", "category": "repurchases"})
            self.assertEqual(res_rep["category"], "repurchases")
            self.assertIn("repurchases", res_rep["formatted_tables"])

        # 3. 测试 lhb 港股机制自陈
        res_lhb = fin_gw.stock_f10({"symbol": "00700", "category": "lhb"})
        self.assertIn("lhb_rules", res_lhb["formatted_tables"])
        self.assertIn("港股市场不设通达信 A 股交易所席位龙虎榜披露机制", res_lhb["无记录说明"])

        # 4. 测试 unlocks 港股全流通自陈
        res_unl = fin_gw.stock_f10({"symbol": "00700", "category": "unlocks"})
        self.assertIn("unlocks_rules", res_unl["formatted_tables"])
        self.assertIn("全流通", res_unl["无记录说明"])

    def test_hk_income_currency_and_eps(self) -> None:
        """测试港股利润表单位百万元声明、美元每股盈利转美分及跨期币种变动警示 (P0 H3, P1 H4, P1 H5, P1 H7)"""
        fin_gw = FinanceGateway()
        mock_sina_html = """
        <html><body>
        <table class="tab05"><tr><td>指标</td></tr></table>
        <table class="tab05"><tr><td>资产负债</td></tr></table>
        <table class="tab05"><tr><td>现金流</td></tr></table>
        <table class="tab05">
            <tr><td>报告期</td><td>2026-06-30</td><td>2024-12-31</td></tr>
            <tr><td>报表类型</td><td>中报</td><td>年报</td></tr>
            <tr><td>营业额</td><td>3,641.10</td><td>38,297.85</td></tr>
            <tr><td>基本每股盈利(仙)</td><td>11.31</td><td>75.00</td></tr>
            <tr><td>币种</td><td>美元</td><td>人民币</td></tr>
        </table>
        </body></html>
        """
        with unittest.mock.patch("mcp_gateway.finance._SECURE_OPENER.open") as mock_open:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.read.return_value = mock_sina_html.encode("gbk")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            res = fin_gw.stock_financials({"symbol": "01133", "report_type": "income", "limit_periods": 2})
            stmts = res["statements"]
            self.assertEqual(len(stmts), 2)
            # 语义校准与单位
            self.assertEqual(stmts[0]["截止日期"], "2026-06-30")
            self.assertEqual(stmts[0]["金额单位"], "百万元")
            self.assertEqual(stmts[0]["报告币种"], "美元")
            self.assertEqual(stmts[0]["每股收益币种单位"], "美分")
            self.assertEqual(stmts[0]["基本每股盈利(美分)"], 11.31)
            self.assertEqual(stmts[0]["基本每股盈利_折合美元"], 0.1131)

            # 跨期货币变动
            self.assertEqual(stmts[1]["报告币种"], "人民币")
            self.assertIn("跨期币种变动说明", stmts[0])
            self.assertIn("跨期币种变动说明", stmts[1])

    def test_hk_quotes_volume_int_and_shares(self) -> None:
        """测试港股盘口成交量为 int 类型、自算总股本与换手率及52周说明 (P2 H10, P3 H11, P3 H12)"""
        fin_gw = FinanceGateway()
        mock_raw = 'v_r_hk00700="100~腾讯控股~00700~393.800~390.000~392.000~37783835.0~0~0~393.800~100~0~0~0~0~0~0~0~0~394.000~200~0~0~0~0~0~0~0~0~37783835.0~2026/10/21 16:08:26~3.800~0.97~396.000~391.200~393.800~37783835.0~14880000000.000~0~20.50~~0~0~1.23~35806.8353~35806.8353~TENCENT~0.00~450.000~260.000~0.00~66.67~0~0~0~0~0~0~0~0~0~0~0~GP~0~0~0~0~0~9092644829.00~9092644829.00~0~0~0~0~HKD~1~50";'
        with unittest.mock.patch("mcp_gateway.finance._SECURE_OPENER.open") as mock_open:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.read.return_value = mock_raw.encode("gbk")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            res = fin_gw.stock_quotes({"symbol": "00700"})
            hq = res["盘口核心行情"]
            ext = res["估值与基本盘面"]
            # P2 H10: 成交量_股 为 int
            self.assertIsInstance(hq["成交量_股"], int)
            self.assertEqual(hq["成交量_股"], 37783835)
            # P3 H11: 自算总股本与换手率
            self.assertEqual(ext["总股本_股"], 9092644829)
            self.assertEqual(ext["流通股本_股"], 9092644829)
            self.assertEqual(hq["换手率_pct"], 0.416)
            self.assertIn("涨跌停说明", hq)
            # P3 H12: 52周说明
            self.assertIn("52周价格区间说明", ext)


    def test_skill_and_tools_interface_alignment(self) -> None:
        """测试全部 8 大金融数据接口与 mcphub-finance/SKILL.md 契约完全对齐"""
        fin_gw = FinanceGateway()

        # 1. stock_lookup: 验证返回 entities 中同时包含 entity_code 与 code, name, setcode, market
        mock_rag = {
            "retrieved_entities": [
                {"entity_code": "300750", "entity_name": "宁德时代", "entity_type": "A股代码", "entity_setcode": 0}
            ]
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_rag):
            res_lookup = fin_gw.stock_lookup({"query": "宁德时代", "range": "AG"})
            self.assertIn("query", res_lookup)
            self.assertIn("range", res_lookup)
            ent0 = res_lookup["entities"][0]
            self.assertEqual(ent0["code"], "300750")
            self.assertEqual(ent0["entity_code"], "300750")
            self.assertEqual(ent0["name"], "宁德时代")
            self.assertEqual(ent0["market"], "SZ")

        # 2. market_context: 验证交易时钟状态
        ctx = fin_gw.market_context({})
        for key in ("current_cst_time", "market_status", "market_status_desc", "is_trading_time", "reference_trading_date"):
            self.assertIn(key, ctx)

        # 3. stock_quotes: 验证 A 股与港股通用字段最高/最低/涨跌额_元/更新时间及滚动市盈率_PETTM/总市值_元
        res_q_a = fin_gw.stock_quotes({"symbol": "600519"})
        hq_a = res_q_a["盘口核心行情"]
        self.assertIn("最高价", hq_a)
        self.assertNotIn("最高", hq_a)
        self.assertIn("最低价", hq_a)
        self.assertNotIn("最低", hq_a)
        self.assertIn("涨跌额_元", hq_a)
        self.assertNotIn("涨跌额", hq_a)
        self.assertIn("日内振幅_pct", hq_a)
        self.assertNotIn("振幅_pct", hq_a)
        self.assertIn("更新时间", hq_a)

        mock_hk_raw = 'v_r_hk00700="100~腾讯控股~00700~393.800~390.000~392.000~37783835.0~0~0~393.800~100~0~0~0~0~0~0~0~0~394.000~200~0~0~0~0~0~0~0~0~37783835.0~2026/10/21 16:08:26~3.800~0.97~396.000~391.200~393.800~37783835.0~14880000000.000~0~20.50~~0~0~1.23~35806.8353~35806.8353~TENCENT~0.00~450.000~260.000~0.00~66.67~0~0~0~0~0~0~0~0~0~0~0~GP~0~0~0~0~0~9092644829.00~9092644829.00~0~0~0~0~HKD~1~50";'
        with unittest.mock.patch("mcp_gateway.finance._SECURE_OPENER.open") as mock_open:
            mock_resp = unittest.mock.MagicMock()
            mock_resp.read.return_value = mock_hk_raw.encode("gbk")
            mock_resp.__enter__.return_value = mock_resp
            mock_open.return_value = mock_resp

            res_q_hk = fin_gw.stock_quotes({"symbol": "00700"})
            hq_hk = res_q_hk["盘口核心行情"]
            ext_hk = res_q_hk["估值与基本盘面"]
            self.assertIn("最高价", hq_hk)
            self.assertNotIn("最高", hq_hk)
            self.assertIn("最低价", hq_hk)
            self.assertNotIn("最低", hq_hk)
            self.assertNotIn("涨跌额_元", hq_hk)
            self.assertIn("涨跌额_港元", hq_hk)
            self.assertNotIn("涨跌额", hq_hk)
            self.assertIn("成交额_港元", hq_hk)
            self.assertIn("日内振幅_pct", hq_hk)
            self.assertNotIn("振幅_pct", hq_hk)
            self.assertIn("更新时间", hq_hk)
            self.assertIn("滚动市盈率_PETTM", ext_hk)
            self.assertIn("动态市盈率_PE", ext_hk)
            self.assertIn("总市值_港元", ext_hk)
            self.assertIn("港股市值_港元", ext_hk)
            self.assertIn("流通市值_港元", ext_hk)
            self.assertNotIn("总市值_元", ext_hk)
            self.assertIn("总股本_万股", ext_hk)

        # 4. stock_kline: 验证 indicators_included 与 datetime / macd_hist / boll_upper / rsi
        res_k = fin_gw.stock_kline({"symbol": "600519", "count": 2})
        self.assertTrue(res_k.get("indicators_included"))
        bar0 = res_k["klines"][-1]
        self.assertIn("datetime", bar0)
        self.assertIn("time", bar0)
        self.assertIn("macd_hist", bar0)
        self.assertIn("boll_upper", bar0)
        self.assertIn("boll_lower", bar0)
        self.assertIn("rsi6", bar0)

        # 5. stock_financials: 验证 period_type 与 会计口径与说明
        res_fin = fin_gw.stock_financials({"symbol": "600519", "report_type": "income", "limit_periods": 1})
        self.assertEqual(res_fin.get("period_type"), "report")
        self.assertIn("会计口径与说明", res_fin)
        self.assertIn("会计口径与合并说明", res_fin)

        # 6. stock_f10: 验证 category (industry, dividends, repurchases, lhb)
        res_f10_ind = fin_gw.stock_f10({"symbol": "600519", "category": "industry"})
        self.assertEqual(res_f10_ind["category"], "industry")
        res_f10_rep = fin_gw.stock_f10({"symbol": "600519", "category": "repurchases"})
        self.assertEqual(res_f10_rep["category"], "repurchases")
        res_f10_bb = fin_gw.stock_f10({"symbol": "600519", "category": "buyback"})
        self.assertEqual(res_f10_bb["category"], "buyback")

        # 7. stock_screener: 验证 top-level message 与 stock 列表中 symbol, matched_indicators
        mock_raw_screener = [
            [0, "", 10, "sess_123", 1],
            ["POS", "MARKET", "CODE", "NAME", "PRICE", "CHG", "ROE"],
            ["--", "--", "--", "--", "--", "--", "--"],
            ["1", "0", "002594", "比亚迪", "285.5", "2.15", "18.2"],
        ]
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_raw_screener):
            res_scr = fin_gw.stock_screener({"message": "主板 绩优 储能", "limit": 5})
            self.assertIn("message", res_scr)
            self.assertEqual(res_scr["message"], "主板 绩优 储能")
            self.assertEqual(res_scr["stocks"][0]["symbol"], "002594")
            self.assertIn("matched_indicators", res_scr["stocks"][0])

        # 8. stock_news: 验证 news 项目中同时包含 datetime 与 summary
        with unittest.mock.patch.object(
            fin_gw.fallback_client,
            "get_news",
            return_value=[{
                "title": "测试新闻",
                "source": "官方公告",
                "time": "2026-10-21 16:00:00",
                "datetime": "2026-10-21 16:00:00",
                "url": "https://test.com",
                "snippet": "测试正文摘要",
                "summary": "测试正文摘要",
            }]
        ):
            res_news = fin_gw.stock_news({"symbol": "600519", "limit": 1})
            n0 = res_news["news"][0]
            self.assertIn("datetime", n0)
            self.assertIn("time", n0)
            self.assertIn("summary", n0)
            self.assertIn("snippet", n0)

    # =========================================================================
    # 【功能块 T1：P0 金融类股票财报双轨自适应与会计准则校验测试】
    # - 输入：isjr=1 的金融标的 (银行/保险) 与 isjr=0 的工商业标的
    # - 功能：验证 isjr 标志精准识别、金融与非金融映射表自适应挂载、0 裸 N 码外泄与恒等式校验
    # - 输出：单元断言通过
    # =========================================================================
    def test_p0_financial_statements_dual_track(self) -> None:
        """测试 P0 缺陷：isjr=1 金融类标的自适应匹配金融企业会计准则映射表"""
        fin_gw = FinanceGateway()

        # 1. 模拟金融类标的 (isjr=1，101列资产负债表与57列利润表)
        mock_fin_balance_raw = {
            "ResultSets": [
                {"ColName": ["isjr"], "Content": [["1"]]},
                {
                    "ColName": ["N001", "N005", "N025", "N046", "N047", "N056", "N084", "N097", "N098"],
                    "Content": [["2024-09-30", "3500000000000.0", "28000000000000.0", "57070593000000.0", "1200000000000.0", "42000000000000.0", "52714749000000.0", "4355844000000.0", "4300000000000.0"]]
                }
            ]
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_fin_balance_raw):
            res_b = fin_gw.stock_financials({"symbol": "601398", "report_type": "balance", "limit_periods": 1})
            self.assertTrue(res_b.get("is_financial"))
            self.assertIn("金融企业会计准则", res_b.get("accounting_standard", ""))
            stmt = res_b["statements"][0]
            self.assertEqual(stmt.get("资产总计"), 57070593000000.0)
            self.assertEqual(stmt.get("负债合计"), 52714749000000.0)
            self.assertEqual(stmt.get("所有者权益合计"), 4355844000000.0)
            self.assertEqual(stmt.get("现金及存放中央银行款项"), 3500000000000.0)
            self.assertEqual(stmt.get("发放贷款及垫款"), 28000000000000.0)
            self.assertNotIn("流动资产合计", stmt)
            # 验证无裸 N 码
            n_codes = [k for k in stmt.keys() if k.startswith("N0") or k.startswith("N1")]
            self.assertEqual(len(n_codes), 0)

        # 2. 模拟工商业实体 (isjr=0，81列工商业模板)
        mock_non_fin_raw = {
            "ResultSets": [
                {"ColName": ["isjr"], "Content": [["0"]]},
                {
                    "ColName": ["N001", "N002", "N015", "N046", "N080"],
                    "Content": [["2024-09-30", "150000000000.0", "220000000000.0", "28000000000.0", "300000000000.0"]]
                }
            ]
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_non_fin_raw):
            res_nb = fin_gw.stock_financials({"symbol": "600519", "report_type": "balance", "limit_periods": 1})
            self.assertFalse(res_nb.get("is_financial"))
            self.assertIn("一般工商业企业会计准则", res_nb.get("accounting_standard", ""))
            stmt_n = res_nb["statements"][0]
            self.assertEqual(stmt_n.get("货币资金"), 150000000000.0)
            self.assertEqual(stmt_n.get("流动资产合计"), 220000000000.0)
            self.assertNotIn("拆出资金", stmt_n)

    # =========================================================================
    # 【功能块 T2：P1 盘口字段清洗与数值类型对齐测试】
    # - 输入：通达信原始盘口数据 (含 Yield/Jjjz 字段串扰与 str/float 漂移)
    # - 功能：验证 Yield 拦截、股票剔除 Jjjz、ItemNum 转明细笔数且为 int、整型字段转 int
    # - 输出：单元断言通过
    # =========================================================================
    def test_p1_quotes_field_cleanup_and_type_alignment(self) -> None:
        """测试 P1 缺陷：盘口 Yield 拦截、Jjjz 股本串扰拦截以及 str/int/float 数值类型规范化"""
        fin_gw = FinanceGateway()

        mock_stock_quote = {
            "HQInfo": {
                "Now": 18.55,
                "Close": 18.20,
                "Open": 18.30,
                "MaxP": 18.90,
                "MinP": 18.15,
                "Volume": "125678",      # 原始为 str
                "Amount": "233123456.0", # 原始为 str
                "Inside": "62000",        # 原始为 str
                "Outside": "63678",       # 原始为 str
                "CJBS": "4560",           # 原始为 str
                "RestVol": "120",         # 原始为 str
                "ItemNum": "88",          # 原始为 str
                "Yield": 350000000000.0,  # 严重串扰：流通股本混入 Yield
                "Jjjz": 350000000000.0,   # 严重串扰：股票非基金却混入 Jjjz
            },
            "ExtInfo": {
                "ZGB": 100000.0,
                "LTGB": 80000.0,
                "BelongRZRQ": "1",
                "BelongHGTAG": "1",
                "BelongSGTAG": "0",
                "BelongHS300": "1",
                "HasKQZ": "0",
                "FreeLtgb": "75000.5",
                "HYZAF": "2.35",
            }
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_stock_quote):
            res_q = fin_gw.stock_quotes({"symbol": "601398"})
            hq = res_q["盘口核心行情"]
            ext = res_q["估值与基本盘面"]

            # 1. 验证 Yield 与 Jjjz 彻底剔除
            self.assertNotIn("Yield", hq)
            self.assertNotIn("收益率_Yield", hq)
            self.assertNotIn("Jjjz", hq)
            self.assertNotIn("基金净值", hq)

            # 2. 验证 ItemNum 汉化且转为 int
            self.assertNotIn("ItemNum", hq)
            self.assertIn("明细笔数", hq)
            self.assertIsInstance(hq["明细笔数"], int)
            self.assertEqual(hq["明细笔数"], 88)

            # 3. 验证整型指标规范化为 int
            for ik in ("成交量_手", "内盘_手", "外盘_手", "成交笔数", "剩余委托量", "明细笔数"):
                self.assertIsInstance(hq[ik], int, f"{ik} 应该为 int 类型")

            # 4. 验证浮点型指标规范化为 float
            for fk in ("现价", "昨收", "今开", "最高价", "最低价", "成交额_元"):
                self.assertIsInstance(hq[fk], float, f"{fk} 应该为 float 类型")

            # 5. 验证 ExtInfo 字段汉化与类型
            self.assertEqual(ext.get("是否属于融资融券标的"), 1)
            self.assertEqual(ext.get("是否属于沪股通标的"), 1)
            self.assertEqual(ext.get("是否属于深股通标的"), 0)
            self.assertEqual(ext.get("是否属于沪深300标的"), 1)
            self.assertEqual(ext.get("是否含权证或转债"), 0)
            self.assertEqual(ext.get("自由流通股本_万股"), 75000.5)
            self.assertEqual(ext.get("行业涨跌幅_pct"), 2.35)
            for raw_k in ("BelongRZRQ", "BelongHGTAG", "BelongSGTAG", "BelongHS300", "HasKQZ", "FreeLtgb", "HYZAF"):
                self.assertNotIn(raw_k, ext)

    # =========================================================================
    # 【功能块 T3：P2 分钟线时间戳纠偏与 events 空记录提示测试】
    # - 输入：底层返回 46800 秒 (13:00) 的上午收市 bar，以及无催化记录的 events 表
    # - 功能：验证 13:00 强制纠正为 11:30，以及 events 无记录时返回明确无记录说明
    # - 输出：单元断言通过
    # =========================================================================
    def test_p2_kline_1300_fix_and_events_empty_notice(self) -> None:
        """测试 P2 缺陷：分钟线 13:00 时间戳纠正为 11:30，以及 events 空记录注入说明"""
        fin_gw = FinanceGateway()

        # 1. 测试分钟线时间戳纠偏
        mock_kline_raw = {
            "ListItem": [
                {"Item": ["20261021", 41400, 10.0, 10.2, 9.9, 10.1, 5000000.0, 50000.0]},  # 11:30 前
                {"Item": ["20261021", 46800, 10.1, 10.3, 10.0, 10.2, 6000000.0, 60000.0]},  # 46800秒 原为 13:00
            ],
            "AttachInfo": {"Name": "测试股票"}
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_kline_raw):
            res_k = fin_gw.stock_kline({"symbol": "002223", "period": "1m", "count": 2})
            bars = res_k["klines"]
            # 验证第二根 bar 时间纠正为 11:30，杜绝 13:00
            self.assertEqual(bars[1]["time"], "2026-10-21 11:30")
            self.assertNotIn("13:00", [b["time"] for b in bars])

        # 2. 测试 events 空记录无静默
        mock_events_raw = {
            "ResultSets": [
                {"ColName": ["cjrq", "sjmc", "rec_id", "sjxz", "cjrzdf"], "Content": []}
            ]
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_events_raw):
            res_ev = fin_gw.stock_f10({"symbol": "688475", "category": "events"})
            self.assertIn("无记录说明", res_ev)
            self.assertIn("事件库属性说明", res_ev)
            self.assertIn("近期事件库无记录", res_ev["无记录说明"])

    # =========================================================================
    # 【功能块 T4：P3 F10 摘要表与融资融券裸英文字段汉化测试】
    # - 输入：包含 jjrq/mll/name/zzhy/pjgdsyll/gxrq/bshow 的 F10 返回字典
    # - 功能：验证无条件弹出英文原键并替换为标准中文标签，杜绝任何英文字段外泄
    # - 输出：单元断言通过
    # =========================================================================
    def test_p3_f10_and_margin_trading_english_key_elimination(self) -> None:
        """测试 P3 缺陷：F10 摘要表与融资融券表中的裸英文字段全部汉化"""
        fin_gw = FinanceGateway()

        # 1. 测试 summary 表各 table 的英文键汉化
        mock_summary_raw = {
            "ResultSets": [
                {
                    "ColName": ["jjrq", "kfjlr", "jqjzcsyl", "gsdw"],
                    "Content": [[None, 45000000000.0, 16.5, "白酒龙头"]]
                },
                {"ColName": ["dummy"], "Content": []}, # table1
                {"ColName": ["dummy"], "Content": []}, # table2
                {"ColName": ["dummy"], "Content": []}, # table3
                {
                    "ColName": ["bgq", "mgsy", "mgjzc", "yysr", "gmjlr", "mll", "zcfzl"],
                    "Content": [["2024-09-30", 35.5, 180.2, 90000000000.0, 45000000000.0, None, 15.2]]
                },
                {"ColName": ["dummy"], "Content": []}, # table5
                {
                    "ColName": ["sylttm", "syllyr", "sjl", "zsz", "zgb", "ltag"],
                    "Content": [[25.4, 28.2, 8.5, 2000000000000.0, 1250000000.0, 1250000000.0]]
                },
                {"ColName": ["dummy"], "Content": []}, # table7
                {
                    "ColName": ["name", "zzhy", "pjgdsyll", "gxrq"],
                    "Content": [["贵州茅台", "酒、饮料和精制茶制造业", 19.74, "2026-10-21"]]
                }
            ]
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_summary_raw):
            res_sum = fin_gw.stock_f10({"symbol": "600519", "category": "summary"})
            tables = res_sum["formatted_tables"]

            # 验证 table0
            t0_row = tables["table0"][0]
            self.assertNotIn("jjrq", t0_row)
            self.assertIn("业绩披露日期", t0_row)
            self.assertIn("扣非净利润_元", t0_row)

            # 验证 table4
            t4_row = tables["table4"][0]
            self.assertNotIn("mll", t4_row)
            self.assertIn("毛利率_pct", t4_row)
            self.assertIn("每股收益_EPS", t4_row)

            # 验证 table6
            t6_row = tables["table6"][0]
            self.assertNotIn("sylttm", t6_row)
            self.assertNotIn("sjl", t6_row)
            self.assertIn("滚动市盈率_PETTM", t6_row)
            self.assertNotIn("动态市盈率_PE_TTM", t6_row)
            self.assertIn("市净率_PB_昨收", t6_row)

            # 验证 table8
            t8_row = tables["table8"][0]
            for raw_k in ("name", "zzhy", "pjgdsyll", "gxrq"):
                self.assertNotIn(raw_k, t8_row)
            self.assertEqual(t8_row.get("股票简称"), "贵州茅台")
            self.assertEqual(t8_row.get("证监会行业"), "酒、饮料和精制茶制造业")
            self.assertEqual(t8_row.get("平均股东收益率_pct"), 19.74)
            self.assertEqual(t8_row.get("财务更新日期"), "2026-10-21")

        # 2. 测试 margin_trading 的 table2 bshow 汉化
        mock_margin_raw = {
            "ResultSets": [
                {"ColName": ["T001", "T003"], "Content": [["2026-10-21", 100000000.0]]}, # table0
                {"ColName": ["bdrq", "T033"], "Content": [["2026-10-21", 125000.0]]},     # table1
                {"ColName": ["bshow"], "Content": [[1]]},                                   # table2
            ]
        }
        with unittest.mock.patch.object(fin_gw.tdx_client, "_post", return_value=mock_margin_raw):
            res_margin = fin_gw.stock_f10({"symbol": "600519", "category": "margin_trading"})
            m_tables = res_margin["formatted_tables"]
            t2_row = m_tables["table2"][0]
            self.assertNotIn("bshow", t2_row)
            self.assertIn("是否展示两融标的", t2_row)
            self.assertEqual(t2_row["是否展示两融标的"], 1)


    def test_hk_invalid_symbol_error_contract(self) -> None:
        """测试 P0: 无效港股代码 001548 的统一错误契约防御（全部抛出明确 ValueError，lookup 返回 exact_match=False）"""
        fin_gw = FinanceGateway()
        for fn in [
            lambda: fin_gw.stock_quotes({"symbol": "001548"}),
            lambda: fin_gw.stock_kline({"symbol": "001548"}),
            lambda: fin_gw.stock_news({"symbol": "001548"}),
            lambda: fin_gw.stock_financials({"symbol": "001548", "report_type": "income"}),
            lambda: fin_gw.stock_f10({"symbol": "001548", "category": "summary"}),
        ]:
            with self.assertRaises(ValueError):
                fn()

        lookup_res = fin_gw.stock_lookup({"query": "001548", "range": "HK-GP"})
        self.assertFalse(lookup_res.get("exact_match", True))
        self.assertIsNone(lookup_res.get("matched_code"))
        self.assertIn("未检索到代码为", lookup_res.get("未命中说明", ""))

    def test_hk_indicators_currency_and_market_cap_alignment(self) -> None:
        """测试 P1: indicators 币种校准为法定损益表同源 CNY，并注入市值口径说明"""
        fin_gw = FinanceGateway()
        res = fin_gw.tdx_client._get_eastmoney_hk_financials("00700", report_type="summary", limit_periods=1)
        if res:
            first_p = res[0]
            self.assertEqual(first_p.get("币种"), "CNY")
            self.assertIn("实际记账编报币种为【CNY】", first_p.get("币种说明", ""))
            self.assertIn("总市值_全部股本乘H股价_港元", first_p)
            self.assertIn("港股市值_H股流通市值_港元", first_p)
            self.assertIn("市值口径说明", first_p)

    def test_hk_single_quarter_differencing_not_inflated(self) -> None:
        """测试 P1: 单季度模式 (quarter) 自动做差，最早一期正确扣减同财年前期（00700 2025Q3 为单季真值 ~1903 亿，非累计 5518 亿）"""
        fin_gw = FinanceGateway()
        res = fin_gw.stock_financials({"symbol": "00700", "report_type": "income", "period_type": "quarter", "limit": 4})
        stmts = res.get("statements", [])
        self.assertTrue(len(stmts) >= 1)
        for s in stmts:
            d = s.get("截止日期")
            rev = s.get("营业额") or s.get("营业收入") or s.get("营业收入_元")
            if d == "2025-09-30" and rev is not None:
                # 真实单季度为 5518亿 - 3615亿 = 1902.76亿，必须远小于 3000 亿，严禁返回 5518 亿累计值
                self.assertLess(float(rev), 300000000000.0)

    def test_hk_top_holders_and_shareholders_contract(self) -> None:
        """测试 P2: top_holders 采用申报顺位与好仓排重，shareholders 剔除重复表"""
        fin_gw = FinanceGateway()
        # 1. 验证 shareholders 仅保留一张 shareholder_events 表
        res_sh = fin_gw.stock_f10({"symbol": "00700", "category": "shareholders"})
        sh_tables = res_sh.get("formatted_tables", {})
        self.assertIn("shareholder_events", sh_tables)
        self.assertNotIn("major_shareholders", sh_tables)

        # 2. 验证 top_holders 采用申报顺位，且均为好仓 (L)
        res_th = fin_gw.stock_f10({"symbol": "00700", "category": "top_holders"})
        th_list = res_th.get("formatted_tables", {}).get("top_holders", [])
        for h in th_list:
            self.assertIn("申报顺位", h)
            self.assertNotIn("名次", h)
            self.assertIn(h.get("目前类别"), ("L", "好仓", "", None))

    def test_hk_concepts_industry_always_populated(self) -> None:
        """测试 P2: concepts 恒生行业分类非空且准确映射（如 00700 资讯科技、00753 航空物流、00168 饮料食品、00728 电讯）"""
        fin_gw = FinanceGateway()
        for code, expected_kw in [
            ("00700", "资讯科技"),
            ("00753", "航空"),
            ("00168", "饮料与食品"),
            ("00728", "电讯"),
        ]:
            res = fin_gw.stock_f10({"symbol": code, "category": "concepts"})
            concepts = res.get("formatted_tables", {}).get("concepts", [])
            ind_name = None
            for c in concepts:
                if c.get("概念分类") == "恒生行业分类":
                    ind_name = c.get("行业名称")
            self.assertIsNotNone(ind_name)
            self.assertTrue(len(ind_name) > 0)
            self.assertIn(expected_kw, ind_name)

    def test_hk_market_cap_and_pe_fx_disclosure(self) -> None:
        """测试 P2/P3: 港股市值细分为纯 H 股与全股本混合参考两口径，估值口径披露 PETTM 与 0.868~0.872 汇率折算"""
        fin_gw = FinanceGateway()
        quotes_res = fin_gw.stock_quotes({"symbol": "00753"})
        val_panel = quotes_res.get("估值与基本盘面", {})
        self.assertIn("港股市值_H股流通市值_亿港元", val_panel)
        self.assertIn("总市值_全部股本乘H股价_亿港元", val_panel)
        self.assertIn("市值口径说明", val_panel)
        self.assertIn("估值口径说明", val_panel)
        self.assertIn("0.868~0.872", val_panel["估值口径说明"])

    def test_screener_hk_query_warning(self) -> None:
        """测试 P3: screener 查询包含港股关键词时注入明确的市场范围提示与 warning 警示"""
        fin_gw = FinanceGateway()
        scr_res = fin_gw.stock_screener({"message": "港股 腾讯"})
        self.assertEqual(scr_res.get("market_scope"), "A股 (中国大陆交易所：沪市/深市/北交所)")
        self.assertTrue(scr_res.get("is_hk_intent"))
        self.assertIn("warning", scr_res)
        self.assertIn("暂不支持港股标的自动筛选", scr_res["warning"])

    def test_v185_hk_minute_kline_and_day_amount(self) -> None:
        """测试 D1/D3: 港股日内分钟线逐分钟切片聚合与日线不复权成交额非零"""
        fin_gw = FinanceGateway()
        # 1. 测试日线 adjust='none' 的成交额必须 > 0
        res_day = fin_gw.stock_kline({"symbol": "00700", "period": "day", "count": 2, "adjust": "none"})
        self.assertGreater(res_day["total_bars"], 0)
        for b in res_day["klines"]:
            self.assertGreater(b.get("amount", 0.0), 0.0)

        # 2. 测试 60m 分钟线必须包含时分时间戳与日内聚合切片
        res_60m = fin_gw.stock_kline({"symbol": "00700", "period": "60m", "count": 6})
        self.assertGreater(res_60m["total_bars"], 0)
        self.assertIn("港股分钟线覆盖说明", res_60m)
        first_bar = res_60m["klines"][0]
        self.assertIn(":", first_bar["time"])  # 包含时分，如 10:30

    def test_v185_financial_cashflow_discrepancy_and_subtotal(self) -> None:
        """测试 D2/D7/T3: 金融类现金流量表差额自陈、间接法声明、筹资流入小计完备性与净额键名双向镜像"""
        fin_gw = FinanceGateway()
        # 1. 测试 601318 金融现金流自陈与间接法
        res_601318 = fin_gw.stock_financials({"symbol": "601318", "report_type": "cashflow", "limit_periods": 1})
        st0 = res_601318["statements"][0]
        self.assertIn("经营活动现金流入不可加总差额_原保险保费及拆入回购等金融科目缺项", st0)
        self.assertIn("补充资料_间接法说明", st0)
        # 修复 P2-4：三净额去重（杜绝整表遍历求和双计），统一为规范的“经营活动现金流量净额”
        self.assertIn("经营活动现金流量净额", st0)
        self.assertNotIn("经营活动产生的现金流量净额", st0)

        # 2. 测试 301230 筹资流入小计绝不为 None
        res_301230 = fin_gw.stock_financials({"symbol": "301230", "report_type": "cashflow", "limit_periods": 4})
        for r in res_301230["statements"]:
            self.assertIsNotNone(r.get("筹资活动现金流入小计"))

    def test_v185_hk_f10_industry_and_shareholders_dedup(self) -> None:
        """测试 D5/D6/D10: 港股 summary 所属行业非空、top_holders 单表规范与 shareholders 0股冗余清洗"""
        fin_gw = FinanceGateway()
        # 1. 测试 00700 summary 所属行业非空
        res_sum = fin_gw.stock_f10({"symbol": "00700", "category": "summary"})
        prof = res_sum["formatted_tables"]["company_profile"][0]
        self.assertIn("所属行业", prof)
        self.assertTrue(len(prof["所属行业"]) > 0)
        self.assertIn("资讯科技", prof["所属行业"])

        # 2. 测试 top_holders 仅包含单一 top_holders 表名，绝不重复
        res_th = fin_gw.stock_f10({"symbol": "00700", "category": "top_holders"})
        self.assertIn("top_holders", res_th["formatted_tables"])
        self.assertNotIn("major_shareholders", res_th["formatted_tables"])

        # 3. 测试 shareholders 清洗同日 0 股冗余穿透行
        res_sh = fin_gw.stock_f10({"symbol": "00700", "category": "shareholders"})
        events = res_sh.get("formatted_tables", {}).get("shareholder_events", [])
        if events:
            nas_2021 = [r for r in events if r.get("事件日期") == "2021-04-08" and "naspers" in str(r.get("持股人名字", "")).lower()]
            self.assertEqual(len(nas_2021), 1)  # 严格为 1 条，已剔除涉及股份为 0 的附注行

    def test_v186_financial_and_f10_enhancements(self) -> None:
        """测试 v1.8.6: 金融报表科目精准修正、F10 质押客观说明、两融空表提示与港股切片聚合"""
        from mcp_gateway.finance import BALANCE_FIELDS_FINANCE
        fin_gw = FinanceGateway()

        # 1. 验证 P2-6：金融资产负债表科目映射严谨性，N003 必须为“货币资金”，N010 为“拆出资金”
        self.assertEqual(BALANCE_FIELDS_FINANCE["N003"], "货币资金")
        self.assertEqual(BALANCE_FIELDS_FINANCE["N010"], "拆出资金")

        # 2. 验证 P2-2 & P2-1：金融利润表公式修正为【营业收入 - 营业支出】
        res_inc = fin_gw.stock_financials({"symbol": "600030", "report_type": "income", "limit_periods": 1})
        st0_inc = res_inc["statements"][0]
        self.assertIn("营业利润 = 营业收入 − 营业支出", st0_inc.get("利润表加总纪律说明", ""))
        yysr = float(st0_inc.get("营业收入", 0.0))
        yyzc = float(st0_inc.get("营业支出", 0.0))
        yylr = float(st0_inc.get("营业利润", 0.0))
        self.assertAlmostEqual(yysr - yyzc, yylr, delta=0.05)

        # 3. 验证 P2-3：金融现金流投资活动自陈差额自动补全
        res_cf = fin_gw.stock_financials({"symbol": "601318", "report_type": "cashflow", "limit_periods": 1})
        st0_cf = res_cf["statements"][0]
        self.assertIn("投资活动现金流入不可加总差额_金融投资细项缺项", st0_cf)

        # 4. 验证 P2-5：F10 摘要质押表 table5 客观描述（非强行断言零质押）
        res_sum = fin_gw.stock_f10({"symbol": "002698", "category": "summary"})
        self.assertIn("质押状态说明", res_sum)
        self.assertIn("未收录当前股东股权质押明细", res_sum["质押状态说明"])

        # 5. 验证 P3：两融空表 table0 主动注入无记录说明
        res_margin = fin_gw.stock_f10({"symbol": "300523", "category": "margin_trading"})
        self.assertIn("无记录说明", res_margin)
        self.assertIn("非融资融券标的证券", res_margin["无记录说明"])

        # 6. 验证 P3：自然语言选股支持 query 别名调用
        res_scr = fin_gw.stock_screener({"query": "储能", "limit": 1})
        self.assertIn("stocks", res_scr)

        # 7. 验证 P3：events 催化日期标准 YYYY-MM-DD 格式
        res_ev = fin_gw.stock_f10({"symbol": "600030", "category": "events"})
        for t_rows in res_ev["formatted_tables"].values():
            for r in t_rows:
                if "催化日期" in r and r["催化日期"]:
                    self.assertRegex(str(r["催化日期"]), r"^\d{4}-\d{2}-\d{2}$")

    def test_v187_empirical_fixes(self) -> None:
        """测试 v1.8.7: 实测 14 项缺陷（P0-P3）全面回归验证"""
        fin_gw = FinanceGateway()

        # 1. 验证 Issue 1 (P0): A 股 stock_f10 支持 indicators / financials 枚举 (P2-1 剔除 formatted_tables 内部同名重复表)
        res_ind = fin_gw.stock_f10({"symbol": "600519", "category": "indicators"})
        self.assertIn("table4", res_ind.get("formatted_tables", {}))
        self.assertNotIn("indicators", res_ind.get("formatted_tables", {}))
        self.assertIn("核心财务与经营指标概览", res_ind.get("category_name", ""))

        # 2. 验证 Issue 2 (P0): stock_lookup 市场范围跨市场隔离与 exact_match 精确匹配
        res_lk_ag = fin_gw.stock_lookup({"query": "09896", "range": "AG"})
        self.assertFalse(res_lk_ag.get("exact_match"))
        res_lk_hk = fin_gw.stock_lookup({"query": "601318", "range": "HK-GP"})
        self.assertFalse(res_lk_hk.get("exact_match"))

        # 3. 验证 Issue 3 (P0): 港股日K线 count 严格对齐请求条数 (杜绝 count+1)
        res_k5 = fin_gw.stock_kline({"symbol": "09896", "period": "day", "count": 5})
        self.assertEqual(len(res_k5.get("klines", [])), 5)
        res_k1 = fin_gw.stock_kline({"symbol": "09896", "period": "day", "count": 1})
        self.assertEqual(len(res_k1.get("klines", [])), 1)

        # 4. 验证 Issue 4 (P0): A 股 dividends 准确映射至分红派息专线 (杜绝串入事件驱动)
        res_div = fin_gw.stock_f10({"symbol": "601318", "category": "dividends"})
        div_rows = res_div.get("formatted_tables", {}).get("dividends", [])
        self.assertGreater(len(div_rows), 0)
        self.assertIn("送股_每10股", div_rows[0])
        self.assertIn("派息_每10股_含税", div_rows[0])

        # 5. 验证 Issue 5 (P1): 港股复权真实生效 (HFQ收盘价与无复权产生显著分化)
        k_none = fin_gw.stock_kline({"symbol": "01177", "period": "day", "count": 10, "adjust": "none"})["klines"]
        k_hfq = fin_gw.stock_kline({"symbol": "01177", "period": "day", "count": 10, "adjust": "hfq"})["klines"]
        self.assertNotEqual(k_hfq[-1]["close"], k_none[-1]["close"])

        # 6. 验证 Issue 7 (P1): 港股半年度披露标的单季度推算保留 H1 并不混淆全年
        res_hk_q = fin_gw.stock_financials({"symbol": "01177", "report_type": "income", "period_type": "quarter", "limit_periods": 3})
        stmts = res_hk_q.get("statements", [])
        self.assertGreaterEqual(len(stmts), 1)
        has_h1 = any("H1" in str(s.get("单期标签", "")) for s in stmts)
        self.assertTrue(has_h1)

        # 7. 验证 Issue 8 (P1): screener 港股意图识别与 warning 告警
        scr_res = fin_gw.stock_screener({"message": "港股 腾讯", "limit": 5})
        self.assertTrue(scr_res.get("is_hk_intent"))
        self.assertIn("warning", scr_res)

        # 8. 验证 Issue 9 (P2): A 股资产负债表非流动资产新租赁准则差额自陈
        res_bal = fin_gw.stock_financials({"symbol": "600519", "report_type": "balance", "limit_periods": 1})
        self.assertIn("非流动资产不可加总差额_新租赁准则使用权资产等", res_bal["statements"][0])

        # 9. 验证 P2-1: industry 分类返回真实证监会与申万行业分类
        res_ind = fin_gw.stock_f10({"symbol": "600519", "category": "industry"})
        t_ind = res_ind.get("formatted_tables", {}).get("industry", [])
        if t_ind:
            self.assertIn("证监会行业", t_ind[0])
            self.assertIn("申万一级行业", t_ind[0])

        # 10. 验证 Issue 11 (P2 / P2-7): A 股 repurchases 真实权威专线接入，返回结构化回购表
        res_rep = fin_gw.stock_f10({"symbol": "600519", "category": "repurchases"})
        self.assertIn("repurchases", res_rep.get("formatted_tables", {}))
        self.assertIn("eastmoney", res_rep.get("source", ""))

        # 11. 验证 Issue 12 (P2): 港股 52 周最高/最低前复权与分红口径说明
        q_hk = fin_gw.stock_quotes({"symbol": "09896"})
        gz = q_hk.get("估值与基本盘面", {})
        self.assertIn("前复权", gz.get("52周价格区间说明", ""))

        # 12. 验证 Issue 14 (P3): 港股 basic_info 与 summary 分立独立表结构
        res_basic = fin_gw.stock_f10({"symbol": "00700", "category": "basic_info"})
        res_summ = fin_gw.stock_f10({"symbol": "00700", "category": "summary"})
        self.assertNotEqual(res_basic.get("category_name"), res_summ.get("category_name"))
        self.assertIn("basic_info", res_basic.get("formatted_tables", {}))

        # 13. 验证 Issue 14 (P3): 现金流量表键序规范 (筹资活动现金流入小计 排在 流出小计 之前)
        res_cf = fin_gw.stock_financials({"symbol": "300908", "report_type": "cashflow", "limit_periods": 1})
        cf_keys = list(res_cf["statements"][0].keys())
        idx_in = cf_keys.index("筹资活动现金流入小计")
        idx_out = cf_keys.index("筹资活动现金流出小计")
        self.assertLess(idx_in, idx_out)

        # 14. 验证 000063 流动负债自陈差额纳入【预计流动负债】，差额降为真实残差 (~0.11亿) 而非 15.41 亿
        res_zte = fin_gw.stock_financials({"symbol": "000063", "report_type": "balance", "limit_periods": 1})
        s0 = res_zte["statements"][0]
        self.assertIn("预计流动负债", s0)
        diff_cl = s0.get("流动负债不可加总差额_吸收存款及金融细项等")
        if diff_cl is not None:
            # 差额应约为 1055 万 (0.11亿)，严格小于 1 亿（杜绝 15.41 亿虚高）
            self.assertLess(diff_cl, 100000000.0)

        # 15. 验证 000063 新闻过滤他股 IPO 快讯
        res_news = fin_gw.stock_news({"symbol": "000063", "limit": 5})
        for n in res_news.get("news", []):
            self.assertNotIn("通则康威", n.get("title", ""))

    def test_v188_empirical_fixes(self) -> None:
        """测试 v1.8.8: 实测 9 项缺陷与口径陷阱全面回归验证"""
        fin_gw = FinanceGateway()

        # 1. 验证 P1-1 & P2-4: top_holders 持股比例分母对齐真实法定总股本，反推限售股非空
        res_h = fin_gw.stock_f10({"symbol": "600875", "category": "top_holders"})
        t0 = res_h.get("formatted_tables", {}).get("table0", [])
        self.assertTrue(len(t0) > 0)
        self.assertEqual(t0[0].get("总股本_股"), 3458360326)
        self.assertEqual(t0[0].get("限售流通股合计_股"), 786993731)

        t1 = res_h.get("formatted_tables", {}).get("table1", [])
        self.assertTrue(len(t1) > 0)
        # 东方电气第一大股东 中国东方电气集团 持股比例 28.617% (杜绝 43.726% 流通A股高估)
        self.assertEqual(t1[0].get("持股比例_pct"), 28.617)
        self.assertEqual(t1[0].get("持股占总股本比例_pct"), 28.617)
        self.assertEqual(t1[0].get("持股占流通A股比例_pct"), 43.726)
        self.assertIn("持股比例分母口径说明", res_h)

        # 2. 验证 P1-2: 300371 f10 summary 动态市盈率_PE_TTM 期次落后自算校准为 80.31
        res_sum = fin_gw.stock_f10({"symbol": "300371", "category": "summary"})
        t6 = res_sum.get("formatted_tables", {}).get("table6", [])
        self.assertTrue(len(t6) > 0)
        pe_snap = float(t6[0].get("滚动市盈率_PETTM"))
        pe_live = float(fin_gw.stock_quotes({"symbol": "300371"})["估值与基本盘面"].get("滚动市盈率_PETTM"))
        self.assertAlmostEqual(pe_snap, pe_live, delta=0.05, msg="F10 现价口径 PE 应与 quotes 同值")
        self.assertIsNotNone(t6[0].get("滚动市盈率_PETTM_昨收口径"))
        self.assertNotIn("动态市盈率_PE_TTM", t6[0])
        self.assertIn("PE_TTM校准说明", t6[0])

        # 3. 验证 P1-3 与 P2-1: 300371 stock_quotes 派生每股指标对齐三季报且 PE 与 EPS 绝对互逆闭环
        q_300371 = fin_gw.stock_quotes({"symbol": "300371"})
        ext_371 = q_300371.get("估值与基本盘面", {})
        self.assertEqual(ext_371.get("报告期每股收益_EPS"), 0.1373)
        self.assertEqual(ext_371.get("每股净资产_BPS"), 5.8093)
        now_p_371 = float(q_300371.get("盘口核心行情", {}).get("现价") or 0.0)
        pe_371 = float(ext_371.get("动态市盈率_PE") or 0.0)
        eps_371 = float(ext_371.get("年化每股收益_EPS") or 0.0)
        self.assertGreater(pe_371, 50.0)
        self.assertGreater(eps_371, 0.15)
        # 严格验证互逆性: 现价 / 年化EPS 与 动态PE 相对误差绝对不超过 0.08%
        self.assertLess(abs((now_p_371 / eps_371) - pe_371) / pe_371, 0.0008)
        self.assertEqual(ext_371.get("市净率_PB"), round(now_p_371 / float(ext_371["每股净资产_BPS"]), 2))
        self.assertEqual(ext_371.get("最新财报报告期"), "2026-09-30")

        # 4. 验证 P2-2: 601658 银行 2026 一季报筹资流入小计结构闭合
        res_cf_bank = fin_gw.stock_financials({"symbol": "601658", "report_type": "cashflow", "limit_periods": 4})
        q1_bank = None
        for s in res_cf_bank.get("statements", []):
            if str(s.get("报告期")).startswith("2026-03-31"):
                q1_bank = s
                break
        if q1_bank:
            self.assertIsNotNone(q1_bank.get("筹资活动现金流入小计"))
            self.assertIn("筹资流入说明", q1_bank)

        # 5. 验证 P2-3: indicators 与 financials, repurchases 与 buyback 参数区分力
        res_ind = fin_gw.stock_f10({"symbol": "600519", "category": "indicators"})
        res_fin = fin_gw.stock_f10({"symbol": "600519", "category": "financials"})
        self.assertNotEqual(set(res_ind.get("formatted_tables", {}).keys()), set(res_fin.get("formatted_tables", {}).keys()))

        res_rep = fin_gw.stock_f10({"symbol": "600519", "category": "repurchases"})
        res_bb = fin_gw.stock_f10({"symbol": "600519", "category": "buyback"})
        self.assertNotEqual(set(res_rep.get("formatted_tables", {}).keys()), set(res_bb.get("formatted_tables", {}).keys()))

        # 6. 验证 P3-1: 610617 stock_lookup 杜绝噪音 600470
        res_lk = fin_gw.stock_lookup({"query": "610617", "range": "AG"})
        cand_symbols = [c.get("symbol") for c in res_lk.get("candidates", [])]
        self.assertNotIn("600470", cand_symbols)

        # 7. 验证 P3-2: lhb 具备盘后入库提示
        res_lhb = fin_gw.stock_f10({"symbol": "688499", "category": "lhb"})
        self.assertIn("盘后入库说明", res_lhb)

    def test_v190_quarter_invalid_code_no_session_leakage(self) -> None:
        """测试 v1.9.0 P0: stock_financials 在 period_type='quarter' 下严密拦截无效代码，杜绝服务端会话泄漏上一只股票数据"""
        fin_gw = FinanceGateway()
        
        # 1. 模拟用户实测复现路径：先查有效代码 600694 (或 601658)，再查无效代码 610617 / 000000
        # 无论在任何状态下，查询无效代码 610617 必须显式抛出 ValueError
        for bad_code in ("610617", "000000"):
            for rt in ("balance", "income", "cashflow"):
                for pt in ("report", "quarter"):
                    with self.assertRaises(ValueError) as cm:
                        fin_gw.stock_financials({"symbol": bad_code, "report_type": rt, "period_type": pt, "limit_periods": 4})
                    self.assertIn(f"未查询到证券代码 '{bad_code}' 的财务报表数据", str(cm.exception))

    # =========================================================================
    # 【功能块 T4：港股金融财报框架与准则双轨制契约对齐测试】
    # - 输入：港股金融标的 (03968/00005) 与工商业标的 (00700)
    # - 功能：验证港股引擎 is_financial 布尔标志、accounting_standard 准则对齐与金融核算说明注入
    # - 输出：单元测试断言 100% 通过
    # =========================================================================
    def test_v190_hk_financials_dual_track_and_financial_framework(self) -> None:
        """测试 v1.9.0: 港股引擎金融财报框架与 A 股契约 100% 对称对齐 (is_financial, accounting_standard 与金融核算说明)"""
        fin_gw = FinanceGateway()

        # 1. 验证港股金融标的 (03968 招商银行)：is_financial 为 True，准则标明金融行业专用列报，资产负债表注入核算说明
        res_bank = fin_gw.stock_financials({"symbol": "03968", "report_type": "balance", "limit_periods": 1})
        self.assertTrue(res_bank.get("is_financial"))
        self.assertIn("金融控股", res_bank.get("accounting_standard", ""))
        self.assertIn("资产负债表核算说明", res_bank["statements"][0])
        self.assertIn("金融企业资产负债表核算规范", res_bank["statements"][0]["资产负债表核算说明"])

        # 2. 验证港股工商业标的 (00700 腾讯控股)：is_financial 为 False，准则标明一般工商业
        res_tech = fin_gw.stock_financials({"symbol": "00700", "report_type": "balance", "limit_periods": 1})
        self.assertFalse(res_tech.get("is_financial"))
        self.assertIn("一般工商业", res_tech.get("accounting_standard", ""))
        self.assertNotIn("资产负债表核算说明", res_tech["statements"][0])

        # 3. 验证港股金融标的利润表与现金流量表说明注入 (00005 汇丰控股)
        res_inc = fin_gw.stock_financials({"symbol": "00005", "report_type": "income", "limit_periods": 1})
        self.assertTrue(res_inc.get("is_financial"))
        self.assertIn("利润表加总纪律说明", res_inc["statements"][0])

    # =========================================================================
    # 【功能块 T5：实测 P1/P2/P3 问题全面收敛测试套件 (v1.9.0 深度收敛闭环)】
    # - 输入：东方电气 (600875)、邮储银行 (601658)、华录百纳 (300371)、百润股份 (002293)
    # - 功能：严格验证 P1-1(unlocks占比自算与明细闭合)、P2-1(PE/EPS互逆)、
    #         P2-2(f10顶层挂载报告期)、P2-3(金融科目无前缀)、P2-4(indicators同源声明)、
    #         P3-1(table5汉化)、P3-2(buyback状态纠偏)、P3-3(两融日期ISO)、
    #         P3-5(A+H流通比说明)、P3-6(quotes总股本个位精度)
    # - 输出：单元测试断言 100% 通过
    # =========================================================================
    def test_v190_p1_p2_p3_issue_convergence(self) -> None:
        """测试 v1.9.0: P1-1/P2-1/P2-2/P2-3/P2-4/P3-1/P3-2/P3-3/P3-5/P3-6 全量闭环验证"""
        fin_gw = FinanceGateway()

        # 1. 验证 P1-1: 600875 unlocks 占比基于法定总股本严格自算闭合 (解禁股数 3309.07 ÷ 345836.031 ≈ 0.957%)
        res_unl = fin_gw.stock_f10({"symbol": "600875", "category": "unlocks"})
        self.assertIn("解禁口径与核算规则说明", res_unl)
        t0_unl = res_unl.get("formatted_tables", {}).get("table0", [])
        if t0_unl:
            row0 = t0_unl[0]
            self.assertIn("解禁股数_万股", row0)
            self.assertIn("占总股本比例_pct", row0)
            self.assertIn("上游申报批次占比_pct", row0)
            self.assertIn("解禁后参考流通股本_万股", row0)
            # 自算比例约 0.957%，绝非上游批次的 15.11%
            self.assertAlmostEqual(float(row0["占总股本比例_pct"]), 0.957, delta=0.05)
            self.assertAlmostEqual(float(row0["上游申报批次占比_pct"]), 15.11, delta=0.1)

        # 验证 P1-1 另一处: 601658 多阶段解禁明细中解禁股数为本次解禁股数(90513万股)而非累计流通盘(387912万股)
        res_unl_bank = fin_gw.stock_f10({"symbol": "601658", "category": "unlocks"})
        multi_unl = res_unl_bank.get("多阶段解禁明细", [])
        if multi_unl:
            m0 = multi_unl[0]
            self.assertIn("解禁后累计流通A股本_万股", m0)
            self.assertIn("占总股本比例_pct", m0)
            self.assertLess(float(m0["解禁股数_万股"]), float(m0["解禁后累计流通A股本_万股"]))

        # 2. 验证 P2-1: 002293 与 300371 换期后 PE 与 EPS 绝对互逆闭环 (现价 / EPS ≈ PE, 误差 <= 0.08%)
        for sym in ("002293", "300371"):
            q_res = fin_gw.stock_quotes({"symbol": sym})
            ext_info = q_res.get("估值与基本盘面", {})
            now_p = float(q_res.get("盘口核心行情", {}).get("现价") or 0.0)
            pe = float(ext_info.get("动态市盈率_PE") or 0.0)
            eps = float(ext_info.get("年化每股收益_EPS") or 0.0)
            if pe > 0 and eps > 0 and now_p > 0:
                calc_pe = now_p / eps
                rel_diff = abs(calc_pe - pe) / pe
                self.assertLess(rel_diff, 0.0008, f"标的 {sym} 动态PE({pe}) 与 现价/EPS({calc_pe}) 互逆误差超标: {rel_diff}")

        # 3. 验证 P2-2: financials / indicators / summary 顶层对称挂载 最新财报报告期 与 派生指标换期说明
        for cat in ("financials", "indicators", "summary"):
            f10_r = fin_gw.stock_f10({"symbol": "600875", "category": cat})
            self.assertIn("最新财报报告期", f10_r, f"分类 {cat} 顶层缺失 最新财报报告期")
            self.assertIn("派生指标换期说明", f10_r, f"分类 {cat} 顶层缺失 派生指标换期说明")

        # 4. 验证 P2-3: 601658 银行利润表科目无 加_/减_ 前缀，与工商模板完全一致
        res_inc = fin_gw.stock_financials({"symbol": "601658", "report_type": "income", "limit_periods": 1})
        inc_stmt = res_inc.get("statements", [])[0]
        self.assertIn("营业外收入", inc_stmt)
        self.assertIn("营业外支出", inc_stmt)
        self.assertIn("所得税费用", inc_stmt)
        self.assertNotIn("加_营业外收入", inc_stmt)
        self.assertNotIn("减_营业外支出", inc_stmt)

        # 5. 验证 P2-4: indicators 顶层挂载同源声明
        res_ind = fin_gw.stock_f10({"symbol": "600875", "category": "indicators"})
        self.assertIn("数据源同源与交叉印证说明", res_ind)

        # 6. 验证 P3-1: indicators.table5 汉化无 zyrq/zygf/zzygf 裸码残留
        ind_t5 = res_ind.get("formatted_tables", {}).get("table5", [])
        if ind_t5:
            for k in ind_t5[0].keys():
                self.assertNotIn(k, ("zyrq", "zygf", "zzygf"))

        # 7. 验证 P3-2: 300371 buyback 状态智能纠偏，完成方案数 >= 8，实施中方案数 <= 1
        res_bb = fin_gw.stock_f10({"symbol": "300371", "category": "buyback"})
        bb_sum = res_bb.get("formatted_tables", {}).get("buyback_summary", [])[0]
        self.assertGreaterEqual(bb_sum["已实施完成方案数"], 8)
        self.assertLessEqual(bb_sum["当前实施中方案数"], 1)

        # 8. 验证 P3-3: margin_trading table0 交易日期标准化为 YYYY-MM-DD
        res_mt = fin_gw.stock_f10({"symbol": "600875", "category": "margin_trading"})
        t0_mt = res_mt.get("formatted_tables", {}).get("table0", [])
        if t0_mt:
            date_val = str(t0_mt[0].get("交易日期", ""))
            self.assertRegex(date_val, r"^\d{4}-\d{2}-\d{2}$")

        # 9. 验证 P3-5: 601658 top_holders 顶层说明声明持股占流通A股比例可大于100%
        res_th = fin_gw.stock_f10({"symbol": "601658", "category": "top_holders"})
        self.assertIn("持股比例分母口径说明", res_th)
        self.assertIn("大于 100%", res_th["持股比例分母口径说明"])

        # 10. 验证 P3-6: 601658 quotes 总股本精确到个位，等于 120095053492
        q_bank = fin_gw.stock_quotes({"symbol": "601658"})
        self.assertEqual(q_bank.get("估值与基本盘面", {}).get("总股本_股"), 120095053492)

    def test_v191_issues_convergence_and_quality_check(self) -> None:
        """测试 v1.9.1 实测 10 项缺陷收敛与口径保障"""
        fin_gw = FinanceGateway()

        # 1. 验证 P1-1: 港股 f10 四组枚举异构解耦 (concepts/industry, events/dividends, indicators/financials, repurchases/buyback)
        f_concepts = fin_gw.stock_f10({"symbol": "00700", "category": "concepts"})
        f_industry = fin_gw.stock_f10({"symbol": "00700", "category": "industry"})
        self.assertNotEqual(f_concepts["category_name"], f_industry["category_name"])
        self.assertIn("concepts", f_concepts.get("formatted_tables", {}))
        self.assertIn("industry", f_industry.get("formatted_tables", {}))

        f_events = fin_gw.stock_f10({"symbol": "00700", "category": "events"})
        f_dividends = fin_gw.stock_f10({"symbol": "00700", "category": "dividends"})
        self.assertNotEqual(f_events["category_name"], f_dividends["category_name"])
        self.assertIn("events", f_events.get("formatted_tables", {}))
        self.assertIn("dividends", f_dividends.get("formatted_tables", {}))

        f_indicators = fin_gw.stock_f10({"symbol": "00700", "category": "indicators"})
        f_financials = fin_gw.stock_f10({"symbol": "00700", "category": "financials"})
        self.assertNotEqual(f_indicators["category_name"], f_financials["category_name"])
        self.assertIn("indicators", f_indicators.get("formatted_tables", {}))
        self.assertIn("income_summary", f_financials.get("formatted_tables", {}))

        f_repurchases = fin_gw.stock_f10({"symbol": "00700", "category": "repurchases"})
        f_buyback = fin_gw.stock_f10({"symbol": "00700", "category": "buyback"})
        self.assertNotEqual(f_repurchases["category_name"], f_buyback["category_name"])
        self.assertIn("repurchases", f_repurchases.get("formatted_tables", {}))
        self.assertIn("buyback_summary", f_buyback.get("formatted_tables", {}))

        # 2. 验证 P1-2: A+H 港股 PE 股本口径披露 (02628 PE_TTM 披露全部已发行总股本口径)
        q_02628 = fin_gw.stock_quotes({"symbol": "02628"})
        val_02628 = q_02628.get("估值与基本盘面", {})
        self.assertIn("市盈率股本计算口径", val_02628)
        self.assertIn("全部已发行总股本(含A+H)乘H股价", val_02628["市盈率股本计算口径"])
        self.assertIn("A+H两地上市标的市盈率全股本口径", val_02628.get("估值口径说明", ""))

        # 3. 验证 P2-3: stock_quotes 校验 setcode (错误市场抛出 ValueError)
        with self.assertRaises(ValueError):
            fin_gw.stock_quotes({"symbol": "600875", "setcode": "0"})
        with self.assertRaises(ValueError):
            fin_gw.stock_quotes({"symbol": "601658", "setcode": "0"})

        # 4. 验证 P2-4: 港股 1m 上限扩容至 500 且支持截断提示
        k_1m = fin_gw.stock_kline({"symbol": "00688", "period": "1m", "count": 500})
        bars = len(k_1m.get("klines", []))
        # 盘中只生成到当前时点：按港股交易时段算出应有根数，避免把"还没走完的分钟"当成缺陷
        now = datetime.datetime.now()
        start, noon, resume, close = (datetime.time(9, 30), datetime.time(12, 0),
                                      datetime.time(13, 0), datetime.time(16, 10))
        t = now.time()
        if t < start:
            expected = 0
        elif t <= noon:
            expected = (now.hour * 60 + now.minute) - (9 * 60 + 30) + 1
        elif t < resume:
            expected = 151
        elif t <= close:
            expected = 151 + ((now.hour * 60 + now.minute) - 13 * 60) + 1
        else:
            expected = 332
        self.assertLessEqual(bars, 500, "1m 根数不得超过 500 上限")
        self.assertGreaterEqual(bars, max(0, expected - 5), f"1m 根数({bars})明显少于当前时段应有根数({expected})")
        self.assertLessEqual(bars, expected + 5)
        k_1m_trunc = fin_gw.stock_kline({"symbol": "00688", "period": "1m", "count": 200})
        self.assertIn("截断说明", k_1m_trunc)

        # 5. 验证 P2-5: 02628 现金流模板标志与行业类型闭合 (金融保险)
        cf_02628 = fin_gw.stock_financials({"symbol": "02628", "report_type": "cashflow", "limit_periods": 1})
        self.assertTrue(cf_02628.get("is_financial"))
        std_text = cf_02628.get("accounting_standard", "") or cf_02628.get("准则类别", "")
        self.assertIn("保险", std_text)
        bal_00700 = fin_gw.stock_financials({"symbol": "00700", "report_type": "balance", "limit_periods": 1})
        self.assertFalse(bal_00700.get("is_financial"))

        # 6. 验证 P2-6: A 股三表日期列机器可读 (截止日期与报告期日期为 ISO-8601，报告期保留文字标签)
        inc_a = fin_gw.stock_financials({"symbol": "600519", "report_type": "income", "limit_periods": 1})
        first_stmt = inc_a.get("statements", [])[0]
        self.assertRegex(str(first_stmt.get("截止日期")), r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(str(first_stmt.get("报告期日期")), r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("报告期", first_stmt)

        # 7. 验证 P3-7: limit_up 历史行成交额缺失时具备明确补充说明
        res_lu = fin_gw.stock_f10({"symbol": "688499", "category": "limit_up"})
        lu_list = res_lu.get("formatted_tables", {}).get("limit_up_records", [])
        self.assertGreater(len(lu_list), 0)
        for item in lu_list:
            if not item.get("当日成交金额_万元"):
                self.assertTrue(len(str(item.get("成交额补充说明", ""))) > 0)

        # 8. 验证 P3-8: 600694 basic_info.table2 (ESG) 为空时注入客观说明
        b_600694 = fin_gw.stock_f10({"symbol": "600694", "category": "basic_info"})
        self.assertIn("ESG披露说明", b_600694)

        # 9. 验证 P3-9: 600875 unlocks 日期为 str，行内与表级状态不自相矛盾
        ul_600875 = fin_gw.stock_f10({"symbol": "600875", "category": "unlocks"})
        ul_rows = ul_600875.get("formatted_tables", {}).get("unlock_schedule", [])
        self.assertGreater(len(ul_rows), 0)
        for row in ul_rows:
            self.assertIsInstance(row.get("解禁日期"), str)
            if "公告日期" in row and row.get("公告日期"):
                self.assertIsInstance(row.get("公告日期"), str)
        if any("未实施" in str(r.get("解禁进度", "")) for r in ul_rows):
            status_desc = ul_600875.get("解禁状态说明", "")
            self.assertIn("未实施", status_desc)
            self.assertNotIn("已实施完成，当前处于全流通状态", status_desc)

        # 10. 验证 P3-10: code_docs_search 元数据准确，查 stock_kline 复权优先返回 MCP 工具官方文档
        import logging
        import tempfile
        from pathlib import Path
        from mcp_gateway.search import SearchGateway
        sg = SearchGateway(
            {"cooldown_seconds": 60, "providers": [
                {"id": "context7", "type": "context7", "enabled": True, "endpoint": "https://context7.com/api/v3/search"}
            ]},
            Path(tempfile.mkdtemp()) / "state.json",
            logging.getLogger("test")
        )
        docs = sg._context7("https://context7.com/api/v3/search", "", "stock_kline 复权", 5, {}, 10)
        self.assertTrue(len(docs) > 0)
        self.assertNotIn("[Unknown]", docs[0].get("title", ""))
        self.assertIn("stock_kline", docs[0].get("title", ""))

    # =========================================================================
    # 【功能块：新一轮 A~J 缺陷收敛验证测试集】
    # =========================================================================
    def test_issue_a_lhb_double_nesting_and_false_negative_fixed(self):
        """验证问题 A (P1): stock_f10 lhb 双层嵌套解开，内外 source 统一，杜绝假否定句"""
        res_600875 = self.gw.stock_f10({"symbol": "600875", "category": "lhb"})
        tables = res_600875.get("formatted_tables", {})
        self.assertIn("billboard_records", tables)
        self.assertIn("seat_details", tables)
        self.assertNotIn("lhb_daily_details", tables)  # P2-1 剔除同哈希重复表
        self.assertGreater(len(tables["billboard_records"]), 0)
        self.assertIsNone(res_600875.get("无记录说明"))
        self.assertEqual(res_600875.get("source"), "eastmoney_billboard (东方财富龙虎榜官方数据专线)")

    def test_issue_b_and_d_hk_buyback_aggregation_and_placeholder_filter(self):
        """验证问题 B (P1) 与 D (P2): 港股 buyback_summary 聚合求和与 02628 回购占位行过滤"""
        # 1. 验证 00688 buyback_summary 累加非 0
        res_00688 = self.gw.stock_f10({"symbol": "00688", "category": "buyback"})
        bb_list = res_00688.get("formatted_tables", {}).get("buyback_summary", [])
        self.assertGreater(len(bb_list), 0)
        bb_sum = bb_list[0]
        self.assertGreater(bb_sum.get("累计回购总金额_万港元", 0.0), 0.0)
        self.assertGreater(bb_sum.get("累计回购股数_万股", 0.0), 0.0)
        self.assertGreater(bb_sum.get("回购实施记录天数", 0), 0)

        # 2. 验证 02628 过滤暂无数据占位行
        res_02628_rep = self.gw.stock_f10({"symbol": "02628", "category": "repurchases"})
        clean_rep = res_02628_rep.get("formatted_tables", {}).get("repurchases", [])
        self.assertEqual(len(clean_rep), 0)
        res_02628_bb = self.gw.stock_f10({"symbol": "02628", "category": "buyback"})
        bb_02628_sum = res_02628_bb.get("formatted_tables", {}).get("buyback_summary", [])[0]
        self.assertEqual(bb_02628_sum.get("回购实施记录天数"), 0)
        self.assertIsNone(bb_02628_sum.get("最新回购日期"))

    def test_issue_c_hk_top_holders_aging_gap_warning(self):
        """验证问题 C (P2): 港股 top_holders 股东口径说明提示最新申报可能多年前"""
        res_00688 = self.gw.stock_f10({"symbol": "00688", "category": "top_holders"})
        note = res_00688.get("股东口径说明", "")
        self.assertIn("时效缺口风险提示", note)
        self.assertIn("数年前", note)

    def test_issue_e_hk_minute_count_limit_notice(self):
        """验证问题 E (P2): 港股 60m/30m/15m count 超限提示"""
        res_60m = self.gw.stock_kline({"symbol": "00688", "period": "60m", "count": 30})
        res_60m_cap = self.gw.stock_kline({"symbol": "00688", "period": "60m", "count": 6})
        bars_60m = len(res_60m.get("klines", []))
        self.assertLessEqual(bars_60m, 6, "60m 单日最多 6 根")
        self.assertEqual(bars_60m, len(res_60m_cap.get("klines", [])), "超限请求应截断到实际可生成根数，而不是多造根")
        self.assertIn("超限提示", res_60m)
        self.assertIn("大于港股当日该周期实际可生成的最大根数", res_60m["超限提示"])

    def test_issue_f_hk_basic_info_field_mapping(self):
        """验证问题 F (P3): 港股 basic_info 多字段空串消除与真实数据对齐"""
        res_basic = self.gw.stock_f10({"symbol": "00688", "category": "basic_info"})
        table = res_basic.get("formatted_tables", {}).get("basic_info", [])
        self.assertGreater(len(table), 0)
        row0 = table[0]
        self.assertTrue(len(row0.get("注册地址", "")) > 0)
        self.assertTrue(len(row0.get("办公地址", "")) > 0)
        self.assertTrue(len(row0.get("官方网址", "")) > 0)
        self.assertTrue(len(row0.get("电子邮箱", "")) > 0)
        self.assertTrue(len(row0.get("董事长", "")) > 0)

    def test_issue_g_and_h_ashare_financials_currency_and_parent_profit_alias(self):
        """验证问题 G 与 H (P3): A股三表统一挂载币种与单位，工商业与银行模板归母净利润别名镜像"""
        # 1. 验证工商业 (贵州茅台)
        inc_mt = self.gw.stock_financials({"symbol": "600519", "report_type": "income", "limit_periods": 1})
        self.assertEqual(inc_mt.get("币种"), "CNY")
        self.assertEqual(inc_mt.get("金额单位"), "元")
        stmt_mt = inc_mt.get("statements", [])[0]
        self.assertEqual(stmt_mt.get("币种"), "CNY")
        self.assertEqual(stmt_mt.get("金额单位"), "元")
        self.assertIn("归母净利润", stmt_mt)
        self.assertIn("归属于母公司股东的净利润", stmt_mt)
        self.assertEqual(stmt_mt["归母净利润"], stmt_mt["归属于母公司股东的净利润"])

        # 2. 验证银行/金融 (邮储银行 601658)
        inc_bank = self.gw.stock_financials({"symbol": "601658", "report_type": "income", "limit_periods": 1})
        self.assertEqual(inc_bank.get("币种"), "CNY")
        self.assertEqual(inc_bank.get("金额单位"), "元")
        stmt_bank = inc_bank.get("statements", [])[0]
        self.assertEqual(stmt_bank.get("币种"), "CNY")
        self.assertEqual(stmt_bank.get("金额单位"), "元")
        self.assertIn("归母净利润", stmt_bank)
        self.assertIn("归属于母公司股东的净利润", stmt_bank)
        self.assertEqual(stmt_bank["归母净利润"], stmt_bank["归属于母公司股东的净利润"])

    def test_issue_i_screener_buffer_and_filter_notes(self):
        """验证问题 I (P3): screener 的分页与 ST 过滤自陈说明"""
        res = self.gw.stock_screener({"message": "主板 绩优 储能", "limit": 3, "filter_st": True})
        self.assertIn("分页与过滤说明", res)
        # 验证该切片内 returned + filtered_st == limit
        self.assertEqual(len(res.get("stocks", [])) + res.get("filtered_st_count", 0), 3)

    def test_p1_screener_pagination_no_doubling(self):
        """验证最新 P1: stock_screener 分页步长严格为 limit，杜绝偏移量翻倍与漏标的"""
        # 测试 '破净 银行'，两页各自返回 10 只，无交集，并集满 20 只
        p1 = self.gw.stock_screener({"message": "破净 银行", "limit": 10, "page": 1, "filter_st": True})
        p2 = self.gw.stock_screener({"message": "破净 银行", "limit": 10, "page": 2, "filter_st": True})
        c1 = [s["code"] for s in p1.get("stocks", [])]
        c2 = [s["code"] for s in p2.get("stocks", [])]
        self.assertEqual(len(c1), 10)
        self.assertEqual(len(c2), 10)
        self.assertEqual(len(set(c1) & set(c2)), 0)  # 两页严格无交集
        self.assertEqual(len(set(c1) | set(c2)), 20)  # 并集严格为 20 只

    def test_p2_equity_special_reserve_diff_and_treasury_stock(self):
        """验证最新 P2: 权益分区专项储备不可加总差额自陈与库存股抵减项规范标注"""
        # 1. 验证 600875 (东方电气) 4期报表自陈专项储备差额 (约为 135.57M)
        res_600875 = self.gw.stock_financials({"symbol": "600875", "report_type": "balance", "limit_periods": 1})
        st_600875 = res_600875.get("statements", [])[0]
        self.assertIn("归母所有者权益不可加总差额_专项储备及其他权益项等", st_600875)
        diff_val = st_600875["归母所有者权益不可加总差额_专项储备及其他权益项等"]
        self.assertGreater(diff_val, 130000000.0)  # 约 1.35 亿元
        self.assertLess(diff_val, 140000000.0)

        # 2. 验证 002293 (罗莱生活) 库存股收敛唯一规范键与抵减项权益闭合 (修复 P2-2)
        res_002293 = self.gw.stock_financials({"symbol": "002293", "report_type": "balance", "limit_periods": 1})
        st_002293 = res_002293.get("statements", [])[0]
        self.assertEqual(st_002293.get("库存股"), 6060960.0)
        self.assertNotIn("库存股_抵减项", st_002293)
        self.assertNotIn("减_库存股", st_002293)
        # 扣除库存股后，所有者权益明细与归母总额完全闭合，无不可加总差额
        self.assertNotIn("归母所有者权益不可加总差额_专项储备及其他权益项等", st_002293)

    def test_issue_j_indicators_report_date_alignment(self):
        """验证问题 J (P3): indicators 报告期与截止日期两市对齐"""
        # A 股
        ind_a = self.gw.stock_f10({"symbol": "600519", "category": "indicators"})
        row_a = ind_a.get("formatted_tables", {}).get("table4", [])[0]
        self.assertIn("报告期", row_a)
        self.assertIn("截止日期", row_a)
        self.assertEqual(row_a["报告期"], row_a["截止日期"])

        # 港股
        ind_hk = self.gw.stock_f10({"symbol": "00700", "category": "indicators"})
        row_hk = ind_hk.get("formatted_tables", {}).get("indicators", [])[0]
        self.assertIn("报告期", row_hk)
        self.assertIn("截止日期", row_hk)
        self.assertRegex(str(row_hk["报告期"]), r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(str(row_hk["截止日期"]), r"^\d{4}-\d{2}-\d{2}$")


    def test_f10_table6_pe_ttm_standard_naming_and_alignment(self):
        """验证最新问题 1: F10 table6 滚动市盈率_PETTM 标准命名与 quotes 滚动市盈率 100% 对齐"""
        # 1. 验证 601658 (邮储银行) F10 summary table6
        res_f10 = self.gw.stock_f10({"symbol": "601658", "category": "summary"})
        t6_rows = res_f10.get("formatted_tables", {}).get("table6", [])
        self.assertGreater(len(t6_rows), 0)
        t6_0 = t6_rows[0]
        # 验证包含标准名 滚动市盈率_PETTM 与规范别名 滚动市盈率_PE_TTM，且严格不含自相矛盾的 动态市盈率_PE_TTM
        self.assertIn("滚动市盈率_PETTM", t6_0)
        self.assertIn("滚动市盈率_PE_TTM", t6_0)
        self.assertNotIn("动态市盈率_PE_TTM", t6_0)
        self.assertIn("口径辨析提示", t6_0)
        self.assertEqual(t6_0["滚动市盈率_PETTM"], t6_0["滚动市盈率_PE_TTM"])

        # 2. 验证与 quotes 滚动市盈率_PETTM 保持同一口径 (约 7.34 倍)
        quotes = self.gw.stock_quotes({"symbol": "601658"})
        q_ttm = quotes.get("估值与基本盘面", {}).get("滚动市盈率_PETTM")
        self.assertIsNotNone(q_ttm)
        self.assertAlmostEqual(float(t6_0["滚动市盈率_PETTM"]), float(q_ttm), delta=0.5)

    # =========================================================================
    # 【功能块：验证实测 7 大新问题回归防护】
    # - 输入：FinanceGateway 各工具接口调用
    # - 功能：核验两融回填断档说明、PE别名剔除、同源声明、N码清理、浮点归一化及盘后字段缺省保底
    # - 输出：断言通过，无任何异常
    # =========================================================================
    def test_new_issues_empirical_fixes(self):
        """验证实测 7 大新问题全量回归防护"""
        import re

        # 1. 验证 P1: margin_trading 注入两融成交回填说明，非两融标的注入无记录说明
        res_margin = self.gw.stock_f10({"symbol": "600875", "category": "margin_trading"})
        self.assertIn("formatted_tables", res_margin)
        t0_margin = res_margin.get("formatted_tables", {}).get("table0", [])
        if t0_margin and any(r.get("成交量_股") is None for r in t0_margin):
            self.assertIn("两融成交回填说明", res_margin)

        res_margin_none = self.gw.stock_f10({"symbol": "300371", "category": "margin_trading"})
        self.assertIn("无记录说明", res_margin_none)

        # 2. 验证 P1/P2: summary table6 彻底剔除 '动态市盈率_PE_TTM'，顶层注入数据源同源说明
        res_sum = self.gw.stock_f10({"symbol": "600875", "category": "summary"})
        self.assertIn("数据源同源说明", res_sum)
        t6_rows = res_sum.get("formatted_tables", {}).get("table6", [])
        self.assertTrue(len(t6_rows) > 0)
        self.assertNotIn("动态市盈率_PE_TTM", t6_rows[0])
        self.assertIn("滚动市盈率_PETTM", t6_rows[0])
        self.assertIn("滚动市盈率_PE_TTM", t6_rows[0])

        # 3. 验证 P2: 利润表键名彻底清除原始 N 码（如 _原始N036, _原始N037, _N101 等）
        res_fin = self.gw.stock_f10({"symbol": "600875", "category": "financials"})
        inc_sheet = res_fin.get("formatted_tables", {}).get("利润表", [])
        if inc_sheet:
            for k in inc_sheet[0].keys():
                self.assertFalse(re.search(r"_原始N\d{2,4}", k), f"键名仍含原始N码残留: {k}")
                self.assertFalse(re.search(r"_N\d{2,4}$", k), f"键名仍含N码残留: {k}")

        # 4. 验证 P3: 个别票盘后字段为 0 而非 None/null (300371, 002293)
        q_371 = self.gw.stock_quotes({"symbol": "300371"})
        h_371 = q_371.get("盘口核心行情", {})
        self.assertIsNotNone(h_371.get("盘后成交量"))
        self.assertIsNotNone(h_371.get("剩余委托量"))
        self.assertIsInstance(h_371.get("盘后成交量"), int)
        self.assertIsInstance(h_371.get("剩余委托量"), int)

        q_293 = self.gw.stock_quotes({"symbol": "002293"})
        h_293 = q_293.get("盘口核心行情", {})
        self.assertIsNotNone(h_293.get("盘后成交量"))
        self.assertIsInstance(h_293.get("盘后成交量"), int)

        # 5. 验证 P3: 数值类型归一化 (float)，杜绝 int/float 漂移
        ctx = self.gw.market_context({"type": "events"})
        events_list = ctx.get("events", [])
        if events_list:
            for ev in events_list[:5]:
                if "当日涨跌幅_pct" in ev:
                    self.assertIsInstance(ev["当日涨跌幅_pct"], float)

        ctx_limit = self.gw.market_context({"type": "limit_up"})
        up_list = ctx_limit.get("limit_up_pool", [])
        if up_list:
            for up in up_list[:5]:
                if "当日成交金额_万元" in up:
                    self.assertIsInstance(up["当日成交金额_万元"], float)
                if "封单金额_万元" in up:
                    self.assertIsInstance(up["封单金额_万元"], float)

        # 6. 验证涨跌停价次日参考派生与口径说明 (彻底解决休市后 2% 隔日偏差)
        q_600875 = self.gw.stock_quotes({"symbol": "600875"})
        ext_875 = q_600875.get("估值与基本盘面", {})
        self.assertIn("当日涨停价", ext_875)
        self.assertIn("次日参考涨停价", ext_875)
        self.assertIn("次日参考跌停价", ext_875)
        self.assertIn("次日涨跌幅限制_pct", ext_875)
        self.assertIn("涨跌停价时效说明", ext_875)
        self.assertEqual(ext_875.get("次日涨跌幅限制_pct"), 10.0)

        # 7. 验证 capital_flow 注入资金流向口径说明与百分比数值规范化
        res_cf = self.gw.stock_f10({"symbol": "600875", "category": "capital_flow"})
        self.assertIn("资金流向口径说明", res_cf)
        cf_rows = res_cf.get("formatted_tables", {}).get("table0", [])
        if cf_rows:
            self.assertIn("主力净占比_pct", cf_rows[0])
            self.assertIsInstance(cf_rows[0]["主力净占比_pct"], float)

        # 8. 验证 shareholders 注入参考股价口径说明
        res_sh = self.gw.stock_f10({"symbol": "600875", "category": "shareholders"})
        self.assertIn("参考股价口径说明", res_sh)

        # 9. 验证 income 利润表中的利润表加总纪律说明与双键同值同源提示
        fin_inc = self.gw.stock_financials({"symbol": "600519", "report_type": "income", "limit_periods": 1})
        stmts = fin_inc.get("statements", [])
        self.assertTrue(len(stmts) > 0)
        self.assertIn("归母净利润双键同值同源提示", stmts[0].get("利润表加总纪律说明", ""))

    # =========================================================================
    # 【功能块：验证 Skill 与 Reference 资料库中数据接口与证券专用工具集 100% 对齐】
    # - 输入：FinanceGateway 实例, mcp_gateway.schemas, mcp_gateway.finance
    # - 功能：自动化核验 8 大金融工具命名、参数 Schema、count 边界(500)、候选为空自陈及文档对齐性
    # - 输出：断言通过，无任何异常
    # =========================================================================
    def test_skill_and_reference_api_alignment(self):
        """验证 Skill 与 Reference 资料库中数据接口与证券专用工具集的 8 大工具 100% 对齐"""
        from mcp_gateway.finance import get_finance_tool_specs
        from mcp_gateway.schemas import (
            STOCK_LOOKUP_SCHEMA,
            STOCK_QUOTES_SCHEMA,
            STOCK_KLINE_SCHEMA,
            STOCK_FINANCIALS_SCHEMA,
            STOCK_F10_SCHEMA,
            STOCK_SCREENER_SCHEMA,
            STOCK_NEWS_SCHEMA,
            MARKET_CONTEXT_SCHEMA,
        )

        tool_specs = get_finance_tool_specs(self.gw)
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
        self.assertEqual(set(tool_specs.keys()), expected_tools)

        # 1. 验证 stock_kline Schema 中的 count maximum 为 500（支持港股 1m 分钟线全天行情覆盖）
        kline_count_prop = STOCK_KLINE_SCHEMA["inputSchema"]["properties"]["count"]
        self.assertEqual(kline_count_prop.get("maximum"), 500)

        # 2. 验证 stock_lookup 在未精准命中与无实体时的防幻觉说明注入
        live_lookup = self.gw.stock_lookup({"query": "不存在的虚拟星际航行测试公司xyz", "range": "AG"})
        self.assertFalse(live_lookup.get("exact_match", True))
        self.assertIn("未命中说明", live_lookup)

        # 验证当底层实体列表为空时注入 '候选为空说明'
        with unittest.mock.patch.object(self.gw.tdx_client, "lookup_stock", return_value=[]):
            empty_lookup = self.gw.stock_lookup({"query": "纯空标的测试", "range": "AG"})
            self.assertFalse(empty_lookup.get("exact_match", True))
            self.assertEqual(empty_lookup.get("entity_count"), 0)
            self.assertIn("候选为空说明", empty_lookup)
            self.assertIn("未检索到与", empty_lookup["候选为空说明"])

        # 3. 验证 stock_f10 18 个 category 枚举定义与实际 handler 均能合法响应
        f10_cats = STOCK_F10_SCHEMA["inputSchema"]["properties"]["category"]["enum"]
        self.assertEqual(len(f10_cats), 18)
        self.assertIn("indicators", f10_cats)
        self.assertIn("financials", f10_cats)
        self.assertIn("lhb", f10_cats)
        self.assertIn("buyback", f10_cats)
        self.assertIn("repurchases", f10_cats)

    # =========================================================================
    # 【功能块：最新实测口径与缺陷回归验证用例集 (P1-1, P2-1, P2-2, P2-4, P3-1)】
    # =========================================================================
    def test_p1_1_cost_sum_residual_closure(self):
        """验证 P1-1: 营业总成本合计补入营业税金及附加，科目完全闭合，虚报缺口彻底消除"""
        stocks = ["002293", "688499", "300687", "600694", "300371"]
        for sym in stocks:
            res = self.gw.stock_financials({"symbol": sym, "report_type": "income", "limit_periods": 1})
            st = res.get("statements", [])[0]
            # 真实残差为 0，不应存在虚假差额说明
            self.assertNotIn("营业总成本构成差额说明", st, f"标的 {sym} 不应报出虚假营业总成本差额")
            self.assertIn("营业总成本构成说明", st)
            self.assertIn("科目完全闭合", st["营业总成本构成说明"])

    def test_p2_1_table_deduplication(self):
        """验证 P2-1: 清除单响应内同名数据双表，杜绝 49 组同哈希重复表造成双计"""
        sym = "600875"
        # 1. limit_up
        res_lu = self.gw.stock_f10({"symbol": sym, "category": "limit_up"})
        lu_tables = res_lu.get("formatted_tables", {})
        self.assertIn("limit_up_records", lu_tables)
        self.assertNotIn("table0", lu_tables)

        # 2. lhb
        res_lhb = self.gw.stock_f10({"symbol": sym, "category": "lhb"})
        lhb_tables = res_lhb.get("formatted_tables", {})
        self.assertIn("billboard_records", lhb_tables)
        self.assertIn("seat_details", lhb_tables)
        self.assertNotIn("lhb_daily_details", lhb_tables)

        # 3. unlocks
        res_unl = self.gw.stock_f10({"symbol": sym, "category": "unlocks"})
        unl_tables = res_unl.get("formatted_tables", {})
        self.assertIn("unlock_schedule", unl_tables)
        self.assertNotIn("table0", unl_tables)

        # 4. indicators
        res_ind = self.gw.stock_f10({"symbol": sym, "category": "indicators"})
        ind_tables = res_ind.get("formatted_tables", {})
        self.assertIn("table0", ind_tables)
        self.assertIn("table4", ind_tables)
        self.assertIn("table6", ind_tables)
        self.assertIn("table8", ind_tables)
        self.assertNotIn("profitability", ind_tables)
        self.assertNotIn("indicators", ind_tables)
        self.assertNotIn("valuation", ind_tables)
        self.assertNotIn("summary", ind_tables)

    def test_p2_2_treasury_stock_single_canonical_key(self):
        """验证 P2-2: 库存股收敛唯一规范键【库存股】，彻底切除【减_库存股】与【库存股_抵减项】"""
        stocks = ["688499", "002293"]
        for sym in stocks:
            res = self.gw.stock_financials({"symbol": sym, "report_type": "balance", "limit_periods": 1})
            st = res.get("statements", [])[0]
            self.assertIn("库存股", st)
            self.assertGreater(float(st["库存股"]), 0.0)
            self.assertNotIn("库存股_抵减项", st)
            self.assertNotIn("减_库存股", st)

    def test_p2_4_loss_pe_negative_status_and_warning(self):
        """验证 P2-4: 亏损股负PE显式标注【亏损不可用】状态并提供高危警示"""
        res_q = self.gw.stock_quotes({"symbol": "300687"})
        base = res_q.get("估值与基本盘面", {})
        self.assertEqual(base.get("市盈率可用状态"), "亏损不可用")
        self.assertTrue(base.get("市盈率亏损标记"))
        self.assertIn("亏损股市盈率不可用", base.get("估值口径说明", ""))

        res_f10 = self.gw.stock_f10({"symbol": "300687", "category": "indicators"})
        t6 = res_f10.get("formatted_tables", {}).get("table6", [])
        self.assertTrue(len(t6) > 0)
        self.assertEqual(t6[0].get("市盈率可用状态"), "亏损不可用")
        self.assertTrue(t6[0].get("市盈率亏损标记"))
        self.assertIn("亏损警示", t6[0])

    def test_p3_1_stock_news_strictly_reverse_chronological(self):
        """验证 P3-1: stock_news 返回新闻严格按时间倒序排列 (首条为最新)"""
        for sym in ["600875", "600371"]:
            res = self.gw.stock_news({"symbol": sym, "limit": 5})
            news = res.get("news", [])
            self.assertGreaterEqual(len(news), 2)
            for i in range(len(news) - 1):
                t_curr = str(news[i].get("time") or news[i].get("datetime") or "")
                t_next = str(news[i + 1].get("time") or news[i + 1].get("datetime") or "")
                self.assertGreaterEqual(t_curr, t_next, f"标的 {sym} 新闻未按时间倒序: {t_curr} < {t_next}")


    def test_p1_1_financial_balance_asset_coverage_gap(self):
        """验证 P1-1: 金融分支资产侧明细覆盖低，自陈资产总计不可加总差额及结构限制说明"""
        # 银行标的 601658 (邮储银行)
        res_bank = self.gw.stock_financials({"symbol": "601658", "report_type": "balance", "limit_periods": 1})
        st_bank = res_bank.get("statements", [])[0]
        self.assertIn("资产总计不可加总差额_金融投资及信贷专有科目缺项", st_bank)
        self.assertIn("金融资产结构限制说明", st_bank)
        self.assertGreater(st_bank["资产总计不可加总差额_金融投资及信贷专有科目缺项"], 5000000000000.0)  # > 5 万亿

        # 保险标的 601336 (新华保险)
        res_ins = self.gw.stock_financials({"symbol": "601336", "report_type": "balance", "limit_periods": 1})
        st_ins = res_ins.get("statements", [])[0]
        self.assertIn("资产总计不可加总差额_金融投资及信贷专有科目缺项", st_ins)
        self.assertIn("金融资产结构限制说明", st_ins)
        self.assertGreater(st_ins["资产总计不可加总差额_金融投资及信贷专有科目缺项"], 700000000000.0)  # > 7 千亿

    def test_p2_1_quotes_pb_note_financial_deduction(self):
        """验证 P2-1: 金融股与含永续债标的 PB 提示必须扣除其他权益工具"""
        res_q = self.gw.stock_quotes({"symbol": "600030"})
        ext = res_q.get("估值与基本盘面", {})
        note = ext.get("估值口径说明", "")
        self.assertIn("其他权益工具/优先股/永续债", note)
        self.assertIn("中信证券", note)

    def test_p2_2_table5_pledge_ratio_and_note(self):
        """验证 P2-2: summary table5 提供质押占总股本比_自算_pct 与质押比例口径说明"""
        res = self.gw.stock_f10({"symbol": "002293", "category": "summary"})
        t5 = res.get("formatted_tables", {}).get("table5", [])
        self.assertTrue(len(t5) > 0)
        self.assertIn("质押占总股本比_自算_pct", t5[0])
        self.assertIn("质押比例口径说明", t5[0])
        self.assertIn("质押比例口径说明", res)

    def test_p3_1_indicators_table4_full_four_date_keys(self):
        """验证 P3-1: indicators table4 具备完整四键（报告期、截止日期、报告期日期、报告期标签）"""
        res = self.gw.stock_f10({"symbol": "600519", "category": "indicators"})
        t4 = res.get("formatted_tables", {}).get("table4", [])[0]
        self.assertIn("报告期", t4)
        self.assertIn("截止日期", t4)
        self.assertIn("报告期日期", t4)
        self.assertIn("报告期标签", t4)
        self.assertRegex(str(t4["报告期日期"]), r"^\d{4}-\d{2}-\d{2}$")

    def test_p3_2_dual_keys_for_yyzcb(self):
        """验证 P3-2: 600875 营业总成本不闭合时同时具备 营业总成本构成说明 与 营业总成本构成差额说明"""
        res = self.gw.stock_financials({"symbol": "600875", "report_type": "income", "limit_periods": 1})
        st = res.get("statements", [])[0]
        self.assertIn("营业总成本构成说明", st)
        self.assertIn("营业总成本构成差额说明", st)

    def test_p3_7_stock_kline_a_stock_truncation_notice(self):
        """验证 P3-7: A股 kline count=301 时返回截断说明"""
        res = self.gw.stock_kline({"symbol": "600519", "period": "1m", "count": 301})
        self.assertEqual(res.get("total_bars"), 300)
        self.assertIn("截断说明", res)
        self.assertIn("300", res["截断说明"])

    def test_q1_quotes_pb_calculated_for_capital_change_and_perpetual_bond(self):
        """验证 Q1: 600030 股本变动与永续债标的挂载 市净率_PB_自算_普通股 (真值约 1.3458)"""
        res_q = self.gw.stock_quotes({"symbol": "600030"})
        ext = res_q.get("估值与基本盘面", {})
        self.assertIn("市净率_PB_自算_普通股", ext)
        pb_calc = ext["市净率_PB_自算_普通股"]
        now_price = float(res_q["盘口核心行情"]["现价"])
        bps_self = float(ext["普通股每股净资产_BPS_自算"])
        self.assertAlmostEqual(float(pb_calc), round(now_price / bps_self, 4), places=3,
                               msg="自算普通股 PB 必须等于 现价 ÷ 普通股 BPS")
        note = ext.get("估值口径说明", "")
        self.assertIn("市净率_PB_自算_普通股", note)

    def test_q2_financial_cashflow_no_duplicate_main_keys(self):
        """验证 Q2: 金融现金流主表切除 现金及现金等价物净增加额_主表，杜绝遍历双计"""
        res_cf = self.gw.stock_financials({"symbol": "601336", "report_type": "cashflow", "limit_periods": 1})
        st = res_cf.get("statements", [])[0]
        self.assertNotIn("现金及现金等价物净增加额_主表", st)
        self.assertIn("现金及现金等价物净增加额", st)
        self.assertIn("补充资料_间接法", st)
        supp = st.get("补充资料_间接法", {})
        self.assertIn("补充资料_现金及现金等价物净增加额", supp)

    def test_q3_limit_up_no_empty_table0(self):
        """验证 Q3: 无涨停标的 (601658) 唯一下发 limit_up_records: []，彻底剔除空 table0"""
        res_lu = self.gw.stock_f10({"symbol": "601658", "category": "limit_up"})
        tables = res_lu.get("formatted_tables", {})
        self.assertNotIn("table0", tables)
        self.assertIn("limit_up_records", tables)
        self.assertEqual(len(tables["limit_up_records"]), 0)
        self.assertIn("无记录说明", res_lu)

    def test_q4_summary_table0_report_date_iso_format(self):
        """验证 Q4: summary.table0 业绩披露日期规范转换为 YYYY-MM-DD 或 None"""
        res_601658 = self.gw.stock_f10({"symbol": "601658", "category": "summary"})
        t0_601658 = res_601658.get("formatted_tables", {}).get("table0", [])[0]
        self.assertIn("业绩披露日期", t0_601658)
        self.assertEqual(t0_601658["业绩披露日期"], "2028-03-28")
        self.assertIn("业绩披露日期说明", t0_601658)

        res_002293 = self.gw.stock_f10({"symbol": "002293", "category": "summary"})
        t0_002293 = res_002293.get("formatted_tables", {}).get("table0", [])[0]
        self.assertEqual(t0_002293["业绩披露日期"], "2027-04-30")

    def test_q7_top_holders_periods_filtering(self):
        """验证 Q7: stock_f10 top_holders 支持 periods=1 过滤，将 18 期大幅压缩为最新 1 期 10 行"""
        res_th = self.gw.stock_f10({"symbol": "600875", "category": "top_holders", "periods": 1})
        tables = res_th.get("formatted_tables", {})
        t0 = tables.get("table0", [])
        t1 = tables.get("table1", [])
        self.assertEqual(len(t0), 1)
        self.assertEqual(len(t1), 10)
        self.assertIn("期次与截断说明", res_th)

    def test_p1_recalculate_pb_denominator_on_period_switch(self):
        """验证 P1: 300371 换期重算覆盖 PB 分母，普通股自算 PB 对齐最新三季报净资产 (1.6301 vs 1.63)"""
        res_q = self.gw.stock_quotes({"symbol": "300371"})
        ext = res_q.get("估值与基本盘面", {})
        self.assertEqual(ext.get("最新财报报告期"), "2026-09-30")
        now_price = float(res_q["盘口核心行情"]["现价"])
        bps = float(ext["每股净资产_BPS"])
        bps_self = float(ext["普通股每股净资产_BPS_自算"])
        self.assertEqual(ext.get("市净率_PB"), round(now_price / bps, 2))
        self.assertAlmostEqual(float(ext.get("市净率_PB_自算_普通股")), round(now_price / bps_self, 4), places=3)
        note = ext.get("派生指标换期说明", "")
        self.assertIn("PB 分母已同步换期重算", note)

    def test_p2_basic_info_table1_empty_note(self):
        """验证 P2: 600030 basic_info table1 为空时包含明确的 无记录说明 与 业务构成说明"""
        res_b = self.gw.stock_f10({"symbol": "600030", "category": "basic_info"})
        tables = res_b.get("formatted_tables", {})
        self.assertEqual(len(tables.get("table1", [])), 0)
        self.assertIn("无记录说明", res_b)
        self.assertIn("table1 业务构成表暂无明细数据", res_b["无记录说明"])
        self.assertIn("业务构成说明", res_b)

    def test_p2_change_period_note_bps_dilution_warning(self):
        """验证 P2: 600030 派生指标换期说明提示 BPS 分母旧股本与永续债偏差"""
        res_q = self.gw.stock_quotes({"symbol": "600030"})
        ext = res_q.get("估值与基本盘面", {})
        note = ext.get("派生指标换期说明", "")
        self.assertIn("行情 BPS", note)
        self.assertIn("分母仍基于报告期旧股本", note)
        self.assertIn("市净率_PB_自算_普通股", note)


    # =========================================================================
    # 【功能块 测试集：A股换期校准、PB昨收重算与实测乘数回归测试】
    # - 输入：标的代码 300371 (换期样本)
    # - 功能：验证 P2-1 市净率_PB_昨收_换期重算与校准说明挂载，验证 P2-2 实测乘数基于新 EPS 重算
    # - 输出：断言通过，消除同 payload 内自相矛盾与假阳性归因
    # =========================================================================
    def test_p2_1_and_p2_2_a_stock_pb_yishou_and_diff_factor(self):
        """验证 P2-1 & P2-2: 300371 换期后挂载 PB_昨收_换期重算与校准说明，实测乘数不产生假阳性"""
        res_q = self.gw.stock_quotes({"symbol": "300371"})
        ext = res_q.get("估值与基本盘面", {})
        self.assertEqual(ext.get("最新财报报告期"), "2026-09-30")
        self.assertIn("市净率_PB_昨收_换期重算", ext)
        self.assertIn("市净率_PB_昨收_校准说明", ext)
        self.assertAlmostEqual(ext.get("市净率_PB_昨收_换期重算"), 1.63, places=1)
        note = ext.get("派生指标换期说明", "")
        # 300371 标准乘数应匹配，不应出现异常股本变动误报
        self.assertNotIn("实测乘数为0.25", note)

    # =========================================================================
    # 【功能块 测试集：A股两融时差说明、K线技术指标别名说明与预测日期ISO化】
    # - 输入：标的代码 002293, 600519
    # - 功能：验证 P2-3 两融入库时差说明、P3-2 K线别名声明、P3-1 预测日期 ISO 化
    # - 输出：断言通过
    # =========================================================================
    def test_p2_3_margin_trading_latency_and_p3_kline_aliases(self):
        """验证 P2-3: 两融包含最新期入库时差说明; P3-2: K线包含指标别名声明"""
        res_margin = self.gw.stock_f10({"symbol": "002293", "category": "margin_trading"})
        self.assertIn("最新期入库时差说明", res_margin)
        self.assertIn("两融最新期入库时差提示", res_margin["最新期入库时差说明"])

        res_kline = self.gw.stock_kline({"symbol": "600519", "period": "daily", "count": 10, "with_indicators": True})
        self.assertIn("技术指标别名说明", res_kline)
        self.assertIn("boll_upper", res_kline["技术指标别名说明"])

    # =========================================================================
    # 【功能块 测试集：港股工商业实体嗅探纠偏与成本科目双向映射】
    # - 输入：标的代码 01186 (中国铁建), 01088 (中国神华)
    # - 功能：验证 P2-2 中国铁建 balance 与 income 均为 is_financial=False; P3-3 成本科目映射
    # - 输出：断言通过，工商业与金融业口径彻底解耦
    # =========================================================================
    def test_hk_p2_2_industrial_sniffer_and_p3_3_cost_mapping(self):
        """验证 HK P2-2: 01186 is_financial=False; HK P3-3: 01088 销售成本与营运支出双向对齐"""
        res_bal = self.gw.stock_financials({"symbol": "01186", "report_type": "balance", "limit_periods": 1})
        res_inc = self.gw.stock_financials({"symbol": "01186", "report_type": "income", "limit_periods": 1})
        self.assertFalse(res_bal.get("is_financial"))
        self.assertFalse(res_inc.get("is_financial"))
        self.assertIn("一般工商业", res_bal.get("accounting_standard", ""))

        res_shenhua = self.gw.stock_financials({"symbol": "01088", "report_type": "income", "limit_periods": 1})
        st_sh = res_shenhua.get("statements", [])[0]
        self.assertIn("销售成本", st_sh)
        self.assertIn("营运支出", st_sh)
        self.assertEqual(st_sh["销售成本"], st_sh["营运支出"])

    # =========================================================================
    # 【功能块 测试集：港股主要股东申报时效提示与规则表有效行数收敛】
    # - 输入：标的代码 00688, 00700
    # - 功能：验证 P2-4 港股大股东最新申报日期暴露与时效提示; 验证规则表数据有效行数=0
    # - 输出：断言通过，下游统计行数时杜绝假阳性
    # =========================================================================
    def test_hk_p2_4_top_holders_staleness_and_rules_tables_zero_rows(self):
        """验证 HK P2-4: top_holders 最新申报日期与时效提示; 机制表数据有效行数=0"""
        res_holders = self.gw.stock_f10({"symbol": "00688", "category": "top_holders"})
        self.assertIn("最新申报日期", res_holders)
        self.assertIn("最新申报时效提示", res_holders)
        self.assertIn("时效风险提示", res_holders.get("formatted_tables", {}).get("top_holders", [])[0])

        for cat in ("lhb", "unlocks", "capital_flow", "margin", "limit_up"):
            res_rule = self.gw.stock_f10({"symbol": "00700", "category": cat})
            self.assertEqual(res_rule.get("数据有效行数"), 0)
            self.assertEqual(res_rule.get("规则声明行数"), 1)
            self.assertIn("记录计数说明", res_rule)

        res_fc = self.gw.stock_f10({"symbol": "00700", "category": "forecast"})
        self.assertFalse(res_fc.get("是否包含未来预测记录"))
        self.assertEqual(res_fc.get("数据有效行数"), 0)

    def test_v196_empirical_new_issues_convergence(self):
        """测试 v1.9.6: 实测新发现的 22 项口径陷阱全面收敛回归验证"""
        # 1. 验证 02026 小马智行法定原币 USD 逆向还原与 BPS 纠偏 (P1-1, P1-3)
        ind_02026 = self.gw.stock_f10({"symbol": "02026", "category": "indicators"})
        d_02026 = ind_02026.get("data", {})
        self.assertEqual(d_02026.get("币种"), "USD")
        self.assertAlmostEqual(float(d_02026.get("每股净资产_BPS_元", 0)), 3.62, places=1)
        self.assertAlmostEqual(float(d_02026.get("营业收入_元", 0)), 70470000.0, delta=100000.0)
        self.assertIn("法定申报原币校准", str(d_02026.get("币种口径校准说明", "")))

        # 2. 验证 09988 阿里巴巴新财年首季不扣减上一财年年报与现金流报表类型 (P1-2, P2-4)
        ali_inc = self.gw.stock_financials({"symbol": "09988", "report_type": "income", "period_type": "quarter", "limit_periods": 1})
        s0 = ali_inc.get("statements", [])[0]
        self.assertEqual(s0.get("单期标签"), "一季报(第一季度单期/Q1)")
        self.assertIn("不扣减上一财年年报", str(s0.get("单季度计算说明", "")))

        ali_cf = self.gw.stock_financials({"symbol": "09988", "report_type": "cashflow", "limit_periods": 1})
        cf0 = ali_cf.get("statements", [])[0]
        self.assertEqual(cf0.get("报表类型"), "一季报")

        # 3. 验证 09988 stock_lookup 剥离架构后缀精准匹配 (P3-7)
        lk_ali = self.gw.stock_lookup({"query": "阿里巴巴", "range": "HK"})
        self.assertTrue(lk_ali.get("exact_match"))
        self.assertEqual(lk_ali.get("matched_code"), "09988")

        # 4. 验证 01088 神华持股比例分母口径说明与已发行股本口径说明 (P2-6, P3-5)
        sh_th = self.gw.stock_f10({"symbol": "01088", "category": "top_holders", "limit": 2})
        self.assertIn("持股比例分母口径提示", str(sh_th.get("持股比例分母口径说明", "")))
        self.assertIn("期次与截断说明", sh_th)
        self.assertEqual(len(sh_th.get("formatted_tables", {}).get("top_holders", [])), 2)

        sh_basic = self.gw.stock_f10({"symbol": "01088", "category": "basic_info"})
        self.assertIn("已发行股本口径说明", sh_basic)

        # 5. 验证 00700 腾讯 1m K线超限提示与分钟线跨日限制说明 (P2-2, P2-3)
        tx_kline = self.gw.stock_kline({"symbol": "00700", "period": "1m", "count": 500})
        self.assertIn("超限提示", tx_kline)
        self.assertIn("分钟线跨日限制说明", tx_kline)

        # 6. 验证 A 股 300371 table6 市净率_PB 别名与昨收口径说明 (P3-3, P2-1)
        a_f10 = self.gw.stock_f10({"symbol": "300371", "category": "summary"})
        t6 = a_f10.get("formatted_tables", {}).get("table6", [])
        self.assertTrue(len(t6) > 0)
        self.assertIn("市净率_PB", t6[0])
        self.assertIn("市净率_PB_昨收", t6[0])
        self.assertIn("市净率_PB_说明", t6[0])

        # 7. 验证 A 股 600519 count=500 时超限提示与截断说明并存 (P3-2)
        k_a = self.gw.stock_kline({"symbol": "600519", "period": "day", "count": 500})
        self.assertIn("截断说明", k_a)
        self.assertIn("超限提示", k_a)

    def test_hk_d1_to_d13_regressions(self) -> None:
        """测试港股实测 D1-D13 缺陷收敛与口径保障"""
        # =========================================================================
        # 1. D1 验证: 02026 (USD 编报) f10.indicators 全量逆向还原与各期汇率完全对齐
        # =========================================================================
        ind_res = self.gw.stock_f10({"symbol": "02026", "category": "indicators"})
        t_ind = ind_res.get("formatted_tables", {}).get("indicators", [])
        self.assertTrue(len(t_ind) >= 4)
        p0 = t_ind[0]
        # 毛利率约为 16.9%，毛利润(USD) / 营业收入(USD) 必须与毛利率一致，杜绝 115% 币种混装
        rev0 = float(p0.get("营业收入_元", 0))
        gp0 = float(p0.get("毛利润_元", 0))
        self.assertAlmostEqual(gp0 / rev0, 0.169, delta=0.01)
        # 验证 2025-09-30 期（第4期）无 4.33% 残留，营收精确为 60876000 USD
        p3 = next((p for p in t_ind if str(p.get("报告期", "")).startswith("2025-09-30")), None)
        if p3:
            self.assertEqual(int(float(p3.get("营业收入_元", 0))), 60876000)

        # =========================================================================
        # 2. D2 验证: 09988 (3月财年) 单期标签与做差逻辑，Q4 行成功保留且标签与自然季匹配
        # =========================================================================
        inc_res = self.gw.stock_financials({"symbol": "09988", "statement_type": "income", "period_type": "quarter"})
        inc_rows = inc_res.get("statements", [])
        q4_row = next((r for r in inc_rows if str(r.get("截止日期", "")).startswith("2026-03-31")), None)
        self.assertIsNotNone(q4_row, "09988 2026-03-31 财年末 Q4 必须存在")
        if q4_row:
            self.assertIn("Q4", str(q4_row.get("单期标签", "")))
        q3_row = next((r for r in inc_rows if str(r.get("截止日期", "")).startswith("2025-12-31")), None)
        if q3_row:
            self.assertIn("Q3", str(q3_row.get("单期标签", "")))
        q2_row = next((r for r in inc_rows if str(r.get("截止日期", "")).startswith("2025-09-30")), None)
        if q2_row:
            self.assertIn("Q2", str(q2_row.get("单期标签", "")))

        # =========================================================================
        # 3. D3 验证: 09988 每股收益_TTM 注入防误用警示说明
        # =========================================================================
        ind_988 = self.gw.stock_f10({"symbol": "09988", "category": "indicators"})
        t_988 = ind_988.get("formatted_tables", {}).get("indicators", [])
        if t_988:
            self.assertIn("每股收益_TTM_说明", t_988[0])
            self.assertIn("09988", t_988[0]["每股收益_TTM_说明"])
            self.assertIn("偏差", t_988[0]["每股收益_TTM_说明"])

        # =========================================================================
        # 4. D4, D5, D12, D13 验证: 港股 quotes 估值面板对齐 A 股键、普通股 PB 校准、02628 过期断言清理
        # =========================================================================
        q_988 = self.gw.stock_quotes({"symbol": "09988"})
        v_988 = q_988.get("估值与基本盘面", {})
        self.assertIn("市净率_PB_自算_普通股", v_988)
        self.assertIn("市净率_PB_F10", v_988)
        self.assertIn("最新财报报告期", v_988)
        self.assertIn("每股净资产_BPS", v_988)
        self.assertIn("报告期每股收益_EPS", v_988)
        self.assertIn("派生指标换期说明", v_988)
        # 09988 普通股 PB 应在 1.55~1.65 之间（已校准至归母净资产口径，非总权益 1.51）
        self.assertGreater(v_988["市净率_PB_自算_普通股"], 1.55)
        self.assertNotIn("1207.63", str(v_988.get("估值口径说明", "")))

        # =========================================================================
        # 5. D6 验证: 股本口径说明涵盖非 A+H 标的工商与二级市场时滞说明
        # =========================================================================
        b_tx = self.gw.stock_f10({"symbol": "00700", "category": "basic_info"})
        self.assertIn("非A+H标的", b_tx.get("已发行股本口径说明", ""))

        # =========================================================================
        # 6. D7, D8 验证: 股东空表文案与 A/H 分母动态自适应
        # =========================================================================
        h_883 = self.gw.stock_f10({"symbol": "00883", "category": "top_holders"})
        self.assertIn("未收录", str(h_883))

        th_939 = self.gw.stock_f10({"symbol": "00939", "category": "top_holders"})
        t_939 = th_939.get("formatted_tables", {}).get("top_holders", [])
        a_row = next((r for r in t_939 if "A" in str(r.get("股份类别", ""))), None)
        if a_row:
            self.assertIn("已发行A股", a_row.get("持股比例分母口径说明", ""))

        # =========================================================================
        # 7. D9, D10 验证: kline 分钟截断语病消除与真上限超限提示
        # =========================================================================
        k_5m = self.gw.stock_kline({"symbol": "00700", "period": "5m", "count": 100})
        self.assertIn("超限提示", k_5m)
        self.assertIn("66 根 5m bar", k_5m["超限提示"])
        if "截断说明" in k_5m:
            self.assertNotIn("上限 100", k_5m["截断说明"])

        # =========================================================================
        # 8. D11 验证: events 模板占位符 -- 已被真实字段替换渲染
        # =========================================================================
        ev_700 = self.gw.stock_f10({"symbol": "00700", "category": "events"})
        t_ev = ev_700.get("formatted_tables", {}).get("events", [])
        for row in t_ev:
            content = str(row.get("事件内容", ""))
            self.assertNotIn("--", content)

    def test_ashare_d1_to_d10_regressions(self):
        """全面回归验证 A 股实测缺陷 D1~D10：
        D1: lhb billboard 金额单位除以 10000 转换为万元，与席位明细量纲一致；
        D2: lhb 市场总成交额多日异动窗口说明与席位占比说明挂载；
        D3: 601336 保险负债侧未收录保险合同负债缺口自陈与金融负债结构限制说明；
        D4: 300371 table6 PB 换期自动重算与 PB校准说明挂载，quotes PB_F10 联动同步；
        D5: buyback 与 repurchases 参数分工、专属数据载荷与表关系说明；
        D6: limit_up 日K线全天总成交金额补齐与时段成交额口径说明；
        D7: 300371 利润表营业外收支自动补齐，四期营业利润+营业外收支=利润总额严格闭合；
        D8: 间接法调整项之和!=经营净额可用性限制声明；
        D9: dividends 除权除息日 '--' 占位区分不分配与待披露；
        D10: basic_info 字段完整性说明。
        """
        # --- D1, D2 验证 (600875) ---
        lhb = self.gw.stock_f10({"symbol": "600875", "category": "lhb"})
        bb = lhb.get("formatted_tables", {}).get("billboard_records", [])[0]
        self.assertIn("龙虎榜买入总额_万元", bb)
        self.assertLess(bb["龙虎榜买入总额_万元"], 1000000.0)  # ~13.4 亿元 = 13.4 万万元，绝非 134008 亿元
        self.assertGreater(bb["龙虎榜买入总额_万元"], 10000.0)
        net_calc = round(bb["龙虎榜买入总额_万元"] - bb["龙虎榜卖出总额_万元"], 2)
        self.assertAlmostEqual(net_calc, bb["龙虎榜净买入额_万元"], places=1)
        self.assertIn("市场总成交额_说明", bb)
        self.assertIn("成交额口径说明", lhb)
        seat = lhb.get("formatted_tables", {}).get("seat_details", [])[0]
        self.assertIn("占总成交比例_说明", seat)

        # --- D3 验证 (601336 新华保险) ---
        res_ins = self.gw.stock_financials({"symbol": "601336", "report_type": "balance", "limit_periods": 1})
        st_ins = res_ins.get("statements", [])[0]
        self.assertIn("负债合计不可加总差额_保险合同负债及金融专有科目缺项", st_ins)
        self.assertNotIn("负债总计不可加总差额", st_ins)  # 修复 P3-1: 剔除重复同值别名以防遍历双计
        self.assertGreater(st_ins["负债合计不可加总差额_保险合同负债及金融专有科目缺项"], 1500000000000.0)  # > 1.5 万亿
        self.assertIn("金融负债结构限制说明", st_ins)

        # --- D4 验证 (300371 换期校准 vs 300687 对照) ---
        f10_371 = self.gw.stock_f10({"symbol": "300371", "category": "indicators"})
        t6_371 = f10_371.get("formatted_tables", {}).get("table6", [{}])[0]
        t6_pb_371 = float(t6_371["市净率_PB"])
        self.assertIn("PB校准说明", t6_371)
        quotes_371 = self.gw.stock_quotes({"symbol": "300371"})
        ext_371 = quotes_371.get("估值与基本盘面", {})
        self.assertLess(abs(t6_pb_371 - float(ext_371["市净率_PB_F10"])), 0.05,
                        "F10 快照 PB 应与 quotes 重算 PB 同口径（允许盘中价格漂移）")

        f10_687 = self.gw.stock_f10({"symbol": "300687", "category": "indicators"})
        t6_687 = f10_687.get("formatted_tables", {}).get("table6", [{}])[0]
        self.assertIsNone(t6_687.get("PB校准说明"))  # 对照票未滞后，不误校准

        # --- D5 验证 (688499 回购分工与表关系) ---
        bb_res = self.gw.stock_f10({"symbol": "688499", "category": "buyback"})
        rp_res = self.gw.stock_f10({"symbol": "688499", "category": "repurchases"})
        self.assertIn("表关系说明", bb_res)
        self.assertIn("表关系说明", rp_res)
        self.assertIsInstance(bb_res["data"], dict)
        self.assertIn("summary", bb_res["data"])
        self.assertIsInstance(rp_res["data"], list)

        # --- D6 验证 (600875 limit_up 全天成交额与口径) ---
        lu_res = self.gw.stock_f10({"symbol": "600875", "category": "limit_up"})
        self.assertIn("涨停成交额口径说明", lu_res)
        lu_records = lu_res.get("formatted_tables", {}).get("limit_up_records", [])
        self.assertGreater(len(lu_records), 0)
        r0 = lu_records[0]
        self.assertIn("全天总成交金额_日K_万元", r0)
        self.assertIn("成交额补充说明", r0)

        # --- D7 验证 (300371 利润表营业外收支闭合) ---
        inc_res = self.gw.stock_financials({"symbol": "300371", "report_type": "income", "limit_periods": 4})
        for st in inc_res.get("statements", []):
            yylr = float(st.get("营业利润") or 0.0)
            in_amt = float(st.get("营业外收入") or 0.0)
            out_amt = float(st.get("营业外支出") or 0.0)
            lrzj = float(st.get("利润总额") or 0.0)
            self.assertAlmostEqual(yylr + in_amt - out_amt, lrzj, places=2)
            self.assertIn("利润总额核算说明", st)

        # --- D8 验证 (600875 现金流间接法声明) ---
        cf_res = self.gw.stock_financials({"symbol": "600875", "report_type": "cashflow", "limit_periods": 1})
        st_cf = cf_res.get("statements", [])[0]
        supp_cf = st_cf.get("补充资料_间接法", {})
        self.assertIn("间接法调整项可用性说明", supp_cf)

        # --- D9 验证 (dividends 除权除息日说明) ---
        div_688 = self.gw.stock_f10({"symbol": "688499", "category": "dividends"})
        r_688 = div_688.get("formatted_tables", {}).get("dividends", [{}])[0]
        self.assertIn("不分配利润，不适用除权除息", r_688.get("除权除息日说明", ""))

        div_ins = self.gw.stock_f10({"symbol": "601336", "category": "dividends"})
        r_ins = div_ins.get("formatted_tables", {}).get("dividends", [{}])[0]
        self.assertIn("待实施公告披露", r_ins.get("除权除息日说明", ""))

        # --- D10 验证 (basic_info 字段完整性说明) ---
        bi_res = self.gw.stock_f10({"symbol": "600875", "category": "basic_info"})
        self.assertIn("字段完整性说明", bi_res)

    # =========================================================================
    # 【功能块：最新实测口径与缺陷回归验证用例集 (P1-1, P1-2, P1-3, P2-1, P2-3, P2-5, P3-1, P3-2)】
    # - 输入：self.gw (FinanceGateway 实例)，以及各典型测试标的代码
    # - 功能：全量闭环验证本轮 8 项核心缺陷修复与口径校准：
    #         1. P1-1: 600030 中信证券普通股真实 PB 对齐与防反向滞后误校准
    #         2. P1-2: 002594 比亚迪专项储备与外币报表折算差额同值双计排重与自陈
    #         3. P1-3: 002223 鱼跃医疗 PETTM 比对阈值收紧至 0.05 实现紧密对齐
    #         4. P2-1: 002698 博实股份 BelongHasKQZ 规范映射为是否含权证或转债
    #         5. P2-3: 业绩披露日期统一 ISO 标准化并自陈排期状态
    #         6. P2-5: 600519 贵州茅台经营活动现金流出负抵减结构说明挂载
    #         7. P3-1: 601336/601658 剔除负债总计不可加总差额重复别名
    #         8. P3-2: 002223 concepts 概念表日期统一 ISO 标准化
    # - 输出：各断言校验通过 (无抛出 AssertionError)
    # =========================================================================
    def test_v197_p1_p2_p3_fixes(self) -> None:
        """测试 v1.9.7: P1-1/P1-2/P1-3/P2-1/P2-3/P2-5/P3-1/P3-2 全量闭环验证"""
        # 1. 验证 P1-1: 600030 中信证券 F10 table6 普通股 PB 对齐 quotes，不被反向误冲
        f10_600030 = self.gw.stock_f10({"symbol": "600030", "category": "indicators"})
        t6_600030 = f10_600030.get("formatted_tables", {}).get("table6", [{}])[0]
        q_600030 = self.gw.stock_quotes({"symbol": "600030"}).get("估值与基本盘面", {})
        self.assertAlmostEqual(float(t6_600030.get("市净率_PB")), float(q_600030.get("市净率_PB_自算_普通股")),
                               delta=0.05, msg="F10 快照 PB 与 quotes 自算 PB 应同口径（允许盘中漂移）")

        # 2. 验证 P1-2: 002594 比亚迪专项储备与外币折算差额同值双计排重与剔除自陈说明
        bs_byd = self.gw.stock_financials({"symbol": "002594", "report_type": "balance", "limit_periods": 4})
        st_byd = bs_byd.get("statements", [])[0]
        self.assertIn("所有者权益双计剔除说明", st_byd)
        diff_byd = float(st_byd.get("归母所有者权益不可加总差额_专项储备及其他权益项等") or 0.0)
        self.assertGreater(diff_byd, 0.0)
        self.assertLess(abs(diff_byd), 100000000.0)

        # 3. 验证 P1-3: 002223 鱼跃医疗 PETTM 阈值收紧至 0.05 完美对齐
        f10_2223 = self.gw.stock_f10({"symbol": "002223", "category": "indicators"})
        t6_2223 = f10_2223.get("formatted_tables", {}).get("table6", [{}])[0]
        q_2223 = self.gw.stock_quotes({"symbol": "002223"}).get("估值与基本盘面", {})
        self.assertAlmostEqual(float(t6_2223.get("滚动市盈率_PETTM")), float(q_2223.get("滚动市盈率_PETTM")), places=2)

        # 4. 验证 P2-1: 002698 博实股份 BelongHasKQZ 规范映射为【是否含权证或转债】
        q_2698 = self.gw.stock_quotes({"symbol": "002698"}).get("估值与基本盘面", {})
        self.assertNotIn("BelongHasKQZ", q_2698)
        self.assertIn("是否含权证或转债", q_2698)

        # 5. 验证 P2-3: 业绩披露日期规范化 ISO 格式并自陈排期状态
        f10_sum = self.gw.stock_f10({"symbol": "600030", "category": "summary"})
        t0_sum = f10_sum.get("formatted_tables", {}).get("table0", [{}])[0]
        self.assertIn("业绩披露日期说明", t0_sum)
        jjrq_val = t0_sum.get("业绩披露日期")
        if jjrq_val is not None:
            self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}$", str(jjrq_val)))

        # 6. 验证 P2-5: 600519 贵州茅台经营活动现金流出负抵减挂载结构说明
        cf_moutai = self.gw.stock_financials({"symbol": "600519", "report_type": "cashflow", "limit_periods": 4})
        has_cf_note = any("经营活动现金流出差额结构说明" in s for s in cf_moutai.get("statements", []))
        self.assertTrue(has_cf_note)

        # 7. 验证 P3-1: 601336 / 601658 剔除【负债总计不可加总差额】重复别名以防双计
        for sym in ("601336", "601658"):
            st_fin = self.gw.stock_financials({"symbol": sym, "report_type": "balance", "limit_periods": 1}).get("statements", [])[0]
            self.assertNotIn("负债总计不可加总差额", st_fin)

        # 8. 验证 P3-2: 002223 概念表日期统一 ISO 标准化
        c_2223 = self.gw.stock_f10({"symbol": "002223", "category": "concepts"})
        r0_c = c_2223.get("formatted_tables", {}).get("table0", [{}])[0]
        for dk in ("催化日期", "最新入选日期", "题材纳入日期"):
            if dk in r0_c and r0_c[dk]:
                self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}$", str(r0_c[dk])))


# =========================================================================
# 【第三道防翻车安全门：28 只核心行业标杆特种标的自动化碰撞测试套件】
# - 标的矩阵（全市场五大板块全面覆盖）：
#   【沪市主板】
#   1. 601658 邮储银行：银行业资产负债表与特有负债（吸收存款）、年化 EPS、深市两融 T+1 时差
#   2. 600030 中信证券：券商准则、永续债 PB 修正、客户资金存款双计剔除
#   3. 601336 新华保险：险企 CAS 25 保险合同负债 1.6 万亿缺口自陈、预案分红说明
#   4. 600875 东方电气：工业白马、龙虎榜多日窗口万元对齐买入13.4亿、日K成交额口径挂载
#   5. 600276 恒瑞医药：创新药龙头、高研发投入费用化/资本化核算与医药管线
#   6. 601012 隆基绿能：光伏硅片组件龙头、大额在建工程与存货跌价准备
#   7. 601933 永辉超市：连锁生鲜商超、新租赁准则使用权资产与租赁负债
#   【深市主板】
#   8. 002594 比亚迪：深市主板新能源汽车制造巨头，超大供应链制造体系、高应收应付周转、A/H 架构
#   9. 000063 中兴通讯：深市主板通信与算力网络龙头，A+H架构(00763)、高研发与合同资产、国际化汇率对冲
#   10. 002277 友阿股份：商业百货与区域新零售、投资性房地产公允价值计量
#   11. 002293 罗莱生活：家纺消费品龙头、营业总成本税金附加完全闭合标杆
#   【创业板】
#   12. 300124 汇川技术：创业板工控自动化白马龙头，创业板 20% 涨跌幅机制、高研发投入与股权激励、高 ROE
#   13. 300450 先导智能：创业板高端锂电装备制造，重订单非标定制化、大额发出商品与合同负债（预收款）、存货跌价准备
#   14. 300866 安克创新：跨境电商消费电子、全球化海外营收结算与供应链库存周转
#   【科创板】
#   15. 688777 中控技术：科创板高端工业软件与控制系统，软件增值税即征即退、政府补助进入其他收益、GDR 发行
#   16. 688008 澜起科技：科创板半导体芯片龙头，Fabless 轻资产运作、百亿级货币资金与理财资产（大存大贷辨析）、超高毛利率
#   【港股主板】
#   17. 02026 小马智行：港股/美股法定美元原币记账、东财汇率 6.8~7.1 倍反解还原
#   18. 09988 阿里巴巴：跨自然年 3 月财年、财年首季守护禁止跨年相减
#   19. 00700 腾讯控股：社交娱乐与数字经济巨头、单季前驱做差真实单季推算还原
#   20. 03690 美团：本地生活即时配送平台龙头、零售核心本地商业模式
#   21. 02628 中国人寿：大型国有寿险央企、全股本市值口径与 A+H 股比价
#   22. 00728 中国电信：基础电信运营商与产业数字化云网融合龙头
#   23. 09969 诺诚健华：创新抗癌靶向药龙头、18A 生物医药研发与商业化
#   24. 03908 中金公司：头部顶级投资银行、跨境资本运作与投行业务
#   25. 00981 中芯国际：晶圆代工制造龙头、重资产资本开支与大额折旧摊销
#   26. 01810 小米集团：手机×AIoT生态龙头、智能电动汽车双轮驱动
#   27. 01585 雅迪控股：全球两轮电动车制造龙头、出海全球化供应链
#   28. 09880 优必选：人形机器人第一股、具身智能与智能制造商业化落地
# =========================================================================
class TestTwelveEliteStocksSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.gw = FinanceGateway()

    def test_01_postal_savings_bank_601658(self) -> None:
        """标杆 1：601658 邮储银行（沪市主板 - 银行业资产负债表与特有负债）"""
        res = self.gw.stock_financials({"symbol": "601658", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", res)
        st = res["statements"][0]
        self.assertTrue(any("存款" in k for k in st.keys()))

    def test_02_citic_securities_600030(self) -> None:
        """标杆 2：600030 中信证券（沪市主板 - 券商客户资金存款双计剔除与永续债修正）"""
        res = self.gw.stock_financials({"symbol": "600030", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", res)
        st = res["statements"][0]
        self.assertTrue(st.get("资产总计") is not None)

    def test_03_new_china_life_601336(self) -> None:
        """标杆 3：601336 新华保险（沪市主板 - 险企 CAS 25 保险合同负债 1.6 万亿缺口自陈与预案分红）"""
        res = self.gw.stock_financials({"symbol": "601336", "report_type": "balance", "limit_periods": 1})
        st = res["statements"][0]
        self.assertIn("负债合计不可加总差额_保险合同负债及金融专有科目缺项", st)
        self.assertIn("金融负债结构限制说明", st)

    def test_04_dongfang_electric_600875(self) -> None:
        """标杆 4：600875 东方电气（沪市主板 - 工业白马、龙虎榜万元绝对量纲买入13.4亿、日K全天成交额）"""
        lhb = self.gw.stock_f10({"symbol": "600875", "category": "lhb"})
        r0 = lhb.get("formatted_tables", {}).get("billboard_records", [])[0]
        buy_amt = float(r0.get("龙虎榜买入总额_万元") or r0.get("买入总额_万元"))
        sell_amt = float(r0.get("龙虎榜卖出总额_万元") or r0.get("卖出总额_万元"))
        net_amt = float(r0.get("龙虎榜净买入额_万元") or r0.get("净买入额_万元"))
        self.assertLess(buy_amt, 500000.0)  # 13.4亿=134007万元，绝不是 1.34亿万元
        self.assertAlmostEqual(buy_amt - sell_amt, net_amt, delta=1.0)

    def test_05_byd_002594(self) -> None:
        """标杆 5：002594 比亚迪（深市主板 - 新能源汽车制造巨头、千亿级制造供应链与应收应付周转）"""
        bs = self.gw.stock_financials({"symbol": "002594", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", bs)
        st = bs["statements"][0]
        self.assertIsNotNone(st.get("资产总计"))
        q = self.gw.stock_quotes({"symbol": "002594"})
        self.assertEqual(q.get("symbol"), "002594")

    def test_06_zte_000063(self) -> None:
        """标杆 6：000063 中兴通讯（深市主板 - 通信与算力网络龙头、A+H架构、高研发与合同资产）"""
        bs = self.gw.stock_financials({"symbol": "000063", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", bs)
        inc = self.gw.stock_financials({"symbol": "000063", "report_type": "income", "limit_periods": 1})
        self.assertIn("statements", inc)
        q = self.gw.stock_quotes({"symbol": "000063"})
        self.assertEqual(q.get("symbol"), "000063")

    def test_07_inovance_300124(self) -> None:
        """标杆 7：300124 汇川技术（创业板 - 工控自动化龙头、创业板20%涨跌幅、高研发投入与高ROE）"""
        bs = self.gw.stock_financials({"symbol": "300124", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", bs)
        q = self.gw.stock_quotes({"symbol": "300124"})
        self.assertEqual(q.get("symbol"), "300124")

    def test_08_lead_intelligent_300450(self) -> None:
        """标杆 8：300450 先导智能（创业板 - 高端锂电智能装备龙头、重订单非标定制、大额合同负债预收款）"""
        bs = self.gw.stock_financials({"symbol": "300450", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", bs)
        q = self.gw.stock_quotes({"symbol": "300450"})
        self.assertEqual(q.get("symbol"), "300450")

    def test_09_supcon_688777(self) -> None:
        """标杆 9：688777 中控技术（科创板 - 工业软件与控制系统龙头、软件退税与政府补助、GDR国际化）"""
        bs = self.gw.stock_financials({"symbol": "688777", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", bs)
        q = self.gw.stock_quotes({"symbol": "688777"})
        self.assertEqual(q.get("symbol"), "688777")

    def test_10_montage_tech_688008(self) -> None:
        """标杆 10：688008 澜起科技（科创板 - 半导体芯片设计龙头、Fabless轻资产、百亿现金理财大存大贷）"""
        bs = self.gw.stock_financials({"symbol": "688008", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", bs)
        q = self.gw.stock_quotes({"symbol": "688008"})
        self.assertEqual(q.get("symbol"), "688008")

    def test_11_pony_ai_02026(self) -> None:
        """标杆 11：02026 小马智行（港股主板 - 港股/美股法定美元原币记账与东财汇率反解）"""
        f10 = self.gw.stock_f10({"symbol": "02026", "category": "financials"})
        self.assertTrue("formatted_tables" in f10 or "data" in f10)

    def test_12_alibaba_09988(self) -> None:
        """标杆 12：09988 阿里巴巴（港股主板 - 跨自然年 3 月财年做差与首季守护）"""
        f10 = self.gw.stock_f10({"symbol": "09988", "category": "financials"})
        self.assertTrue("formatted_tables" in f10 or "data" in f10)

    def test_13_anker_innovations_300866(self) -> None:
        """标杆 13：300866 安克创新（创业板 - 跨境电商与消费电子、海外营收占比与供应链周转）"""
        res = self.gw.stock_financials({"symbol": "300866", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", res)
        st = res["statements"][0]
        self.assertIsNotNone(st.get("资产总计"))
        q = self.gw.stock_quotes({"symbol": "300866"})
        self.assertEqual(q.get("symbol"), "300866")

    def test_14_hengrui_medicine_600276(self) -> None:
        """标杆 14：600276 恒瑞医药（沪市主板 - 创新药龙头、高研发投入费用化/资本化核算）"""
        res = self.gw.stock_financials({"symbol": "600276", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", res)
        st = res["statements"][0]
        self.assertIsNotNone(st.get("资产总计"))
        q = self.gw.stock_quotes({"symbol": "600276"})
        self.assertEqual(q.get("symbol"), "600276")

    def test_15_longi_green_energy_601012(self) -> None:
        """标杆 15：601012 隆基绿能（沪市主板 - 光伏硅片与单晶组件龙头、大额在建工程与存货跌价准备）"""
        res = self.gw.stock_financials({"symbol": "601012", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", res)
        st = res["statements"][0]
        self.assertIsNotNone(st.get("资产总计"))
        q = self.gw.stock_quotes({"symbol": "601012"})
        self.assertEqual(q.get("symbol"), "601012")

    def test_16_yonghui_superstores_601933(self) -> None:
        """标杆 16：601933 永辉超市（沪市主板 - 连锁生鲜商超、新租赁准则使用权资产与租赁负债）"""
        res = self.gw.stock_financials({"symbol": "601933", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", res)
        st = res["statements"][0]
        self.assertIsNotNone(st.get("资产总计"))
        q = self.gw.stock_quotes({"symbol": "601933"})
        self.assertEqual(q.get("symbol"), "601933")

    def test_17_youa_group_002277(self) -> None:
        """标杆 17：002277 友阿股份（深市主板 - 商业百货与区域新零售、投资性房地产公允价值计量）"""
        res = self.gw.stock_financials({"symbol": "002277", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", res)
        st = res["statements"][0]
        self.assertIsNotNone(st.get("资产总计"))
        q = self.gw.stock_quotes({"symbol": "002277"})
        self.assertEqual(q.get("symbol"), "002277")

    def test_18_luolai_lifestyle_002293(self) -> None:
        """标杆 18：002293 罗莱生活（深市主板 - 家纺消费品龙头、营业总成本税金附加完全闭合标杆）"""
        res = self.gw.stock_financials({"symbol": "002293", "report_type": "balance", "limit_periods": 1})
        self.assertIn("statements", res)
        st = res["statements"][0]
        self.assertIsNotNone(st.get("资产总计"))
        q = self.gw.stock_quotes({"symbol": "002293"})
        self.assertEqual(q.get("symbol"), "002293")

    def test_19_tencent_00700(self) -> None:
        """标杆 19：00700 腾讯控股（港股主板 - 社交娱乐与云科技巨头、单季前驱做差真实单季还原）"""
        q = self.gw.stock_quotes({"symbol": "00700"})
        self.assertEqual(q.get("symbol"), "00700")
        f10 = self.gw.stock_f10({"symbol": "00700", "category": "financials"})
        self.assertTrue("formatted_tables" in f10 or "data" in f10 or "statements" in f10)

    def test_20_meituan_03690(self) -> None:
        """标杆 20：03690 美团（港股主板 - 本地生活即时配送平台、科技零售综合业务）"""
        q = self.gw.stock_quotes({"symbol": "03690"})
        self.assertEqual(q.get("symbol"), "03690")
        f10 = self.gw.stock_f10({"symbol": "03690", "category": "financials"})
        self.assertTrue("formatted_tables" in f10 or "data" in f10 or "statements" in f10)

    def test_21_china_life_02628(self) -> None:
        """标杆 21：02628 中国人寿（港股主板 - 大型国有寿险央企、全股本市值口径与A+H比价）"""
        q = self.gw.stock_quotes({"symbol": "02628"})
        self.assertEqual(q.get("symbol"), "02628")
        f10 = self.gw.stock_f10({"symbol": "02628", "category": "financials"})
        self.assertTrue("formatted_tables" in f10 or "data" in f10 or "statements" in f10)

    def test_22_china_telecom_00728(self) -> None:
        """标杆 22：00728 中国电信（港股主板 - 电信通信运营商与产业数字化云网融合龙头）"""
        q = self.gw.stock_quotes({"symbol": "00728"})
        self.assertEqual(q.get("symbol"), "00728")
        f10 = self.gw.stock_f10({"symbol": "00728", "category": "financials"})
        self.assertTrue("formatted_tables" in f10 or "data" in f10 or "statements" in f10)

    def test_23_innocare_pharma_09969(self) -> None:
        """标杆 23：09969 诺诚健华（港股主板 - 创新抗癌靶向药、18A生物科技标的研发与商业化）"""
        q = self.gw.stock_quotes({"symbol": "09969"})
        self.assertEqual(q.get("symbol"), "09969")
        f10 = self.gw.stock_f10({"symbol": "09969", "category": "financials"})
        self.assertTrue("formatted_tables" in f10 or "data" in f10 or "statements" in f10)

    def test_24_cicc_03908(self) -> None:
        """标杆 24：03908 中金公司（港股主板 - 头部顶级投资银行、跨境资本市场业务与机构经纪）"""
        q = self.gw.stock_quotes({"symbol": "03908"})
        self.assertEqual(q.get("symbol"), "03908")
        f10 = self.gw.stock_f10({"symbol": "03908", "category": "financials"})
        self.assertTrue("formatted_tables" in f10 or "data" in f10 or "statements" in f10)

    def test_25_smic_00981(self) -> None:
        """标杆 25：00981 中芯国际（港股主板 - 晶圆代工制造龙头、重资产资本开支与折旧摊销）"""
        q = self.gw.stock_quotes({"symbol": "00981"})
        self.assertEqual(q.get("symbol"), "00981")
        f10 = self.gw.stock_f10({"symbol": "00981", "category": "financials"})
        self.assertTrue("formatted_tables" in f10 or "data" in f10 or "statements" in f10)

    def test_26_xiaomi_01810(self) -> None:
        """标杆 26：01810 小米集团（港股主板 - 手机×AIoT生态龙头、智能电动汽车双轮驱动）"""
        q = self.gw.stock_quotes({"symbol": "01810"})
        self.assertEqual(q.get("symbol"), "01810")
        f10 = self.gw.stock_f10({"symbol": "01810", "category": "financials"})
        self.assertTrue("formatted_tables" in f10 or "data" in f10 or "statements" in f10)

    def test_27_yadea_01585(self) -> None:
        """标杆 27：01585 雅迪控股（港股主板 - 全球两轮电动车制造龙头、出海全球化供应链）"""
        q = self.gw.stock_quotes({"symbol": "01585"})
        self.assertEqual(q.get("symbol"), "01585")
        f10 = self.gw.stock_f10({"symbol": "01585", "category": "financials"})
        self.assertTrue("formatted_tables" in f10 or "data" in f10 or "statements" in f10)

    def test_28_ubtech_09880(self) -> None:
        """标杆 28：09880 优必选（港股主板 - 人形机器人第一股、具身智能与智能制造商业化）"""
        q = self.gw.stock_quotes({"symbol": "09880"})
        self.assertEqual(q.get("symbol"), "09880")
        f10 = self.gw.stock_f10({"symbol": "09880", "category": "financials"})
        self.assertTrue("formatted_tables" in f10 or "data" in f10 or "statements" in f10)


    # =========================================================================
    # 【功能块 8.8：港股 P1、P2、P3 级新缺陷与口径自陈综合验证回归测试】
    # - 输入：self.gw (FinanceGateway 实例), 涉及标的 01088, 02026, 00700, 00939, 00883 等
    # - 功能：
    #   1. P1-1: 验证 top_holders 股份类别判定逻辑，01088 (H Shares 判定为 H 股分母)、
    #      00700 (Ordinary Shares 判定为普通股分母)、02026 (判定为对应类别/其他非H股分母)；
    #   2. P1-2: 验证 00939 建设银行资产负债表挂载【归母所有者权益不可加总差额_优先股永续债及其他权益工具等】自陈；
    #   3. P2-1: 验证港股 1m K 线在 count=340 越界时同时下发【超限提示】与【截断说明】；
    #   4. P2-2: 验证 quotes 与 indicators 中关于汇率快照源独立性与隐含汇率差异自陈说明；
    #   5. P2-3: 验证 dividends 成功正则提取【每股派息_港元】数值并下发【分红数据提取说明】；
    #   6. P2-4: 验证 cashflow 挂载【现金流量表细项披露自陈说明】自陈季度结构降级；
    #   7. P2-5: 验证 quarter 单季度模式下【报告期】严格保持标准日期格式，中文季度移至【报告期标签】；
    #   8. P2-6: 验证 00939 银行利润表兼容映射【营业收入】并挂载【金融/银行收入与支出语义说明】；
    #   9. P3-1: 验证 quotes 顶层挂载【盘口深度说明】；
    #   10. P3-2: 验证 00883 空 top_holders 包含【无记录说明】；
    #   11. P3-3: 验证 00939 repurchases data 层双向镜像【无记录说明】；
    #   12. P3-4: 验证 buyback_summary 挂载【累计字段口径自陈】；
    #   13. P3-5: 验证 indicators 挂载【每股摊薄盈利披露说明】；
    #   14. P3-6: 验证 02026 现金口径说明覆盖极微小残差自陈。
    # - 输出：各断言验证全部通过
    # =========================================================================
    def test_hk_p1_p2_p3_comprehensive_fixes(self) -> None:
        """验证港股 P1-1 至 P3-6 全部缺陷修复与口径自陈"""
        # 1. 验证 P1-1: top_holders 股份类别分母判定 (01088, 00700, 02026)
        th_01088 = self.gw.stock_f10({"symbol": "01088", "category": "top_holders"})
        rows_01088 = th_01088.get("formatted_tables", {}).get("top_holders", [])
        for r in rows_01088:
            cat = str(r.get("股份类别") or r.get("类别") or "").upper()
            if "H" in cat and "SHARES" in cat:
                self.assertIn("H股股份总数", r.get("持股比例分母口径说明", ""))
                self.assertNotIn("A股股份总数", r.get("持股比例分母口径说明", ""))

        th_00700 = self.gw.stock_f10({"symbol": "00700", "category": "top_holders"})
        rows_00700 = th_00700.get("formatted_tables", {}).get("top_holders", [])
        for r in rows_00700:
            cat = str(r.get("股份类别") or r.get("类别") or "").upper()
            if "ORDINARY" in cat:
                self.assertIn("普通股", r.get("持股比例分母口径说明", ""))
                self.assertNotIn("已发行A股", r.get("持股比例分母口径说明", ""))

        th_02026 = self.gw.stock_f10({"symbol": "02026", "category": "top_holders"})
        rows_02026 = th_02026.get("formatted_tables", {}).get("top_holders", [])
        if rows_02026:
            for r in rows_02026:
                cat = str(r.get("股份类别") or r.get("类别") or "")
                if "其他" in cat or "Other" in cat:
                    self.assertIn("特定类别股份", r.get("持股比例分母口径说明", ""))

        # 2. 验证 P1-2: 00939 建设银行资产负债表归母权益不可加总差额自陈
        bal_00939 = self.gw.stock_financials({"symbol": "00939", "report_type": "balance", "limit_periods": 1})
        stmts_00939 = bal_00939.get("statements", [])
        if stmts_00939:
            s0 = stmts_00939[0]
            if "归属于母公司股东权益" in s0 and "股本" in s0:
                self.assertIn("归母所有者权益不可加总差额_优先股永续债及其他权益工具等", s0)
                self.assertIn("权益不可加总差额自陈说明", s0)

        # 3. 验证 P2-1: 港股 1m K 线 count=340 越界时同时包含【超限提示】与【截断说明】
        kline_340 = self.gw.stock_kline({"symbol": "00700", "period": "1m", "count": 340})
        self.assertIn("超限提示", kline_340)
        self.assertIn("截断说明", kline_340)

        # 4. 验证 P2-2 与 P3-1: quotes 包含盘口深度说明与汇率自陈说明
        q_00700 = self.gw.stock_quotes({"symbol": "00700"})
        self.assertIn("盘口深度说明", q_00700)
        val_desc = q_00700.get("估值与基本盘面", {}).get("估值口径说明", "")
        self.assertIn("quotes 与 f10 indicators 估值差自陈", val_desc)
        self.assertIn("PB 差可超 5%", val_desc)
        self.assertIn("以 f10 indicators 为基准", val_desc)
        self.assertNotIn("09988", val_desc, "文案不应再引用具体票号样例")
        self.assertEqual(q_00700["估值与基本盘面"].get("估值差基准"), "f10_indicators")

        # 5. 验证 P2-3: 港股 dividends 包含每股派息 float 数值与分红数据提取说明
        div_00700 = self.gw.stock_f10({"symbol": "00700", "category": "dividends"})
        self.assertIn("分红数据提取说明", div_00700)
        div_rows = div_00700.get("formatted_tables", {}).get("dividends", [])
        if div_rows:
            self.assertIn("每股派息_港元", div_rows[0])
            self.assertIn("是否派息", div_rows[0])

        # 6. 验证 P2-4: 现金流量表细项披露自陈说明
        cf_00700 = self.gw.stock_financials({"symbol": "00700", "report_type": "cashflow", "limit_periods": 1})
        cf_stmts = cf_00700.get("statements", [])
        if cf_stmts:
            self.assertIn("现金流量表细项披露自陈说明", cf_stmts[0])

        # 7. 验证 P2-5: quarter 单季度模式下报告期字段严格保持标准日期
        q_stmts = self.gw.stock_financials({"symbol": "00700", "report_type": "income", "period_type": "quarter", "limit_periods": 2})
        for qs in q_stmts.get("statements", []):
            rep_date = str(qs.get("报告期", ""))
            self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}$", rep_date), f"报告期字段必须为标准日期格式: {rep_date}")
            self.assertIn("报告期标签", qs)

        # 8. 验证 P2-6: 银行利润表营业收入别名与语义说明
        inc_00939 = self.gw.stock_financials({"symbol": "00939", "report_type": "income", "limit_periods": 1})
        inc_stmts = inc_00939.get("statements", [])
        if inc_stmts:
            self.assertIn("营业收入", inc_stmts[0])
            self.assertIn("金融/银行收入与支出语义说明", inc_stmts[0])

        # 9. 验证 P3-2: 00883 空 top_holders 包含无记录说明
        th_00883 = self.gw.stock_f10({"symbol": "00883", "category": "top_holders"})
        if not th_00883.get("formatted_tables", {}).get("top_holders"):
            self.assertIn("无记录说明", th_00883)
            self.assertIn("无记录说明", th_00883.get("data", {}))

        # 10. 验证 P3-3: 00939 repurchases data 层双向镜像无记录说明
        rep_00939 = self.gw.stock_f10({"symbol": "00939", "category": "repurchases"})
        if not rep_00939.get("formatted_tables", {}).get("repurchases"):
            self.assertIn("无记录说明", rep_00939)
            self.assertIn("无记录说明", rep_00939.get("data", {}))

        # 11. 验证 P3-4: buyback_summary 累计字段口径自陈与天数去重
        bb_00700 = self.gw.stock_f10({"symbol": "00700", "category": "buyback"})
        bb_data = bb_00700.get("data", {})
        self.assertIn("累计字段口径自陈", bb_data)
        self.assertIn("样本回购实施记录天数", bb_data)

        # 12. 验证 P3-5: indicators 包含每股摊薄盈利披露说明
        ind_01088 = self.gw.stock_f10({"symbol": "01088", "category": "indicators"})
        self.assertIn("每股摊薄盈利披露说明", ind_01088)

        # 13. 验证 P3-6 & P3-1: 02026 现金口径说明覆盖受限存款还原机制自陈，剔除过期残差文案
        cf_02026 = self.gw.stock_financials({"symbol": "02026", "report_type": "cashflow", "limit_periods": 1})
        cf_02026_stmts = cf_02026.get("statements", [])
        if cf_02026_stmts:
            cash_desc = str(cf_02026_stmts[0].get("现金口径说明", ""))
            self.assertIn("受限存款列报口径", cash_desc)
            self.assertNotIn("0.035%", cash_desc)

    # =========================================================================
    # 【功能块 8.5：A股与港股实测 D1~D14 专项问题修复全量回归测试】
    # - 输入：网关调用 stock_financials, stock_quotes, stock_f10, stock_kline
    # - 功能：
    #   1. D1: 600030 券商现金流无保险科目，映射为交易目的资产净支出，利润表挂证券收入说明
    #   2. D2: 002594 比亚迪自算普通股 PB 为 2.8461，002698 为 2.7869
    #   3. D3: 002594 资产负债表物理剔除重复别名键【专项储备】
    #   4. D4: 港股行情挂载【总市值_港元】并自陈币种为 HKD
    #   5. D5: 港股财报清洗 DATE_TYPE_CODE 裸码
    #   6. D7: 02026 现金流勾稽声明去除绝对化无缝勾稽
    #   7. D8: 港股财报说明动态感知记账币种，消除 USD 标的写 CNY 的自相矛盾
    #   8. D9: 港股分钟线成交量说明声明已覆盖 16:08 CAS 竞价且总量闭合
    #   9. D10: indicators.table5 空表时注入明确质押状态说明
    #   10. D13: 回购卡片跨市场统一挂载【样本区间回购总额_万元】
    #   11. D14: 00700 等有回购记录时不保留为 null 的无记录说明键
    # - 输出：所有断言全部成功通过
    # =========================================================================
    def test_d1_to_d14_fixes(self):
        """验证实测 A 股与港股 D1~D14 专项问题修复收敛性与口径自洽性"""
        # 1. 验证 D1: 600030 中信证券现金流纠正与利润表说明无保险词眼
        cf_600030 = self.gw.stock_financials({"symbol": "600030", "report_type": "cashflow", "limit_periods": 1})
        cf_row = cf_600030["statements"][0]
        self.assertNotIn("支付原保险合同赔付款项的现金", cf_row)
        self.assertIn("为交易目的而持有的金融资产及融出资金等净支出", cf_row)
        self.assertGreater(cf_row["为交易目的而持有的金融资产及融出资金等净支出"], 40000000000.0)
        self.assertIn("证券企业现金流科目纠偏说明", cf_row.get("现金流量表加总纪律说明", ""))
        self.assertIn("收到的税费返还", cf_row.get("现金流量表加总纪律说明", ""))

        inc_600030 = self.gw.stock_financials({"symbol": "600030", "report_type": "income", "limit_periods": 1})
        inc_row = inc_600030["statements"][0]
        self.assertNotIn("保险企业收入说明", inc_row)
        self.assertIn("证券企业收入说明", inc_row)

        # 验证 601336 真保险不受影响
        inc_601336 = self.gw.stock_financials({"symbol": "601336", "report_type": "income", "limit_periods": 1})
        self.assertIn("保险企业收入说明", inc_601336["statements"][0])

        # 2. 验证 D2: 002594 比亚迪自算普通股 PB 为 2.8461，002698 为 2.7869
        q_002594 = self.gw.stock_quotes({"symbol": "002594"})
        ext_002594 = q_002594.get("估值与基本盘面", {})
        price_2594 = float(q_002594["盘口核心行情"]["现价"])
        bps_2594 = float(ext_002594["普通股每股净资产_BPS_自算"])
        self.assertAlmostEqual(float(ext_002594["市净率_PB_自算_普通股"]), round(price_2594 / bps_2594, 4), places=3)

        q_002698 = self.gw.stock_quotes({"symbol": "002698"})
        ext_002698 = q_002698.get("估值与基本盘面", {})
        price_2698 = float(q_002698["盘口核心行情"]["现价"])
        bps_2698 = float(ext_002698["普通股每股净资产_BPS_自算"])
        self.assertAlmostEqual(float(ext_002698["市净率_PB_自算_普通股"]), round(price_2698 / bps_2698, 4), places=3)

        # 3. 验证 D3: 002594 资产负债表物理剔除重复别名键【专项储备】
        bal_002594 = self.gw.stock_financials({"symbol": "002594", "report_type": "balance", "limit_periods": 1})
        bal_row = bal_002594["statements"][0]
        self.assertNotIn("专项储备", bal_row)
        self.assertIn("外币报表折算差额", bal_row)
        self.assertIn("所有者权益双计剔除说明", bal_row)

        # 4. 验证 D4 & P2-1: 港股行情挂载【总市值_港元】、【流通市值_港元】，彻底规范去除易误解的【_元】
        q_00700 = self.gw.stock_quotes({"symbol": "00700"})
        ext_00700 = q_00700.get("估值与基本盘面", {})
        self.assertIn("总市值_港元", ext_00700)
        self.assertIn("流通市值_港元", ext_00700)
        self.assertNotIn("总市值_元", ext_00700)
        self.assertNotIn("流通市值_元", ext_00700)
        self.assertEqual(ext_00700.get("市值货币单位"), "HKD (港元)")
        self.assertIn("港股市值计价货币特别提示", ext_00700.get("市值口径说明", ""))

        # 5. 验证 D5: 港股财报清洗 DATE_TYPE_CODE 裸码
        fin_00700 = self.gw.stock_financials({"symbol": "00700", "report_type": "income", "limit_periods": 2})
        for s in fin_00700.get("statements", []):
            self.assertNotIn("DATE_TYPE_CODE", s)

        # 6. 验证 D7: 02026 现金流跨表说明去除“无缝勾稽”
        cf_02026 = self.gw.stock_financials({"symbol": "02026", "report_type": "cashflow", "limit_periods": 1})
        cf_02026_row = cf_02026["statements"][0]
        cross_note = cf_02026_row.get("跨表币种勾稽说明", "")
        self.assertNotIn("无缝勾稽", cross_note)
        self.assertIn("准则残差", cross_note)

        # 7. 验证 D8: 02026 财报会计口径说明不称统一为 CNY，体现 USD
        account_note = cf_02026.get("会计口径与说明", "")
        self.assertNotIn("币种统一为【CNY】", account_note)
        self.assertIn("USD", account_note)

        # 8. 验证 D9: 港股分钟线成交量说明声明已覆盖 16:08 CAS 竞价且总量闭合
        kline_00700 = self.gw.stock_kline({"symbol": "00700", "period": "1m", "count": 10})
        vol_note = kline_00700.get("成交量口径说明", "")
        self.assertIn("收市竞价交易时段 (CAS)", vol_note)
        self.assertIn("100% 代数闭合对齐", vol_note)

        # 9. 验证 D10: indicators.table5 空表时注入明确质押状态说明
        f10_ind = self.gw.stock_f10({"symbol": "600519", "category": "indicators"})
        self.assertIn("质押状态说明", f10_ind)

        # 10. 验证 D13 & P2-2: 港股回购卡片统一挂载规范的【_万港元】并杜绝误导的【_万元】人民币标签别名
        bb_hk = self.gw.stock_f10({"symbol": "00700", "category": "buyback"})
        self.assertIn("累计已实施回购总额_万港元", bb_hk.get("data", {}))
        self.assertIn("样本区间回购总额_万港元", bb_hk.get("data", {}))
        self.assertNotIn("累计已实施回购总额_万元", bb_hk.get("data", {}))
        self.assertNotIn("样本区间回购总额_万元", bb_hk.get("data", {}))

        # 11. 验证 D14: 00700 等有回购记录时不保留为 null 的无记录说明键
        self.assertNotIn("无记录说明", bb_hk)
        rep_hk = self.gw.stock_f10({"symbol": "00700", "category": "repurchases"})
        self.assertNotIn("无记录说明", rep_hk)

    def test_hk_p1_to_p3_fixes(self):
        """验证港股实测 P1-1 至 P3-8 深度回归套件"""
        # 1. P1-1 优先回归：00939 建行、03988 中行 权益自陈与真残差严格闭合（判据：自陈 − 真残差 = 0）
        # 00939: 归母权益 38,101.82 亿，已披露明细和 35,802.05 亿，真残差 = 2,299.77 亿（优先股 599.77 + 永续债 1700）
        fin_00939 = self.gw.stock_financials({"symbol": "00939", "report_type": "balance", "limit_periods": 1})
        b_00939 = fin_00939["statements"][0]
        self.assertIn("保留溢利(累计亏损)", b_00939)
        self.assertIn("保留溢利", b_00939)  # 双向别名
        self.assertIn("公积金", b_00939)
        self.assertIn("其他综合收益", b_00939)
        diff_00939 = b_00939.get("归母所有者权益不可加总差额_优先股永续债及其他权益工具等")
        true_residual_00939 = 229977000000.0  # 2299.77 亿
        self.assertEqual(diff_00939, true_residual_00939)
        self.assertEqual(diff_00939 - true_residual_00939, 0.0)

        # 03988: 归母权益 31,006.55 亿，已披露明细和 27,506.91 亿，真残差 = 3,499.64 亿（优先股 999.69 + 永续债 2499.95）
        fin_03988 = self.gw.stock_financials({"symbol": "03988", "report_type": "balance", "limit_periods": 1})
        b_03988 = fin_03988["statements"][0]
        diff_03988 = b_03988.get("归母所有者权益不可加总差额_优先股永续债及其他权益工具等")
        true_residual_03988 = 349964000000.0  # 3499.64 亿
        self.assertEqual(diff_03988, true_residual_03988)
        self.assertEqual(diff_03988 - true_residual_03988, 0.0)

        # 2. P3-2 & P3-3: 09988 阿里巴巴（识别其他综合性收益）与 02628 中国人寿（识别储备）权益 100% 闭合无残差
        fin_09988 = self.gw.stock_financials({"symbol": "09988", "report_type": "balance", "limit_periods": 1})
        b_09988 = fin_09988["statements"][0]
        self.assertIn("其他综合性收益", b_09988)
        self.assertIn("其他综合收益", b_09988)
        self.assertIsNone(b_09988.get("归母所有者权益不可加总差额_优先股永续债及其他权益工具等"))

        fin_02628 = self.gw.stock_financials({"symbol": "02628", "report_type": "balance", "limit_periods": 1})
        b_02628 = fin_02628["statements"][0]
        self.assertIn("储备", b_02628)
        self.assertIn("其他储备", b_02628)
        self.assertIsNone(b_02628.get("归母所有者权益不可加总差额_优先股永续债及其他权益工具等"))

        # 3. P3-1: 00939 跨期科目证券投资 ↔ 其他证券投资双向平滑互通
        fin_00939_4p = self.gw.stock_financials({"symbol": "00939", "report_type": "balance", "limit_periods": 4})
        for s in fin_00939_4p["statements"]:
            self.assertIn("证券投资", s)
            self.assertIn("其他证券投资", s)
            self.assertEqual(s["证券投资"], s["其他证券投资"])

        # 4. P2-1 & P3-7 & P3-8: 行情端统一 _港元，涨跌额_港元，买卖一档委托量说明
        q_00700 = self.gw.stock_quotes({"symbol": "00700"})
        core_00700 = q_00700.get("盘口核心行情", {})
        self.assertIn("涨跌额_港元", core_00700)
        self.assertIn("成交额_港元", core_00700)
        depth_note = q_00700.get("盘口深度说明", "")
        self.assertIn("恒置为 0", depth_note)
        self.assertIn("说明", q_00700["五档深度盘口"]["买一"])

        # 5. P2-2: F10 indicators 彻底清洗 DATE_TYPE_CODE
        f10_ind = self.gw.stock_f10({"symbol": "00700", "category": "indicators"})
        for row in f10_ind.get("formatted_tables", {}).get("indicators", []):
            self.assertNotIn("DATE_TYPE_CODE", row)
            self.assertNotIn("_DATE_TYPE_CODE", row)

        # 6. P2-4: 财报说明动态感知记账币种与金额单位
        fin_02026 = self.gw.stock_financials({"symbol": "02026", "report_type": "income", "limit_periods": 1})
        acc_note = fin_02026.get("会计口径与说明", "")
        self.assertIn("法定记账货币为【USD】", acc_note)
        self.assertIn("金额单位为【美元】", acc_note)

        # 7. P2-6: 02026 top_holders 区分未在港流通普通股说明
        holders_02026 = self.gw.stock_f10({"symbol": "02026", "category": "top_holders"})
        holders_note = holders_02026.get("持股比例分母口径说明", "")
        self.assertIn("02026", holders_note)
        self.assertIn("0.81 亿", holders_note)

        # 8. P3-4: 港股 dividends 结构化每10股派息字段
        div_00700 = self.gw.stock_f10({"symbol": "00700", "category": "dividends"})
        div_rows = div_00700.get("formatted_tables", {}).get("dividends", [])
        if div_rows:
            d0 = div_rows[0]
            self.assertIn("每股派息_港元", d0)
            self.assertIn("派息_每10股_港元", d0)
            self.assertIn("派息_每10股_含税", d0)

        # 9. P3-6: 股本时滞区间扩充至 -6%~+5%
        info_03800 = self.gw.stock_f10({"symbol": "03800", "category": "basic_info"})
        cap_note = info_03800.get("已发行股本口径说明", "")
        self.assertIn("-6%~+5%", cap_note)

    def test_hk_framework_upgrade_fixes(self):
        """
        ========================================================================
        功能块：港股框架升级实测缺陷深度回归测试套件 (P1-1 至 P4-1)
        ------------------------------------------------------------------------
        输入：
            - 非法枚举参数（如 period='year', report_type='invalid_type', category='not_a_category'）
            - 港股标的代码（如 '00700', '01088', '00883', '02026' 等）
            - 带截断参数 limit 的 F10 请求
        实现了什么功能：
            1. P1-1: 验证非法枚举值在网关层被严格拦截并抛出 ValueError，杜绝静默回退邻接分支；
            2. P2-1: 验证港股 F10 分支下 repurchases, buyback_details, dividends, events 均严格生效 limit 参数；
            3. P2-2: 验证港股 buyback 回购数据彻底剔除【_万元】人民币标签别名，仅保留规范的【_万港元】；
            4. P2-3 & P3-1: 验证现金口径说明完整阐述三种机理并剔除 0.035% 跨票泄漏；
            5. P3-2: 验证每股收益_TTM_说明区分自然财年（偏差 ≤1.35% 可信）与非自然财年；
            6. P3-3: 验证 60 分钟 K 线切分说明与全天 330 分钟交易机制自洽；
            7. P3-4: 验证盘口仅保留【涨跌额_港元】，彻底移除【涨跌额_元】；
            8. P3-5 & P3-7: 验证日 K 线附加说明包含【成交量口径说明】与【复权口径说明】；
            9. P3-6: 验证 F10 indicators 挂载【PE_TTM校准说明】与【估值时效提示】；
            10. P4-1: 验证日 K 与分钟 K 线成交量字段规范化为整数类型 (int)。
        输出：
            - 所有断言全部通过，确保无回归缺陷。
        ========================================================================
        """
        # 1. 验证 P1-1: 非法枚举值严格报错拦截，不发生静默映射
        with self.assertRaises(ValueError):
            self.gw.stock_kline({"symbol": "00700", "period": "year"})

        with self.assertRaises(ValueError):
            self.gw.stock_financials({"symbol": "00700", "report_type": "invalid_type"})

        with self.assertRaises(ValueError):
            self.gw.stock_f10({"symbol": "00700", "category": "not_a_category"})

        # 2. 验证 P2-1: F10 各分类严格生效 limit 参数
        f10_rep = self.gw.stock_f10({"symbol": "00700", "category": "repurchases", "limit": 5})
        self.assertLessEqual(len(f10_rep.get("formatted_tables", {}).get("repurchases", [])), 5)
        self.assertTrue("期次与截断说明" in f10_rep or "截断说明" in f10_rep)

        f10_bb = self.gw.stock_f10({"symbol": "00700", "category": "buyback", "limit": 3})
        self.assertLessEqual(len(f10_bb.get("formatted_tables", {}).get("buyback_details", [])), 3)

        f10_div = self.gw.stock_f10({"symbol": "00700", "category": "dividends", "limit": 3})
        self.assertLessEqual(len(f10_div.get("formatted_tables", {}).get("dividends", [])), 3)

        # 3. 验证 P2-2: 彻底清除 _万元 人民币标签，保留 _万港元
        bb_data = f10_bb.get("data", {})
        self.assertNotIn("样本区间回购总额_万元", bb_data)
        self.assertNotIn("累计已实施回购总额_万元", bb_data)
        self.assertIn("样本区间回购总额_万港元", bb_data)
        self.assertIn("累计已实施回购总额_万港元", bb_data)

        # 4. 验证 P2-3 & P3-1: 现金口径说明无 0.035% 泄露并阐明三种机理
        fin_cf = self.gw.stock_financials({"symbol": "00700", "report_type": "cash_flow", "limit_periods": 1})
        cf_stmts = fin_cf.get("statements", [])
        cf_note = cf_stmts[0].get("现金口径说明", "") if cf_stmts else ""
        self.assertNotIn("0.035%", cf_note)
        self.assertIn("受限存款列报口径", cf_note)
        self.assertIn("集团财务公司资金归集", cf_note)

        # 5. 验证 P3-2: 每股收益_TTM 说明区分自然财年与非自然财年
        f10_ind_00700 = self.gw.stock_f10({"symbol": "00700", "category": "indicators"})
        eps_note = f10_ind_00700.get("每股收益_TTM_说明", "")
        self.assertIn("≤1.35%", eps_note)
        self.assertIn("09988", eps_note)

        # 6. 验证 P3-3: 60 分钟切分说明数值自洽 (330分钟)
        kline_60m = self.gw.stock_kline({"symbol": "00700", "period": "60m"})
        split_note = kline_60m.get("港股60分钟跨午休切分机制说明") or kline_60m.get("60分钟切分机制说明", "")
        self.assertIn("330 分钟", split_note)
        self.assertNotIn("30+60+60+30", split_note)

        # 7. 验证 P3-4: 盘口仅保留涨跌额_港元，移除涨跌额_元
        quotes_hk = self.gw.stock_quotes({"symbol": "00700"})
        hq_hk = quotes_hk.get("盘口核心行情", {})
        self.assertIn("涨跌额_港元", hq_hk)
        self.assertNotIn("涨跌额_元", hq_hk)

        # 8. 验证 P3-5 & P3-7: 日 K 附加说明包含成交量口径说明与复权口径说明
        kline_day = self.gw.stock_kline({"symbol": "00700", "period": "day"})
        self.assertIn("成交量口径说明", kline_day)
        self.assertIn("复权口径说明", kline_day)

        # 9. 验证 P3-6: F10 indicators 挂载 PE_TTM校准说明 与 估值时效提示
        f10_ind = self.gw.stock_f10({"symbol": "00700", "category": "indicators"})
        self.assertIn("PE_TTM校准说明", f10_ind)
        self.assertIn("估值时效提示", f10_ind)

        # 10. 验证 P4-1: 日 K 与 60m K 线成交量规整为整数
        day_rows = kline_day.get("klines") or kline_day.get("data", [])
        if day_rows:
            vol_d = day_rows[0].get("volume") if "volume" in day_rows[0] else day_rows[0].get("成交量")
            self.assertIsInstance(vol_d, int)
        m60_rows = kline_60m.get("klines") or kline_60m.get("data", [])
        if m60_rows:
            vol_m = m60_rows[0].get("volume") if "volume" in m60_rows[0] else m60_rows[0].get("成交量")
            self.assertIsInstance(vol_m, int)

    # =========================================================================
    # 【功能块 测试集：A股实测缺陷 (P1-1, P2-1, P2-2, P4-1) 综合回归验证】
    # - 输入：网关调用 stock_kline, stock_financials, stock_f10, stock_quotes
    # - 功能：
    #   1. 验证 P1-1: A 股与港股 stock_kline 拒绝非法周期 period='year'，抛出 ValueError
    #   2. 验证 P1-1: 港股 stock_financials 拒绝非法报表类型 report_type='invalid_type'，抛出 ValueError
    #   3. 验证 P1-1: 港股 stock_f10 拒绝非法分类 category='not_a_category'，抛出 ValueError
    #   4. 验证 P2-1: A 股 600519 stock_f10 repurchases limit=1 截断生效（<=1行）
    #   5. 验证 P2-1: A 股 600519 stock_f10 dividends limit=3 截断生效（<=3行）
    #   6. 验证 P2-1: A 股 600519 stock_f10 events limit=2 截断生效（<=2行）
    #   7. 验证 P2-1: 港股 00700 stock_f10 repurchases limit=5 截断生效（<=5行）
    #   8. 验证 P2-2: 002594 比亚迪 BPS 漏项导致 PB 偏离时，说明包含 BPS口径偏差与重算警示，绝无'无需换期重算'
    #   9. 验证 P4-1: A 股日 K 线 volume 严格规整为整型 (int)
    # - 输出：全部断言通过，无任何回归故障
    # =========================================================================
    def test_a_share_p1_p2_p4_comprehensive_fixes(self):
        """全面验证 A 股与港股 P1-1 非法枚举严格校验、P2-1 全分类 limit 截断生效、P2-2 比亚迪 PB 偏离警示与 P4-1 日K volume 整型化"""
        # 1. 验证 P1-1: A 股与港股 stock_kline 非法 period 严格拒绝
        with self.assertRaises(ValueError):
            self.gw.stock_kline({"symbol": "600519", "period": "year"})
        with self.assertRaises(ValueError):
            self.gw.stock_kline({"symbol": "00700", "period": "year"})

        # 2. 验证 P1-1: 港股 stock_financials 非法 report_type 严格拒绝
        with self.assertRaises(ValueError):
            self.gw.stock_financials({"symbol": "00700", "report_type": "invalid_type"})

        # 3. 验证 P1-1: 港股 stock_f10 非法 category 严格拒绝
        with self.assertRaises(ValueError):
            self.gw.stock_f10({"symbol": "00700", "category": "not_a_category"})

        # 4. 验证 P2-1: A 股 600519 各表 limit 截断生效
        # 4.1 repurchases limit=1
        f10_rep = self.gw.stock_f10({"symbol": "600519", "category": "repurchases", "limit": 1})
        rep_rows = f10_rep.get("formatted_tables", {}).get("repurchases", [])
        self.assertLessEqual(len(rep_rows), 1)
        self.assertIn("期次与截断说明", f10_rep)

        # 4.2 dividends limit=3
        f10_div = self.gw.stock_f10({"symbol": "600519", "category": "dividends", "limit": 3})
        div_rows = f10_div.get("formatted_tables", {}).get("dividends", [])
        self.assertLessEqual(len(div_rows), 3)
        self.assertIn("期次与截断说明", f10_div)

        # 4.3 events limit=2
        f10_events = self.gw.stock_f10({"symbol": "600519", "category": "events", "limit": 2})
        events_rows = f10_events.get("formatted_tables", {}).get("table0", [])
        self.assertLessEqual(len(events_rows), 2)
        self.assertIn("期次与截断说明", f10_events)

        # 5. 验证 P2-1: 港股 00700 repurchases limit=5 截断生效
        f10_hk_rep = self.gw.stock_f10({"symbol": "00700", "category": "repurchases", "limit": 5})
        hk_rep_rows = f10_hk_rep.get("formatted_tables", {}).get("repurchases", [])
        self.assertLessEqual(len(hk_rep_rows), 5)
        self.assertIn("期次与截断说明", f10_hk_rep)

        # 6. 验证 P2-2: 002594 比亚迪 BPS 漏项导致 PB 偏离时，说明包含 BPS口径偏差与重算警示，绝无'无需换期重算'
        res_byd = self.gw.stock_quotes({"symbol": "002594"})
        byd_ext = res_byd.get("估值与基本盘面", {})
        byd_note = byd_ext.get("派生指标换期说明", "")
        self.assertNotIn("无需换期重算", byd_note)
        self.assertIn("市净率_PB_自算_普通股", byd_note)

        # 7. 验证 P4-1: A 股日 K 线 volume 严格规整为整型 (int)
        kline_a = self.gw.stock_kline({"symbol": "600519", "period": "day", "count": 2})
        bars = kline_a.get("klines", [])
        self.assertTrue(len(bars) > 0)
        self.assertIsInstance(bars[0]["volume"], int)



class TestAShareNewRoundP2P3Convergence(unittest.TestCase):
    """测试实测 A 股新一轮 P2-1、P3-1 至 P3-5 缺陷全面收敛与 8 大口径准则对齐"""

    def setUp(self):
        from mcp_gateway.finance import FinanceGateway
        self.gw = FinanceGateway()

    def test_p2_1_f10_table6_pb_yesterday_not_overwritten(self):
        """验证 P2-1: table6.市净率_PB_昨收 保持基准价统一，不被覆盖为上一日昨收计算值"""
        res = self.gw.stock_f10({"symbol": "002223", "category": "summary"})
        t6 = res.get("formatted_tables", {}).get("table6", [])
        if t6:
            row = t6[0]
            self.assertIn("市净率_PB_昨收", row)
            # 若触发换期重算，独立挂载键
            if "市净率_PB_昨收_换期重算" in row:
                self.assertIsNotNone(row["市净率_PB_昨收_换期重算"])

    def test_p3_1_parameter_out_of_bounds_validation(self):
        """验证 P3-1: 全工具入参越界强拦截，杜绝静默返回或空表假成功"""
        # 1. stock_kline count 校验 (1~500)
        with self.assertRaises(ValueError):
            self.gw.stock_kline({"symbol": "600519", "count": 0})
        with self.assertRaises(ValueError):
            self.gw.stock_kline({"symbol": "600519", "count": -5})
        with self.assertRaises(ValueError):
            self.gw.stock_kline({"symbol": "600519", "count": 501})

        # 2. stock_financials limit_periods 校验 (1~10)
        with self.assertRaises(ValueError):
            self.gw.stock_financials({"symbol": "600519", "report_type": "balance", "limit_periods": 0})
        with self.assertRaises(ValueError):
            self.gw.stock_financials({"symbol": "600519", "report_type": "balance", "limit_periods": 11})

        # 3. stock_f10 periods (1~30) 与 limit (1~200) 校验
        with self.assertRaises(ValueError):
            self.gw.stock_f10({"symbol": "600519", "category": "summary", "periods": 0})
        with self.assertRaises(ValueError):
            self.gw.stock_f10({"symbol": "600519", "category": "summary", "periods": 31})
        with self.assertRaises(ValueError):
            self.gw.stock_f10({"symbol": "600519", "category": "summary", "limit": 0})
        with self.assertRaises(ValueError):
            self.gw.stock_f10({"symbol": "600519", "category": "summary", "limit": 201})

        # 4. stock_screener limit (1~100) 与 page (1~20) 校验
        with self.assertRaises(ValueError):
            self.gw.stock_screener({"message": "白酒", "limit": 0})
        with self.assertRaises(ValueError):
            self.gw.stock_screener({"message": "白酒", "limit": 101})
        with self.assertRaises(ValueError):
            self.gw.stock_screener({"message": "白酒", "page": 0})
        with self.assertRaises(ValueError):
            self.gw.stock_screener({"message": "白酒", "page": 21})

        # 5. stock_news limit (1~10) 校验
        with self.assertRaises(ValueError):
            self.gw.stock_news({"symbol": "600519", "limit": 0})
        with self.assertRaises(ValueError):
            self.gw.stock_news({"symbol": "600519", "limit": 11})

    def test_p3_2_valuation_note_no_perpetual_bond_contradiction(self):
        """验证 P3-2: 002594 估值口径说明严格指明高估偏差，无永续债时不出现未扣永续债低估字样"""
        res = self.gw.stock_quotes({"symbol": "002594"})
        ext = res.get("估值与基本盘面", {})
        note = ext.get("估值口径说明", "")
        # 当行情 PB > 自算普通股 PB 时，必须提示高估偏差，且不可在说明中称 002594 自身未扣永续债低估
        if ext.get("市净率_PB", 0) > ext.get("市净率_PB_自算_普通股", 0):
            self.assertIn("高估偏差", note)
            self.assertNotIn("存在约 4.9%~16% 的低估偏差", note)
            self.assertNotIn("未扣除其他权益工具(永续债/优先股)的归母总权益计算而存在约", note)

    def test_p3_3_stock_lookup_irrelevant_name_query_filtered(self):
        """验证 P3-3: stock_lookup 名称型未命中严格过滤无交集近邻，返回空候选说明"""
        res_a = self.gw.stock_lookup({"query": "不存在的公司名XYZ", "range": "AG"})
        self.assertEqual(len(res_a.get("entities", [])), 0)
        self.assertIn("候选为空说明", res_a)

        res_hk = self.gw.stock_lookup({"query": "不存在的港股XYZ", "range": "HK-GP"})
        self.assertEqual(len(res_hk.get("entities", [])), 0)
        self.assertIn("候选为空说明", res_hk)

        res_num = self.gw.stock_lookup({"query": "888888", "range": "AG"})
        self.assertEqual(len(res_num.get("entities", [])), 0)

    def test_p3_4_quotes_weibi_weicha_fields(self):
        """验证 P3-4: quotes 盘口核心行情包含 委买总委托量_手, 委卖总委托量_手, 委差_手, 委比_pct"""
        # A 股
        res_a = self.gw.stock_quotes({"symbol": "600519"})
        hq_a = res_a.get("盘口核心行情", {})
        self.assertIn("委买总委托量_手", hq_a)
        self.assertIn("委卖总委托量_手", hq_a)
        self.assertIn("委差_手", hq_a)
        self.assertIn("委比_pct", hq_a)
        self.assertIsInstance(hq_a["委买总委托量_手"], int)
        self.assertIsInstance(hq_a["委卖总委托量_手"], int)
        self.assertIsInstance(hq_a["委差_手"], int)
        self.assertIsInstance(hq_a["委比_pct"], (float, int))

        # 港股
        res_hk = self.gw.stock_quotes({"symbol": "00700"})
        hq_hk = res_hk.get("盘口核心行情", {})
        self.assertIn("委买总委托量_手", hq_hk)
        self.assertIn("委卖总委托量_手", hq_hk)
        self.assertIn("委差_手", hq_hk)
        self.assertIn("委比_pct", hq_hk)
        self.assertEqual(hq_hk["委买总委托量_手"], 0)
        self.assertEqual(hq_hk["委卖总委托量_手"], 0)
        self.assertEqual(hq_hk["委差_手"], 0)
        self.assertEqual(hq_hk["委比_pct"], 0.0)

    def test_p3_5_version_consistency(self):
        """验证版本全链路：README / SKILL.md / tool_api_reference.md 统一为 v2.0.2"""
        import os
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # README.md
        with open(os.path.join(base_dir, "README.md"), "r", encoding="utf-8") as f:
            readme_text = f.read()
        self.assertIn("# Local MCP Search, Coding & Finance Fallback Gateway (v2.0.2)", readme_text)
        self.assertIn("### v2.0.2 — 港股残留文案/结构对齐与 A 股委比、PE 口径、screener 分页收敛", readme_text)
        self.assertIn("### v2.0.1 — 港股 P1~P3 与 A 股 P2~P3 遗留缺陷收敛", readme_text)
        self.assertIn("### v2.0.0 — 实测 A 股 P2-1、P3-1~P3-5 缺陷全面收敛", readme_text)

        # SKILL.md
        with open(os.path.join(base_dir, "mcphub-finance", "SKILL.md"), "r", encoding="utf-8") as f:
            skill_text = f.read()
        self.assertIn("version: 2.0.2", skill_text)
        self.assertIn("陷阱 125", skill_text)
        self.assertIn("陷阱 127", skill_text)
        self.assertIn("陷阱 128", skill_text)

        # tool_api_reference.md
        with open(os.path.join(base_dir, "mcphub-finance", "references", "tool_api_reference.md"), "r", encoding="utf-8") as f:
            ref_text = f.read()
        self.assertIn("版本**：v2.0.2", ref_text)
        self.assertIn("2.4 v2.0.1 新增字段与口径变更清单", ref_text)
        self.assertIn("2.5 v2.0.2 新增字段与口径变更清单", ref_text)
        self.assertIn("委买总委托量_手", ref_text)



# =========================================================================
# 【功能块 8.7：港股最新实测 P2-1、P2-2、P3-1~P3-5 与 9 大口径全面收敛测试】
# - 输入：FinanceGateway 调用 stock_quotes, stock_kline, stock_financials, stock_f10
# - 功能：1. 验证 P2-1: 00883 模板轴分裂自陈（一/三季报 64 键 vs 中报/年报 52~53 键，跨表现金差千亿级，结构不可比）；
#         2. 验证 P2-2: 02601 保险两侧明细空洞化（约 88% 资产及 ≥90% 负债归入其他项目，严禁做细分结构分析）；
#         3. 验证 P3-1: 52周最高与52周最高_前复权逐位同值，前复权口径自陈消除下游误解；
#         4. 验证 P3-2: basic_info 权威上市日期（YYYY-MM-DD）与每手股数（100/200/500/1000）多源补齐非空；
#         5. 验证 P3-3: top_holders 彻底消除 00688/02628 硬编码跨票文案泄漏；
#         6. 验证 P3-4: repurchases (纯明细列表) 与 buyback (聚合卡片+明细) 参数定位与分工说明；
#         7. 验证 P3-5: 60m 最后一根 16:00 bar 专用于吸收 CAS 集中撮合量（量额比等同 30m 16:00 单根）；
#         8. 验证 WARN-2: 01088 / 02601 现金差额客观自陈为【口径不可分解】，杜绝强套受限存款解释；
#         9. 验证 WARN-3: 00700 腾讯主要股东 Naspers 申报时点股本漂移（28.86% vs 30.46%）；
#         10. 验证 Skill 与 References 接口对齐性。
# - 输出：各断言 100% 验证通过
# =========================================================================
class TestHKStockNewEmpiricalConvergence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gw = FinanceGateway()

    def test_p2_1_and_warn_2_template_split_and_unreconciled_cash(self):
        """验证 P2-1: 00883 报告期模板轴分裂自陈 与 WARN-2: 01088 现金不可分解自陈"""
        # 1. 00883 模板轴分裂说明 (结构不可比)
        res_00883 = self.gw.stock_financials({"symbol": "00883", "report_type": "cashflow", "limit_periods": 2})
        stmts_883 = res_00883.get("statements", [])
        if stmts_883:
            cash_note = stmts_883[0].get("现金口径说明", "")
            self.assertIn("报告期模板轴分裂", cash_note)
            self.assertIn("结构不可比", cash_note)
            self.assertIn("严禁跨季度进行结构对比", cash_note)

        # 2. 01088 现金差额不可分解自陈
        res_01088 = self.gw.stock_financials({"symbol": "01088", "report_type": "cashflow", "limit_periods": 2})
        stmts_1088 = res_01088.get("statements", [])
        if stmts_1088:
            cash_note_1088 = stmts_1088[0].get("现金口径说明", "")
            self.assertIn("口径不可分解", cash_note_1088)
            self.assertIn("严禁分析端强套受限存款假说解释", cash_note_1088)

    def test_p2_2_insurance_hollow_out_structural_restriction(self):
        """验证 P2-2: 02601 中国太保资产负债两侧明细空洞化与结构限制说明"""
        res_02601 = self.gw.stock_financials({"symbol": "02601", "report_type": "balance", "limit_periods": 1})
        stmts_2601 = res_02601.get("statements", [])
        self.assertGreater(len(stmts_2601), 0)
        stmt0 = stmts_2601[0]
        note = stmt0.get("保险资产负债两侧明细空洞化与结构限制说明", "")
        self.assertIn("明细严重空洞化", note)
        self.assertIn("严禁进行资产/负债细分结构分析", note)
        self.assertIn("核心总额", note)

    def test_p3_1_52_week_qfq_price_and_disclosure(self):
        """验证 P3-1: 52周最高与52周最高_前复权逐位等值，说明明确前复权口径"""
        res_quotes = self.gw.stock_quotes({"symbol": "00700"})
        valuation = res_quotes.get("估值与基本盘面", {})
        h_52 = valuation.get("52周最高")
        h_52_qfq = valuation.get("52周最高_前复权")
        self.assertIsNotNone(h_52)
        self.assertIsNotNone(h_52_qfq)
        self.assertEqual(h_52, h_52_qfq)

        note_52 = valuation.get("52周价格区间说明", "")
        self.assertIn("前复权口径自陈", note_52)
        self.assertIn("同源逐位等值", note_52)
        self.assertIn("下游切勿按'不带后缀=不复权'产生误解", note_52)

    def test_p3_2_basic_info_listing_date_and_lot_size_populated(self):
        """验证 P3-2: 00700 等标的 basic_info 上市日期与每手股数权威下发非空"""
        res_basic = self.gw.stock_f10({"symbol": "00700", "category": "basic_info"})
        table = res_basic.get("formatted_tables", {}).get("basic_info", [])
        self.assertGreater(len(table), 0)
        row0 = table[0]
        # 上市日期 (2004-06-16)
        listing_date = row0.get("上市日期", "")
        self.assertTrue(len(listing_date) >= 10)
        self.assertIn("-", listing_date)
        # 每手股数 (100)
        lot_size = str(row0.get("每手股数", ""))
        self.assertEqual(lot_size, "100")
        # 董事长 (马化腾)
        dsz = row0.get("董事长", "")
        self.assertIn("马化腾", dsz)

    def test_p3_3_and_warn_3_top_holders_no_leakage_and_capital_drift(self):
        """验证 P3-3: top_holders 消除别家硬编码泄露 与 WARN-3: 股本漂移说明"""
        # 1. 00883 top_holders 检查不包含 00688 或 02628 硬编码举例
        res_00883 = self.gw.stock_f10({"symbol": "00883", "category": "top_holders"})
        tip_883 = res_00883.get("最新申报时效提示", "")
        note_883 = res_00883.get("股东口径说明", "")
        self.assertNotIn("00688", tip_883)
        self.assertNotIn("02628", tip_883)
        self.assertNotIn("00688", note_883)
        self.assertNotIn("02628", note_883)

        # 2. 00700 top_holders 检查 WARN-3 股本漂移说明
        res_00700 = self.gw.stock_f10({"symbol": "00700", "category": "top_holders"})
        drift_note = res_00700.get("申报时点股本漂移说明", "")
        self.assertIn("申报时点股本漂移机理说明", drift_note)
        self.assertIn("Naspers", drift_note)
        self.assertIn("回购注销引发的『申报时点股本漂移』", drift_note)

    def test_p3_4_repurchases_vs_buyback_parameter_positioning(self):
        """验证 P3-4: repurchases 与 buyback 参数分工与定位说明"""
        # repurchases: 纯明细流水
        res_rep = self.gw.stock_f10({"symbol": "00700", "category": "repurchases"})
        self.assertEqual(res_rep.get("category"), "repurchases")
        self.assertIn("repurchases", res_rep.get("formatted_tables", {}))
        self.assertIn("参数定位与分工说明", res_rep)
        self.assertIn("定位于纯流水明细列表", res_rep["参数定位与分工说明"])

        # buyback: 宏观聚合分析 + 明细
        res_bb = self.gw.stock_f10({"symbol": "00700", "category": "buyback"})
        self.assertEqual(res_bb.get("category"), "buyback")
        self.assertIn("buyback_summary", res_bb.get("formatted_tables", {}))
        self.assertIn("buyback_details", res_bb.get("formatted_tables", {}))
        self.assertIn("参数定位与分工说明", res_bb)
        self.assertIn("定位于宏观聚合分析", res_bb["参数定位与分工说明"])

    def test_p3_5_kline_60m_cas_bar_explanation(self):
        """验证 P3-5: 60m 最后一根 bar 明确自陈收市竞价时段 (CAS) 集中撮合量"""
        res_60m = self.gw.stock_kline({"symbol": "00700", "period": "60m", "count": 6})
        self.assertIn("港股60分钟跨午休切分机制说明", res_60m)
        cas_note = res_60m["港股60分钟跨午休切分机制说明"]
        self.assertIn("收市竞价交易时段 (CAS)", cas_note)
        self.assertIn("集中撮合量", cas_note)
        self.assertIn("与 30m 最后一根 16:00 bar 完全等值", cas_note)

    def test_skill_trap_126_and_reference_alignment(self):
        """验证文档层: SKILL.md 陷阱 126 与 tool_api_reference.md 全面对齐"""
        import os
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base_dir, "mcphub-finance", "SKILL.md"), "r", encoding="utf-8") as f:
            skill_text = f.read()
        self.assertIn("陷阱 126", skill_text)
        self.assertIn("报告期模板轴分裂与结构不可比自陈", skill_text)
        self.assertIn("保险资产负债两侧明细空洞化与结构限制", skill_text)

        with open(os.path.join(base_dir, "mcphub-finance", "references", "tool_api_reference.md"), "r", encoding="utf-8") as f:
            ref_text = f.read()
        self.assertIn("P2-1 报告期模板轴分裂自陈", ref_text)
        self.assertIn("P2-2 保险两侧明细空洞化限制", ref_text)
        self.assertIn("WARN-2 现金差额口径不可分解", ref_text)
        self.assertIn("申报时点股本漂移说明", ref_text)


class _FakeHKResponse:
    """腾讯行情专线的最小响应桩：只需 read / 上下文管理协议。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeHKResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class TestHKInvalidCodeGateOffline(unittest.TestCase):
    """批次0：港股无效码闸门（离线三态 + 结果缓存 + 网关路由覆盖）"""

    def setUp(self) -> None:
        from mcp_gateway.engines.hk_stock import HKStockEngine

        self.engine = HKStockEngine(timeout=1)

    @staticmethod
    def _quote_stub(payload: bytes | None = None, error: Exception | None = None):
        from mcp_gateway.engines import hk_stock as hk_module

        if error is not None:
            return unittest.mock.patch.object(hk_module._SECURE_OPENER, "open", side_effect=error)
        return unittest.mock.patch.object(
            hk_module._SECURE_OPENER,
            "open",
            side_effect=lambda *args, **kwargs: _FakeHKResponse(payload or b""),
        )

    def test_none_match_blocks_before_dispatch(self) -> None:
        """上游明确 none_match 时必须在分发前拦截，绝不能落进 F10 伪数据分支"""
        with self._quote_stub(b'v_pv_none_match="1";'):
            with unittest.mock.patch.object(
                self.engine, "_get_hk_f10", side_effect=AssertionError("闸门未生效，已进入 F10 分发")
            ):
                with self.assertRaises(ValueError) as ctx:
                    self.engine.get_f10("88888", category="industry")
        self.assertIn("88888", str(ctx.exception))

    def test_valid_code_passes_and_caches(self) -> None:
        """有效码护栏：放行到 F10 分发，且存在性结果只请求一次"""
        raw = 'v_r_hk00700="100~腾讯控股~00700~386.000";'.encode("gb18030")
        calls = {"n": 0}

        def counting_open(*args, **kwargs):
            calls["n"] += 1
            return _FakeHKResponse(raw)

        from mcp_gateway.engines import hk_stock as hk_module

        with unittest.mock.patch.object(hk_module._SECURE_OPENER, "open", side_effect=counting_open):
            with unittest.mock.patch.object(self.engine, "_get_hk_f10", return_value={"ok": True}) as dispatch:
                self.assertEqual(self.engine.get_f10("00700", category="summary"), {"ok": True})
                self.assertEqual(self.engine.get_f10("00700", category="summary"), {"ok": True})
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(calls["n"], 1, "确定结果未命中缓存，重复请求了腾讯行情")

    def test_network_error_fails_open_and_does_not_cache(self) -> None:
        """网络异常必须 fail-open 放行，且异常结果不入缓存（下次仍重试）"""
        from urllib.error import URLError

        calls = {"n": 0}

        def failing_open(*args, **kwargs):
            calls["n"] += 1
            raise URLError("simulated timeout")

        from mcp_gateway.engines import hk_stock as hk_module

        with unittest.mock.patch.object(hk_module._SECURE_OPENER, "open", side_effect=failing_open):
            with unittest.mock.patch.object(self.engine, "_get_hk_f10", return_value={"ok": True}) as dispatch:
                self.assertEqual(self.engine.get_f10("00700", category="summary"), {"ok": True})
                self.assertEqual(self.engine.get_f10("00700", category="summary"), {"ok": True})
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(calls["n"], 2, "不确定结果被缓存，后续调用不会再重试")

    def test_gateway_routing_reaches_engine_gate(self) -> None:
        """网关层 stock_f10 的港股分支必须落到引擎闸门（不是绕过它直接取 F10）"""
        gw = FinanceGateway()
        with self._quote_stub(b'v_pv_none_match="1";'):
            with unittest.mock.patch.object(
                gw.hk_engine, "_get_hk_f10", side_effect=AssertionError("网关绕过了引擎闸门")
            ):
                with self.assertRaises(ValueError) as ctx:
                    gw.stock_f10({"symbol": "88888", "category": "summary"})
        self.assertIn("88888", str(ctx.exception))


class TestHKLookupAndF10CleaningOffline(unittest.TestCase):
    """批次1：语义近邻收紧、summary 键名归一、top_holders 零股/空名清洗（全部离线）"""

    def test_name_candidates_single_char_overlap_is_not_relevance(self) -> None:
        from mcp_gateway.finance import _filter_name_candidates

        noisy = [{"name": "零在科技金融", "aliases": ["lzkjjr"]}]
        self.assertEqual(
            _filter_name_candidates(noisy, "不存在的公司名XYZ"), [],
            "单字交集仍被判为相关，假近邻没清掉",
        )

    def test_name_candidates_keep_real_hits(self) -> None:
        from mcp_gateway.finance import _filter_name_candidates

        cands = [
            {"name": "腾讯控股", "aliases": ["TENCENT"]},
            {"name": "阿里健康", "aliases": ["ALIHEALTH"]},
            {"name": "宁德时代", "aliases": ["CATL"]},
        ]
        self.assertEqual(len(_filter_name_candidates(cands, "腾讯控股")), 1)
        self.assertEqual(len(_filter_name_candidates(cands, "腾讯")), 1)
        self.assertEqual(len(_filter_name_candidates(cands, "宁德")), 1, "子串命中被误杀")
        self.assertEqual(len(_filter_name_candidates(cands, "阿里巴巴")), 1, "2 字交集应保留相关同业候选")
        self.assertEqual(len(_filter_name_candidates(cands, "tencent")), 1)

    def test_common_code_prefix_len(self) -> None:
        from mcp_gateway.finance import _common_code_prefix_len

        self.assertEqual(_common_code_prefix_len("88888", "08347"), 0)
        self.assertEqual(_common_code_prefix_len("600519", "600520"), 4)
        self.assertEqual(_common_code_prefix_len("88888", "88888"), 5)

    def test_lookup_prunes_numeric_hk_near_neighbours(self) -> None:
        """港股数字码 88888：近邻公共前缀 <3 全部剔除，并留下过滤说明"""
        gw = FinanceGateway()
        fake_rag = {
            "retrieved_entities": [
                {"entity_code": "08347", "entity_name": "F8企业", "entity_type": "港股代码", "entity_setcode": 31},
                {"entity_code": "00888", "entity_name": "链信控股", "entity_type": "港股代码", "entity_setcode": 31},
            ]
        }
        with unittest.mock.patch.object(gw.tdx_client, "_post", return_value=fake_rag):
            with unittest.mock.patch.object(gw.tdx_client, "_resolve_exact_security_entity", return_value=None):
                res = gw.stock_lookup({"query": "88888", "range": "HK"})
        self.assertEqual(res["entities"], [])
        self.assertIn("近邻过滤说明", res)
        self.assertIn("未命中说明", res)

    def test_lookup_prunes_name_near_neighbours_in_hk(self) -> None:
        gw = FinanceGateway()
        fake_rag = {
            "retrieved_entities": [
                {"entity_code": "00093", "entity_name": "零在科技金融", "entity_type": "港股代码", "entity_setcode": 31},
            ]
        }
        with unittest.mock.patch.object(gw.tdx_client, "_post", return_value=fake_rag):
            res = gw.stock_lookup({"query": "不存在的公司名XYZ", "range": "HK"})
        self.assertEqual(res["entities"], [])
        self.assertIn("近邻过滤说明", res)

    def test_summary_key_typo_normalized_and_code_alias(self) -> None:
        from mcp_gateway.engines import hk_stock as hk_module

        engine = hk_module.HKStockEngine(timeout=1)
        html = (
            "<html><body><table>"
            "<tr><td>证劵代码</td><td>00700</td></tr>"
            "<tr><td>公司名称(中文)</td><td>腾讯控股有限公司</td></tr>"
            "</table></body></html>"
        )
        with unittest.mock.patch.object(
            hk_module._SECURE_OPENER, "open", side_effect=lambda *a, **k: _FakeHKResponse(html.encode("gbk"))
        ):
            with unittest.mock.patch.object(engine, "_get_eastmoney_hk_company_profile", return_value={}):
                with unittest.mock.patch.object(engine, "get_quotes", return_value={}):
                    info = engine._get_sina_hk_company_info("00700")
        self.assertIsNotNone(info)
        assert info is not None  # 供类型检查收窄
        self.assertEqual(info.get("证券代码"), "00700")
        self.assertNotIn("证劵代码", info)

        with unittest.mock.patch.object(engine, "_get_sina_hk_company_info", return_value=dict(info)):
            res = engine._get_hk_f10("00700", category="summary")
        self.assertEqual(res["data"].get("股份代码"), "00700")

    def test_top_holders_drops_zero_share_and_nameless_rows(self) -> None:
        from mcp_gateway.engines import hk_stock as hk_module

        engine = hk_module.HKStockEngine(timeout=1)
        rows = [
            {"持股人名字": "Naspers Limited", "中文股东名称": "", "目前类别": "L", "目前结余(股)": "2769333600", "事件日期": "2024-01-02"},
            {"持股人名字": "The Northern Trust Company (ALA)", "中文股东名称": "", "目前类别": "L", "目前结余(股)": "0", "事件日期": "2023-01-02"},
            {"持股人名字": "", "中文股东名称": "", "目前类别": "L", "目前结余(股)": "100", "事件日期": "2023-01-02"},
        ]
        with unittest.mock.patch.object(engine, "_get_sina_hk_table", return_value=[dict(r) for r in rows]):
            res = engine._get_hk_f10("00700", category="top_holders")
        table = res["formatted_tables"]["top_holders"]
        self.assertEqual(len(table), 1)
        self.assertEqual(table[0]["股东名称"], "Naspers Limited")
        self.assertIn("零持股", res["股东行清洗说明"])
        self.assertIn("无股东名称", res["股东行清洗说明"])


class TestHKFinancialTemplateAndDividendsOffline(unittest.TestCase):
    """批次2：权益残差归因、季报模板缺口自陈、美元派息与 EPS 计量币种（全部离线）"""

    def test_equity_residual_includes_fx_translation(self) -> None:
        from mcp_gateway.core.normalizer import calculate_hk_parent_equity_residual

        row = {
            "归属于母公司股东权益": 1042379000000.0,
            "股本": 1000000.0,
            "其他储备": 16286000000.0,
            "保留溢利(累计亏损)": 666784000000.0,
            "公积金": 387147000000.0,
            "其他综合收益": 109000000.0,
            "股东权益其他项目": 9884000000.0,
            "库存股": -36162000000.0,
            "外币报表折算差额": -1670000000.0,
        }
        diff, note = calculate_hk_parent_equity_residual(row)
        self.assertIsNone(diff, "外币报表折算差额未纳入明细，残差仍被误报")
        self.assertIsNone(note)

    def test_equity_residual_names_matching_item(self) -> None:
        from mcp_gateway.core.normalizer import calculate_hk_parent_equity_residual

        row = {
            "归属于母公司股东权益": 1000.0,
            "股本": 100.0,
            "其他储备": 200.0,
            "其他权益工具": 700.0,
        }
        diff, note = calculate_hk_parent_equity_residual(row)
        self.assertIsNotNone(diff)
        self.assertIn("其他权益工具", note)
        self.assertIn("并非优先股或永续债", note)

    def test_balance_template_gap_annotated(self) -> None:
        from mcp_gateway.engines.hk_stock import _annotate_balance_template_gaps

        full = {"截止日期": "2026-06-30", "股本": 1.0, "储备": 2.0, "保留溢利": 3.0, "其他综合收益": 4.0}
        short = {"截止日期": "2026-03-31"}
        note = _annotate_balance_template_gaps([short, full])
        self.assertIsNotNone(note)
        self.assertIn("2026-03-31", note)
        self.assertIn("权益明细模板完整性自陈", short)
        self.assertNotIn("权益明细模板完整性自陈", full)
        self.assertIn("股本", short["权益明细模板完整性自陈"])
        self.assertIn("不得按缺失补零", short["权益明细模板完整性自陈"])

    def test_balance_template_gap_in_financials_response(self) -> None:
        from mcp_gateway.engines import hk_stock as hk_module

        engine = hk_module.HKStockEngine(timeout=1)
        full = {"截止日期": "2026-06-30", "币种": "USD", "股本": 1.0, "储备": 2.0, "保留溢利": 3.0, "归属于母公司股东权益": 10.0}
        short = {"截止日期": "2026-03-31", "币种": "USD", "归属于母公司股东权益": 10.0}
        with unittest.mock.patch.object(engine, "_get_eastmoney_hk_financials", return_value=[dict(short), dict(full)]):
            with unittest.mock.patch.object(engine, "_resolve_exact_security_entity", return_value=None):
                res = engine.get_financials("02026", report_type="balance", limit_periods=2)
        self.assertIn("报表模板完整性说明", res)
        self.assertIn("权益明细模板完整性自陈", res["statements"][0])

    def test_dividends_usd_and_multi_tranche(self) -> None:
        from mcp_gateway.engines import hk_stock as hk_module

        engine = hk_module.HKStockEngine(timeout=1)
        rows = [
            {"公布日期": "2026-05-20", "派息内容": "末期息0.13125美元(或每股美国存托股1.05美元);"},
            {"公布日期": "2025-11-20", "派息内容": "末期息0.13125美元(或每股美国存托股1.05美元);末期特别息0.11875美元(或每股美国存托股0.95美元)"},
            {"公布日期": "2025-05-20", "派息内容": "不派息"},
            {"公布日期": "2024-11-20", "派息内容": "每股派息0.3元人民币(相当于0.33港元)"},
        ]
        with unittest.mock.patch.object(engine, "_get_sina_hk_table", return_value=[dict(r) for r in rows]):
            res = engine._get_hk_f10("09988", category="dividends")
        divs = res["formatted_tables"]["dividends"]

        self.assertEqual(divs[0]["每股派息_美元"], 0.13125)
        self.assertEqual(divs[0]["派息_计量币种"], "USD")
        self.assertIsNone(divs[0]["每股派息_港元"])
        self.assertNotIn("派息明细_多笔", divs[0])

        self.assertEqual(len(divs[1]["派息明细_多笔"]), 2)
        amounts = [t["每股派息"] for t in divs[1]["派息明细_多笔"]]
        self.assertEqual(amounts, [0.13125, 0.11875])

        self.assertFalse(divs[2]["是否派息"])
        self.assertIsNone(divs[2]["派息_计量币种"])

        # 人民币宣派 + 港元等值：原有港元/人民币双列行为不变
        self.assertEqual(divs[3]["每股派息_人民币"], 0.3)
        self.assertEqual(divs[3]["每股派息_港元"], 0.33)
        self.assertEqual(divs[3]["派息_计量币种"], "CNY")


class TestAShareLhbAndCashflowOrderOffline(unittest.TestCase):
    """批次3：龙虎榜买卖双向去重/同日多榜次 + 现金流未知键继承锚点（全部离线）"""

    def test_order_by_canonical_keeps_broker_key_before_subtotal(self) -> None:
        from mcp_gateway.engines.a_stock import _order_by_canonical

        canonical = ["流入小计", "券商科目", "流出小计", "期末现金", "说明"]
        keys = ["说明", "期末现金", "流出小计", "流入小计", "券商科目"]  # 上游把券商科目排在最后
        ordered = _order_by_canonical(keys, canonical)
        self.assertLess(ordered.index("券商科目"), ordered.index("流出小计"))
        self.assertLess(ordered.index("流出小计"), ordered.index("期末现金"))

    def test_order_by_canonical_unknown_key_inherits_anchor(self) -> None:
        from mcp_gateway.engines.a_stock import _order_by_canonical

        canonical = ["A", "B", "C"]
        ordered = _order_by_canonical(["A", "X未知科目", "B"], canonical)
        self.assertEqual(ordered, ["A", "X未知科目", "B"])

    def test_lhb_merges_two_way_seat_and_labels_batch(self) -> None:
        from mcp_gateway.engines import a_stock as a_module

        def row(date: str, reason: str, seat: str, buy: float, sell: float, ratio: float) -> dict:
            return {
                "TRADE_DATE": f"{date} 00:00:00",
                "OPERATEDEPT_NAME": seat,
                "BUY": buy,
                "SELL": sell,
                "NET": buy - sell,
                "TOTAL_BUYRIO": ratio,
                "TOTAL_SELLRIO": ratio,
                "EXPLANATION": reason,
            }

        reason_a = "日涨幅达到15%的前5只证券"
        reason_b = "日换手率达到30%的前5只证券"
        board = {"result": {"data": [
            {"TRADE_DATE": "2025-03-31 00:00:00", "CLOSE_PRICE": 12.3, "CHANGE_RATE": 15.0,
             "BILLBOARD_BUY_AMT": 301105000.0, "BILLBOARD_SELL_AMT": 173553000.0,
             "BILLBOARD_NET_AMT": 127552000.0, "ACCUM_AMOUNT": 2000000000.0, "EXPLANATION": reason_a},
            {"TRADE_DATE": "2025-03-31 00:00:00", "CLOSE_PRICE": 12.3, "CHANGE_RATE": 15.0,
             "BILLBOARD_BUY_AMT": 99000000.0, "BILLBOARD_SELL_AMT": 5000000.0,
             "BILLBOARD_NET_AMT": 94000000.0, "ACCUM_AMOUNT": 2000000000.0, "EXPLANATION": reason_b},
        ]}}
        buy = {"result": {"data": [
            row("2025-03-31", reason_a, "机构专用", 200000000.0, 50000000.0, 0.10),
            row("2025-03-31", reason_a, "深股通专用", 101105000.0, 0.0, 0.05),
        ]}}
        sell = {"result": {"data": [
            row("2025-03-31", reason_a, "机构专用", 200000000.0, 50000000.0, 0.10),
        ]}}

        def fake_open(req, timeout=None):
            url = getattr(req, "full_url", "")
            if "RPT_DAILYBILLBOARD_DETAILS" in url:
                payload = board
            elif "RPT_BILLBOARD_DAILYDETAILSBUY" in url:
                payload = buy
            else:
                payload = sell
            return _FakeHKResponse(json.dumps(payload).encode("utf-8"))

        engine = a_module.AStockEngine(timeout=1)
        with unittest.mock.patch.object(a_module._SECURE_OPENER, "open", side_effect=fake_open):
            res = engine._get_ashare_lhb("300687")

        seats = res["formatted_tables"]["seat_details"]
        self.assertEqual(len(seats), 2, "买卖两侧同一席位未合并")
        two_way = [s for s in seats if s["营业部席位名称"] == "机构专用"][0]
        self.assertEqual(two_way["席位类型"], "买卖前五双向席位")
        self.assertIn("占总成交比例_pct_买", two_way)
        self.assertIn("占总成交比例_pct_卖", two_way)

        reason_a_seats = [s for s in seats if s["上榜异动说明"] == reason_a]
        self.assertAlmostEqual(sum(s["买入金额_万元"] for s in reason_a_seats), 30110.5, places=2)

        boards = res["formatted_tables"]["billboard_records"]
        self.assertEqual([b["榜单批次"] for b in boards], ["2025-03-31-1", "2025-03-31-2"])
        self.assertIn("同日多榜次说明", boards[0])
        self.assertIn("同日多榜次说明", res)
        self.assertIn("席位去重口径说明", res)
        self.assertIn("本次折叠 1 条", res["席位去重口径说明"])
        self.assertEqual({s["榜单批次"] for s in reason_a_seats}, {"2025-03-31-1"})


    def test_gateway_passes_through_lhb_notes(self) -> None:
        gw = FinanceGateway()
        fake = {
            "formatted_tables": {"billboard_records": [{"交易日期": "2025-03-31"}], "seat_details": []},
            "席位去重口径说明": "去重说明X",
            "同日多榜次说明": "多榜次X",
        }
        with unittest.mock.patch.object(gw.tdx_client, "_get_ashare_lhb", return_value=fake):
            with unittest.mock.patch.object(
                gw.tdx_client, "_resolve_exact_security_entity", return_value={"entity_code": "300687", "entity_name": "赛意信息", "entity_type": "A股"}
            ):
                res = gw.stock_f10({"symbol": "300687", "category": "lhb"})
        self.assertEqual(res["席位去重口径说明"], "去重说明X")
        self.assertEqual(res["同日多榜次说明"], "多榜次X")


class TestHKQuoteWeiBiAndNewsOffline(unittest.TestCase):
    """批次4：港股委比不可计算声明 + 新闻去重与相关度标签（全部离线）"""

    def test_hk_quotes_declares_weibi_not_computable(self) -> None:
        from mcp_gateway.engines import hk_stock as hk_module

        engine = hk_module.HKStockEngine(timeout=1)
        raw = 'v_r_hk00700="100~腾讯控股~00700~386.000~382.400";'
        with unittest.mock.patch.object(
            hk_module._SECURE_OPENER, "open", side_effect=lambda *a, **k: _FakeHKResponse(raw.encode("gb18030"))
        ):
            with unittest.mock.patch.object(engine, "_get_eastmoney_hk_financials", return_value=[]):
                res = engine._get_hk_quotes("00700")
        pc = res["盘口核心行情"]
        self.assertEqual(pc["委差_手"], 0)
        self.assertEqual(pc["委比_pct"], 0.0)
        self.assertFalse(pc["委差_可计算"])
        self.assertFalse(pc["委比_pct_可计算"])
        self.assertIn("不可计算", pc["委比委差说明"])
        self.assertIn("占位值", pc["委比委差说明"])

    def test_news_dedupes_and_tags_relevance(self) -> None:
        gw = FinanceGateway()
        payload = json.dumps({
            "data": {"list": [
                {"title": "腾讯控股完成发行18亿美元及人民币260亿元票据", "intro": "腾讯控股公告...",
                 "media": "新浪财经", "dataTime": "2026-10-23 10:00:00"},
                {"title": "腾讯控股完成发行18亿美元及人民币260亿元票据 ", "intro": "转载稿...",
                 "media": "证券时报", "dataTime": "2026-10-23 10:05:00"},
                {"title": "港股23日涨0.69% 收报23093.37点", "intro": "……腾讯控股涨1.2%……",
                 "media": "新浪财经", "dataTime": "2026-10-23 16:00:00"},
                {"title": "阿里巴巴跌超6%", "intro": "恒生科技指数下挫，腾讯跟跌",
                 "media": "财联社", "dataTime": "2026-10-22 15:00:00"},
                {"title": "某公司完成回购注销", "intro": "与本标的无关",
                 "media": "新浪财经", "dataTime": "2026-10-21 09:00:00"},
            ]}
        })

        def fake_urlopen(req, *args, **kwargs):
            m = unittest.mock.MagicMock()
            m.read.return_value = payload.encode("utf-8")
            m.__enter__.return_value = m
            return m

        entity = {"entity_code": "00700", "entity_name": "腾讯控股", "entity_type": "港股代码"}
        with unittest.mock.patch.object(gw.tdx_client, "_resolve_exact_security_entity", return_value=entity):
            with unittest.mock.patch("mcp_gateway.finance.urlopen", side_effect=fake_urlopen):
                res = gw.stock_news({"symbol": "00700", "limit": 5})

        news = res["news"]
        self.assertEqual(len(news), 3, "重复标题未去重或无关稿未剔除")
        self.assertEqual(news[0]["title"], "港股23日涨0.69% 收报23093.37点")
        self.assertEqual(news[0]["相关度"], "摘要命中")
        self.assertEqual(news[1]["相关度"], "标题命中")
        self.assertEqual(news[2]["相关度"], "摘要命中")
        self.assertEqual([n["title"] for n in news].count("腾讯控股完成发行18亿美元及人民币260亿元票据"), 1)
        # 时间倒序契约不变
        times = [n["time"] for n in news]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertIn("新闻相关度说明", res)
        self.assertIn("标题命中", res["新闻相关度说明"])


class TestHKCompanyInfoCacheOffline(unittest.TestCase):
    """观察项：公司概况缓存 + 每手股数轻量取数（不再每次 summary 重打整条 quotes 链）"""

    @staticmethod
    def _quote_payload(lot: str) -> bytes:
        parts = [""] * 61
        parts[1] = "腾讯控股"
        parts[2] = "00700"
        parts[60] = lot
        return ('v_r_hk00700="' + "~".join(parts) + '";').encode("gb18030")

    def test_company_info_cached_and_lot_size_light(self) -> None:
        from mcp_gateway.engines import hk_stock as hk_module

        engine = hk_module.HKStockEngine(timeout=1)
        html = (
            "<html><body><table>"
            "<tr><td>证券代码</td><td>00700</td></tr>"
            "<tr><td>公司名称(中文)</td><td>腾讯控股有限公司</td></tr>"
            "</table></body></html>"
        ).encode("gbk")
        calls = {"sina": 0, "gtimg": 0}

        def fake_open(req, timeout=None):
            url = getattr(req, "full_url", "")
            if "qt.gtimg.cn" in url:
                calls["gtimg"] += 1
                return _FakeHKResponse(self._quote_payload("100"))
            calls["sina"] += 1
            return _FakeHKResponse(html)

        with unittest.mock.patch.object(hk_module._SECURE_OPENER, "open", side_effect=fake_open):
            with unittest.mock.patch.object(engine, "_get_eastmoney_hk_company_profile", return_value={}):
                with unittest.mock.patch.object(engine, "get_quotes", side_effect=AssertionError("仍走了完整 quotes 链")):
                    first = engine._get_sina_hk_company_info("00700")
                    second = engine._get_sina_hk_company_info("00700")

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first["每手股数"], "100")
        self.assertEqual(second["每手股数"], "100")
        self.assertIsNot(first, second, "缓存返回了同一对象，调用方改字段会污染缓存")
        self.assertEqual(calls["sina"], 1, "公司概况未命中缓存，重复抓取新浪页")
        self.assertEqual(calls["gtimg"], 1, "每手股数未命中缓存，重复请求腾讯行情")

    def test_lot_size_falls_back_to_eastmoney_when_quote_fails(self) -> None:
        from urllib.error import URLError

        from mcp_gateway.engines import hk_stock as hk_module

        engine = hk_module.HKStockEngine(timeout=1)
        html = "<html><body><table><tr><td>公司名称(中文)</td><td>腾讯控股有限公司</td></tr></table></body></html>".encode("gbk")

        def fake_open(req, timeout=None):
            url = getattr(req, "full_url", "")
            if "qt.gtimg.cn" in url:
                raise URLError("simulated timeout")
            return _FakeHKResponse(html)

        with unittest.mock.patch.object(hk_module._SECURE_OPENER, "open", side_effect=fake_open):
            with unittest.mock.patch.object(
                engine, "_get_eastmoney_hk_company_profile", return_value={"zqzl": {"zxjydw": "500"}}
            ):
                info = engine._get_sina_hk_company_info("00700")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["每手股数"], "500")


class TestBatch6OfflineFixes(unittest.TestCase):
    """批次6：A 股委比、screener 页内去重、A 股分红空页重试（离线）"""

    def test_calc_order_imbalance(self) -> None:
        from mcp_gateway.engines.a_stock import _calc_order_imbalance

        bsp = {
            "买一": {"价格": 10.0, "委托量_手": 38},
            "买二": {"价格": 9.9, "委托量_手": 1},
            "买三": {"价格": 9.8, "委托量_手": 1},
            "买四": {"价格": 9.7, "委托量_手": 1},
            "买五": {"价格": 9.6, "委托量_手": 1},
            "卖一": {"价格": 10.1, "委托量_手": 5},
            "卖二": {"价格": 10.2, "委托量_手": 5},
            "卖三": {"价格": 10.3, "委托量_手": 5},
            "卖四": {"价格": 10.4, "委托量_手": 5},
            "卖五": {"价格": 10.5, "委托量_手": 5},
        }
        buy, sell, diff, pct, ok = _calc_order_imbalance(bsp)
        self.assertEqual((buy, sell, diff), (42, 25, 17))
        self.assertAlmostEqual(pct, 25.37, places=2)
        self.assertTrue(ok)
        self.assertEqual(_calc_order_imbalance({}), (0, 0, 0, 0.0, False))

    def test_screener_page_dedup(self) -> None:
        gw = FinanceGateway()
        payload = [
            [0, "", 3, "s", 3],
            ["POS", "market", "sec_code", "sec_name", "now_price", "chg"],
            ["", "", "", "", "", ""],
            [1, "0", "002223", "博实股份", "25.15", "-1.5"],
            [2, "0", "002223", "博实股份", "25.15", "-1.5"],
            [3, "0", "002514", "宝馨科技", "5.10", "2.1"],
        ]
        with unittest.mock.patch.object(gw.tdx_client, "_post", return_value=payload):
            res = gw.stock_screener({"message": "储能", "limit": 10})
        self.assertEqual(res["returned_count"], 2)
        self.assertEqual(res["页内去重条数"], 1)
        self.assertEqual([s["code"] for s in res["stocks"]], ["002223", "002514"])
        self.assertIn("页间不重不漏", res["分页与过滤说明"])
        self.assertIn("TTL 60 秒", res["分页快照说明"])

    def test_ashare_dividends_retry_on_empty_page(self) -> None:
        from mcp_gateway.engines import a_stock as a_module

        empty = b"<html><body>no table</body></html>"
        good = (
            '<html><body><table id="sharebonus_1">'
            "<tr><td>公告日期</td><td>送股</td><td>转增</td><td>派息</td><td>进度</td><td>除权除息日</td><td>股权登记日</td><td>红股上市日</td></tr>"
            "<tr><td>2026-06-30</td><td>0</td><td>0</td><td>3.5</td><td>实施</td><td>2026-07-10</td><td>2026-07-09</td><td>--</td></tr>"
            "</table></body></html>"
        ).encode("gbk")
        calls = {"n": 0}

        def fake_open(req, timeout=None):
            calls["n"] += 1
            return _FakeHKResponse(empty if calls["n"] == 1 else good)

        engine = a_module.AStockEngine(timeout=1)
        with unittest.mock.patch.object(a_module._SECURE_OPENER, "open", side_effect=fake_open):
            with unittest.mock.patch.object(a_module.time, "sleep", return_value=None):
                rows = engine._get_ashare_dividends("600519")
        self.assertEqual(calls["n"], 2, "空页未触发重试")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["派息_每10股_含税"], 3.5)


class TestHKV202BatchOffline(unittest.TestCase):
    """H 批：模板缺口措辞按报表类型、A/H 股本结构轻量取数与缓存（离线）"""

    def test_gap_origin_phrase_by_report_type(self) -> None:
        from mcp_gateway.engines.hk_stock import _gap_origin_phrase

        self.assertIn("季报简表", _gap_origin_phrase("一季报"))
        self.assertIn("季报简表", _gap_origin_phrase("三季报"))
        self.assertNotIn("季报", _gap_origin_phrase("中报"))
        self.assertIn("本期为中报", _gap_origin_phrase("中报"))
        self.assertNotIn("季报", _gap_origin_phrase("年报"))
        self.assertIn("本期为年报", _gap_origin_phrase("年报"))
        self.assertTrue(_gap_origin_phrase(None))

    def test_share_structure_parse_and_cache(self) -> None:
        from mcp_gateway.engines import hk_stock as hk

        parts = ["0"] * 71
        parts[69] = "21689434304"
        parts[70] = "3377482000"
        raw = 'v_hk01088="' + "~".join(parts) + '";'
        calls = {"n": 0}

        def fake_open(req, timeout=None):
            calls["n"] += 1
            return _FakeHKResponse(raw.encode("gb18030"))

        engine = hk.HKStockEngine(timeout=1)
        with unittest.mock.patch.object(hk._SECURE_OPENER, "open", side_effect=fake_open):
            first = engine._get_hk_share_structure("01088")
            second = engine._get_hk_share_structure("01088")
        self.assertEqual(first, (21689434304, 3377482000))
        self.assertEqual(second, first)
        self.assertEqual(calls["n"], 1, "二次调用未命中 10 分钟缓存")


    def test_orderbook_level_contract_and_snapshot(self) -> None:
        from mcp_gateway.engines.a_stock import (
            ORDERBOOK_BUY_LEVELS,
            ORDERBOOK_SELL_LEVELS,
            _bsp_from_snapshot,
            _calc_order_imbalance,
        )

        self.assertEqual(ORDERBOOK_BUY_LEVELS, ("买一", "买二", "买三", "买四", "买五"))
        self.assertEqual(ORDERBOOK_SELL_LEVELS, ("卖一", "卖二", "卖三", "卖四", "卖五"))
        snap: dict[str, object] = {}
        for i in range(1, 6):
            snap[f"BuyVol{i}"] = 2
            snap[f"SellVol{i}"] = 1
            snap[f"BuyPrice{i}"] = 10.0 - i * 0.1
            snap[f"SellPrice{i}"] = 10.0 + i * 0.1
        bsp = _bsp_from_snapshot(snap)
        self.assertEqual(list(bsp.keys()), list(ORDERBOOK_BUY_LEVELS + ORDERBOOK_SELL_LEVELS))
        buy, sell, diff, pct, ok = _calc_order_imbalance(bsp)
        self.assertEqual((buy, sell, diff), (10, 5, 5))
        self.assertAlmostEqual(pct, 33.33, places=2)
        self.assertTrue(ok)


class TestValuationCaliberSSOT(unittest.TestCase):
    """7.2：A 股估值口径阈值与 PB 偏差文案的单一事实源（离线）"""

    def test_pb_diff_note_branches(self) -> None:
        from mcp_gateway.engines.a_stock import (
            A_PB_SNAPSHOT_DIFF_THRESHOLD,
            _pb_snapshot_diff_note,
        )

        self.assertEqual(A_PB_SNAPSHOT_DIFF_THRESHOLD, 0.03)
        self.assertEqual(_pb_snapshot_diff_note(1.00, 1.02), "", "阈值内不应出提示")
        self.assertEqual(_pb_snapshot_diff_note(None, 1.0), "")
        self.assertIn("高估", _pb_snapshot_diff_note(3.09, 2.8461))
        self.assertIn("永续债", _pb_snapshot_diff_note(0.60, 0.70, True))
        low = _pb_snapshot_diff_note(0.60, 0.70, False)
        self.assertIn("低估", low)
        self.assertNotIn("永续债", low)

    def test_finance_pe_thresholds(self) -> None:
        from mcp_gateway import finance

        self.assertEqual(finance.A_PE_SNAPSHOT_ALIGN_ABS, 0.05)
        self.assertEqual(finance.A_PE_IMPLIED_NETPROFIT_GAP_PCT, 2.0)


class TestScreenerSnapshot73(unittest.TestCase):
    """7.3：同一查询 60 秒快照，跨页从同一数组切片（离线，模拟上游排序漂移）"""

    def test_cross_page_consistency_and_ttl(self) -> None:
        gw = FinanceGateway()
        base_rows = [[i, "0", f"{600000 + i:06d}", f"股票{i}", "10.0", "1.0"] for i in range(1, 21)]
        header = [0, "", 40, "s", 20]
        cols = ["POS", "market", "sec_code", "sec_name", "now_price", "chg"]
        types_row = ["", "", "", "", "", ""]
        calls = {"n": 0}

        def fake_post(url, body):
            calls["n"] += 1
            rows = base_rows if calls["n"] == 1 else list(reversed(base_rows))
            return [header, cols, types_row] + rows

        with unittest.mock.patch.object(gw.tdx_client, "_post", side_effect=fake_post):
            p1 = gw.stock_screener({"message": "储能", "limit": 10, "page": 1})
            p2 = gw.stock_screener({"message": "储能", "limit": 10, "page": 2})

        c1 = [s["code"] for s in p1["stocks"]]
        c2 = [s["code"] for s in p2["stocks"]]
        self.assertEqual(len(c1), 10)
        self.assertEqual(len(c2), 10)
        self.assertEqual(set(c1) & set(c2), set(), "跨页出现重复标的")
        self.assertEqual(calls["n"], 1, "TTL 内应复用同一份快照")
        self.assertEqual(p1["matched_count"], 40)
        self.assertEqual(p1["快照累计行数"], 20)

        # 快照过期后重新取数（上游排序漂移不影响单次快照内部一致性）
        gw.tdx_client.a_engine._screener_cache["储能"]["ts"] -= 61
        with unittest.mock.patch.object(gw.tdx_client, "_post", side_effect=fake_post):
            p3 = gw.stock_screener({"message": "储能", "limit": 10, "page": 1})
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(p3["stocks"]), 10)


class TestFieldDictionary74(unittest.TestCase):
    """7.4：字段字典按市场分表，清单字段必须仍存在于对应源码（离线）"""

    def test_contract_fields_present(self) -> None:
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "scripts" / "check_field_dictionary.py"
        spec = importlib.util.spec_from_file_location("check_field_dictionary", path)
        self.assertIsNotNone(spec.loader)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.check(), [], "字段字典与源码不一致")
        contract = mod.json.loads((mod.ROOT / "tests" / "field_contract.json").read_text(encoding="utf-8"))
        self.assertIn("a_share", contract, "A 股字典缺失")
        self.assertIn("hk", contract, "港股字典缺失")


class TestBatch8DisclosureGrading(unittest.TestCase):
    """批次8：自陈按分级——不串票、标基准、算不了给 null + 可算位（离线）"""

    def test_ah_note_has_no_borrowed_numbers(self) -> None:
        from mcp_gateway.engines.hk_stock import _ah_caliber_note

        note = _ah_caliber_note(21689434304, 3377482000)
        self.assertNotIn("01088", note, "通用提示不得引用具体票号")
        self.assertNotIn("%", note, "通用提示不得带百分比样例")
        self.assertIn("3,377,482,000", note)
        self.assertIn("不代表差异为 0", note)

    def test_hk_income_ah_flags_are_false(self) -> None:
        """A_H 差异位必须是 null + false（本引擎无 A 腿），不能是 0。"""
        import inspect

        from mcp_gateway.engines import hk_stock as hk

        src = inspect.getsource(hk)
        self.assertIn('return_dict["A_H差异_pct"] = None', src)
        self.assertIn('return_dict["A_H差异可算"] = False', src)

    def test_handler_gross_profit_note_mentions_other_cost(self) -> None:
        from mcp_gateway.handlers.hk_ifrs_industrial import HKIFRSIndustrialHandler

        handler = HKIFRSIndustrialHandler()
        rows = handler.process_statements([{"营业额": 100.0}], "income", "report", {"real_currency": "CNY"})
        self.assertIn("其他成本", rows[0]["工商业毛利说明"])
        self.assertIn("不可直比", rows[0]["工商业毛利说明"])


class TestBatch9DatetimeNormalization(unittest.TestCase):
    """批次9：行情时间跨市场归一为 ISO（A 股紧凑格式 + 港股斜杠格式）"""

    def test_normalize_two_source_formats(self) -> None:
        from mcp_gateway.core.market_utils import normalize_datetime_iso as f

        self.assertEqual(f("20261026", "131802"), "2026-10-26 13:18:02")
        self.assertEqual(f("2026/10/26 13:18:01"), "2026-10-26 13:18:01")
        self.assertEqual(f("2026-10-26 09:30"), "2026-10-26 09:30:00")
        self.assertEqual(f("20261026"), "2026-10-26 00:00:00")
        self.assertIsNone(f("", "131802"))
        self.assertIsNone(f("bad"))
        self.assertIsNone(f("20261026", "256199"), "非法时分秒应返回 None")

    def test_quote_datetime_iso_cross_market(self) -> None:
        import re

        gw = FinanceGateway()
        pat = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        for code in ("600519", "00700"):
            hq = gw.stock_quotes({"symbol": code})["盘口核心行情"]
            value = hq.get("行情datetime")
            self.assertTrue(pat.match(str(value)), f"{code} 行情datetime 非 ISO: {value}")


if __name__ == "__main__":
    unittest.main()







