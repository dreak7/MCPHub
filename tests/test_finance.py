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


if __name__ == "__main__":
    unittest.main()




