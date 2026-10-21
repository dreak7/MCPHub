"""通达信金融数据专线与多源高可用金融服务适配器 (Finance Gateway)
提供 A 股/港股/美股/ETF 的实体检索、实时盘口、多周期K线（含内置量化指标计算）、
三大财务报表、F10 深度基本面、自然语言智能选股与市场交易时钟。
"""

from __future__ import annotations

import datetime
import gzip
import json
import logging
import math
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


# =========================================================================
# 【功能块 0：安全 HTTP 传输层与重定向拦截处理器】
# - 输入：HTTP 请求对象与重定向目标 URL
# - 功能：严格拦截 POST 请求发生时的任何 301/302/303 重定向跳转，
#         彻底阻断 Authorization/Token 凭据被重定向转发泄露到第三方恶意主机或私网目标
# - 输出：抛出 HTTPError 明确阻断
# =========================================================================
class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise HTTPError(req.full_url, code, f"HTTP redirect ({code}) is disallowed for security in financial gateway", headers, fp)

_SECURE_OPENER = build_opener(NoRedirectHandler)


# =========================================================================
# 【功能块 1：高频盘口与量化行情数据字段映射表】
# - 输入：通达信原始盘口英文缩写
# - 功能：将底层十六进制与英文缩写规范化映射为清晰、标准的中文金融业务指标
# - 输出：dict 映射字典
# =========================================================================
HQ_FIELD_MAP: dict[str, str] = {
    "Now": "现价",
    "Close": "昨收",
    "Open": "今开",
    "MaxP": "最高价",
    "MinP": "最低价",
    "Average": "成交均价",
    "Volume": "成交量_手",
    "Amount": "成交额_元",
    "Inside": "内盘_手",
    "Outside": "外盘_手",
    "InOutFlag": "内外盘强弱标志",
    "Yield": "总股本_股",
    "CJBS": "成交笔数",
    "LB": "量比",
    "HSL": "换手率_pct",
    "PHVolume": "盘后成交量",
    "RestVol": "剩余委托量",
    "HQDate": "行情日期",
    "HQTime": "行情时间",
}

EXT_FIELD_MAP: dict[str, str] = {
    "ZTPrice": "涨停价",
    "DTPrice": "跌停价",
    "ZSZ": "总市值_元",
    "SYL": "动态市盈率_PE",
    "MGSY": "报告期每股收益_EPS",
    "MGJZC": "每股净资产_BPS",
    "MGGX": "每股公积金",
    "ZGB": "总股本_万股",
    "LTGB": "流通股本_万股",
    "BelongHY": "所属行业代码",
    "Addr": "地域代码",
}

# =========================================================================
# 【功能块 1.1：F10 深度基本面关键财务与估值指标映射表】
# - 输入：通达信 F10 摘要表底层英文缩写代码（如 mgjzc, gmjlr, gmjlrtbzzl, sjl）
# - 功能：将底层缩写规范化映射为清晰、标准的中文金融业务指标
# - 输出：dict 映射字典
# =========================================================================
F10_TABLE4_FINANCIAL_MAP: dict[str, str] = {
    "mgsy": "每股收益_EPS",
    "mgjzc": "每股净资产_BPS",
    "yysr": "营业收入_元",
    "gmjlr": "归母净利润_元",
    "mll": "毛利率_pct",
    "zcfzl": "资产负债率_pct",
    "kfjlrtbzzl": "扣非净利润同比增减_pct",
    "yysrtbzzl": "营业收入同比增减_pct",
    "gmjlrtbzzl": "归母净利润同比增减_pct",
    "mgzbgj": "每股资本公积",
    "mgwfplr": "每股未分配利润",
    "mgjjxjl": "每股经营现金流",
    "bgq": "报告期",
}

F10_TABLE6_VALUATION_MAP: dict[str, str] = {
    "sylttm": "动态市盈率_PE_TTM",
    "syllyr": "静态市盈率_PE_LYR",
    "sjl": "市净率_PB_昨收",
    "zsz": "总市值_元",
    "zgb": "总股本_股",
    "ltag": "流通A股_股",
}

# =========================================================================
# 【功能块 2：三大财务报表科目字段映射表（非金融实体企业）】
# - 输入：通达信 CW 财报原始列代码（如 N001, N002）
# - 功能：将资产负债表、利润表、现金流量表代码精确转换为会计科目名称
# - 输出：dict 映射字典
# =========================================================================
INCOME_FIELDS_NON_FINANCE: dict[str, str] = {
    "N001": "截止日期",
    "N002": "营业总收入",
    "N003": "营业收入",
    "N004": "营业总成本",
    "N005": "营业成本",
    "N006": "营业税金及附加",
    "N007": "销售费用",
    "N008": "管理费用",
    "N009": "研发费用",
    "N010": "财务费用",
    "N012": "资产减值损失",
    "N013": "信用减值损失",
    "N014": "公允价值变动净收益",
    "N015": "投资收益",
    "N019": "营业利润",
    "N020": "营业外收入",
    "N021": "营业外支出",
    "N022": "利润总额",
    "N023": "所得税费用",
    "N024": "净利润",
    "N025": "归母净利润",
    "N026": "少数股东损益",
    "N029": "基本每股收益_EPS",
    "N030": "稀释每股收益",
    "N035": "扣非净利润",
    "Tdate": "报告期日期",
}

BALANCE_FIELDS_NON_FINANCE: dict[str, str] = {
    "N001": "截止日期",
    "N002": "货币资金",
    "N003": "交易性金融资产",
    "N004": "应收票据及账款合计",
    "N005": "应收票据",
    "N006": "应收账款",
    "N007": "预付款项",
    "N008": "其他应收款",
    "N009": "应收利息",
    "N010": "应收股利",
    "N011": "存货",
    "N012": "一年内到期的非流动资产",
    "N013": "合同资产",
    "N014": "其他流动资产",
    "N015": "流动资产合计",
    "N016": "可供出售金融资产",
    "N017": "债权投资",
    "N018": "其他债权投资",
    "N019": "其他权益工具投资",
    "N020": "其他非流动金融资产",
    "N021": "持有至到期投资",
    "N022": "长期应收款",
    "N023": "长期股权投资",
    "N024": "投资性房地产",
    "N025": "固定资产",
    "N026": "固定资产清理",
    "N027": "在建工程",
    "N028": "工程物资",
    "N029": "生产性生物资产",
    "N030": "油气资产",
    "N031": "使用权资产",
    "N032": "无形资产",
    "N033": "开发支出",
    "N034": "商誉",
    "N035": "长期待摊费用",
    "N036": "递延所得税资产",
    "N037": "非流动资产合计",
    "N038": "资产总计",
    "N039": "短期借款",
    "N040": "交易性金融负债",
    "N041": "衍生金融负债",
    "N042": "吸收存款及同业存放",
    "N043": "应付票据及应付账款合计",
    "N044": "应付票据",
    "N045": "应付账款",
    "N046": "预收款项",
    "N047": "合同负债",
    "N048": "应付职工薪酬",
    "N049": "应交税费",
    "N050": "应付利息",
    "N051": "其他应付款",
    "N052": "一年内到期的非流动负债",
    "N053": "其他流动负债",
    "N054": "待转销项税额",
    "N055": "长期借款",
    "N056": "应付债券",
    "N057": "流动负债合计",
    "N058": "长期借款",
    "N059": "应付债券",
    "N060": "租赁负债",
    "N061": "长期应付款",
    "N062": "专项应付款",
    "N063": "预计负债",
    "N064": "递延所得税负债",
    "N065": "其他非流动负债",
    "N066": "非流动负债合计",
    "N067": "负债合计",
    "N068": "实收资本_股本",
    "N069": "其他权益工具",
    "N070": "资本公积",
    "N071": "其他综合收益",
    "N072": "专项储备",
    "N073": "盈余公积",
    "N074": "库存股",
    "N075": "一般风险准备",
    "N076": "未分配利润",
    "N077": "所有者权益合计",
    "N078": "归母所有者权益合计",
    "N079": "少数股东权益",
    "N080": "负债和所有者权益总计",
    "Tdate": "报告期日期",
}

CASHFLOW_FIELDS_NON_FINANCE: dict[str, str] = {
    "N001": "截止日期",
    "N002": "销售商品提供劳务收到的现金",
    "N003": "收到的税费返还",
    "N004": "收到其他与经营活动有关的现金",
    "N005": "经营活动现金流入小计",
    "N006": "购买商品接受劳务支付的现金",
    "N007": "支付给职工以及为职工支付的现金",
    "N008": "支付的各项税费",
    "N009": "支付其他与经营活动有关的现金",
    "N010": "经营活动现金流出小计",
    "N011": "经营活动现金流量净额",
    "N012": "收回投资收到的现金",
    "N013": "取得投资收益收到的现金",
    "N014": "处置固定资产无形资产收回的现金净额",
    "N015": "处置子公司及其他营业单位收到的现金净额",
    "N016": "收到其他与投资活动有关的现金",
    "N017": "投资活动现金流入小计",
    "N018": "购建固定资产无形资产支付的现金",
    "N019": "投资支付的现金",
    "N020": "取得子公司及其他营业单位支付的现金净额",
    "N021": "支付其他与投资活动有关的现金",
    "N022": "投资活动现金流出小计",
    "N023": "投资活动现金流量净额",
    "N024": "吸收投资收到的现金",
    "N025": "取得借款收到的现金",
    "N026": "发行债券收到的现金",
    "N027": "收到其他与筹资活动有关的现金",
    "N028": "筹资活动现金流入小计",
    "N029": "偿还债务支付的现金",
    "N030": "分配股利利润或偿付利息支付的现金",
    "N031": "支付其他与筹资活动有关的现金",
    "N032": "筹资活动现金流出小计",
    "N033": "筹资活动现金流量净额",
    "N034": "汇率变动对现金及等价物的影响",
    "N035": "现金及现金等价物净增加额",
    "N036": "期初现金及现金等价物余额",
    "N037": "期末现金及现金等价物余额",
    "Tdate": "报告期日期",
}

# 融资融券交易数据 (margin_trading) 表头汉化映射表
MARGIN_TRADING_FIELDS: dict[str, str] = {
    "T001": "交易日期",
    "T003": "融资余额_元",
    "T004": "融资买入额_元",
    "T005": "融资偿还额_元",
    "T006": "融券余量_股",
    "T007": "融券卖出量_股",
    "T008": "融券偿还量_股",
    "bt009": "成交量_手",
    "bt010": "成交金额_元",
    "ClosePrice": "收盘价",
}

# 主力资金流向 (capital_flow) 表头汉化映射表
CAPITAL_FLOW_FIELDS: dict[str, str] = {
    "rq": "交易日期",
    "N001": "主力净流入_元",
    "N002": "主力净占比_pct",
    "N003": "超大单净流入_元",
    "N004": "超大单净占比_pct",
    "N005": "大单净流入_元",
    "N006": "大单净占比_pct",
    "N007": "中单净流入_元",
    "N008": "中单净占比_pct",
    "N015": "收盘价",
}

# 历史重大事件驱动催化 (events) 表头汉化映射表
EVENTS_FIELDS: dict[str, str] = {
    "cjrq": "催化日期",
    "sjmc": "事件名称",
    "rec_id": "事件记录ID",
    "sjxz": "事件性质",
    "cjrzdf": "当日涨跌幅_pct",
}


# =========================================================================
# 【功能块 3：证券交易所与市场代码智能推导器】
# - 输入：symbol (str): 股票、基金或指数代码（如 "600519", "000001", "835185"）
# - 功能：遵循中国证券市场编制规则，自动推导市场 setcode 代码：
#         '1'=上海证券交易所(沪市), '0'=深圳证券交易所(深市), '2'=北京证券交易所(北交所)
# - 输出：str: 推导得到的市场代码
# =========================================================================
def infer_market_setcode(symbol: str) -> str:
    clean_sym = symbol.strip().upper()
    # 港股标的 (5位纯数字或带有 HK 前缀，如 00700, 09988, HK00700)
    if clean_sym.startswith("HK") or (clean_sym.isdigit() and len(clean_sym) == 5):
        return "31"
    # 优先匹配北交所专属代码 (包含920新代码号段、8开头及4开头)
    if clean_sym.startswith(("BJ", "8", "4", "920")):
        return "2"
    # 沪市主板、科创板、B股、基金与转债
    if clean_sym.startswith(("SH", "6", "9", "5", "11")):
        return "1"
    # 深市主板、创业板、B股、基金与转债
    if clean_sym.startswith(("SZ", "0", "3", "2", "1", "12", "15")):
        return "0"
    return "0"


# =========================================================================
# 【功能块 4：纯 Python 原生轻量量化指标计算引擎 (Zero-Dependency)】
# - 输入：kline_list (list[dict]): 包含 open, close, high, low, volume 的 K 线列表
# - 功能：为历史 K 线滚动计算常用趋势与摆动指标：
#         1. 移动平均线: MA5, MA10, MA20
#         2. 异同移动平均线: MACD (DIF, DEA, MACD柱状图)
#         3. 随机指标: KDJ (9, 3, 3)
#         4. 布林线: BOLL (20日中轨、上轨、下轨)
#         5. 相对强弱指标: RSI (6, 12)
# - 输出：list[dict]: 附加了量化计算字段的 K 线字典数组，杜绝大模型计算幻觉
# =========================================================================
def calculate_indicators(kline_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not kline_list:
        return []

    enriched: list[dict[str, Any]] = [dict(item) for item in kline_list]
    closes = [float(k.get("close", 0.0)) for k in enriched]
    highs = [float(k.get("high", 0.0)) for k in enriched]
    lows = [float(k.get("low", 0.0)) for k in enriched]
    total_len = len(closes)

    # 1. 简单移动平均线 (MA5, MA10, MA20)
    for i in range(total_len):
        enriched[i]["ma5"] = round(sum(closes[max(0, i - 4):i + 1]) / (i - max(0, i - 4) + 1), 2) if i >= 4 else None
        enriched[i]["ma10"] = round(sum(closes[max(0, i - 9):i + 1]) / (i - max(0, i - 9) + 1), 2) if i >= 9 else None
        enriched[i]["ma20"] = round(sum(closes[max(0, i - 19):i + 1]) / (i - max(0, i - 19) + 1), 2) if i >= 19 else None

    # 2. 布林带 (BOLL, 20周期, 2倍标准差)
    for i in range(total_len):
        if i >= 19:
            window = closes[i - 19:i + 1]
            mean_val = sum(window) / 20.0
            variance = sum((x - mean_val) ** 2 for x in window) / 20.0
            std_dev = math.sqrt(variance)
            enriched[i]["boll_mid"] = round(mean_val, 2)
            enriched[i]["boll_up"] = round(mean_val + 2 * std_dev, 2)
            enriched[i]["boll_dn"] = round(mean_val - 2 * std_dev, 2)
        else:
            enriched[i]["boll_mid"] = None
            enriched[i]["boll_up"] = None
            enriched[i]["boll_dn"] = None

    # 3. MACD 指标 (12, 26, 9)
    ema12, ema26, dea = 0.0, 0.0, 0.0
    for i, c in enumerate(closes):
        if i == 0:
            ema12 = c
            ema26 = c
            dif = 0.0
            dea = 0.0
        else:
            ema12 = ema12 * (11 / 13) + c * (2 / 13)
            ema26 = ema26 * (25 / 27) + c * (2 / 27)
            dif = ema12 - ema26
            dea = dea * (8 / 10) + dif * (2 / 10)
        macd_bar = 2 * (dif - dea)
        enriched[i]["macd_dif"] = round(dif, 3)
        enriched[i]["macd_dea"] = round(dea, 3)
        enriched[i]["macd_bar"] = round(macd_bar, 3)

    # 4. KDJ 指标 (9, 3, 3)
    k_val, d_val = 50.0, 50.0
    for i in range(total_len):
        start_idx = max(0, i - 8)
        c_low = min(lows[start_idx:i + 1])
        c_high = max(highs[start_idx:i + 1])
        rsv = 50.0
        if c_high > c_low:
            rsv = ((closes[i] - c_low) / (c_high - c_low)) * 100.0
        k_val = (2.0 / 3.0) * k_val + (1.0 / 3.0) * rsv
        d_val = (2.0 / 3.0) * d_val + (1.0 / 3.0) * k_val
        j_val = 3.0 * k_val - 2.0 * d_val
        enriched[i]["kdj_k"] = round(k_val, 2)
        enriched[i]["kdj_d"] = round(d_val, 2)
        enriched[i]["kdj_j"] = round(j_val, 2)

    # 5. RSI 相对强弱指标 (6周期 与 12周期)
    for period, name in ((6, "rsi6"), (12, "rsi12")):
        for i in range(total_len):
            if i >= period:
                gains, losses = 0.0, 0.0
                for j in range(i - period + 1, i + 1):
                    diff = closes[j] - closes[j - 1]
                    if diff > 0:
                        gains += diff
                    else:
                        losses -= diff
                if losses == 0.0:
                    enriched[i][name] = 100.0
                else:
                    rs = gains / losses
                    enriched[i][name] = round(100.0 - (100.0 / (1.0 + rs)), 2)
            else:
                enriched[i][name] = None

    return enriched


# =========================================================================
# 【功能块 5：A股交易日历与时钟状态感知器】
# - 输入：无
# - 功能：判断当前北京时间（UTC+8）处于哪个交易阶段（开盘竞价/连续交易/午间休市/已收盘/周末），
#         提供基准交易日，防止大模型在非交易时段产生“股票异常停牌”等幻觉
# - 输出：dict: 包含当前交易状态与交易上下文
# =========================================================================
def get_market_context() -> dict[str, Any]:
    # 强制以北京时间 (UTC+8) 计算
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cst_time = now_utc + datetime.timedelta(hours=8)
    weekday = cst_time.weekday()  # 0=周一, 6=周日
    current_time_str = cst_time.strftime("%H:%M:%S")
    date_str = cst_time.strftime("%Y-%m-%d")

    is_weekend = weekday in (5, 6)
    time_val = cst_time.hour * 100 + cst_time.minute

    if is_weekend:
        status = "weekend_closed"
        status_desc = "周末休市"
    elif time_val < 915:
        status = "pre_market_closed"
        status_desc = "盘前未开盘 (等待9:15开盘集合竞价)"
    elif 915 <= time_val <= 925:
        status = "opening_auction"
        status_desc = "开盘集合竞价阶段 (9:15-9:25)"
    elif 930 <= time_val <= 1130:
        status = "trading_morning"
        status_desc = "早盘连续交易进行中 (9:30-11:30)"
    elif 1130 < time_val < 1300:
        status = "lunch_break"
        status_desc = "午盘休市中 (11:30-13:00)"
    elif 1300 <= time_val <= 1500:
        status = "trading_afternoon"
        status_desc = "午后连续交易进行中 (13:00-15:00)"
    else:
        status = "closed_today"
        status_desc = "今日已收盘 (15:00后结算状态)"

    # 计算上一个可用交易日基准
    offset = 1
    if weekday == 0 and time_val < 915:  # 周一开盘前看上周五
        offset = 3
    elif weekday == 5:  # 周六看周五
        offset = 1
    elif weekday == 6:  # 周日看周五
        offset = 2
    elif time_val < 915:
        offset = 1

    last_trading_day = (cst_time - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")

    return {
        "current_cst_time": f"{date_str} {current_time_str}",
        "market_status": status,
        "market_status_desc": status_desc,
        "is_trading_time": status in ("trading_morning", "trading_afternoon", "opening_auction"),
        "reference_trading_date": last_trading_day,
    }


# =========================================================================
# 【功能块 6：通达信官方专线金融客户端核心实现 (TdxClient)】
# - 输入：token (str): 通达信授权令牌；endpoint (str): 官方 TQLEX 网关端点
# - 功能：实现行情 (PBHQInfo)、K线 (PBFXT)、三大财务报表 (TdxShareCW)、
#         F10 深度题材与筹码 (TdxSharePCCW)、智能选股 (wendaQuery) 及
#         AI RAG 实体检索 (ai.icfqs.com) 的网络通信与结构化清洗
# - 输出：TdxClient 实例
# =========================================================================
class TdxClient:
    def __init__(
        self,
        token: str = "",
        api_endpoint: str = "http://tdxhub.icfqs.com:7615/TQLEX",
        rag_endpoint: str = "https://ai.icfqs.com:8965/v1/rag-entity-retrieve",
        timeout: int = 15,
        logger: logging.Logger | None = None,
    ):
        self.token = token.strip()
        self.api_endpoint = api_endpoint.rstrip("/")
        self.rag_endpoint = rag_endpoint
        self.timeout = timeout
        self.logger = logger or logging.getLogger("mcp_gateway.finance")

    def _post(self, url: str, body: dict[str, Any] | list[Any]) -> Any:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "MCPHub-Finance-Gateway/1.0",
        }
        if self.token:
            headers["token"] = self.token

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=payload, headers=headers, method="POST")

        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                resp_bytes = resp.read()
                return json.loads(resp_bytes.decode("utf-8", errors="replace"))
        except HTTPError as exc:
            err_msg = f"HTTP {exc.code} {exc.reason}"
            try:
                detail = exc.read().decode("utf-8", errors="replace")
                err_msg += f": {detail}"
            except Exception:
                pass
            raise RuntimeError(f"TDX API request failed ({err_msg})") from exc
        except URLError as exc:
            raise RuntimeError(f"TDX API network connection error ({exc.reason})") from exc

    # =========================================================================
    # 【功能块 3.5：纯数字证券代码正主实体精准反查与保全解析器】
    # - 输入参数：clean_code (str): 6位A股纯数字代码或5位港股纯数字代码
    # - 实现功能：
    #     解决 A 股代码与公募基金/债券等同名代码碰撞时，RAG 向量库遗漏股票正主或被挤占排名的缺陷
    #     （典型案例如：002698 既是博实股份也是博时纯债基金，RAG 仅返回基金而漏掉博实股份；
    #      000001 平安银行被华夏成长挤占等）。
    #     采用三轨防御反查：
    #     1. 第一轨：腾讯实时行情专线 (qt.gtimg.cn)，毫秒级覆盖沪深北全市场股票名称；
    #     2. 第二轨：新浪财经公开行情专线 (hq.sinajs.cn) 备份兜底；
    #     3. 第三轨：通达信官方专线 F10 摘要表 (table8.name) 权威反查。
    # - 输出结果：dict[str, Any] | None: 标准股票实体字典（含 entity_code, entity_name, entity_type, entity_setcode, aliases）
    # =========================================================================
    def _resolve_exact_security_entity(self, clean_code: str) -> dict[str, Any] | None:
        clean_code = clean_code.strip()
        if clean_code.isdigit() and len(clean_code) == 6:
            # A 股 6 位数字代码
            if clean_code.startswith(("60", "68", "90")):
                prefix = "sh"
                setcode = 1
            elif clean_code.startswith(("00", "30", "20")):
                prefix = "sz"
                setcode = 0
            else:
                prefix = "bj"
                setcode = 2

            # 第一轨：腾讯官方公开快速行情提取证券名称
            url = f"http://qt.gtimg.cn/q={prefix}{clean_code}"
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            try:
                with _SECURE_OPENER.open(req, timeout=3) as resp:
                    raw = resp.read().decode("gb18030", errors="replace")
                if "~" in raw and "none_match" not in raw:
                    parts = raw.split("~")
                    if len(parts) > 1 and parts[1].strip():
                        name = parts[1].strip()
                        return {
                            "entity_code": clean_code,
                            "entity_name": name,
                            "entity_type": "A股代码",
                            "entity_setcode": setcode,
                            "aliases": [name],
                        }
            except Exception:
                pass

            # 第二轨：新浪财经公开快速行情提取证券名称
            try:
                sina_url = f"http://hq.sinajs.cn/list={prefix}{clean_code}"
                sina_req = Request(
                    sina_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                        "Referer": "https://finance.sina.com.cn",
                    },
                )
                with _SECURE_OPENER.open(sina_req, timeout=3) as resp:
                    raw = resp.read().decode("gbk", errors="replace")
                if '="' in raw and '=""' not in raw:
                    payload = raw.split('="', 1)[1]
                    name = payload.split(",", 1)[0].strip()
                    if name:
                        return {
                            "entity_code": clean_code,
                            "entity_name": name,
                            "entity_type": "A股代码",
                            "entity_setcode": setcode,
                            "aliases": [name],
                        }
            except Exception:
                pass

            # 第三轨：通达信官方专线 F10 摘要表 table8.name 权威兜底
            try:
                f10_res = self.get_f10(symbol=clean_code, category="summary")
                tables = f10_res.get("formatted_tables", {})
                if "table8" in tables and tables["table8"]:
                    t8_name = tables["table8"][0].get("name")
                    if t8_name and str(t8_name).strip():
                        name = str(t8_name).strip()
                        return {
                            "entity_code": clean_code,
                            "entity_name": name,
                            "entity_type": "A股代码",
                            "entity_setcode": setcode,
                            "aliases": [name],
                        }
            except Exception:
                pass

        elif clean_code.isdigit() and len(clean_code) == 5:
            # 港股 5 位数字代码
            url = f"https://qt.gtimg.cn/q=r_hk{clean_code}"
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            try:
                with _SECURE_OPENER.open(req, timeout=3) as resp:
                    raw = resp.read().decode("gb18030", errors="replace")
                if "~" in raw and "none_match" not in raw:
                    parts = raw.split("~")
                    if len(parts) > 1 and parts[1].strip():
                        name = parts[1].strip()
                        return {
                            "entity_code": clean_code,
                            "entity_name": name,
                            "entity_type": "港股代码",
                            "entity_setcode": 31,
                            "aliases": [name],
                        }
            except Exception:
                pass

        return None

    def lookup_stock(self, query: str, market_range: str = "AG") -> list[dict[str, Any]]:
        """AI RAG 语义代码检索 (支持 A股, 港股 HK-GP, 美股 MG-GP, 基金 JJ, 指数 ZS)"""
        raw_query = query.strip()
        # 清洗可能带有的交易所前缀或后缀（如 sh600519 -> 600519, 002698.sz -> 002698）
        clean_code = re.sub(r"^(sh|sz|bj|hk)", "", raw_query, flags=re.I)
        clean_code = re.sub(r"\.(sh|sz|bj|hk|ss)$", "", clean_code, flags=re.I)

        req_body = {"query": raw_query, "range": market_range}
        resp = self._post(self.rag_endpoint, req_body)
        entities: list[dict[str, Any]] = []
        if isinstance(resp, dict) and "retrieved_entities" in resp:
            raw_entities = resp["retrieved_entities"]
            if isinstance(raw_entities, list):
                entities = raw_entities

        # 智能全市场双重嗅探：如果默认搜 AG 返回为空，但用户可能问的是港股（如“腾讯”、“阿里”、“美团”），自动降级尝试 HK-GP
        if not entities and market_range == "AG":
            try:
                hk_resp = self._post(self.rag_endpoint, {"query": raw_query, "range": "HK-GP"})
                if isinstance(hk_resp, dict) and "retrieved_entities" in hk_resp:
                    hk_entities = hk_resp["retrieved_entities"]
                    if isinstance(hk_entities, list) and hk_entities:
                        entities = hk_entities
            except Exception:
                pass

        # 纯数字代码正主保全与首位置顶逻辑（解决基金/债券等同名代码挤占或遗漏股票正主的问题）
        if clean_code.isdigit() and len(clean_code) in (5, 6) and market_range in ("AG", "all", "ALL", ""):
            exact_target = self._resolve_exact_security_entity(clean_code)
            if exact_target:
                target_code = exact_target["entity_code"]
                target_type = exact_target["entity_type"]

                # 检查 RAG 返回列表中是否已存在该股票正主
                found_idx = -1
                for idx, item in enumerate(entities):
                    if (
                        item.get("entity_code") == target_code
                        and item.get("entity_type") == target_type
                    ):
                        found_idx = idx
                        break

                if found_idx > 0:
                    # 正主排在基金等其他实体之后，将其移至首位置顶
                    exact_item = entities.pop(found_idx)
                    entities.insert(0, exact_item)
                elif found_idx < 0:
                    # 正主被 RAG 完全漏掉，将解析出的权威正主直接置顶插入第 0 位
                    entities.insert(0, exact_target)

        return entities

    # =========================================================================
    # 【功能块：腾讯免费公开港股实时行情获取与格式标准化】
    # - 输入：clean_code (str): 5位港股数字代码（如 "00700", "09988"）
    # - 功能：调用腾讯财经官方开放行情专线 (qt.gtimg.cn)，提取现价、五档委托、
    #         市盈率、总市值、成交额及52周高低点，标准化映射为统一盘口格式
    # - 输出：dict[str, Any]: 结构化港股微观盘口行情字典
    # =========================================================================
    def _get_hk_quotes(self, clean_code: str) -> dict[str, Any]:
        norm_code = clean_code.zfill(5)
        url = f"https://qt.gtimg.cn/q=r_hk{norm_code}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                raw_text = raw_bytes.decode("gb18030", errors="replace")
        except Exception as exc:
            raise RuntimeError(f"腾讯免费港股实时行情接口请求失败: {exc}") from exc

        if "none_match" in raw_text or "~" not in raw_text:
            raise ValueError(f"未找到港股代码 '{clean_code}' 的有效行情，请核实代码是否存在（提示：港股通常为 5 位数字代码，如腾讯控股为 '00700'）。")

        try:
            payload_str = raw_text.split('"', 2)[1]
            parts = payload_str.split("~")
        except Exception as exc:
            raise RuntimeError(f"解析腾讯港股行情数据结构失败: {exc}") from exc

        name = parts[1] if len(parts) > 1 and parts[1] else f"港股_{norm_code}"

        def _safe_float(val: Any) -> float:
            try:
                return round(float(val), 3) if val and str(val).strip() else 0.0
            except (ValueError, TypeError):
                return 0.0

        def _safe_opt_float(val: Any) -> float | None:
            try:
                return round(float(val), 2) if val and str(val).strip() else None
            except (ValueError, TypeError):
                return None

        # 动态解析结算货币（默认为港元 HKD，双柜台可能包含 CNY/RMB）
        curr_val = "HKD"
        for item in parts[50:]:
            clean_item = item.strip().upper()
            if clean_item in ("HKD", "CNY", "USD", "RMB"):
                curr_val = clean_item
                break

        result: dict[str, Any] = {
            "symbol": norm_code,
            "name": name,
            "setcode": "31",
            "market": "香港交易所 (HKEX)",
            "source": "tencent_free_hk_quote (腾讯免费港股专线)",
            "盘口核心行情": {
                "股票名称": name,
                "现价": _safe_float(parts[3] if len(parts) > 3 else 0),
                "昨收": _safe_float(parts[4] if len(parts) > 4 else 0),
                "今开": _safe_float(parts[5] if len(parts) > 5 else 0),
                "最高": _safe_float(parts[33] if len(parts) > 33 else 0),
                "最低": _safe_float(parts[34] if len(parts) > 34 else 0),
                "涨跌额": _safe_float(parts[31] if len(parts) > 31 else 0),
                "涨跌幅_pct": _safe_float(parts[32] if len(parts) > 32 else 0),
                "成交量_股": _safe_float(parts[6] if len(parts) > 6 else 0),
                "成交额_港元": _safe_float(parts[37] if len(parts) > 37 else 0),
                "振幅_pct": _safe_float(parts[43] if len(parts) > 43 else 0),
                "行情时间": parts[30] if len(parts) > 30 else "",
                "结算货币": curr_val,
            },
            "估值与基本盘面": {
                "市盈率_TTM": _safe_opt_float(parts[39] if len(parts) > 39 else None),
                "总市值_亿港元": _safe_opt_float(parts[44] if len(parts) > 44 else None),
                "流通市值_亿港元": _safe_opt_float(parts[45] if len(parts) > 45 else None),
                "52周最高": _safe_opt_float(parts[48] if len(parts) > 48 else None),
                "52周最低": _safe_opt_float(parts[49] if len(parts) > 49 else None),
            },
            "五档深度盘口": {
                "买一": {"价格": _safe_float(parts[9] if len(parts) > 9 else 0), "委托量": int(_safe_float(parts[10] if len(parts) > 10 else 0))},
                "买二": {"价格": None, "委托量": None, "说明": "港交所公开免费源仅提供即时买一/卖一撮合盘口"},
                "买三": {"价格": None, "委托量": None},
                "买四": {"价格": None, "委托量": None},
                "买五": {"价格": None, "委托量": None},
                "卖一": {"价格": _safe_float(parts[19] if len(parts) > 19 else 0), "委托量": int(_safe_float(parts[20] if len(parts) > 20 else 0))},
                "卖二": {"价格": None, "委托量": None, "说明": "港交所公开免费源仅提供即时买一/卖一撮合盘口"},
                "卖三": {"价格": None, "委托量": None},
                "卖四": {"价格": None, "委托量": None},
                "卖五": {"价格": None, "委托量": None},
            },
        }
        return result

    # =========================================================================
    # 【功能块：腾讯免费公开港股多周期历史 K 线获取与量化指标计算】
    # - 输入：clean_code (str): 5位港股数字代码（如 "00700"）
    #         period (str): 周期类型 ("day", "week", "month", "60m", "30m", "15m", "5m")
    #         count (int): 获取条数上限 (1-300)
    #         adjust (str): 复权方式 ("qfq", "hfq", "none")
    #         with_indicators (bool): 是否在底层计算量化指标
    # - 功能：调用腾讯财经官方开放 K 线专线 (web.ifzq.gtimg.cn)，拉取历史行情数据，
    #         清洗时间、开高低收成交量，并在纯 Python 算法层原生计算 MA/MACD/KDJ/BOLL/RSI
    # - 输出：dict[str, Any]: 结构化港股 K 线与指标字典
    # =========================================================================
    def _get_hk_kline(
        self,
        clean_code: str,
        period: str = "day",
        count: int = 30,
        adjust: str = "qfq",
        with_indicators: bool = True,
    ) -> dict[str, Any]:
        norm_code = clean_code.zfill(5)
        period_map = {
            "day": "day",
            "week": "week",
            "month": "month",
            "5m": "m5",
            "15m": "m15",
            "30m": "m30",
            "60m": "m60",
        }
        p_str = period_map.get(period.lower(), "day")
        limit_cnt = max(1, min(int(count), 300))
        adj_param = "qfq" if adjust == "qfq" else ("hfq" if adjust == "hfq" else "")
        url = f"https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?_var=kline_{p_str}{adj_param}&param=hk{norm_code},{p_str},,,{limit_cnt},{adj_param}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                raw_text = raw_bytes.decode("utf-8", errors="replace")
        except Exception as exc:
            raise RuntimeError(f"腾讯免费港股历史K线接口请求失败: {exc}") from exc

        if "=" not in raw_text:
            return {
                "symbol": norm_code,
                "name": f"港股_{norm_code}",
                "setcode": "31",
                "period": period,
                "adjust": adjust,
                "total_bars": 0,
                "klines": [],
            }

        try:
            json_str = raw_text.split("=", 1)[1]
            data = json.loads(json_str)
            stock_data = data.get("data", {}).get(f"hk{norm_code}", {})
            name = stock_data.get("qt", {}).get(f"hk{norm_code}", ["", f"港股_{norm_code}"])[1]
            raw_klines = (
                stock_data.get(f"{adj_param}{p_str}")
                or stock_data.get(p_str)
                or []
            )
        except Exception:
            name = f"港股_{norm_code}"
            raw_klines = []

        parsed_bars: list[dict[str, Any]] = []
        for k in raw_klines:
            if not isinstance(k, list) or len(k) < 6:
                continue
            dt_str = str(k[0])
            open_p = float(k[1]) if str(k[1]).replace(".", "").isdigit() else 0.0
            close_p = float(k[2]) if str(k[2]).replace(".", "").isdigit() else 0.0
            high_p = float(k[3]) if str(k[3]).replace(".", "").isdigit() else 0.0
            low_p = float(k[4]) if str(k[4]).replace(".", "").isdigit() else 0.0
            vol = float(k[5]) if str(k[5]).replace(".", "").replace("e", "").replace("+", "").isdigit() else 0.0
            amt = float(k[8]) if len(k) > 8 and str(k[8]).replace(".", "").isdigit() else 0.0

            parsed_bars.append({
                "time": dt_str,
                "open": round(open_p, 3),
                "high": round(high_p, 3),
                "low": round(low_p, 3),
                "close": round(close_p, 3),
                "volume": vol,
                "amount": amt,
            })

        if with_indicators and parsed_bars:
            parsed_bars = calculate_indicators(parsed_bars)

        return {
            "symbol": norm_code,
            "name": name,
            "setcode": "31",
            "period": period,
            "adjust": adjust,
            "total_bars": len(parsed_bars),
            "klines": parsed_bars,
        }

    def get_quotes(self, symbol: str, setcode: str | None = None) -> dict[str, Any]:
        """获取实时盘口、深度量化指标与五档买卖盘（支持 A 股与港股）"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        if not clean_code:
            raise ValueError(f"无效的证券代码 '{symbol}'，证券标的代码必须包含有效数字编号（如 A股'600519'、港股'00700'）。若仅知道公司名称，请先调用 stock_lookup 工具查找对应代码。")

        # 智能判定是否为港股标的 (市场显式指定为 31/116/HK，或原始代码带 HK 前缀，或数字代码为 5 位)
        is_hk = (
            (setcode is not None and str(setcode).upper() in ("31", "116", "HK"))
            or symbol.strip().upper().startswith("HK")
            or len(clean_code) == 5
        )
        if is_hk:
            return self._get_hk_quotes(clean_code)

        m_setcode = setcode if setcode is not None else infer_market_setcode(clean_code)

        req_body = {
            "Head": {"Target": "0", "CharSet": "UTF8"},
            "Code": clean_code,
            "Setcode": str(m_setcode),
            "HasHQInfo": "1",
            "HasExtInfo": "1",
            "BspNum": "5",
            "HasProInfo": "1",
            "HasCalcInfo": "0",
            "HasCwInfo": "0",
            "HasStatInfo": "0",
        }
        url = f"{self.api_endpoint}?Entry=TdxShare.PBHQInfo"
        raw = self._post(url, req_body)

        result: dict[str, Any] = {
            "symbol": clean_code,
            "setcode": m_setcode,
            "source": "tdx_official_专线",
        }

        # 1. 核心行情字段中文化映射
        hq_raw = raw.get("HQInfo", {})
        if isinstance(hq_raw, dict):
            mapped_hq = {HQ_FIELD_MAP.get(k, k): v for k, v in hq_raw.items()}
            # 自动计算涨跌幅与日内真实振幅（遵循交易所标准公式：(最高-最低)/昨收*100，彻底避免通达信Yield字段污染）
            now_p = float(hq_raw.get("Now", 0.0) or 0.0)
            close_p = float(hq_raw.get("Close", 0.0) or 0.0)
            high_p = float(hq_raw.get("MaxP", 0.0) or 0.0)
            low_p = float(hq_raw.get("MinP", 0.0) or 0.0)
            if close_p > 0 and now_p > 0:
                mapped_hq["涨跌幅_pct"] = round(((now_p - close_p) / close_p) * 100.0, 2)
                mapped_hq["涨跌额"] = round(now_p - close_p, 3)
            if close_p > 0 and high_p > 0 and low_p > 0:
                amp_val = round(((high_p - low_p) / close_p) * 100.0, 2)
                mapped_hq["日内振幅_pct"] = amp_val
                mapped_hq["振幅_pct"] = amp_val
            result["盘口核心行情"] = mapped_hq

        # 2. 估值与股本指标中文化映射与年化口径对齐
        ext_raw = raw.get("ExtInfo", {})
        pro_raw = raw.get("ProInfo", {})
        if isinstance(ext_raw, dict):
            mapped_ext = {EXT_FIELD_MAP.get(k, k): v for k, v in ext_raw.items()}
            # 计算市净率 PB、年化每股收益，并附加口径防幻觉说明
            now_p = float(hq_raw.get("Now", 0.0) or 0.0) if isinstance(hq_raw, dict) else 0.0
            mgjzc = float(ext_raw.get("MGJZC", 0.0) or 0.0)
            syl = float(ext_raw.get("SYL", 0.0) or 0.0)
            mgsy = float(ext_raw.get("MGSY", 0.0) or 0.0)
            if mgjzc > 0 and now_p > 0:
                mapped_ext["市净率_PB"] = round(now_p / mgjzc, 2)
            if syl > 0 and now_p > 0:
                mapped_ext["动态市盈率_PE"] = round(syl, 2)
                annualized_eps = round(now_p / syl, 3)
                mapped_ext["年化每股收益_EPS"] = annualized_eps

            # 从 ProInfo 中提取真正滚动过去 4 季度的 TTM 滚动市盈率与静态市盈率
            if isinstance(pro_raw, dict):
                pe_ttm = pro_raw.get("PETTM")
                pe_static = pro_raw.get("PEStatic")
                if pe_ttm is not None and str(pe_ttm).strip() != "":
                    try:
                        mapped_ext["滚动市盈率_PETTM"] = round(float(pe_ttm), 2)
                    except (ValueError, TypeError):
                        pass
                if pe_static is not None and str(pe_static).strip() != "":
                    try:
                        mapped_ext["静态市盈率_PEStatic"] = round(float(pe_static), 2)
                    except (ValueError, TypeError):
                        pass

            ttm_note = f"滚动市盈率_PETTM为 {mapped_ext.get('滚动市盈率_PETTM')} 倍(按过去4季度滚动净利润测算的真实TTM口径)；" if "滚动市盈率_PETTM" in mapped_ext else ""
            mapped_ext["估值口径说明"] = (
                f"{ttm_note}动态市盈率_PE({mapped_ext.get('动态市盈率_PE')}倍)采用最新报告期年化折算口径(年化每股收益≈{mapped_ext.get('年化每股收益_EPS')}元，现价/年化EPS=动态市盈率)；"
                f"报告期每股收益_EPS({mgsy}元)为非年化单期累计值，两者口径不同，禁止直接互乘推导价格或市值。"
            )
            result["估值与基本盘面"] = mapped_ext

        # 3. 五档深度买卖盘口整理（通达信官方 BspInfo 返回 5 项明细数组）
        bsp_raw = raw.get("BspInfo", [])
        if isinstance(bsp_raw, list) and bsp_raw:
            bsp_dict = {}
            buy_names = ["买一", "买二", "买三", "买四", "买五"]
            sell_names = ["卖一", "卖二", "卖三", "卖四", "卖五"]
            for idx, item in enumerate(bsp_raw[:5]):
                if isinstance(item, dict):
                    b_label = buy_names[idx] if idx < len(buy_names) else f"买{idx+1}"
                    s_label = sell_names[idx] if idx < len(sell_names) else f"卖{idx+1}"
                    b_price = item.get("BuyP")
                    s_price = item.get("SellP")
                    b_vol = item.get("BuyV")
                    s_vol = item.get("SellV")
                    bsp_dict[b_label] = {
                        "价格": float(b_price) if b_price is not None and str(b_price).strip() else None,
                        "委托量_手": int(b_vol) if (b_vol is not None and str(b_vol).isdigit()) else b_vol,
                    }
                    bsp_dict[s_label] = {
                        "价格": float(s_price) if s_price is not None and str(s_price).strip() else None,
                        "委托量_手": int(s_vol) if (s_vol is not None and str(s_vol).isdigit()) else s_vol,
                    }
            if bsp_dict:
                result["五档深度盘口"] = bsp_dict
                # 检查是否处于盘后休市时段（交易所清空连续竞价撮合委托，盘口仅剩一档收盘撮合价）
                valid_tiers = sum(1 for v in bsp_dict.values() if isinstance(v, dict) and v.get("价格") is not None)
                if valid_tiers <= 1:
                    result["盘口深度说明"] = (
                        "当前处于休市/盘后时段，交易所撮合系统已清空连续竞价订单簿，仅保留收盘撮合价；"
                        "完整五档买卖深度将在交易日 9:15-11:30、13:00-15:00 连续竞价期间动态全量呈现。"
                    )
        elif isinstance(bsp_raw, dict) and bsp_raw:
            bsp_dict = {
                "买一": {"价格": bsp_raw.get("BuyPrice1") or bsp_raw.get("BuyP1"), "委托量_手": bsp_raw.get("BuyVol1") or bsp_raw.get("BuyV1")},
                "买二": {"价格": bsp_raw.get("BuyPrice2") or bsp_raw.get("BuyP2"), "委托量_手": bsp_raw.get("BuyVol2") or bsp_raw.get("BuyV2")},
                "买三": {"价格": bsp_raw.get("BuyPrice3") or bsp_raw.get("BuyP3"), "委托量_手": bsp_raw.get("BuyVol3") or bsp_raw.get("BuyV3")},
                "买四": {"价格": bsp_raw.get("BuyPrice4") or bsp_raw.get("BuyP4"), "委托量_手": bsp_raw.get("BuyVol4") or bsp_raw.get("BuyV4")},
                "买五": {"价格": bsp_raw.get("BuyPrice5") or bsp_raw.get("BuyP5"), "委托量_手": bsp_raw.get("BuyVol5") or bsp_raw.get("BuyV5")},
                "卖一": {"价格": bsp_raw.get("SellPrice1") or bsp_raw.get("SellP1"), "委托量_手": bsp_raw.get("SellVol1") or bsp_raw.get("SellV1")},
                "卖二": {"价格": bsp_raw.get("SellPrice2") or bsp_raw.get("SellP2"), "委托量_手": bsp_raw.get("SellVol2") or bsp_raw.get("SellV2")},
                "卖三": {"价格": bsp_raw.get("SellPrice3") or bsp_raw.get("SellP3"), "委托量_手": bsp_raw.get("SellVol3") or bsp_raw.get("SellV3")},
                "卖四": {"价格": bsp_raw.get("SellPrice4") or bsp_raw.get("SellP4"), "委托量_手": bsp_raw.get("SellVol4") or bsp_raw.get("SellV4")},
                "卖五": {"价格": bsp_raw.get("SellPrice5") or bsp_raw.get("SellP5"), "委托量_手": bsp_raw.get("SellVol5") or bsp_raw.get("SellV5")},
            }
            result["五档深度盘口"] = bsp_dict

        return result

    def get_kline(
        self,
        symbol: str,
        period: str = "day",
        count: int = 30,
        adjust: str = "qfq",
        setcode: str | None = None,
        with_indicators: bool = True,
    ) -> dict[str, Any]:
        """获取多周期历史 K 线走势，并可选附加 MACD, KDJ, BOLL 等量化指标计算结果（支持 A 股与港股）"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        if not clean_code:
            raise ValueError(f"无效的证券代码 '{symbol}'，证券标的代码必须包含有效数字编号（如 A股'600519'、港股'00700'）。若仅知道公司名称，请先调用 stock_lookup 工具查找对应代码。")

        is_hk = (
            (setcode is not None and str(setcode).upper() in ("31", "116", "HK"))
            or symbol.strip().upper().startswith("HK")
            or len(clean_code) == 5
        )
        if is_hk:
            return self._get_hk_kline(
                clean_code,
                period=period,
                count=count,
                adjust=adjust,
                with_indicators=with_indicators,
            )

        m_setcode = setcode if setcode is not None else infer_market_setcode(clean_code)

        # 周期映射: day->4, week->5, month->6, 60m->3, 30m->2, 15m->1, 5m->0, 1m->7
        period_map = {"5m": 0, "15m": 1, "30m": 2, "60m": 3, "day": 4, "week": 5, "month": 6, "1m": 7}
        p_val = period_map.get(period.lower(), 4)

        # 复权映射: qfq->11, hfq->12, none->0
        adjust_map = {"qfq": 11, "hfq": 12, "none": 0}
        tq_val = adjust_map.get(adjust.lower(), 11)

        req_body = {
            "Head": {"Target": 0, "CharSet": "UTF8"},
            "Code": clean_code,
            "Setcode": int(m_setcode),
            "Period": p_val,
            "Startxh": 0,
            "WantNum": max(1, min(count, 300)),
            "TQFlag": tq_val,
            "MPData": 0,
            "HasAttachInfo": 1,
            "HasLtgb": 0,
            "ForRefresh": 0,
            "HasIpoPrice": 0,
        }

        url = f"{self.api_endpoint}?Entry=TdxShare.PBFXT"
        raw = self._post(url, req_body)

        list_items = raw.get("ListItem", [])
        attach_info = raw.get("AttachInfo", {})
        stock_name = attach_info.get("Name", "") if isinstance(attach_info, dict) else ""
        parsed_bars: list[dict[str, Any]] = []

        for item in list_items:
            # 兼容 {'Item': [...]} 或直接 [...] 两种上游返回格式
            raw_row = item.get("Item") if isinstance(item, dict) else item
            if not isinstance(raw_row, list) or len(raw_row) < 6:
                continue
            # 通达信 ListItem Item 结构: [日期, 时间, 今开, 最高, 最低, 收盘, 成交额, 成交额, 成交量...]
            dt_str = str(raw_row[0])
            open_p = float(raw_row[2])
            high_p = float(raw_row[3])
            low_p = float(raw_row[4])
            close_p = float(raw_row[5])
            amt_val = float(raw_row[6]) if len(raw_row) > 6 else 0.0
            vol_val = float(raw_row[8]) if len(raw_row) > 8 else (float(raw_row[7]) if len(raw_row) > 7 else 0.0)

            parsed_bars.append({
                "time": dt_str,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": vol_val,
                "amount": amt_val,
            })

        # 按时间正序排列后计算量化指标
        if with_indicators and parsed_bars:
            parsed_bars = calculate_indicators(parsed_bars)

        return {
            "symbol": clean_code,
            "name": stock_name,
            "setcode": m_setcode,
            "period": period,
            "adjust": adjust,
            "total_bars": len(parsed_bars),
            "klines": parsed_bars,
        }

    def get_financial_statements(
        self,
        symbol: str,
        report_type: str = "balance",
        period_type: str = "report",
        limit_periods: int = 4,
    ) -> dict[str, Any]:
        """获取三大财务报表 (资产负债表 balance, 利润表 income, 现金流量表 cashflow) 与经营构成"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        rtype = report_type.lower()
        fixed_tag = "00102" if period_type.lower() == "quarter" else "00101"

        if rtype in ("balance", "balance_sheet", "zcfzb"):
            entry = "TdxShareCW.ph_agf10_cw_zcfzb"
            params = [clean_code]
            field_map = BALANCE_FIELDS_NON_FINANCE
            type_name = "资产负债表"
        elif rtype in ("income", "income_statement", "lyb"):
            entry = "TdxShareCW.ph_agf10_cw_lyb"
            params = [fixed_tag, clean_code]
            field_map = INCOME_FIELDS_NON_FINANCE
            type_name = f"利润表({ '单季度' if period_type == 'quarter' else '报告期' })"
        elif rtype in ("cashflow", "cashflow_statement", "xjllb"):
            entry = "TdxShareCW.ph_agf10_cw_xjllb"
            params = [fixed_tag, clean_code]
            field_map = CASHFLOW_FIELDS_NON_FINANCE
            type_name = f"现金流量表({ '单季度' if period_type == 'quarter' else '报告期' })"
        else:
            raise ValueError(f"Unsupported report_type: {report_type}. Available: balance, income, cashflow")

        url = f"{self.api_endpoint}?Entry={entry}"
        raw = self._post(url, {"Params": params})

        result_periods: list[dict[str, Any]] = []
        result_sets = raw.get("ResultSets", [])
        if isinstance(result_sets, list):
            for rs in result_sets:
                col_names = rs.get("ColName", [])
                contents = rs.get("Content", [])
                if not col_names or not contents or col_names == ["isjr"]:
                    continue
                # 遍历历史财报期
                for row in contents[:limit_periods]:
                    period_dict: dict[str, Any] = {}
                    for col_code, val in zip(col_names, row):
                        if not str(val).strip():
                            continue
                        clean_col_name = field_map.get(col_code, col_code)
                        # 数值型字段友好转换
                        try:
                            if "." in str(val):
                                period_dict[clean_col_name] = round(float(val), 2)
                            else:
                                period_dict[clean_col_name] = int(val) if str(val).isdigit() else str(val)
                        except (ValueError, TypeError):
                            period_dict[clean_col_name] = str(val)
                    if period_dict:
                        result_periods.append(period_dict)
                break

        return {
            "symbol": clean_code,
            "statement_type": type_name,
            "period_count": len(result_periods),
            "statements": result_periods,
            "会计口径与合并说明": (
                "本表会计科目按工商业企业准则标准映射对齐。若查询标的属于大型集团且控股持牌金融机构（如贵州茅台控股贵州茅台集团财务有限公司、"
                "大型央国企控股财务公司），其集团合并报表中包含拆出资金、吸收存款及同业存放、金融借贷等金融专属科目；"
                "工商业明细科目加总若与流入/流出小计或流动资产合计产生残差，该差额属于集团财务公司金融合并口径，非数据错误。"
            ),
        }

    def get_f10(self, symbol: str, category: str = "concepts") -> dict[str, Any]:
        """获取 F10 深度基本面（热点题材板块族谱、机构盈利预测、股东与主力持仓、龙虎榜异动等）"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        cat = category.lower()

        # 通达信 PCCW 接口严格参数规范映射表：
        # - concepts (热点题材): [code, "zttzbkz"]
        # - events (事件驱动): [code, "sjcd"]
        # - forecast (机构盈利预测): [code, "yzyq"]
        # - shareholders (股东人数筹码): [code, "gdrs", "", "", "1", "1", "10"]
        # - top_holders (十大流通股东): [code, "ltgd", "", "", "1", "1", "10"]
        # - margin_trading (融资融券): [code, "rzrq", ""]
        # - capital_flow (主力资金流向): [code, "zjlx", ""]
        # - limit_up (涨停分析异动): [code, "ztfx", ""]
        # - unlocks (限售解禁日程): [code, "xslt"]
        # - summary (公司概要提示): [code, "gsgy", ""]
        # - basic_info (公司基本工商): ["0", clean_code, ""]
        cat_configs: dict[str, tuple[str, list[str], str]] = {
            "concepts": ("TdxSharePCCW.tdxf10_gg_rdtc", [clean_code, "zttzbkz"], "热点题材板块族谱"),
            "events": ("TdxSharePCCW.tdxf10_gg_rdtc", [clean_code, "sjcd"], "事件驱动催化"),
            "forecast": ("TdxSharePCCW.tdxf10_gg_ybpj", [clean_code, "yzyq"], "机构盈利预测"),
            "shareholders": ("TdxSharePCCW.tdxf10_gg_gdyj", [clean_code, "gdrs", "", "", "1", "1", "10"], "股东人数与筹码集中度"),
            "top_holders": ("TdxSharePCCW.tdxf10_gg_gdyj", [clean_code, "ltgd", "", "", "1", "1", "10"], "十大流通股东"),
            "margin_trading": ("TdxSharePCCW.tdxf10_gg_jyds", [clean_code, "rzrq", ""], "融资融券交易数据"),
            "capital_flow": ("TdxSharePCCW.tdxf10_gg_jyds", [clean_code, "zjlx", ""], "主力资金流向"),
            "limit_up": ("TdxSharePCCW.tdxf10_gg_jyds", [clean_code, "ztfx", ""], "涨停分析与异动"),
            "unlocks": ("TdxSharePCCW.tdxf10_gg_gbjg", [clean_code, "xslt"], "限售解禁日程表"),
            "summary": ("TdxSharePCCW.tdxf10_gg_zxts", [clean_code, "gsgy", ""], "公司概要与最新提示"),
            "basic_info": ("TdxSharePCCW.tdxf10_gg_gsgk", ["0", clean_code, ""], "公司基本工商与发行信息"),
        }

        if cat not in cat_configs:
            available = ", ".join(cat_configs.keys())
            raise ValueError(f"Unknown F10 category: {category}. Available: {available}")

        entry, params, desc = cat_configs[cat]
        url = f"{self.api_endpoint}?Entry={entry}"
        raw = self._post(url, {"Params": params})

        return {
            "symbol": clean_code,
            "category": cat,
            "category_name": desc,
            "data": raw,
        }

    def screener(self, message: str, limit: int = 10, page: int = 1, filter_st: bool = True) -> dict[str, Any]:
        """通达信自然语言智能选股 (输入中文自然语言条件，如'主板 绩优 储能 连板')"""
        # 如果开启过滤 ST，向底层多请求适量候选以补齐过滤损耗
        fetch_size = max(1, min(limit * 2 if filter_st else limit, 50))
        req_body = [{
            "message": message.strip(),
            "rang": "AG",
            "pageNo": str(page),
            "pageSize": str(fetch_size),
        }]
        url = f"{self.api_endpoint}?Entry=JNLPSE:wendaQuery"
        raw = self._post(url, req_body)

        results: list[dict[str, Any]] = []
        headers: list[str] = []
        if isinstance(raw, list) and len(raw) >= 3:
            # 通达信 wendaQuery 原生二维数组结构说明：
            # - Row 0: 查询元信息 [0, '', 31, session_id, count]
            # - Row 1: 列标题表头 ['POS', 'market', 'sec_code', 'sec_name', 'now_price', 'chg', ...]
            # - Row 2: 数据类型行 ['', '', '', '', '2|0|0', ...]
            # - Row 3 起: 真实股票数据行 [序号, 市场代码, 股票代码, 股票名称, 现价, 涨跌幅, 业务指标...]
            raw_headers = raw[1] if isinstance(raw[1], list) else []
            headers = [str(h).strip() for h in raw_headers]

            data_rows = raw[3:] if len(raw) > 3 else []
            for item in data_rows:
                if not isinstance(item, list) or len(item) < 4:
                    continue
                # 剔除表头行残留或空行
                if str(item[0]).strip().upper() in ("POS", ""):
                    continue

                market_val = str(item[1]).strip()
                sec_code = str(item[2]).strip()
                sec_name = str(item[3]).strip()

                # 必须符合 6 位标准证券代码
                if not re.match(r"^\d{6}$", sec_code):
                    continue

                is_st = ("ST" in sec_name.upper()) or ("退" in sec_name)
                if filter_st and is_st:
                    continue

                now_price = None
                try:
                    now_price = round(float(item[4]), 2) if len(item) > 4 and str(item[4]).strip() != "" else None
                except (ValueError, TypeError):
                    pass

                chg_pct = None
                try:
                    chg_pct = round(float(item[5]), 2) if len(item) > 5 and str(item[5]).strip() != "" else None
                except (ValueError, TypeError):
                    pass

                # 动态提取后续业务指标列（如 ROE、流通市值、概念匹配等）
                dynamic_indicators: dict[str, Any] = {}
                for col_idx in range(6, len(item)):
                    col_name = headers[col_idx] if col_idx < len(headers) else f"指标_{col_idx}"
                    clean_col_name = re.sub(r"[0-9#|]+$", "", col_name).strip()
                    val_str = str(item[col_idx]).strip()
                    try:
                        dynamic_indicators[clean_col_name] = round(float(val_str), 4) if "." in val_str else (int(val_str) if val_str.isdigit() else val_str)
                    except Exception:
                        dynamic_indicators[clean_col_name] = val_str

                market_name = "深市" if market_val == "0" else ("沪市" if market_val == "1" else ("北交所" if market_val == "2" else market_val))

                results.append({
                    "code": sec_code,
                    "name": sec_name,
                    "market": market_name,
                    "price": now_price,
                    "change_pct": chg_pct,
                    "is_st": is_st,
                    "indicators": dynamic_indicators,
                })

                if len(results) >= limit:
                    break

        return {
            "query": message,
            "filter_st": filter_st,
            "matched_count": len(results),
            "stocks": results,
            "raw_summary": raw[0] if (isinstance(raw, list) and raw) else None,
        }


# =========================================================================
# 【功能块 7：东方财富/公开财经 REST 接口备用兜底客户端 (Zero Dependency)】
# - 输入：标的代码或检索词
# - 功能：当通达信专线未配置 Token、遇到网络抖动或需要补充个股突发新闻时，
#         直接使用标准库发起公开 REST 查询，零体积零额外依赖进行高可用兜底
# - 输出：PublicFallbackClient 实例
# =========================================================================
class PublicFallbackClient:
    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    # =========================================================================
    # 【功能块：个股最新新闻资讯与重大公告双轨获取引擎】
    # - 输入：symbol (str): 证券代码；limit (int): 返回条目上限 (1-10)
    # - 功能：新浪财经为主力通道（低频控、原生标准JSON、高可用），东方财富为备用通道
    # - 输出：list[dict[str, Any]]: 结构化新闻列表（标题、时间、媒体、链接、正文摘要）
    # =========================================================================
    def get_news(self, symbol: str, limit: int = 5) -> list[dict[str, Any]]:
        raw_query = symbol.strip()
        clean_code = re.sub(r"[^\d]", "", raw_query)
        # 智能确定检索词：若有 6 位代码优先用代码，若传入中文名称（如'贵州茅台'）则直接使用名称
        search_term = clean_code if clean_code else raw_query
        req_limit = max(1, min(limit, 10))

        # 1. 主力通道：新浪财经新闻搜索接口（风控宽松、响应极速、原生标准 JSON）
        try:
            encoded_q = urllib.parse.quote(search_term)
            sina_url = f"https://search.sina.com.cn/api/news?q={encoded_q}&size={req_limit}&page=1"
            req = Request(
                sina_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
                    "Referer": "https://search.sina.com.cn/",
                },
                method="GET",
            )
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                items = data.get("data", {}).get("list", [])
                if items and isinstance(items, list):
                    news_list: list[dict[str, Any]] = []
                    for item in items[:req_limit]:
                        title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                        snippet = re.sub(r"<[^>]+>", "", item.get("intro", "")).strip()
                        media_name = item.get("media", "").strip() or "新浪财经"
                        news_list.append({
                            "title": title,
                            "source": media_name,
                            "time": item.get("dataTime", ""),
                            "url": item.get("url", ""),
                            "snippet": snippet[:150] + "..." if len(snippet) > 150 else snippet,
                        })
                    if news_list:
                        return news_list
        except Exception:
            pass

        # 2. 备用通道一：东方财富全文检索 JSONP 接口
        try:
            em_params = {
                "cb": "jQuery_news",
                "param": json.dumps({
                    "code": clean_code if clean_code else search_term,
                    "type": ["cmsArticleWebOld"],
                    "pageindex": 1,
                    "pagesize": req_limit,
                    "keyword": search_term,
                }, ensure_ascii=False),
            }
            em_url = "https://search-api-web.eastmoney.com/search/jsonp?" + urllib.parse.urlencode(em_params)
            req = Request(
                em_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                method="GET",
            )
            with urlopen(req, timeout=self.timeout) as resp:
                raw_text = resp.read().decode("utf-8", errors="replace")
                match = re.search(r"jQuery_news\((.*)\)", raw_text, re.DOTALL)
                if match:
                    data = json.loads(match.group(1))
                    articles = data.get("result", {}).get("cmsArticleWebOld", [])
                    news_list = []
                    for item in articles[:req_limit]:
                        title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                        snippet = re.sub(r"<[^>]+>", "", item.get("content", "")).strip()
                        media_name = item.get("mediaName", "").strip() or "东方财富"
                        news_list.append({
                            "title": title,
                            "source": media_name,
                            "time": item.get("date", ""),
                            "url": item.get("url", ""),
                            "snippet": snippet[:150] + "..." if len(snippet) > 150 else snippet,
                        })
                    if news_list:
                        return news_list
        except Exception:
            pass

        # 3. 备用通道二：东方财富官方权威上市公司重大公告专线（重大公告兜底，100% 有数据）
        if clean_code:
            try:
                ann_url = f"https://np-anotice-stock.eastmoney.com/api/security/ann?page_size={req_limit}&page_index=1&ann_type=A&client_source=web&stock_list={clean_code}"
                req = Request(
                    ann_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                    method="GET",
                )
                with urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    ann_items = data.get("data", {}).get("list", [])
                    if ann_items and isinstance(ann_items, list):
                        ann_list = []
                        for item in ann_items[:req_limit]:
                            ann_title = (item.get("title_ch") or item.get("title") or "").strip()
                            art_code = item.get("art_code", "")
                            ann_url_link = f"https://data.eastmoney.com/notices/detail/{clean_code}/{art_code}.html" if art_code else ""
                            ann_list.append({
                                "title": ann_title,
                                "source": "上市公司官方公告",
                                "time": (item.get("notice_date") or "")[:19],
                                "url": ann_url_link,
                                "snippet": f"【上市公司法定公告】{ann_title}",
                            })
                        if ann_list:
                            return ann_list
            except Exception:
                pass

        return []

    # =========================================================================
    # 【功能块：公开数据中心 F10 深度资料自动高可用降级引擎】
    # - 输入：symbol (str): 证券代码；category (str): 分类 (shareholders, top_holders, concepts, summary, unlocks)
    # - 功能：当通达信官方专线 PCCW 库未开放或返回错误码时，自动从公开数据中心获取结构化基本面
    # - 输出：dict[str, Any] | list[Any] | None: 结构化 F10 数据字典或列表
    # =========================================================================
    def get_f10(self, symbol: str, category: str = "concepts") -> dict[str, Any] | list[Any] | None:
        clean_code = re.sub(r"[^\d]", "", symbol)
        cat = category.lower()

        # 根据代码前缀推导市场后缀 (.SH / .SZ / .BJ) 与前缀 (SH / SZ / BJ)
        if clean_code.startswith(("60", "68", "90")):
            secucode_dot = f"{clean_code}.SH"
            secucode_pre = f"SH{clean_code}"
        elif clean_code.startswith(("00", "30", "20")):
            secucode_dot = f"{clean_code}.SZ"
            secucode_pre = f"SZ{clean_code}"
        elif clean_code.startswith(("8", "4", "92")):
            secucode_dot = f"{clean_code}.BJ"
            secucode_pre = f"BJ{clean_code}"
        else:
            secucode_dot = f"{clean_code}.SH"
            secucode_pre = f"SH{clean_code}"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            "Referer": "https://emweb.securities.eastmoney.com/",
        }

        try:
            # 1. 股东人数与筹码集中度
            if cat == "shareholders":
                url = f"https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_HOLDERNUMLATEST&columns=ALL&filter=(SECUCODE=%22{secucode_dot}%22)"
                req = Request(url, headers=headers)
                with urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    items = data.get("result", {}).get("data", [])
                    if items:
                        item = items[0]
                        return {
                            "最新股东户数": item.get("HOLDER_NUM"),
                            "较上期增减户数": item.get("HOLDER_NUM_CHANGE"),
                            "户数变动比例_pct": item.get("HOLDER_NUM_RATIO"),
                            "户均持股市值_元": item.get("AVG_MARKET_CAP"),
                            "户均持股数量": item.get("AVG_HOLD_NUM"),
                            "统计截止日期": (item.get("END_DATE") or "")[:10],
                            "公告日期": (item.get("HOLD_NOTICE_DATE") or "")[:10],
                        }

            # 2. 十大流通股东
            elif cat == "top_holders":
                url = f"https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_EH_FREEHOLDERS&columns=ALL&filter=(SECUCODE=%22{secucode_dot}%22)&pageSize=10"
                req = Request(url, headers=headers)
                with urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    items = data.get("result", {}).get("data", [])
                    if items:
                        holders = []
                        for h in items[:10]:
                            holders.append({
                                "名次": h.get("HOLDER_RANK"),
                                "股东名称": h.get("HOLDER_NAME"),
                                "持股数": h.get("HOLD_NUM"),
                                "占流通盘比例_pct": h.get("FREE_HOLDNUM_RATIO"),
                                "持股变动": h.get("HOLD_CHANGE"),
                            })
                        return {"前十大流通股东": holders, "报告期": (items[0].get("END_DATE") or "")[:10]}

            # 3. 热点题材与板块族谱
            elif cat == "concepts":
                url = f"http://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?code={secucode_pre}"
                req = Request(url, headers=headers)
                with urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                    boards = [x.get("BOARD_NAME") for x in data.get("ssbk", []) if x.get("BOARD_NAME")]
                    topics = [
                        {"主题": x.get("KEYWORD"), "核心要点": x.get("MAINPOINT_CONTENT")}
                        for x in data.get("hxtc", [])
                        if x.get("KEYWORD")
                    ]
                    return {"所属板块": boards, "核心题材": topics[:8]}

            # 4. 公司概要与基本面信息
            elif cat == "summary":
                url = f"http://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={secucode_pre}"
                req = Request(url, headers=headers)
                with urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                    jbzl = data.get("jbzl", [{}])[0]
                    return {
                        "公司全称": jbzl.get("ORG_NAME"),
                        "所属行业": jbzl.get("INDUSTRYCSRC1"),
                        "法定代表人": jbzl.get("LEGAL_PERSON"),
                        "董事长": jbzl.get("CHAIRMAN"),
                        "注册资本_万元": jbzl.get("REG_CAPITAL"),
                        "员工总数": jbzl.get("EMP_NUM"),
                        "主营业务": jbzl.get("MAIN_BUSINESS"),
                        "经营范围": jbzl.get("BUSINESS_SCOPE"),
                        "公司简介": (jbzl.get("ORG_PROFILE") or "").strip()[:300],
                    }

            # 5. 限售解禁日程
            elif cat == "unlocks":
                url = f"https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LIFT_STAGE&columns=ALL&filter=(SECURITY_CODE=%22{clean_code}%22)&pageNumber=1&pageSize=5"
                req = Request(url, headers=headers)
                with urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    items = data.get("result", {}).get("data", []) if data.get("result") else []
                    return [
                        {
                            "解禁日期": (x.get("FREE_DATE") or "")[:10],
                            "解禁股数_万股": x.get("FREE_SHARES"),
                            "解禁类型": x.get("LIMITED_TYPE_NAME"),
                        }
                        for x in items
                    ]
        except Exception:
            return None

        return None


# =========================================================================
# 【功能块 8：金融高可用调度网关核心类 (FinanceGateway)】
# - 输入：配置参数、日志记录器
# - 功能：整合通达信官方专线与公开备用数据源，负责参数校验、多源熔断切换、
#         Token 掩码脱敏与大模型友好响应封装
# - 输出：FinanceGateway 实例
# =========================================================================
class FinanceGateway:
    def __init__(
        self,
        tdx_token: str = "",
        api_endpoint: str = "http://tdxhub.icfqs.com:7615/TQLEX",
        rag_endpoint: str = "https://ai.icfqs.com:8965/v1/rag-entity-retrieve",
        timeout: int = 15,
        logger: logging.Logger | None = None,
    ):
        self.tdx_client = TdxClient(token=tdx_token, api_endpoint=api_endpoint, rag_endpoint=rag_endpoint, timeout=timeout, logger=logger)
        self.fallback_client = PublicFallbackClient(timeout=timeout)
        self.logger = logger or logging.getLogger("mcp_gateway.finance")

    def stock_lookup(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 1：实体代码/名称 AI RAG 语义检索"""
        query = arguments.get("query", "").strip()
        market_range = arguments.get("range", "AG")
        if not query:
            raise ValueError("query 不能为空，请输入股票简称或代码（如'贵州茅台'、'腾讯'）")
        entities = self.tdx_client.lookup_stock(query=query, market_range=market_range)
        return {"query": query, "range": market_range, "entities": entities}

    def stock_quotes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 2：实时盘口与完整量化行情"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空，请输入证券代码（如'600519'）")
        setcode = arguments.get("setcode")
        return self.tdx_client.get_quotes(symbol=symbol, setcode=setcode)

    def stock_kline(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 3：多周期历史 K 线走势 + 内置量化指标计算 (MACD/KDJ/BOLL/MA)"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空")
        period = arguments.get("period", "day")
        count = int(arguments.get("count", 30))
        adjust = arguments.get("adjust", "qfq")
        setcode = arguments.get("setcode")
        with_indicators = bool(arguments.get("with_indicators", True))
        return self.tdx_client.get_kline(
            symbol=symbol,
            period=period,
            count=count,
            adjust=adjust,
            setcode=setcode,
            with_indicators=with_indicators,
        )

    def stock_financials(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 4：三大财务报表 (资产负债表, 利润表, 现金流量表) 与经营主营分析"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空")
        report_type = arguments.get("report_type", "balance")
        period_type = arguments.get("period_type", "report")
        limit_periods = int(arguments.get("limit_periods", 4))
        return self.tdx_client.get_financial_statements(
            symbol=symbol,
            report_type=report_type,
            period_type=period_type,
            limit_periods=limit_periods,
        )

    def stock_f10(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 5：F10 深度基本面（热点题材板块族谱、业绩预告、股东变动、龙虎榜、限售解禁）"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空")
        category = arguments.get("category", "concepts")

        # 1. 首先尝试通达信官方专线获取（优先官方授权渠道）
        try:
            tdx_res = self.tdx_client.get_f10(symbol=symbol, category=category)
            raw_data = tdx_res.get("data", {})
            need_fallback = False
            # 检查通达信服务端是否返回了 -1005 等数据库执行错误，或空的占位结构 [[0]]
            if not raw_data or (isinstance(raw_data, dict) and raw_data.get("ErrorCode", 0) != 0):
                need_fallback = True
            elif isinstance(raw_data, dict) and "ResultSets" in raw_data:
                rs = raw_data.get("ResultSets", [])
                if not rs:
                    need_fallback = True
                elif len(rs) == 1:
                    first_table = rs[0]
                    if first_table.get("ColName") == ["0"] or not first_table.get("Content") or first_table.get("Content") == [[0]]:
                        need_fallback = True
            if not need_fallback:
                tdx_res["source"] = "tdx_official_vip (通达信官方授权专线)"
                # 将 ResultSets 中的各表结构化转换为行对象字典列表，便于大模型与前端直观阅读
                formatted_tables = {}
                for idx, table in enumerate(raw_data.get("ResultSets", [])):
                    t_key = table.get("ResultSetKey", f"table{idx}")
                    cols = table.get("ColName", [])
                    rows = table.get("Content", [])
                    table_rows = []
                    for row in rows:
                        row_dict = {}
                        for c_idx, val in enumerate(row):
                            col_raw = cols[c_idx] if c_idx < len(cols) else f"col_{c_idx}"
                            if category == "margin_trading":
                                col_title = MARGIN_TRADING_FIELDS.get(col_raw, col_raw)
                            elif category == "capital_flow":
                                col_title = CAPITAL_FLOW_FIELDS.get(col_raw, col_raw)
                            elif category == "events":
                                col_title = EVENTS_FIELDS.get(col_raw, col_raw)
                            else:
                                col_title = col_raw
                            row_dict[col_title] = val
                        table_rows.append(row_dict)
                    if category == "events" and table_rows:
                        table_rows = list(reversed(table_rows))
                    formatted_tables[t_key] = table_rows
                tdx_res["formatted_tables"] = formatted_tables
                if category == "events":
                    tdx_res["事件库属性说明"] = (
                        "本表为通达信收录的历史重大行业题材与宏观里程碑事件库（已按发生日期倒序排列，非实时个股公告）；"
                        "如需获取上市公司最新分钟级法定披露公告与突发新闻，请调用 stock_news 工具。"
                    )
                # 增强 F10 摘要表：汉化关键财务与估值指标、依据“利润变动方向”精确校准亏损标的同比含义、注入隔日风格标签与破净动态复核指引
                if category == "summary" or ("table3" in formatted_tables and "table4" in formatted_tables):
                    self._enrich_f10_summary_tables(formatted_tables, tdx_res)
                return tdx_res
        except Exception:
            pass

        # 2. 通达信未开放或异常时，自动平滑降级至公开数据中心高可用 F10
        fallback_data = self.fallback_client.get_f10(symbol=symbol, category=category)
        clean_code = re.sub(r"[^\d]", "", symbol)
        category_names = {
            "concepts": "热点题材板块族谱",
            "events": "事件驱动催化",
            "forecast": "机构盈利预测",
            "shareholders": "股东人数与筹码集中度",
            "top_holders": "十大流通股东",
            "margin_trading": "融资融券交易数据",
            "capital_flow": "主力资金流向",
            "limit_up": "涨停分析与异动",
            "summary": "公司概要与最新提示",
            "unlocks": "限售解禁日程表",
            "basic_info": "公司基本工商与发行信息",
        }
        return {
            "symbol": clean_code,
            "category": category,
            "category_name": category_names.get(category, category),
            "source": "public_f10_fallback",
            "data": fallback_data if fallback_data is not None else {"status": "暂无数据"},
        }

    # =========================================================================
    # 【功能块 8.5：F10 摘要表业务增强与防幻觉校准处理器】
    # - 输入：formatted_tables (各表行对象字典列表), tdx_res (F10 返回字典)
    # - 功能：
    #   1. 解决隔日标签盘中失效问题：提取清算基准日 (gxrq)、每股净资产 (mgjzc) 与破净股等风格标签，
    #      注入隔日时效警示与盘中动态复核指引，引导大模型按 [现价/BPS] 重算 PB，杜绝颠倒结论。
    #   2. 解决亏损标的同比记号歧义问题：针对当期净利润为负的标的，依据“利润变动方向”法则
    #      将同比负号精准判定为【亏损扩大 XX%】，同比正号判定为【亏损收窄/减亏 XX%】，
    #      彻底杜绝大模型将两负数相除误读为“盈利增长”。
    # - 输出：原地更新 formatted_tables 与 tdx_res，注入汉化字段与防幻觉业务提示
    # =========================================================================
    def _enrich_f10_summary_tables(
        self, formatted_tables: dict[str, list[dict[str, Any]]], tdx_res: dict[str, Any]
    ) -> None:
        gxrq = ""
        if "table8" in formatted_tables and formatted_tables["table8"]:
            gxrq = str(formatted_tables["table8"][0].get("gxrq", "") or "")

        mgjzc = None
        if "table4" in formatted_tables and formatted_tables["table4"]:
            mgjzc = formatted_tables["table4"][0].get("mgjzc")

        sjl = None
        if "table6" in formatted_tables and formatted_tables["table6"]:
            sjl = formatted_tables["table6"][0].get("sjl")

        # 1. 增强 table4 (关键财务指标映射与亏损同比方向精确判定)
        if "table4" in formatted_tables:
            for row in formatted_tables["table4"]:
                for ok, cn in F10_TABLE4_FINANCIAL_MAP.items():
                    if ok in row and row[ok] is not None:
                        row[cn] = row[ok]

                g_profit = row.get("gmjlr")
                g_tb = row.get("gmjlrtbzzl")
                if g_profit is not None and g_tb is not None:
                    try:
                        p_val = float(g_profit)
                        tb_val = float(g_tb)
                        if p_val < 0:
                            if tb_val < 0:
                                row["归母净利润变动方向判定"] = f"亏损扩大 {abs(tb_val):.2f}%"
                                row["亏损标的同比解读警示"] = (
                                    f"【方向判定警示】该标的报告期归母净利润为负({p_val:,.2f}元)，"
                                    f"通达信同比记号按利润变动方向记负号，表达【亏损扩大 {abs(tb_val):.2f}%】。"
                                    "数学上两负数相除切勿误读为盈利增长！"
                                )
                            else:
                                row["归母净利润变动方向判定"] = f"亏损收窄 {abs(tb_val):.2f}% (减亏)"
                                row["亏损标的同比解读警示"] = (
                                    f"【方向判定警示】该标的报告期归母净利润为负({p_val:,.2f}元)，"
                                    f"同比正号表达【亏损收窄/减亏 {abs(tb_val):.2f}%】，非普通盈利增长。"
                                )
                        else:
                            if tb_val >= 0:
                                row["归母净利润变动方向判定"] = f"盈利同比增长 {tb_val:.2f}%"
                            else:
                                row["归母净利润变动方向判定"] = f"盈利同比下滑 {abs(tb_val):.2f}%"
                    except (ValueError, TypeError):
                        pass

                # 扣非净利润变动方向判定
                k_tb = row.get("kfjlrtbzzl")
                k_profit = row.get("kfjlr")
                if k_profit is None and "table0" in formatted_tables and formatted_tables["table0"]:
                    k_profit = formatted_tables["table0"][0].get("kfjlr")
                if k_tb is not None:
                    try:
                        tb_k_val = float(k_tb)
                        if k_profit is not None and float(k_profit) < 0:
                            if tb_k_val < 0:
                                row["扣非净利润变动方向判定"] = f"扣非亏损扩大 {abs(tb_k_val):.2f}%"
                            else:
                                row["扣非净利润变动方向判定"] = f"扣非亏损收窄 {abs(tb_k_val):.2f}% (减亏)"
                        else:
                            if tb_k_val >= 0:
                                row["扣非净利润变动方向判定"] = f"扣非净利润同比增长 {tb_k_val:.2f}%"
                            else:
                                row["扣非净利润变动方向判定"] = f"扣非净利润同比下滑 {abs(tb_k_val):.2f}%"
                    except (ValueError, TypeError):
                        pass

        # 2. 增强 table3 (概念与风格标签隔日状态及破净动态复核指南)
        if "table3" in formatted_tables:
            for row in formatted_tables["table3"]:
                tags = [v for v in row.values() if isinstance(v, str) and v.strip()]
                row["有效风格标签"] = tags
                row["隔日状态与时效警示"] = (
                    f"【核心时效警示】上述风格标签（如 {tags}）及静态估值由通达信服务端于上一清算日（{gxrq or '前一交易日'}）批处理生成，具有隔日滞后性！"
                    "盘中股价异动（如大涨、涨停或大跌）时可能产生完全相反的结论。"
                )
                if "破净股" in tags and mgjzc is not None:
                    row["破净动态复核指引"] = (
                        f"该标的前一交易日清算 PB={sjl or '未知'}（昨收价 < BPS {mgjzc}元）。"
                        f"盘中若股价上涨且最新现价 >= {mgjzc}元（即即时 PB >= 1.0），则已脱离破净状态！"
                        "盘中必须调用 stock_quotes 取得最新价核算，严禁直接盲从隔日静态标签。"
                    )

        # 3. 增强 table6 (估值指标映射与盘中重算提示)
        if "table6" in formatted_tables:
            for row in formatted_tables["table6"]:
                for ok, cn in F10_TABLE6_VALUATION_MAP.items():
                    if ok in row and row[ok] is not None:
                        row[cn] = row[ok]
                row["估值时效提示"] = f"市净率(PB)/市盈率(PE)基于上一交易日清算日({gxrq or '前一交易日'})，盘中请结合 stock_quotes 实时现价动态重算。"

        # 4. 在根对象注入顶层审计警示字典
        tdx_res["f10_audit_notice"] = {
            "隔日概念标签提示": (
                f"F10 摘要与概念标签（如破净股、绩优等）生成于上一交易日清算({gxrq or '昨日'})，盘中股价波动会导致标签反转失效。"
                "盘中研判必须按 [stock_quotes 最新现价 ÷ 每股净资产] 动态重算即时 PB，严禁盲从静态标签。"
            ),
            "亏损公司同比记号规则": (
                "亏损标的(净利润<0)的同比增减率记号代表'利润变动方向'：负号表示亏损扩大(恶化)，正号表示亏损收窄/减亏(改善)。"
                "严禁使用两个负数相除计算得出正数并误读为盈利增长！"
            ),
        }

    def stock_screener(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 6：自然语言智能选股 (如'主板 小盘 绩优 储能 连板')"""
        message = arguments.get("message", "").strip()
        if not message:
            raise ValueError("message 不能为空，请输入自然语言选股条件")
        limit = int(arguments.get("limit", 10))
        page = int(arguments.get("page", 1))
        filter_st = bool(arguments.get("filter_st", True))
        return self.tdx_client.screener(message=message, limit=limit, page=page, filter_st=filter_st)

    def stock_news(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 7：个股最新新闻资讯与重大公告"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空")
        limit = int(arguments.get("limit", 5))
        news = self.fallback_client.get_news(symbol=symbol, limit=limit)
        return {"symbol": symbol, "news_count": len(news), "news": news}

    def market_context(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """工具 8：A股交易日历与时钟状态感知"""
        return get_market_context()


# =========================================================================
# 【功能块 9：8 大金融工具标准 MCP JSON Schema 元数据定义】
# - 输入：无
# - 功能：向大模型精确描述每个工具的用途、参数类型、默认值与取值范围
# - 输出：dict 映射表
# =========================================================================
STOCK_LOOKUP_SCHEMA = {
    "name": "stock_lookup",
    "description": "根据公司简称、股票别名或代码模糊检索标的代码（如'贵州茅台'、'腾讯'、'半导体ETF'）。支持A股(AG)、港股(HK-GP)、美股(MG-GP)、基金(JJ)、指数(ZS)。在进行股票或行业分析时，若用户提供了股票简称而非6位数字代码，应优先调用本工具精确查找标的代码与交易所代码。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "实体名称或简称，如'贵州茅台'、'腾讯'、'上证指数'"},
            "range": {"type": "string", "enum": ["AG", "HK-GP", "HK-JJ", "JJ", "MG-GP", "ZS"], "default": "AG", "description": "市场类型：AG=A股(默认), HK-GP=港股, HK-JJ=港股基金, JJ=基金, MG-GP=美股, ZS=指数"}
        },
        "required": ["query"],
        "additionalProperties": False
    }
}

STOCK_QUOTES_SCHEMA = {
    "name": "stock_quotes",
    "description": "查询指定股票的实时五档盘口、买卖挂单、最新现价、涨跌幅、换手率、量比、内外盘强弱、总市值与动态市盈率等高频盘口与量化估值指标。全面支持 A 股（通达信官方专线）与港股（腾讯免费专线回退，如 '00700' 腾讯控股、'09988' 阿里巴巴）。数据全面中文化映射。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'、'000001'），港股为5位数字（如'00700'、'09988'，支持'HK'前缀）"},
            "setcode": {"type": "string", "description": "市场代码：0=深市，1=沪市，2=北交所，31=港交所。若不传将自动智能推导"}
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

STOCK_KLINE_SCHEMA = {
    "name": "stock_kline",
    "description": "获取指定股票的多周期历史复权K线走势，并自动计算输出关键量化指标（MA5/MA10/MA20均线、MACD指标DIF/DEA/柱状图、KDJ随机指标、BOLL布林带上中下轨）。全面支持 A 股及港股（腾讯免费专线）。支持日线(day)、周线(week)、月线(month)、60分钟(60m)、15分钟(15m)、5分钟(5m)。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'），港股为5位数字（如'00700'）"},
            "period": {"type": "string", "enum": ["day", "week", "month", "60m", "30m", "15m", "5m", "1m"], "default": "day", "description": "K线周期，默认为 day (日线)"},
            "count": {"type": "integer", "minimum": 1, "maximum": 300, "default": 30, "description": "请求K线根数（1-300），默认30根"},
            "adjust": {"type": "string", "enum": ["qfq", "hfq", "none"], "default": "qfq", "description": "复权方式：qfq=前复权(默认), hfq=后复权, none=不复权"},
            "setcode": {"type": "string", "description": "市场代码：0=深市，1=沪市，2=北交所，31=港交所。默认自动推导"},
            "with_indicators": {"type": "boolean", "default": True, "description": "是否自动附带量化指标计算结果（MA、MACD、KDJ、BOLL），默认为 true"}
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

STOCK_FINANCIALS_SCHEMA = {
    "name": "stock_financials",
    "description": "查询上市公司三大财务报表（资产负债表 balance、利润表 income、现金流量表 cashflow）与主营构成。支持报告期累计(report)与单季度(quarter)双口径切换，支持历史多期财报横向对比，所有会计科目均中文化映射。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码（如'600519'）"},
            "report_type": {"type": "string", "enum": ["balance", "income", "cashflow"], "default": "balance", "description": "财报类型：balance=资产负债表, income=利润表, cashflow=现金流量表"},
            "period_type": {"type": "string", "enum": ["report", "quarter"], "default": "report", "description": "统计口径：report=报告期累计(一季报/中报/三季报/年报), quarter=单季度口径"},
            "limit_periods": {"type": "integer", "minimum": 1, "maximum": 10, "default": 4, "description": "返回的历史财报期数，默认4期"}
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

STOCK_F10_SCHEMA = {
    "name": "stock_f10",
    "description": "查询上市公司深度基本面与筹码数据。包括热点题材板块族谱(concepts)、事件驱动催化(events)、机构盈利预测与评级(forecast)、股东人数变化与筹码集中度(shareholders)、十大流通股东(top_holders)、融资融券明细(margin_trading)、主力资金流向(capital_flow)、涨停分析与异动(limit_up)、限售解禁日程(unlocks)、公司概要与最新提示(summary)。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码（如'600519'）"},
            "category": {
                "type": "string",
                "enum": ["concepts", "events", "forecast", "shareholders", "top_holders", "margin_trading", "capital_flow", "limit_up", "unlocks", "summary"],
                "default": "concepts",
                "description": "基本面分类：concepts=热点题材板块族谱, forecast=机构盈利预测, shareholders=股东人数筹码, top_holders=十大流通股东, capital_flow=主力资金流向, margin_trading=融资融券, limit_up=涨停分析, unlocks=限售解禁, summary=公司概要"
            }
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

STOCK_SCREENER_SCHEMA = {
    "name": "stock_screener",
    "description": "自然语言智能选股与行情条件筛选。支持自然语言组合条件提问（如'主板 绩优 储能 连板'、'低市盈率 储能 突破年线'），直接返回符合条件的股票标的列表及指标概览。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "自然语言选股条件，如'主板 绩优 储能'、'低市盈率 连板'"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10, "description": "最多返回标的数量（1-50）"},
            "page": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1, "description": "分页页码"},
            "filter_st": {"type": "boolean", "default": True, "description": "是否自动过滤 ST/*ST 风险警示股票，默认为 true"}
        },
        "required": ["message"],
        "additionalProperties": False
    }
}

STOCK_NEWS_SCHEMA = {
    "name": "stock_news",
    "description": "获取指定个股最近发布的突发新闻资讯、重大公告、研报与媒体快讯，辅助解读股价异动与盘中突发利好/利空。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码（如'600519'）"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5, "description": "返回新闻篇数（1-10）"}
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

MARKET_CONTEXT_SCHEMA = {
    "name": "market_context",
    "description": "获取 A 股当前实时交易时钟与市场运行状态（如开盘竞价、早盘交易中、午盘休市、午后交易中、今日已收盘、周末休市），并提供基准交易日。在分析行情或解盘前可先调用本工具确认市场当前所处的时段。",
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False
    }
}


# =========================================================================
# 【功能块 10：金融工具集构建导出函数】
# - 输入：fin_gw (FinanceGateway): 已配置完成的金融网关实例
# - 功能：将 8 大工具的 Schema 定义与网关回调处理函数打包为标准字典返回
# - 输出：dict[str, tuple[dict[str, Any], Callable]]: 工具定义字典
# =========================================================================
def get_finance_tool_specs(fin_gw: FinanceGateway) -> dict[str, tuple[dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]]:
    return {
        "stock_lookup": (STOCK_LOOKUP_SCHEMA, fin_gw.stock_lookup),
        "stock_quotes": (STOCK_QUOTES_SCHEMA, fin_gw.stock_quotes),
        "stock_kline": (STOCK_KLINE_SCHEMA, fin_gw.stock_kline),
        "stock_financials": (STOCK_FINANCIALS_SCHEMA, fin_gw.stock_financials),
        "stock_f10": (STOCK_F10_SCHEMA, fin_gw.stock_f10),
        "stock_screener": (STOCK_SCREENER_SCHEMA, fin_gw.stock_screener),
        "stock_news": (STOCK_NEWS_SCHEMA, fin_gw.stock_news),
        "market_context": (MARKET_CONTEXT_SCHEMA, fin_gw.market_context),
    }
