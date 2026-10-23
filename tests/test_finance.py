"""金融网关核心功能单元测试套件 (Finance Gateway Tests)
测试市场代码推导、内置技术指标计算引擎 (MA/MACD/KDJ/BOLL)、交易日历时钟以及网关分发。
"""

import json
import unittest
import unittest.mock
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

        # 3. 验证 stock_financials 显式报错（不再返回 period_count: 0 与空表壳）
        with self.assertRaises(ValueError) as cm_f:
            fin_gw.stock_financials({"symbol": bad_code})
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
        self.assertIn("最高", hq_a)
        self.assertIn("最高价", hq_a)
        self.assertIn("最低", hq_a)
        self.assertIn("最低价", hq_a)
        self.assertIn("涨跌额_元", hq_a)
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
            self.assertIn("最高", hq_hk)
            self.assertIn("涨跌额_元", hq_hk)
            self.assertIn("日内振幅_pct", hq_hk)
            self.assertIn("更新时间", hq_hk)
            self.assertIn("滚动市盈率_PETTM", ext_hk)
            self.assertIn("动态市盈率_PE", ext_hk)
            self.assertIn("总市值_元", ext_hk)
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

        # 6. stock_f10: 验证 category 别名 (industry, dividends, repurchases, lhb)
        res_f10_ind = fin_gw.stock_f10({"symbol": "600519", "category": "industry"})
        self.assertEqual(res_f10_ind["category"], "concepts")
        res_f10_rep = fin_gw.stock_f10({"symbol": "600519", "category": "buyback"})
        self.assertEqual(res_f10_rep["category"], "repurchases")

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
            self.assertIn("动态市盈率_PE_TTM", t6_row)
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

        # 1. 验证 Issue 1 (P0): A 股 stock_f10 支持 indicators / financials 枚举
        res_ind = fin_gw.stock_f10({"symbol": "600519", "category": "indicators"})
        self.assertIn("indicators", res_ind.get("formatted_tables", {}))
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

        # 9. 验证 Issue 10 & 13 (P2/P3): industry 分类字段汉化映射无裸拼音键
        res_ind = fin_gw.stock_f10({"symbol": "600519", "category": "industry"})
        t0 = res_ind.get("formatted_tables", {}).get("table0", [])
        if t0:
            self.assertIn("题材概念名称", t0[0])

        # 10. 验证 Issue 11 (P2): A 股 repurchases 返回结构化空表与明确无记录说明
        res_rep = fin_gw.stock_f10({"symbol": "600519", "category": "repurchases"})
        self.assertEqual(len(res_rep.get("formatted_tables", {}).get("repurchases", [])), 0)
        self.assertIn("无记录说明", res_rep)

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


if __name__ == "__main__":
    unittest.main()





