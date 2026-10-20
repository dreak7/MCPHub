"""通达信金融数据专线与多源高可用金融服务适配器 (Finance Gateway)
提供 A 股/港股/美股/ETF 的实体检索、实时盘口、多周期K线（含内置量化指标计算）、
三大财务报表、F10 深度基本面、自然语言智能选股与市场交易时钟。
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import os
import re
import time
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
    "Yield": "日内振幅_pct",
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
    "SYL": "动态市盈率_PE_TTM",
    "MGSY": "每股收益_EPS",
    "MGJZC": "每股净资产_BPS",
    "MGGX": "每股公积金",
    "ZGB": "总股本_万股",
    "LTGB": "流通股本_万股",
    "BelongHY": "所属行业代码",
    "Addr": "地域代码",
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
    "N004": "应收票据及账款",
    "N006": "应收账款",
    "N007": "预付款项",
    "N008": "其他应收款",
    "N011": "存货",
    "N013": "合同资产",
    "N015": "流动资产合计",
    "N023": "长期股权投资",
    "N025": "固定资产",
    "N027": "在建工程",
    "N031": "无形资产",
    "N033": "商誉",
    "N037": "非流动资产合计",
    "N038": "资产总计",
    "N039": "短期借款",
    "N044": "应付账款",
    "N045": "预收款项",
    "N046": "应付职工薪酬",
    "N047": "应交税费",
    "N054": "合同负债",
    "N056": "流动负债合计",
    "N057": "长期借款",
    "N058": "应付债券",
    "N065": "非流动负债合计",
    "N066": "负债合计",
    "N067": "实收资本_股本",
    "N068": "资本公积",
    "N071": "盈余公积",
    "N072": "未分配利润",
    "N073": "归母所有者权益合计",
    "N074": "少数股东权益",
    "N075": "所有者权益合计",
    "Tdate": "报告期日期",
}

CASHFLOW_FIELDS_NON_FINANCE: dict[str, str] = {
    "N001": "截止日期",
    "N002": "销售商品提供劳务收到的现金",
    "N005": "经营活动现金流入小计",
    "N006": "购买商品接受劳务支付的现金",
    "N007": "支付给职工以及为职工支付的现金",
    "N008": "支付的各项税费",
    "N010": "经营活动现金流出小计",
    "N011": "经营活动现金流量净额",
    "N012": "收回投资收到的现金",
    "N013": "取得投资收益收到的现金",
    "N017": "投资活动现金流入小计",
    "N018": "购建固定资产无形资产支付的现金",
    "N019": "投资支付的现金",
    "N022": "投资活动现金流出小计",
    "N023": "投资活动现金流量净额",
    "N024": "吸收投资收到的现金",
    "N025": "取得借款收到的现金",
    "N028": "筹资活动现金流入小计",
    "N029": "偿还债务支付的现金",
    "N030": "分配股利利润或偿付利息支付的现金",
    "N032": "筹资活动现金流出小计",
    "N033": "筹资活动现金流量净额",
    "N035": "现金及现金等价物净增加额",
    "N037": "期末现金及等价物余额",
    "Tdate": "报告期日期",
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

    def lookup_stock(self, query: str, market_range: str = "AG") -> list[dict[str, Any]]:
        """AI RAG 语义代码检索 (支持 A股, 港股 HK-GP, 美股 MG-GP, 基金 JJ, 指数 ZS)"""
        req_body = {"query": query.strip(), "range": market_range}
        resp = self._post(self.rag_endpoint, req_body)
        entities: list[dict[str, Any]] = []
        if isinstance(resp, dict) and "retrieved_entities" in resp:
            raw_entities = resp["retrieved_entities"]
            if isinstance(raw_entities, list):
                entities = raw_entities

        # 智能全市场双重嗅探：如果默认搜 AG 返回为空，但用户可能问的是港股（如“腾讯”、“阿里”、“美团”），自动降级尝试 HK-GP
        if not entities and market_range == "AG":
            try:
                hk_resp = self._post(self.rag_endpoint, {"query": query.strip(), "range": "HK-GP"})
                if isinstance(hk_resp, dict) and "retrieved_entities" in hk_resp:
                    hk_entities = hk_resp["retrieved_entities"]
                    if isinstance(hk_entities, list) and hk_entities:
                        entities = hk_entities
            except Exception:
                pass

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
                "买二": {"价格": None, "委托量": None},
                "买三": {"价格": None, "委托量": None},
                "买四": {"价格": None, "委托量": None},
                "买五": {"价格": None, "委托量": None},
                "卖一": {"价格": _safe_float(parts[19] if len(parts) > 19 else 0), "委托量": int(_safe_float(parts[20] if len(parts) > 20 else 0))},
                "卖二": {"价格": None, "委托量": None},
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
            # 自动计算涨跌幅
            now_p = float(hq_raw.get("Now", 0.0) or 0.0)
            close_p = float(hq_raw.get("Close", 0.0) or 0.0)
            if close_p > 0 and now_p > 0:
                mapped_hq["涨跌幅_pct"] = round(((now_p - close_p) / close_p) * 100.0, 2)
                mapped_hq["涨跌额"] = round(now_p - close_p, 3)
            result["盘口核心行情"] = mapped_hq

        # 2. 估值与股本指标中文化映射
        ext_raw = raw.get("ExtInfo", {})
        if isinstance(ext_raw, dict):
            mapped_ext = {EXT_FIELD_MAP.get(k, k): v for k, v in ext_raw.items()}
            result["估值与基本盘面"] = mapped_ext

        # 3. 五档盘口中文化整理
        bsp_raw = raw.get("BspInfo", {})
        if isinstance(bsp_raw, dict):
            bsp_dict = {
                "买一": {"价格": bsp_raw.get("BuyPrice1"), "委托量": bsp_raw.get("BuyVol1")},
                "买二": {"价格": bsp_raw.get("BuyPrice2"), "委托量": bsp_raw.get("BuyVol2")},
                "买三": {"价格": bsp_raw.get("BuyPrice3"), "委托量": bsp_raw.get("BuyVol3")},
                "买四": {"价格": bsp_raw.get("BuyPrice4"), "委托量": bsp_raw.get("BuyVol4")},
                "买五": {"价格": bsp_raw.get("BuyPrice5"), "委托量": bsp_raw.get("BuyVol5")},
                "卖一": {"价格": bsp_raw.get("SellPrice1"), "委托量": bsp_raw.get("SellVol1")},
                "卖二": {"价格": bsp_raw.get("SellPrice2"), "委托量": bsp_raw.get("SellVol2")},
                "卖三": {"价格": bsp_raw.get("SellPrice3"), "委托量": bsp_raw.get("SellVol3")},
                "卖四": {"价格": bsp_raw.get("SellPrice4"), "委托量": bsp_raw.get("SellVol4")},
                "卖五": {"价格": bsp_raw.get("SellPrice5"), "委托量": bsp_raw.get("SellVol5")},
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
        }

    def get_f10(self, symbol: str, category: str = "concepts") -> dict[str, Any]:
        """获取 F10 深度基本面（热点题材板块族谱、机构盈利预测、股东与主力持仓、龙虎榜异动等）"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        cat = category.lower()

        cat_configs: dict[str, tuple[str, list[str], str]] = {
            "concepts": ("TdxSharePCCW.tdxf10_gg_rdtc", ["zttzbkz", clean_code], "热点题材板块族谱"),
            "events": ("TdxSharePCCW.tdxf10_gg_rdtc", ["sjcd", clean_code], "事件驱动催化"),
            "forecast": ("TdxSharePCCW.tdxf10_gg_ybpj", ["yzyq", clean_code], "机构盈利预测"),
            "shareholders": ("TdxSharePCCW.tdxf10_gg_gdyj", ["gdrs", clean_code], "股东人数与筹码集中度"),
            "top_holders": ("TdxSharePCCW.tdxf10_gg_gdyj", ["ltgd", clean_code], "十大流通股东"),
            "margin_trading": ("TdxSharePCCW.tdxf10_gg_jyds", ["rzrq", clean_code], "融资融券交易数据"),
            "capital_flow": ("TdxSharePCCW.tdxf10_gg_jyds", ["zjlx", clean_code], "主力资金流向"),
            "limit_up": ("TdxSharePCCW.tdxf10_gg_jyds", ["ztfx", clean_code], "涨停分析与异动"),
            "unlocks": ("TdxSharePCCW.tdxf10_gg_gbjg", ["xslt", clean_code], "限售解禁日程表"),
            "summary": ("TdxSharePCCW.tdxf10_gg_zxts", ["gsgy", clean_code], "公司概要与最新提示"),
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

    def screener(self, message: str, limit: int = 10, page: int = 1) -> dict[str, Any]:
        """通达信自然语言智能选股 (输入中文自然语言条件，如'主板 绩优 储能 连板')"""
        req_body = [{
            "message": message.strip(),
            "rang": "AG",
            "pageNo": str(page),
            "pageSize": str(max(1, min(limit, 50))),
        }]
        url = f"{self.api_endpoint}?Entry=JNLPSE:wendaQuery"
        raw = self._post(url, req_body)

        results: list[dict[str, Any]] = []
        if isinstance(raw, list) and len(raw) > 1:
            for item in raw[1:]:
                if isinstance(item, list) and len(item) >= 3:
                    results.append({
                        "code": str(item[0]),
                        "name": str(item[1]),
                        "raw_info": item[2:],
                    })

        return {
            "query": message,
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

    def get_news(self, symbol: str, limit: int = 5) -> list[dict[str, Any]]:
        """从东方财富公开资讯接口获取个股最新新闻与公告"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        url = f"https://search-api-web.eastmoney.com/search/json?keywords={clean_code}&pageindex=1&pagesize={max(1, min(limit, 10))}&type=news"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, method="GET")

        try:
            with urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                items = data.get("Data", [])
                news_list: list[dict[str, Any]] = []
                for item in items:
                    title = re.sub(r"<[^>]+>", "", item.get("Title", ""))
                    snippet = re.sub(r"<[^>]+>", "", item.get("Content", ""))
                    news_list.append({
                        "title": title,
                        "source": item.get("MediaName", "东方财富"),
                        "time": item.get("ShowTime", ""),
                        "url": item.get("Url", ""),
                        "snippet": snippet[:150] + "..." if len(snippet) > 150 else snippet,
                    })
                return news_list
        except Exception:
            return []


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
        return self.tdx_client.get_f10(symbol=symbol, category=category)

    def stock_screener(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 6：自然语言智能选股 (如'主板 小盘 绩优 储能 连板')"""
        message = arguments.get("message", "").strip()
        if not message:
            raise ValueError("message 不能为空，请输入自然语言选股条件")
        limit = int(arguments.get("limit", 10))
        page = int(arguments.get("page", 1))
        return self.tdx_client.screener(message=message, limit=limit, page=page)

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
            "page": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1, "description": "分页页码"}
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
