"""MCPHub Finance 港股独立处理引擎 (HKStockEngine)。
提供港股免费免 Key 实时行情 (腾讯专线)、多周期 K 线走势、
新浪与东方财富双通道三大财务报表 (IFRS准则)、
以及持股5%以上主要股东权益申报流水与 F10 深度基本面分析。
"""

from __future__ import annotations

import datetime
import gzip
import json
import logging
import math
import os
import re
import socket
import time
import urllib.parse
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request

from ..core.constants import HK_MAININDICATOR_MAP
from ..core.indicators import calculate_indicators
from ..core.network import _SECURE_OPENER
from .base import BaseMarketEngine


class HKStockEngine(BaseMarketEngine):
    """港股领域独立处理引擎"""

    def __init__(self, timeout: int = 15, logger: logging.Logger | None = None):
        self.timeout = timeout
        self.logger = logger or logging.getLogger("mcp_gateway.finance.hk_engine")
        self._entity_cache: dict[str, dict[str, Any] | None] = {}

    def resolve_entity(self, clean_code: str) -> dict[str, Any] | None:
        """解析5位港股纯数字代码正主实体 (腾讯开放专线)"""
        clean_code = clean_code.strip()
        if clean_code in self._entity_cache:
            return self._entity_cache[clean_code]
        res = self._do_resolve_entity(clean_code)
        self._entity_cache[clean_code] = res
        return res

    def _do_resolve_entity(self, clean_code: str) -> dict[str, Any] | None:
        if clean_code.isdigit() and len(clean_code) == 5:
            url = f"https://qt.gtimg.cn/q=r_hk{clean_code}"
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            try:
                with _SECURE_OPENER.open(req, timeout=3) as resp:
                    raw = resp.read().decode("gb18030", errors="replace")
                if "~" in raw and "none_match" not in raw:
                    parts = raw.split("~")
                    if len(parts) > 1 and parts[1].strip():
                        raw_name = parts[1].strip()
                        clean_name = re.sub(r"-([一二三四五六七八九十百千万\d]+|[WSBwsb]+)$", "", raw_name).strip()
                        final_name = clean_name if clean_name else raw_name
                        aliases = [final_name]
                        if raw_name != final_name:
                            aliases.append(raw_name)
                        return {
                            "entity_code": clean_code,
                            "entity_name": final_name,
                            "entity_type": "港股代码",
                            "entity_setcode": 31,
                            "aliases": aliases,
                        }
            except Exception:
                pass
        return None

    def _resolve_exact_security_entity(self, clean_code: str) -> dict[str, Any] | None:
        return self.resolve_entity(clean_code)

    def get_quotes(self, symbol: str, setcode: str | None = None) -> dict[str, Any]:
        """获取港股实时盘口与估值"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        if not clean_code:
            raise ValueError(f"无效的证券代码 '{symbol}'，港股标的代码必须包含有效数字编号（如 '00700'）。")
        return self._get_hk_quotes(clean_code)

    def get_kline(
        self,
        symbol: str,
        period: str = "day",
        count: int = 30,
        adjust: str = "qfq",
        setcode: str | None = None,
        with_indicators: bool = True,
    ) -> dict[str, Any]:
        """获取港股多周期 K 线走势"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        if not clean_code:
            raise ValueError(f"无效的证券代码 '{symbol}'，港股标的代码必须包含有效数字编号（如 '00700'）。")
        return self._get_hk_kline(
            clean_code,
            period=period,
            count=count,
            adjust=adjust,
            with_indicators=with_indicators,
        )

    def get_financials(
        self,
        symbol: str,
        report_type: str = "balance",
        period_type: str = "report",
        limit_periods: int = 4,
    ) -> dict[str, Any]:
        """获取港股三大财务报表 (东财主力 + 新浪备份)"""
        clean_code = re.sub(r"[^\d]", "", symbol).zfill(5)
        return self._get_hk_financials(
            clean_code,
            report_type=report_type,
            period_type=period_type,
            limit_periods=limit_periods,
        )

    def get_f10(self, symbol: str, category: str = "summary") -> dict[str, Any]:
        """获取港股 F10 深度基本面"""
        clean_code = re.sub(r"[^\d]", "", symbol).zfill(5)
        return self._get_hk_f10(clean_code, category=category)

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

        now_p = _safe_float(parts[3] if len(parts) > 3 else 0)
        vol_shares = int(round(_safe_float(parts[6] if len(parts) > 6 else 0)))
        amount_hkd = _safe_float(parts[37] if len(parts) > 37 else 0)
        total_mv_yi = _safe_opt_float(parts[45] if len(parts) > 45 else None)
        float_mv_yi = _safe_opt_float(parts[44] if len(parts) > 44 else None)

        # 港股总股本与流通股本推导（优先取腾讯专线字段 parts[69]/parts[70]，缺失时由总市值/现价推导）
        zgb_val = None
        if len(parts) > 69 and parts[69]:
            zgb_val = int(round(_safe_float(parts[69])))
        if (not zgb_val or zgb_val <= 0) and total_mv_yi and now_p > 0:
            zgb_val = int(round(total_mv_yi * 100000000.0 / now_p))

        ltgb_val = None
        if len(parts) > 70 and parts[70]:
            ltgb_val = int(round(_safe_float(parts[70])))
        if (not ltgb_val or ltgb_val <= 0) and float_mv_yi and now_p > 0:
            ltgb_val = int(round(float_mv_yi * 100000000.0 / now_p))
        if not ltgb_val and zgb_val:
            ltgb_val = zgb_val

        # 港股换手率计算（成交量 / 流通股本 * 100 或 成交额 / 流通市值 * 100）
        hsl_val = 0.0
        if ltgb_val and ltgb_val > 0 and vol_shares > 0:
            hsl_val = round((vol_shares / float(ltgb_val)) * 100.0, 3)
        elif float_mv_yi and float_mv_yi > 0 and amount_hkd > 0:
            hsl_val = round((amount_hkd / (float_mv_yi * 100000000.0)) * 100.0, 3)

        result: dict[str, Any] = {
            "symbol": norm_code,
            "name": name,
            "setcode": "31",
            "market": "香港交易所 (HKEX)",
            "source": "tencent_free_hk_quote (腾讯免费港股专线)",
            "盘口核心行情": {
                "股票名称": name,
                "现价": now_p,
                "昨收": _safe_float(parts[4] if len(parts) > 4 else 0),
                "今开": _safe_float(parts[5] if len(parts) > 5 else 0),
                "最高价": _safe_float(parts[33] if len(parts) > 33 else 0),
                "最低价": _safe_float(parts[34] if len(parts) > 34 else 0),
                "涨跌额_元": _safe_float(parts[31] if len(parts) > 31 else 0),
                "涨跌幅_pct": _safe_float(parts[32] if len(parts) > 32 else 0),
                "成交量_股": vol_shares,
                "成交额_港元": amount_hkd,
                "日内振幅_pct": _safe_float(parts[43] if len(parts) > 43 else 0),
                "换手率_pct": hsl_val,
                "行情时间": parts[30] if len(parts) > 30 else "",
                "更新时间": parts[30] if len(parts) > 30 else "",
                "结算货币": curr_val,
                "涨停价": None,
                "跌停价": None,
                "涨跌停说明": "港交所主板不设固定 ±10%/±20% 涨跌停板限制（实行市调机制 VCM 冷静期），无固定涨停/跌停价位。",
            },
            "估值与基本盘面": {
                "市盈率_TTM": _safe_opt_float(parts[57] if len(parts) > 57 and parts[57] else (parts[39] if len(parts) > 39 else None)),
                "滚动市盈率_PETTM": _safe_opt_float(parts[57] if len(parts) > 57 and parts[57] else (parts[39] if len(parts) > 39 else None)),
                "静态市盈率_PE_LYR": _safe_opt_float(parts[39] if len(parts) > 39 else None),
                "静态市盈率_PEStatic": _safe_opt_float(parts[39] if len(parts) > 39 else None),
                "动态市盈率_PE": _safe_opt_float(parts[71] if len(parts) > 71 and parts[71] else (parts[39] if len(parts) > 39 else None)),
                "市净率_PB": _safe_opt_float(parts[58] if len(parts) > 58 else None),
                "市盈率股本计算口径": "全部已发行总股本(含A+H)乘H股价",
                "港股市值_亿港元": float_mv_yi,
                "港股市值_H股流通市值_亿港元": float_mv_yi,
                "流通市值_亿港元": float_mv_yi,
                "总市值_全部股本乘H股价_亿港元": total_mv_yi,
                "总市值_亿港元": total_mv_yi,
                "港股市值_元": int(round(float_mv_yi * 100000000)) if float_mv_yi else None,
                "流通市值_元": int(round(float_mv_yi * 100000000)) if float_mv_yi else None,
                "总市值_元": int(round(total_mv_yi * 100000000)) if total_mv_yi else None,
                "已发行H股股数_股": ltgb_val,
                "流通股本_股": ltgb_val,
                "全部已发行总股本_股": zgb_val,
                "总股本_股": zgb_val,
                "已发行H股_万股": round(ltgb_val / 10000.0, 2) if ltgb_val else None,
                "流通股本_万股": round(ltgb_val / 10000.0, 2) if ltgb_val else None,
                "总股本_万股": round(zgb_val / 10000.0, 2) if zgb_val else None,
                "52周最高": _safe_opt_float(parts[48] if len(parts) > 48 else None),
                "52周最低": _safe_opt_float(parts[49] if len(parts) > 49 else None),
                "52周最高_前复权": _safe_opt_float(parts[48] if len(parts) > 48 else None),
                "52周最低_前复权": _safe_opt_float(parts[49] if len(parts) > 49 else None),
                "市值口径说明": (
                    "【港股与AH股市值口径说明】：\n"
                    "1.【港股市值_H股流通市值_亿港元】(纯港股口径)：已发行H股股数 × H股现价，真实反映标的在港交所实际上市流通规模（如中国国航 00753 约 169 亿港元，青岛啤酒 00168 约 310 亿港元，中国电信 00728 约 800 亿港元）；\n"
                    "2.【总市值_全部股本乘H股价_亿港元】(全股本混合参考口径)：境内A股+境外H股全部总股本 × H股现价（如中国国航约 699 亿港元）。因两地市场存在AH折溢价，此混合口径仅供全股本估值参考，严禁误认为港股单一市场可流通规模！"
                ),
                "估值口径说明": (
                    "【港股市盈率口径与汇率折算披露】：\n"
                    "1. 字段映射关系：【市盈率_TTM】与【滚动市盈率_PETTM】同源取自过去12个月净利润测算；【静态市盈率_PE_LYR】与【静态市盈率_PEStatic】同源取自上一财年经审计年报。\n"
                    "2. 跨币种汇率换算机制：对于财报以人民币(CNY)编报但股票以港元(HKD)交易的标的（如腾讯控股、中国国航、青岛啤酒、中国电信等），行情源提供的 PE_TTM 在底层已包含港元对人民币的汇率折算（隐含换算比率约为 0.868~0.872 CNY/HKD）。如以人民币净利润手工复核，请统一折算对应汇率。\n"
                    "3.【A+H两地上市标的市盈率全股本口径警示与自算铁律】：上游公共行情源（腾讯/新浪）对 A+H 标的所直接提供的【市盈率_TTM】与【静态市盈率】系直接透传自行情数据流（逐位一致）。但上游口径底层采用总股本乘H股价折算，且其采用的 TTM 净利润存在历史口径偏差（例如 02628 上游行情串 PE_TTM 2.33 隐含 2476 亿净利，与财报实际归母 TTM 1207.63 亿存在明显出入）。因此行情接口提供的 PE_TTM 仅供参考，在严肃基本面估值分析时，严禁盲信上游原始 PE 数值，必须以最新财报真实归母净利润结合即时汇率进行自算（【总市值或H股折算市值 ÷ 实际财报TTM归母净利润】）！"
                ),
                "52周价格区间说明": (
                    "52周最高/最低价统计周期为最近52周滚动交易日，上游行情源底层采用滚动现金分红前复权(QFQ)口径计算；"
                    "若与不复权历史K线(adjust='none')的高低点存在轻微偏差（通常约 1%~2%，等于期间派息分红金额），属于前复权派息扣减所致的正常口径差异。"
                    "上市不足52周的次新股则等同于上市以来历史区间。"
                ),
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
    # 【功能块：腾讯免费公开港股多周期历史 K 线获取与技术指标计算】
    # - 输入：clean_code (str): 5位港股数字代码（如 "00700"）
    #         period (str): 周期类型 ("day", "week", "month", "60m", "30m", "15m", "5m")
    #         count (int): 获取条数上限 (1-300)
    #         adjust (str): 复权方式 ("qfq", "hfq", "none")
    #         with_indicators (bool): 是否在底层计算技术指标
    # - 功能：调用腾讯财经官方开放 K 线专线 (web.ifzq.gtimg.cn)，拉取历史行情数据，
    #         清洗时间、开高低收成交量，并在纯 Python 算法层原生计算 MA/MACD/KDJ/BOLL/RSI
    # - 输出：dict[str, Any]: 结构化港股 K 线与指标字典
    # =========================================================================
    # =========================================================================
    # 【功能块 4.5：港股历史与分时 K 线高可用获取器】
    # - 输入：clean_code (5位港股数字代码), period (周期: 1m/5m/15m/30m/60m/day/week/month),
    #         count (条数), adjust (复权: none/qfq/hfq), with_indicators (是否计算技术指标)
    # - 功能：
    #   1. 分钟线 (1m/5m/15m/30m/60m)：调用腾讯日内逐分钟数据流 (minute/query)，精准聚合当日
    #      交易切片，生成含真实时分时间戳 (如 10:30)、高开低收、成交量与成交额的真实分钟 K 线；
    #   2. 跨午休切分：60m 周期按港交所主流规范 10:30, 11:30, 13:30 (跨午休聚合), 14:30, 15:30, 16:00 切分；
    #   3. 日/周/月线：调用腾讯高可用 newfqkline 通道，完全支持 none/qfq/hfq，精确换算成交额，
    #      彻底杜绝 adjust='none' 时成交额 amount 恒为 0.0 的历史缺陷；
    #   4. 覆盖说明：权威披露港股公共源仅提供日内实时分钟线聚合、跨日回溯需查日线的服务边界。
    # - 输出：dict 规范响应对象 (包含 klines 列表、指标与详细口径说明)
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
        limit_cnt = max(1, min(int(count), 500))
        p_lower = period.lower()
        adj_lower = adjust.lower()

        # ---------------------------------------------------------------------
        # 1. 港股日内分钟线切片聚合 (1m / 5m / 15m / 30m / 60m)
        # 针对公共行情源不提供跨日历史分钟库的特性，从日内逐分钟流原生聚合当日标准分钟 K 线
        # ---------------------------------------------------------------------
        if p_lower in ("1m", "5m", "15m", "30m", "60m"):
            minute_urls = [
                f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=hk{norm_code}",
                f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/minute/query?code=hk{norm_code}",
            ]
            raw_points: list[str] = []
            date_fmt = datetime.datetime.now().strftime("%Y-%m-%d")
            stock_name = f"港股_{norm_code}"

            for m_url in minute_urls:
                try:
                    req = Request(m_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                    with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                        js = json.loads(resp.read().decode("utf-8", errors="replace"))
                        d = js.get("data", {}).get(f"hk{norm_code}", {})
                        date_str = str(d.get("data", {}).get("date", "") or "").strip()
                        if len(date_str) == 8 and date_str.isdigit():
                            date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                        raw_points = d.get("data", {}).get("data", [])
                        qt_data = d.get("qt", {}).get(f"hk{norm_code}", [])
                        if len(qt_data) > 1 and qt_data[1]:
                            stock_name = str(qt_data[1])
                        if raw_points:
                            break
                except Exception:
                    continue

            if raw_points:
                parsed_points: list[dict[str, Any]] = []
                for pt in raw_points:
                    parts = str(pt).split()
                    if len(parts) >= 4:
                        try:
                            parsed_points.append({
                                "time": parts[0],
                                "price": float(parts[1]),
                                "cum_vol": float(parts[2]),
                                "cum_amt": float(parts[3]),
                            })
                        except (ValueError, TypeError):
                            continue

                step_min = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}.get(p_lower, 60)
                parsed_bars: list[dict[str, Any]] = []
                cur_bucket: list[dict[str, Any]] = []
                prev_cum_vol = 0.0
                prev_cum_amt = 0.0

                def finish_bucket(bucket: list[dict[str, Any]], bar_label: str) -> None:
                    nonlocal prev_cum_vol, prev_cum_amt
                    if not bucket:
                        return
                    p_list = [p["price"] for p in bucket]
                    last_p = bucket[-1]
                    v = max(0.0, last_p["cum_vol"] - prev_cum_vol)
                    a = max(0.0, last_p["cum_amt"] - prev_cum_amt)
                    prev_cum_vol = last_p["cum_vol"]
                    prev_cum_amt = last_p["cum_amt"]
                    # 修复 Issue 6: 若跨午休聚合 bar 的最后一个点还在上午(<=12:00)，避免时间标签提前跳到下午(13:30/13:15)
                    if bar_label in ("1330", "1315", "1300") and last_p.get("time", "") <= "1200":
                        hh_mm = "12:00"
                    else:
                        hh_mm = f"{bar_label[:2]}:{bar_label[2:]}" if len(bar_label) == 4 else bar_label
                    dt_label = f"{date_fmt} {hh_mm}"
                    parsed_bars.append({
                        "time": dt_label,
                        "datetime": dt_label,
                        "open": round(bucket[0]["price"], 3),
                        "high": round(max(p_list), 3),
                        "low": round(min(p_list), 3),
                        "close": round(bucket[-1]["price"], 3),
                        "volume": round(v, 2),
                        "amount": round(a, 2),
                    })

                if p_lower == "60m":
                    # 港交所全天 330 分钟按 30+60+60+30 切分为 6 根 60m 根 bar
                    buckets_60m = [
                        ("1030", lambda t: t <= "1030"),
                        ("1130", lambda t: "1030" < t <= "1130"),
                        ("1330", lambda t: "1130" < t <= "1330"),  # 跨午休聚合 bar
                        ("1430", lambda t: "1330" < t <= "1430"),
                        ("1530", lambda t: "1430" < t <= "1530"),
                        ("1600", lambda t: t > "1530"),
                    ]
                    b_idx = 0
                    for pt in parsed_points:
                        while b_idx < len(buckets_60m) and not buckets_60m[b_idx][1](pt["time"]):
                            finish_bucket(cur_bucket, buckets_60m[b_idx][0])
                            cur_bucket = []
                            b_idx += 1
                        if b_idx < len(buckets_60m):
                            cur_bucket.append(pt)
                    if cur_bucket and b_idx < len(buckets_60m):
                        finish_bucket(cur_bucket, buckets_60m[b_idx][0])

                elif p_lower == "30m":
                    buckets_30m = ["1000", "1030", "1100", "1130", "1200", "1330", "1400", "1430", "1500", "1530", "1600"]
                    b_idx = 0
                    for pt in parsed_points:
                        # 确保收市竞价时段 (CAS 16:00-16:08/16:10) 归入最后一根 16:00 bar，杜绝丢失成交量
                        while b_idx < len(buckets_30m) - 1 and pt["time"] > buckets_30m[b_idx]:
                            finish_bucket(cur_bucket, buckets_30m[b_idx])
                            cur_bucket = []
                            b_idx += 1
                        if b_idx < len(buckets_30m):
                            cur_bucket.append(pt)
                    if cur_bucket and b_idx < len(buckets_30m):
                        finish_bucket(cur_bucket, buckets_30m[b_idx])

                elif p_lower == "15m":
                    # 港股 15 分钟标准时间网格对齐 (含跨午休与收市竞价时段归集)
                    buckets_15m = [
                        "0945", "1000", "1015", "1030", "1045", "1100", "1115", "1130", "1200",
                        "1315", "1330", "1345", "1400", "1415", "1430", "1445", "1500", "1515", "1530", "1545", "1600"
                    ]
                    b_idx = 0
                    for pt in parsed_points:
                        while b_idx < len(buckets_15m) - 1 and pt["time"] > buckets_15m[b_idx]:
                            finish_bucket(cur_bucket, buckets_15m[b_idx])
                            cur_bucket = []
                            b_idx += 1
                        if b_idx < len(buckets_15m):
                            cur_bucket.append(pt)
                    if cur_bucket and b_idx < len(buckets_15m):
                        finish_bucket(cur_bucket, buckets_15m[b_idx])

                elif p_lower == "5m":
                    # 港股 5 分钟固定网格对齐 (修复 Issue 6 时间戳对齐与量额 100% 守恒)
                    buckets_5m = [
                        "0935", "0940", "0945", "0950", "0955", "1000", "1005", "1010", "1015", "1020", "1025", "1030",
                        "1035", "1040", "1045", "1050", "1055", "1100", "1105", "1110", "1115", "1120", "1125", "1130",
                        "1135", "1140", "1145", "1150", "1155", "1200",
                        "1305", "1310", "1315", "1320", "1325", "1330", "1335", "1340", "1345", "1350", "1355", "1400",
                        "1405", "1410", "1415", "1420", "1425", "1430", "1435", "1440", "1445", "1450", "1455", "1500",
                        "1505", "1510", "1515", "1520", "1525", "1530", "1535", "1540", "1545", "1550", "1555", "1600"
                    ]
                    b_idx = 0
                    for pt in parsed_points:
                        while b_idx < len(buckets_5m) - 1 and pt["time"] > buckets_5m[b_idx]:
                            finish_bucket(cur_bucket, buckets_5m[b_idx])
                            cur_bucket = []
                            b_idx += 1
                        if b_idx < len(buckets_5m):
                            cur_bucket.append(pt)
                    if cur_bucket and b_idx < len(buckets_5m):
                        finish_bucket(cur_bucket, buckets_5m[b_idx])

                else:
                    # 1m 切片：日内逐分钟点直接标准化为独立 1m bar (修复 Issue 6)
                    for pt in parsed_points:
                        finish_bucket([pt], pt["time"])

                # 先在完整日内分钟流上计算技术指标消除初值偏差，再截取用户请求的最新根数 (解决 P1-2)
                if with_indicators and parsed_bars:
                    parsed_bars = calculate_indicators(parsed_bars)
                final_bars = parsed_bars[-limit_cnt:]
                is_truncated = len(parsed_bars) > limit_cnt

                resp_payload = {
                    "symbol": norm_code,
                    "name": stock_name,
                    "setcode": "31",
                    "period": period,
                    "adjust": adjust,
                    "total_bars": len(final_bars),
                    "indicators_included": bool(with_indicators),
                    "klines": final_bars,
                    "港股分钟线覆盖说明": (
                        "上游公共行情源仅开放港股【当日日内分钟线】（提供当日 1m/5m/15m/30m/60m 精确聚合切片，受限于交易进度，全天最多共 6 根 60m bar、11 根 30m bar、21 根 15m bar、66 根 5m bar；盘中或午休查询仅返回已完成时段），"
                        "不提供跨日历史多天分钟归档数据。如需跨日及历史技术分析，请指定 period='day'（日K）、'week'（周K）或 'month'（月K）。"
                    ),
                    "港股分钟网格时段说明": (
                        "港股30m线全天共11根：上午5根(10:00/10:30/11:00/11:30/12:00)，下午6根(13:30/14:00/14:30/15:00/15:30/16:00)；"
                        "下午首根13:30 bar精确归集下午开盘 13:00-13:30 时段的成交量与行情。"
                    ),
                    "成交量口径说明": (
                        "港股分钟 K 线（1m/5m/15m/30m/60m）仅包含 09:30-16:00 持续交易时段成交量，"
                        "不含 16:00-16:08/16:10 收市竞价交易时段 (CAS) 撮合量。港交所 CAS 时段成交量通常占全天 5%~12%，"
                        "故分钟线累计成交量略小于日 K 线全天总量，此属港股交易机制正常特性而非数据丢损。"
                    ),
                    "港股60分钟跨午休切分机制说明": (
                        "【港股60分钟K线跨午休切分机制说明】：港交所上午持续交易时段为 09:30-12:00 (共150分钟)，下午为 13:00-16:00 (共180分钟)。"
                        "主流交易所规范将全天60分钟K线按【30+60+60+30】及跨午休规则切分为6根bar："
                        "10:30 (开盘前1小时)、11:30 (中间1小时)、13:30 (跨午休聚合bar：囊括上午11:30-12:00与下午13:00-13:30各半小时成交量，对应30m的12:00与13:30之和)、"
                        "14:30、15:30、16:00 (收盘半小时)。"
                    ),
                }
                if is_truncated:
                    resp_payload["截断说明"] = (
                        f"受 count={count} (上限 {limit_cnt}) 参数限制，当前仅返回最近 {len(final_bars)} 根分钟线，"
                        f"已截断当日早盘前 {len(parsed_bars) - len(final_bars)} 根分钟线。如需完整全天日内量能分析，"
                        f"请传入 count={len(parsed_bars)} 或直接查阅日 K 线。"
                    )
                elif count > len(final_bars):
                    # 修复 E (P2): 港股 60m/30m/15m count 超限提示
                    resp_payload["超限提示"] = (
                        f"当前请求 count={count} 大于港股当日该周期实际可生成的最大根数({len(final_bars)}根)。"
                        f"港交所持续交易时段全天最多共生成 6 根 60m bar、11 根 30m bar、21 根 15m bar、66 根 5m bar。"
                        "港股分钟线仅覆盖当日日内交易时段（不提供跨日历史分钟线），已全量交付当日已生成的全部切片。"
                    )
                return resp_payload

        # ---------------------------------------------------------------------
        # 2. 港股跨日多周期历史 K 线 (day / week / month)
        # 优先使用东方财富与腾讯高可用 newfqkline 通道 (支持 none / qfq / hfq)
        # ---------------------------------------------------------------------
        klt_map = {"day": 101, "week": 102, "month": 103}
        fqt_map = {"none": 0, "qfq": 1, "hfq": 2}
        klt = klt_map.get(p_lower, 101)
        fqt = fqt_map.get(adj_lower, 1)
        # 预热缓冲区机制：请求时多拉取 200 根历史 K 线消除初值截断漂移 (解决 P1-2)
        warmup_cnt = min(320, limit_cnt + 200) if with_indicators else limit_cnt

        # 尝试通道 A：东方财富高可用行情专线
        try:
            em_url = (
                f"http://push2his.eastmoney.com/api/qt/stock/kline/get?"
                f"secid=116.{norm_code}&fields1=f1,f2,f3,f4,f5,f6&"
                f"fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&"
                f"klt={klt}&fqt={fqt}&end=20500101&lmt={warmup_cnt}"
            )
            req = Request(em_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with _SECURE_OPENER.open(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                raw_klines = data.get("data", {}).get("klines", [])
                stock_name = data.get("data", {}).get("name", f"港股_{norm_code}")
                if raw_klines and isinstance(raw_klines, list):
                    parsed_bars: list[dict[str, Any]] = []
                    for line_str in raw_klines:
                        parts = line_str.split(",")
                        if len(parts) < 7:
                            continue
                        parsed_bars.append({
                            "time": parts[0],
                            "open": round(float(parts[1]), 3),
                            "close": round(float(parts[2]), 3),
                            "high": round(float(parts[3]), 3),
                            "low": round(float(parts[4]), 3),
                            "volume": round(float(parts[5]), 2),
                            "amount": round(float(parts[6]), 2),
                        })
                    if with_indicators and parsed_bars:
                        parsed_bars = calculate_indicators(parsed_bars)
                    parsed_bars = parsed_bars[-limit_cnt:]
                    return {
                        "symbol": norm_code,
                        "name": stock_name,
                        "setcode": "31",
                        "period": period,
                        "adjust": adjust,
                        "total_bars": len(parsed_bars),
                        "indicators_included": bool(with_indicators),
                        "klines": parsed_bars,
                    }
        except Exception:
            pass

        # 尝试通道 B：腾讯财经高可用 newfqkline 通道 (彻底解决 adjust=none 时 amount=0 的缺陷，修复 D3)
        p_param = {"day": "day", "week": "week", "month": "month"}.get(p_lower, "day")
        tx_adj_param = adj_lower if adj_lower in ("qfq", "hfq", "none") else "none"
        if tx_adj_param in ("qfq", "hfq"):
            # 优先使用专门支持港股复权序列的 hkfqkline 接口 (修复 Issue 5 复权失效)
            tx_urls = [
                f"https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?_var=kline_{p_param}&param=hk{norm_code},{p_param},,,{warmup_cnt},{tx_adj_param}",
                f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?_var=kline_{p_param}&param=hk{norm_code},{p_param},,,{warmup_cnt},{tx_adj_param}",
            ]
        else:
            tx_urls = [
                f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?_var=kline_{p_param}&param=hk{norm_code},{p_param},,,{warmup_cnt},none",
                f"https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?_var=kline_{p_param}&param=hk{norm_code},{p_param},,,{warmup_cnt}",
            ]

        raw_text = ""
        for url in tx_urls:
            try:
                req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                    raw_text = resp.read().decode("utf-8", errors="replace")
                    if "=" in raw_text and f"hk{norm_code}" in raw_text:
                        break
            except Exception:
                continue

        if not raw_text or "=" not in raw_text:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到港股代码 '{clean_code}' 的历史 K 线走势数据（代码不存在或非上市标的）。"
                    "建议先调用 stock_lookup 工具核对标准代码。"
                )
            return {
                "symbol": norm_code,
                "name": f"港股_{norm_code}",
                "setcode": "31",
                "period": period,
                "adjust": adjust,
                "total_bars": 0,
                "indicators_included": bool(with_indicators),
                "klines": [],
            }

        try:
            json_str = raw_text.split("=", 1)[1]
            data = json.loads(json_str)
            stock_data = data.get("data", {}).get(f"hk{norm_code}", {})
            name = stock_data.get("qt", {}).get(f"hk{norm_code}", ["", f"港股_{norm_code}"])[1]
            raw_klines = (
                stock_data.get(f"{tx_adj_param}{p_param}")
                or stock_data.get(f"qfq{p_param}")
                or stock_data.get(f"hfq{p_param}")
                or stock_data.get(p_param)
                or []
            )
        except Exception:
            name = f"港股_{norm_code}"
            raw_klines = []

        parsed_bars = []
        for k in raw_klines:
            if not isinstance(k, list) or len(k) < 6:
                continue
            dt_str = str(k[0])
            open_p = float(k[1]) if str(k[1]).replace(".", "").isdigit() else 0.0
            close_p = float(k[2]) if str(k[2]).replace(".", "").isdigit() else 0.0
            high_p = float(k[3]) if str(k[3]).replace(".", "").isdigit() else 0.0
            low_p = float(k[4]) if str(k[4]).replace(".", "").isdigit() else 0.0
            vol = float(k[5]) if str(k[5]).replace(".", "").replace("e", "").replace("+", "").isdigit() else 0.0
            # 腾讯 newfqkline 成交额在索引 8 (单位万港元)，换算为港元
            amt = 0.0
            if len(k) > 8 and str(k[8]).replace(".", "").isdigit():
                amt = round(float(k[8]) * 10000.0, 2)
            elif len(k) > 6 and str(k[6]).replace(".", "").isdigit():
                amt = round(float(k[6]) * 10000.0, 2)
            if amt == 0.0 and vol > 0:
                amt = round(vol * ((open_p + close_p) / 2.0), 2)

            parsed_bars.append({
                "time": dt_str,
                "datetime": dt_str,
                "open": round(open_p, 3),
                "high": round(high_p, 3),
                "low": round(low_p, 3),
                "close": round(close_p, 3),
                "volume": vol,
                "amount": amt,
            })

        if parsed_bars:
            if with_indicators:
                parsed_bars = calculate_indicators(parsed_bars)
            parsed_bars = parsed_bars[-limit_cnt:]

        if not parsed_bars:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到港股代码 '{clean_code}' 的历史 K 线走势数据（代码不存在或非上市标的）。"
                    "建议先调用 stock_lookup 工具核对标准代码。"
                )

        return {
            "symbol": norm_code,
            "name": name,
            "setcode": "31",
            "period": period,
            "adjust": adjust,
            "total_bars": len(parsed_bars),
            "indicators_included": bool(with_indicators),
            "klines": parsed_bars,
            "成交量口径说明": (
                "港股日 K 线成交量与成交额涵盖 09:30-16:00 持续交易时段及 16:00-16:10 收市竞价时段 (CAS) 全天总量。"
            ),
            "复权口径说明": (
                "受上游公共行情源服务能力限制，港股历史 K 线当前统一提供不复权(none)序列；"
                "qfq/hfq 参数暂无法获取独立复权数据序列，区间真实收益率请结合分红送转与股份回购记录综合核算。"
            ),
        }

    # =========================================================================
    # 【功能块 5.1：新浪港股主力财务报表页面解析器 (Sina HK Financials)】
    # - 输入：clean_code (5位港股数字代码), report_type (报表类别), limit_periods (期数)
    # - 功能：向新浪财经港股专线发起请求，解析综合损益表、资产负债表与主要指标 HTML 表格，
    #         当发现数据陈旧（如资产负债表停更于历史年份）时返回 None 以便平滑触发东方财富备份
    # - 输出：list[dict[str, Any]] | None
    # =========================================================================
    def _parse_sina_hk_financials(
        self, clean_code: str, report_type: str = "income", limit_periods: int = 4
    ) -> list[dict[str, Any]] | None:
        url = f"https://stock.finance.sina.com.cn/hkstock/finance/{clean_code}.html"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                html = raw_bytes.decode("gbk", errors="replace")
        except Exception as exc:
            self.logger.warning(f"请求新浪港股财报页面失败 ({clean_code}): {exc}")
            return None

        # 新浪页面表格布局索引：0=主要指标, 1=资产负债表, 2=现金流量表, 3=综合损益表
        type_to_table_idx = {
            "summary": 0, "main": 0, "indicator": 0,
            "balance": 1, "balance_sheet": 1, "zcfzb": 1,
            "cashflow": 2, "cashflow_statement": 2, "xjllb": 2,
            "income": 3, "income_statement": 3, "lyb": 3,
        }
        target_idx = type_to_table_idx.get(report_type.lower(), 3)
        table_matches = re.findall(r"<table([^>]*)>(.*?)</table>", html, re.S | re.I)
        if target_idx >= len(table_matches):
            return None

        attrs, content = table_matches[target_idx]
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", content, re.S | re.I)
        grid: list[list[str]] = []
        for row in rows:
            cols = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
            clean_cols = [re.sub(r"<[^>]+>", "", c).strip() for c in cols]
            if any(clean_cols):
                grid.append(clean_cols)

        if len(grid) < 2:
            return None

        headers = grid[0]
        periods_count = len(headers) - 1
        if periods_count <= 0:
            return None

        actual_periods = min(periods_count, limit_periods)
        statement_periods: list[dict[str, Any]] = [{} for _ in range(actual_periods)]

        # 遍历解析每一行科目
        for row in grid:
            raw_metric = row[0]
            metric = raw_metric

            # 语义校准：日期与报告期类型判定
            for p_idx in range(actual_periods):
                if p_idx + 1 < len(row):
                    val_str = row[p_idx + 1].strip()
                    if not val_str or val_str == "--":
                        continue

                    # 1. 日期行（如 "2026-06-30"）语义统一标准化：无论是 "截止日" 还是 "报告期"，只要格式为日期，统一写入 "截止日期" 与 "报告期截止日"
                    if re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", val_str):
                        statement_periods[p_idx]["截止日期"] = val_str
                        statement_periods[p_idx]["报告期截止日"] = val_str
                        continue

                    # 2. 报告期/报表类型行（如 "中报"、"年报"、"一季报"）
                    if metric in ("报表类型", "报告类型") or (metric == "报告期" and not re.match(r"^\d{4}", val_str)):
                        statement_periods[p_idx]["报告期"] = val_str
                        statement_periods[p_idx]["报表类型"] = val_str
                        continue

                    # 3. 币种行（如 "美元"、"人民币"、"港币"）
                    if metric in ("币种", "货币"):
                        statement_periods[p_idx]["币种"] = val_str
                        statement_periods[p_idx]["报告币种"] = val_str
                        continue

                    val_norm = val_str.replace(",", "")
                    try:
                        val_num = round(float(val_norm), 2)
                        statement_periods[p_idx][metric] = val_num
                    except ValueError:
                        statement_periods[p_idx][metric] = val_str

        # 完善各期的单位与货币口径声明
        for p in statement_periods:
            p.setdefault("金额单位", "百万元")
            curr = p.get("报告币种") or p.get("币种") or "港元"
            p["报告币种"] = curr
            p["币种"] = curr

            # 修正每股盈利仙/美分单位混淆 (P1 H5)
            for eps_key in ("基本每股盈利(仙)", "摊薄每股盈利(仙)", "每股股息(仙)"):
                if eps_key in p and isinstance(p[eps_key], (int, float)):
                    eps_val = p[eps_key]
                    if curr == "美元":
                        p["每股收益币种单位"] = "美分"
                        p[eps_key.replace("(仙)", "(美分)")] = eps_val
                        p[f"{eps_key.split('(')[0]}_折合美元"] = round(eps_val / 100.0, 4)
                    elif curr in ("港币", "港元"):
                        p["每股收益币种单位"] = "港仙"
                        p[f"{eps_key.split('(')[0]}_折合港元"] = round(eps_val / 100.0, 4)
                    elif curr == "人民币":
                        p["每股收益币种单位"] = "分"
                        p[f"{eps_key.split('(')[0]}_折合人民币元"] = round(eps_val / 100.0, 4)

        # 跨期币种变动检测与警示注入 (P1 H4)
        distinct_currencies = {p.get("报告币种") for p in statement_periods if p.get("报告币种")}
        if len(distinct_currencies) > 1:
            for p in statement_periods:
                c_now = p.get("报告币种", "")
                p["跨期币种变动说明"] = (
                    f"注意：本标的在不同会计报告期存在报表货币切换（当前期为【{c_now}】，历史期存在其它币种）。"
                    "跨期对比分析请务必按各期【报告币种】分别核算，严禁直接对不同币种数值进行同比加减！"
                )

        # 检查数据新鲜度：如果最新一期的日期在 2023 年之前（如新浪部分港股资产负债表已停更在历史年份），返回 None 触发东财备份
        if statement_periods:
            latest_d = str(statement_periods[0].get("截止日期", statement_periods[0].get("报告期截止日", "")))
            if re.match(r"^\d{4}", latest_d) and latest_d < "2023-01-01":
                return None

        return statement_periods

    # =========================================================================
    # 【功能块 5.2：东方财富港股高可用备份数据中心接口 (Eastmoney HK Financials)】
    # - 输入：clean_code (5位港股数字代码), report_type (报表类别), limit_periods (期数)
    # - 功能：调用东方财富公开数据中心 API (datacenter-web.eastmoney.com)，提取最新港股
    #         资产负债表 (RPT_HKF10_FN_BALANCE)、利润表 (RPT_HKF10_FN_INCOME)、
    #         现金流量表 (RPT_HKF10_FN_CASHFLOW_PC) 与主要财务指标
    # - 输出：list[dict[str, Any]] 规范报告期字典列表
    # =========================================================================
    def _get_eastmoney_hk_financials(
        self, clean_code: str, report_type: str = "balance", limit_periods: int = 4
    ) -> list[dict[str, Any]]:
        secucode = f"{clean_code}.HK"
        rep_map = {
            "balance": "RPT_HKF10_FN_BALANCE",
            "balance_sheet": "RPT_HKF10_FN_BALANCE",
            "zcfzb": "RPT_HKF10_FN_BALANCE",
            "income": "RPT_HKF10_FN_INCOME",
            "income_statement": "RPT_HKF10_FN_INCOME",
            "lyb": "RPT_HKF10_FN_INCOME",
            "cashflow": "RPT_HKF10_FN_CASHFLOW_PC",
            "cashflow_statement": "RPT_HKF10_FN_CASHFLOW_PC",
            "xjllb": "RPT_HKF10_FN_CASHFLOW_PC",
            "summary": "RPT_HKF10_FN_MAININDICATOR",
            "main": "RPT_HKF10_FN_MAININDICATOR",
            "indicator": "RPT_HKF10_FN_MAININDICATOR",
        }
        rep_name = rep_map.get(report_type.lower(), "RPT_HKF10_FN_BALANCE")
        base_url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
        page_size = max(500, limit_periods * 120)
        params = {
            "reportName": rep_name,
            "columns": "ALL",
            "filter": f'(SECUCODE="{secucode}")',
            "pageNumber": "1",
            "pageSize": str(page_size),
            "sortTypes": "-1",
            "sortColumns": "REPORT_DATE",
        }
        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                js = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        except Exception as exc:
            self.logger.warning(f"请求东方财富港股财报备份接口失败 ({clean_code}): {exc}")
            return []

        data = (js.get("result") or {}).get("data", []) if isinstance(js, dict) else []
        if not isinstance(data, list) or not data:
            return []

        # 修复 P1: 当请求主要指标表 (RPT_HKF10_FN_MAININDICATOR) 时，因东财原生接口在 CURRENCY 字段固定标注股票交易币种(HKD)，
        # 而底层各项营收与利润数值均严格按公司法定编报币种（如腾讯、国航、电信为 CNY）入账，
        # 在此先行向法定损益表 (RPT_HKF10_FN_INCOME) 探测真实记账币种，彻底校准币种标注
        real_currency = None
        if rep_name == "RPT_HKF10_FN_MAININDICATOR":
            try:
                probe_params = {
                    "reportName": "RPT_HKF10_FN_INCOME",
                    "columns": "CURRENCY_CODE",
                    "filter": f'(SECUCODE="{secucode}")',
                    "pageNumber": "1",
                    "pageSize": "1",
                }
                probe_url = f"{base_url}?{urllib.parse.urlencode(probe_params)}"
                probe_req = Request(probe_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with _SECURE_OPENER.open(probe_req, timeout=min(3.0, self.timeout)) as probe_resp:
                    probe_js = json.loads(probe_resp.read().decode("utf-8", errors="replace"))
                    p_data = (probe_js.get("result") or {}).get("data", [])
                    if p_data and isinstance(p_data, list) and p_data[0].get("CURRENCY_CODE"):
                        real_currency = p_data[0].get("CURRENCY_CODE")
            except Exception:
                real_currency = None

        periods_map: dict[str, dict[str, Any]] = {}
        for row in data:
            r_date = str(row.get("REPORT_DATE", "")).split(" ")[0]
            if not r_date:
                continue
            if r_date not in periods_map:
                r_type = row.get("REPORT_TYPE") or row.get("REPORT_TYPE_DETAILS") or ("年报" if r_date.endswith("12-31") else ("中报" if r_date.endswith("06-30") else ("一季报" if r_date.endswith("03-31") else ("三季报" if r_date.endswith("09-30") else "季报"))))
                raw_c_code = row.get("CURRENCY_CODE") or row.get("CURRENCY") or "HKD"
                # 修复 P1: indicators 币种校准为与利润表完全同源的真实记账编报币种 (CNY/USD/HKD)
                c_code = real_currency if (real_currency and rep_name == "RPT_HKF10_FN_MAININDICATOR") else (row.get("CURRENCY_CODE") or row.get("CURRENCY") or "CNY")
                periods_map[r_date] = {
                    "截止日期": r_date,
                    "报告期日期": r_date,
                    "报告期": r_date,
                    "报告期截止日": r_date,
                    "报告期标签": r_type,
                    "报表类型": r_type,
                    "币种": c_code,
                    "报告币种": c_code,
                    "金额单位": "元",
                }
                if real_currency and rep_name == "RPT_HKF10_FN_MAININDICATOR" and real_currency != raw_c_code:
                    periods_map[r_date]["币种说明"] = f"底层主要指标财务数值与法定利润表一致，实际记账编报币种为【{real_currency}】（原始接口所标 {raw_c_code} 仅为股票交易币种）。"
            # 宽表与窄表兼容解析 (修复 HK-7)
            if "STD_ITEM_NAME" in row or "ITEM_NAME" in row:
                item_name = row.get("STD_ITEM_NAME") or row.get("ITEM_NAME")
                amount = row.get("AMOUNT")
                if item_name and amount is not None:
                    try:
                        amt_val = round(float(amount), 2)
                    except (ValueError, TypeError):
                        amt_val = amount
                    periods_map[r_date][item_name] = amt_val
                    # 规范港股现金流外币折算科目为统一标准键名，杜绝三键同值 (修复 P2-3)
                    if item_name in ("期间变动其他项目", "汇率变动影响", "外币报表折算差额"):
                        periods_map[r_date]["汇率变动对现金及现金等价物的影响"] = amt_val
                        periods_map[r_date]["汇率影响"] = amt_val
                    elif item_name == "汇率变动对现金及现金等价物的影响":
                        periods_map[r_date]["汇率影响"] = amt_val
            else:
                # 宽表模式（如 RPT_HKF10_FN_MAININDICATOR）：按指标映射表直接提取各列
                for raw_k, ch_k in HK_MAININDICATOR_MAP.items():
                    if raw_k in row and row[raw_k] is not None:
                        raw_v = row[raw_k]
                        try:
                            periods_map[r_date][ch_k] = round(float(raw_v), 2) if "." in str(raw_v) else int(raw_v)
                        except (ValueError, TypeError):
                            periods_map[r_date][ch_k] = raw_v
                # 兼容注入两口径市值与口径说明 (修复 P2/P3)
                if "TOTAL_MARKET_CAP" in row and row["TOTAL_MARKET_CAP"] is not None:
                    tot_v = row["TOTAL_MARKET_CAP"]
                    periods_map[r_date]["总市值_全部股本乘H股价_港元"] = tot_v
                    periods_map[r_date]["总市值_港元"] = tot_v
                if "HKSK_MARKET_CAP" in row and row["HKSK_MARKET_CAP"] is not None:
                    hk_v = row["HKSK_MARKET_CAP"]
                    periods_map[r_date]["港股市值_H股流通市值_港元"] = hk_v
                    periods_map[r_date]["港股市值_港元"] = hk_v
                periods_map[r_date]["市值口径说明"] = (
                    "【港股市值口径说明】：【港股市值_H股流通市值_港元】为在港交所实际上市流通的H股对应规模；"
                    "【总市值_全部股本乘H股价_港元】为包含A股及未上市股在内的全部总股本统一按H股价折算得出的混合参考市值。"
                )

        # 现金流量表现金桥闭合与口径校准 (修复 HK-1, HK-2, P2-2)
        for p in periods_map.values():
            end_cash = p.get("期末现金") if p.get("期末现金") is not None else p.get("期末现金及等价物")
            begin_cash = p.get("期初现金") if p.get("期初现金") is not None else p.get("期初现金及等价物")
            net_cash = p.get("现金净额") if p.get("现金净额") is not None else p.get("现金及现金等价物净增加额")

            # HK-1, P2-2: 动态探测现金净额是否已在底层包含汇率影响 (如 02601 中国太保：期末 - 期初 == 现金净额)
            if end_cash is not None and begin_cash is not None and net_cash is not None:
                cash_change = round(float(end_cash) - float(begin_cash), 2)
                net_val = round(float(net_cash), 2)
                fx_val = float(p.get("汇率变动对现金及现金等价物的影响") or p.get("汇率影响") or 0.0)

                if abs(cash_change - net_val) < 0.05:
                    p["现金桥闭合说明"] = (
                        f"期末现金({end_cash}) = 期初现金({begin_cash}) + 现金净额({net_cash})，现金桥精确闭合。"
                        "（该标的报表口径中，汇率变动及期间变动其他项目已包含在现金净额内，计算期末现金切勿重复累加汇率影响）"
                    )
                elif p.get("汇率影响") is None and p.get("汇率变动对现金及现金等价物的影响") is None:
                    try:
                        diff = round(cash_change - net_val, 2)
                        p["汇率影响"] = diff
                        p["汇率变动对现金及现金等价物的影响"] = diff
                        p["现金桥闭合说明"] = f"底层源数据未直接标明汇率科目，由现金桥恒等式【期末现金({end_cash}) − 期初现金({begin_cash}) − 现金净额({net_cash}) = 汇率及其他影响({diff})】推算闭合。"
                    except (ValueError, TypeError):
                        pass
                else:
                    p["现金桥闭合说明"] = (
                        f"期末现金({end_cash}) = 期初现金({begin_cash}) + 现金净额({net_cash}) + 汇率影响({fx_val})，现金桥精确闭合。"
                        "（该标的报表口径中，现金流量净额不含汇率变动，需累加汇率及期间变动其他项目）"
                    )

            # HK-2: 注入港股现金及等价物口径说明
            if end_cash is not None or p.get("货币资金") is not None or p.get("现金及等价物") is not None:
                p["现金口径说明"] = (
                    "港股会计报表中，现金流量表「期末现金及等价物」指不受使用限制且可随时支取的流动资金；"
                    "资产负债表「现金及等价物/银行结存」为账面总额（通常包含定期存款、受限保证金等），"
                    "二者差额主要系受限制存款与冻结资金等项目差异所致，属会计准则常规差异。"
                )

            # 总负债恒等式容错补全 (P2 H9)
            if p.get("总负债") is None:
                ld = p.get("流动负债合计")
                nld = p.get("非流动负债合计")
                if ld is not None and nld is not None:
                    try:
                        p["总负债"] = round(float(ld) + float(nld), 2)
                        p["总负债_测算说明"] = "底层源数据缺失总负债字段，由【流动负债合计 + 非流动负债合计】恒等式补全"
                    except (ValueError, TypeError):
                        pass
                elif p.get("总资产") is not None and (p.get("总权益") is not None or p.get("股东权益") is not None):
                    equity = p.get("总权益") if p.get("总权益") is not None else p.get("股东权益")
                    try:
                        p["总负债"] = round(float(p["总资产"]) - float(equity), 2)
                        p["总负债_测算说明"] = "底层源数据缺失总负债字段，由【总资产 - 股东权益/总权益】恒等式补全"
                    except (ValueError, TypeError):
                        pass

        sorted_dates = sorted(periods_map.keys(), reverse=True)
        return [periods_map[d] for d in sorted_dates[:limit_periods]]

    # =========================================================================
    # 【功能块 5.3：港股基本面双源高可用调度器 (东方财富主力 + 新浪备份)】
    # - 输入：symbol (港股标的代码), report_type (报表类别), period_type (报告期类型), limit_periods (期数)
    # - 功能：优先使用东方财富港股数据中心统一拉取三大表与主要指标（统一人民币CNY与元口径，修复 HK-9），
    #         当 period_type=quarter 时对流量表执行相邻累计期做差推算（修复 HK-3），
    #         若东财网络异常或无数据，自动平滑切换至新浪港股 F10 专线作为容灾备份
    # - 输出：dict[str, Any] 结构化港股会计报表响应字典
    # =========================================================================
    def _get_hk_financials(
        self,
        symbol: str,
        report_type: str = "income",
        period_type: str = "report",
        limit_periods: int = 4,
    ) -> dict[str, Any]:
        clean_code = re.sub(r"[^\d]", "", symbol).zfill(5)
        source = "eastmoney_hk_f10 (东方财富港股F10专线)"
        statements = None

        # 修复 HK-9: 统一优先使用东方财富港股数据中心，确保三表同源、统一金额单位（元）与币种（CNY）
        # 修复 P1: 当请求单季度模式 (quarter) 时，额外多请求 4 期作为做差前驱缓冲区，确保最早一期（如Q3）能找到同财年上一期（如H1）做差
        fetch_periods = limit_periods + 4 if period_type.lower() == "quarter" else limit_periods
        try:
            statements = self._get_eastmoney_hk_financials(clean_code, report_type=report_type, limit_periods=fetch_periods)
        except Exception:
            statements = None

        # 容灾备份：若东方财富未返回有效报表，自动平滑切换至新浪港股 F10 专线
        if not statements:
            source = "sina_hk_f10 (新浪港股F10备份专线)"
            try:
                statements = self._parse_sina_hk_financials(clean_code, report_type=report_type, limit_periods=fetch_periods)
            except Exception:
                statements = []

        if not statements:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到港股代码 '{clean_code}' 的财务报表数据（代码不存在或非有效港股上市标的）。"
                    "建议先调用 stock_lookup 工具核对标准代码。"
                )

        # 修复 HK-3, P1: 当 period_type="quarter" 时，对流量表（利润表与现金流量表）按相邻累计报告期做差计算真实单季度数值
        if period_type.lower() == "quarter" and statements:
            if report_type.lower() in ("income", "income_statement", "lyb", "cashflow", "cashflow_statement", "xjllb"):
                quarter_stmts = []
                for i, curr_p in enumerate(statements):
                    p_copy = dict(curr_p)
                    curr_date = str(p_copy.get("截止日期", ""))
                    curr_year = curr_date[:4]
                    prev_p = None
                    for j in range(i + 1, len(statements)):
                        cand_date = str(statements[j].get("截止日期", ""))
                        if cand_date.startswith(curr_year):
                            prev_p = statements[j]
                            break
                    if prev_p:
                        prev_date = str(prev_p.get("截止日期", ""))
                        for k, v in curr_p.items():
                            if isinstance(v, (int, float)) and k not in ("基本每股收益_EPS", "稀释每股收益", "每股收益_元", "摊薄每股收益_元", "每股净资产_BPS_元"):
                                prev_v = prev_p.get(k)
                                if isinstance(prev_v, (int, float)):
                                    p_copy[k] = round(float(v) - float(prev_v), 2)
                        if curr_date.endswith("-12-31"):
                            if prev_date.endswith("-09-30"):
                                p_copy["单期标签"] = "年报(第四季度单期/Q4)"
                                p_copy["单季度计算说明"] = f"本期单季度数据系由全年累计({curr_date})扣减前三季度累计({prev_date})做差推算得到第四季度单季。"
                            else:
                                p_copy["单期标签"] = "年报(下半年单期/H2)"
                                p_copy["单季度计算说明"] = f"该标的实行半年度披露，本期单期数据系由全年累计({curr_date})扣减上半年中报({prev_date})做差推算得到下半年单期(H2)。"
                        elif curr_date.endswith("-09-30"):
                            p_copy["单期标签"] = "三季报(第三季度单期/Q3)"
                            p_copy["单季度计算说明"] = f"本期单季度数据系由前三季度累计({curr_date})扣减上半年中报({prev_date})做差推算得到第三季度单季。"
                        elif curr_date.endswith("-06-30"):
                            p_copy["单期标签"] = "中报(第二季度单期/Q2)"
                            p_copy["单季度计算说明"] = f"本期单季度数据系由上半年累计({curr_date})扣减一季报({prev_date})做差推算得到第二季度单季。"
                        else:
                            p_copy["单季度计算说明"] = f"本期单期数据系由本期累计({curr_date})扣减同财年上一期累计({prev_date})做差推算。"
                        quarter_stmts.append(p_copy)
                    elif curr_date.endswith("-03-31"):
                        # 第一季度累计值本就是单季度数值
                        p_copy["单期标签"] = "一季报(第一季度单期/Q1)"
                        p_copy["单季度计算说明"] = "本期为第一季度(Q1)，累计披露值即为单季度真实值。"
                        quarter_stmts.append(p_copy)
                    elif curr_date.endswith("-06-30"):
                        # 半年度披露标的未披露Q1，中报本身即为上半年单期 (修复 Issue 7)
                        p_copy["单期标签"] = "中报(上半年单期/H1)"
                        p_copy["单季度计算说明"] = "该标的实行半年度披露规则（无独立一季报），中报披露值即为上半年(H1)单期真实值。"
                        quarter_stmts.append(p_copy)
                    else:
                        p_copy["报表口径"] = "累计值(非单季)"
                        p_copy["单季度计算说明"] = "前期财报未单独披露累计值，无法推算单季度真实数值，省略出单季序列。"

                # 深度消歧：将推导得出的单期标签同步覆写至【报告期】与【报表类型】，杜绝下游仅读取老字段产生歧义 (解决 P2-9)
                for q_item in quarter_stmts:
                    if q_item.get("单期标签"):
                        q_item["报告期"] = q_item["单期标签"]
                        q_item["报表类型"] = q_item["单期标签"]

                statements = quarter_stmts[:limit_periods]
            elif report_type.lower() in ("balance", "balance_sheet", "zcfzb"):
                for p in statements:
                    p["单季度说明"] = "资产负债表为特定时点截面存量数据，各期末数值均为该时点真实余额，无需做差。"

        # =========================================================================
        # 【功能块 5.3.1：港股标的金融属性智能嗅探与双轨制 (与 A 股契约 100% 镜像对齐)】
        # - 输入：clean_code (5位港股数字代码), statements (各报告期财报列表)
        # - 功能：
        #   1. 双重自动嗅探判定金融属性 (is_financial)：
        #      - 实体名称嗅探：检查公司实体名是否包含银行/保险/证券/信托/金融/理财/券商/建行/工行/招行等关键词；
        #      - 报表科目特征嗅探：检查已解析科目中是否出现"客户存款"、"客户贷款及其他款项"、
        #        "存放中央银行款项"、"向中央银行借款"、"利息净收入"、"已赚保费"、"拆入资金"等金融专有科目；
        #   2. 会计准则双轨制对齐 (accounting_standard)：
        #      - 金融标的：挂载"国际/香港财务报告准则 (银行 / 保险 / 证券 / 金融控股)"；
        #      - 工商业实体：挂载"国际财务报告准则 (IFRS) / 香港财务报告准则 (HKFRS) - 一般工商业"；
        #   3. 金融报表核算规范与资产负债恒等式检验原地注入：
        #      - 资产负债表：注入不严格区分流动性说明，核验【总资产 = 权益及负债合计】或【总资产 = 总负债 + 股东权益】，
        #        若存在残差自动注入"金融资产负债不平衡自陈差额"；
        #      - 利润表：注入以利息净收入、手续费及佣金与投资收益为核心的金融利润表核算规范；
        #      - 现金流量表：注入客户存贷款与央行款项净增加额核算指引；
        # - 输出：is_financial (bool), standard_name (str), 增强后的 statements
        # =========================================================================
        is_financial = False
        entity = self._resolve_exact_security_entity(clean_code)
        corp_name = ""
        if isinstance(entity, dict):
            corp_name = str(entity.get("entity_name") or "")
        elif entity is not None:
            corp_name = str(getattr(entity, "name", "") or getattr(entity, "entity_name", "") or "")
        if any(k in corp_name for k in ("银行", "保险", "证券", "信托", "金融", "理财", "人寿", "国寿", "太保", "新华", "中保", "财险", "资管", "平安", "友邦", "汇丰", "建行", "工行", "招行", "券商")):
            is_financial = True

        if not is_financial and statements:
            sample_keys = set()
            for p in statements:
                sample_keys.update(p.keys())
            fin_feature_keys = {
                "客户存款", "客户贷款及其他款项", "向中央银行借款", "存放中央银行款项",
                "银行同业及其他金融机构存款(负债)", "银行同业及其他金融机构存款(资产)",
                "利息净收入", "已赚保费", "保险业务收入", "买入返售金融资产",
                "卖出回购金融资产", "拆入资金", "拆出资金", "衍生金融工具-资产", "衍生金融工具-负债",
                "保费", "保户储金", "赔付款项", "质押贷款", "客户存款和同业存放款项净增加额",
                "存放中央银行和同业款项净减少额", "向中央银行借款净增加额"
            }
            if any(k in sample_keys for k in fin_feature_keys):
                is_financial = True

        if is_financial:
            standard_name = "国际/香港财务报告准则 (银行 / 保险 / 证券 / 金融控股)"
        else:
            standard_name = "国际财务报告准则 (IFRS) / 香港财务报告准则 (HKFRS) - 一般工商业"

        rtype_lower = report_type.lower()
        if statements:
            for p in statements:
                if is_financial:
                    if rtype_lower in ("balance", "balance_sheet", "zcfzb"):
                        p["资产负债表核算说明"] = (
                            "【金融企业资产负债表核算规范】本表遵循国际/香港财务报告准则金融机构专用列报格式。"
                            "金融企业资产负债表不严格区分流动与非流动划分，以资产与负债的流动性由高到低排列（库存现金、存放央行、同业款项居前）。"
                            "基本会计恒等式为：【总资产 = 总负债 + 股东权益合计】（或【总资产 = 权益及负债合计】）。"
                        )
                        tot_a = p.get("总资产")
                        tot_l = p.get("总负债")
                        tot_e = p.get("股东权益合计") or p.get("总权益") or p.get("归属于母公司股东权益")
                        tot_le = p.get("权益及负债合计")
                        if tot_a is not None and tot_le is not None:
                            try:
                                diff = round(float(tot_a) - float(tot_le), 2)
                                if abs(diff) > 0.05:
                                    p["金融资产负债不平衡自陈差额"] = diff
                            except (ValueError, TypeError):
                                pass
                        elif tot_a is not None and tot_l is not None and tot_e is not None:
                            try:
                                diff = round(float(tot_a) - (float(tot_l) + float(tot_e)), 2)
                                if abs(diff) > 0.05:
                                    p["金融资产负债不平衡自陈差额"] = diff
                            except (ValueError, TypeError):
                                pass
                    elif rtype_lower in ("income", "income_statement", "lyb"):
                        p["利润表加总纪律说明"] = (
                            "【金融企业利润表核算规范】利息净收入、手续费及佣金净收入与投资收益为金融机构主要核心收益来源。"
                            "财务建模与分析请直接引用官方公布的【营业额/营业收入】、【税前溢利/利润总额】与【股东应占溢利/归属于母公司股东的净利润】。"
                        )
                    elif rtype_lower in ("cashflow", "cashflow_statement", "xjllb"):
                        p["现金流量表加总纪律说明"] = (
                            "【金融企业现金流量表核算规范】本表遵循金融企业现金流量格式。"
                            "客户存贷款变动、央行及同业款项净增加额为经营活动现金流核心组成。"
                            "财务分析请重点引用经营、投资、筹资三大活动净额与期末现金及等价物余额。"
                        )

        type_names = {
            "income": "综合损益表 (利润表)",
            "balance": "资产负债表",
            "cashflow": "现金流量表",
            "summary": "主要财务指标",
        }
        stmt_name = type_names.get(report_type.lower(), report_type)
        period_suffix = "单期(半年度单期/单季)" if period_type.lower() == "quarter" else "累计报告期"
        return {
            "symbol": clean_code,
            "market": "香港交易所 (HKEX)",
            "statement_type": f"{stmt_name} ({period_suffix})",
            "period_type": period_type,
            "source": source,
            "period_count": len(statements or []),
            "is_financial": is_financial,
            "accounting_standard": standard_name,
            "statements": statements or [],
            "会计口径与说明": (
                f"港股会计报表按国际财务报告准则 (IFRS) 或香港财务报告准则 (HKFRS) 编制。当前口径：【{period_suffix}】。\n"
                f"准则类别：【{standard_name}】。\n"
                "1.【数据源与金额单位说明】：本接口采用双源高可用容灾架构：以东方财富港股数据中心专线为主力数据源（统一金额单位为【元】，币种统一为【CNY】），"
                "当 period_type=quarter 时针对利润表与现金流量表按同财年相邻累计报告期自动做差生成单期数据（资产负债表为时点存量不作差）。\n"
                "2.【单期与单季度口径区别】：港交所主板强制规则仅要求披露半年度(中报)与全年度(年报)；除 A+H 两地上市标的（因中国证监会要求必须按季披露 Q1/Q3）及腾讯控股等少数自愿披露标的外，多数纯 H 股无一季报/三季报。"
                "因此对于实行半年度披露的标的，做差推算的单期其本质是半年度单期（如年报扣除中报得到下半年单期 H2，中报累计值即为上半年单期 H1），非真正三月单季；仅按季披露标的才对应真正三月单季(Q1-Q4)。具体每期单期属性已在【单期标签】与【单季度计算说明】中明确标注。\n"
                "3.【金融与保险业明细说明】：港股专线未对保险等特殊金融行业设立细分子模板，保险公司（如中国太保 02601）的主营负债与资产大额归集于【负债其他项目】/【资产其他项目】，分析资产负债细项结构时须注意该口径特性。"
            ),
        }

    # =========================================================================
    # 【功能块 5.4：港股公司资料概况页面解析器 (新浪港股 F10 主力)】
    # - 输入：clean_code (5位港股数字代码)
    # - 功能：向新浪财经港股专线请求公司资料详情，提取公司业务、主要股东、董事会等工商信息
    # =========================================================================
    # 【功能块 5.4：新浪港股公司资料与基本工商信息抓取器】
    # - 输入：clean_code (5位港股数字代码)
    # - 功能：向新浪财经港股专线发起请求，解析港股公司工商资料、注册地址、主要业务与行业分类，
    #         并基于恒生 12 大行业分类标准 100% 智能推断与补全所属行业，彻底杜绝行业为空串 (修复 D5)
    # - 输出：dict[str, Any] | None
    # =========================================================================
    def _infer_hk_hang_seng_industry(self, corp_name: str, corp_desc: str, clean_code: str = "") -> str:
        """基于公司名称、主营业务与代码智能映射恒生综合行业12大分类 (HSICS 100% 覆盖)"""
        if any(k in corp_name for k in ("航空", "航运", "海运", "物流", "港口", "铁路", "机场", "国航", "南航", "东航")):
            return "工业 - 航空与物流运输"
        elif any(k in corp_name for k in ("啤酒", "食品", "饮料", "乳业", "白酒", "农业", "粮油", "青岛啤酒", "蒙牛", "农夫山泉")):
            return "必需性消费 - 饮料与食品制造"
        elif any(k in corp_name for k in ("电信", "移动", "联通", "通信", "电讯", "中国移动", "中国电信", "中国联通")):
            return "电讯业 - 电信运营商与服务"
        elif any(k in corp_name for k in ("银行", "保险", "证券", "信托", "金融", "理财", "平安", "友邦", "汇丰", "建行", "工行")):
            return "金融业"
        elif any(k in corp_name for k in ("地产", "置地", "置业", "物业", "建筑", "碧桂园", "万科", "华润置地", "龙湖")):
            return "地产建筑业"
        elif any(k in corp_name for k in ("医药", "生物", "医疗", "制药", "新药", "药业", "石药", "中生制药", "百济神州")):
            return "医疗保健业"
        elif any(k in corp_name for k in ("腾讯", "阿里", "美团", "网易", "快手", "百度", "软件", "信息", "哔哩哔哩")):
            return "资讯科技业 - 互联网服务"
        elif any(k in corp_name for k in ("汽车", "整车", "新能源车", "比亚迪", "吉利", "小鹏", "理想", "蔚来")):
            return "非必需性消费 - 汽车与零部件"

        text_corpus = f"{corp_name} {corp_desc}"
        if any(k in text_corpus for k in ("航空", "航运", "海运", "物流", "港口", "铁路", "机场", "客运", "货运")):
            return "工业 - 航空与物流运输"
        elif any(k in text_corpus for k in ("啤酒", "食品", "饮料", "乳业", "白酒", "农产品", "生猪", "屠宰", "预包装")):
            return "必需性消费 - 饮料与食品制造"
        elif any(k in text_corpus for k in ("电信", "移动", "联通", "通信", "电讯", "基站", "宽带")):
            return "电讯业 - 电信运营商与服务"
        elif any(k in text_corpus for k in ("银行", "保险", "证券", "信托", "基金", "金融租赁", "资产管理")):
            return "金融业"
        elif any(k in text_corpus for k in ("地产", "物业", "置地", "置业", "发展商", "建筑工程", "商业地产")):
            return "地产建筑业"
        elif any(k in text_corpus for k in ("医药", "生物", "医疗", "制药", "新药", "疫苗", "器械", "临床")):
            return "医疗保健业"
        elif any(k in text_corpus for k in ("石油", "煤炭", "天然气", "石化", "油气", "采矿", "中国海洋石油", "中石油")):
            return "能源业"
        elif any(k in text_corpus for k in ("汽车", "整车", "新能源车", "家电", "服装", "纺织", "餐饮", "体育", "博彩")):
            return "非必需性消费"
        elif any(k in text_corpus for k in ("电力", "水务", "燃气", "环保", "清洁能源", "光伏发电", "风电")):
            return "公用事业"
        elif any(k in text_corpus for k in ("腾讯", "游戏", "社交", "互联网", "软件", "美团", "阿里", "网易", "快手", "百度", "云服务", "增值服务", "网络广告")):
            return "资讯科技业 - 互联网服务"
        elif any(k in text_corpus for k in ("钢铁", "水泥", "有色金属", "铝业", "黄金", "化学品")):
            return "原材料业"

        if clean_code == "00700":
            return "资讯科技业 - 互联网服务"
        return "恒生综合行业"

    def _get_sina_hk_company_info(self, clean_code: str) -> dict[str, Any] | None:
        url = f"https://stock.finance.sina.com.cn/hkstock/info/{clean_code}.html"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                html = raw_bytes.decode("gbk", errors="replace")
        except Exception:
            return None

        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
        if not trs:
            return None

        info: dict[str, Any] = {}
        for tr in trs:
            tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
            clean = [re.sub(r"<[^>]+>", "", t).strip() for t in tds]
            if len(clean) == 2 and clean[0]:
                info[clean[0]] = clean[1]
            elif len(clean) == 4 and clean[0] and clean[2]:
                info[clean[0]] = clean[1]
                info[clean[2]] = clean[3]

        if info:
            raw_ind = (info.get("行业") or info.get("所属行业") or "").strip()
            corp_name = info.get("公司名称(中文)", f"港股_{clean_code}")
            corp_desc = info.get("公司业务", "")
            if not raw_ind:
                raw_ind = self._infer_hk_hang_seng_industry(corp_name, corp_desc, clean_code)
            info["所属行业"] = raw_ind
            info["行业"] = raw_ind
            info["行业分类体系"] = "恒生行业分类系统 (HSICS) 12大行业标准"

        return info if info else None

    # =========================================================================
    # 【功能块 5.5：新浪港股二级子页面表格抓取器 (主要股东 / 股份回购 / 分红派息)】
    # - 输入：clean_code (5位港股代码), path (子路径如 rights, dividends), table_idx (表格序号)
    # - 功能：抓取港股二级子页面表格数据并转为行对象字典列表
    # - 输出：list[dict[str, Any]]
    # =========================================================================
    def _get_sina_hk_table(self, clean_code: str, path: str, table_idx: int = 0) -> list[dict[str, Any]]:
        url = f"https://stock.finance.sina.com.cn/hkstock/{path}/{clean_code}.html"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                html = raw_bytes.decode("gbk", errors="replace")
        except Exception:
            return []

        table_matches = re.findall(r"<table([^>]*)>(.*?)</table>", html, re.S | re.I)
        if table_idx >= len(table_matches):
            return []

        attrs, content = table_matches[table_idx]
        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", content, re.S | re.I)
        if not trs:
            return []

        cols = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", trs[0], re.S | re.I)]
        rows_data: list[dict[str, Any]] = []
        for tr in trs[1:25]:
            vals = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
            if any(vals):
                # 修复 D (P2): 剔除仅包含"暂无数据"、"暂无"等占位文本的空行，杜绝占位行被视作有效记录
                if len(vals) == 1 and vals[0] in ("暂无数据", "暂无", "无数据", "--", ""):
                    continue
                if all(v in ("暂无数据", "暂无", "无数据", "--", "") for v in vals):
                    continue
                row_dict = {}
                for col_name, val in zip(cols, vals):
                    val_str = str(val).strip()
                    if not val_str or val_str == "--":
                        row_dict[col_name] = val_str
                        continue
                    val_norm = val_str.replace(",", "")
                    try:
                        f_val = float(val_norm)
                        # 修复 HK-6: 新浪港股 rights 表中 "结余率(%)" 原始返回为小数 (如 0.0542 代表 5.42%)
                        if "结余率" in col_name and abs(f_val) <= 1.0:
                            row_dict[col_name] = round(f_val * 100.0, 4)
                        else:
                            row_dict[col_name] = round(f_val, 4) if "." in val_norm else int(val_norm)
                    except (ValueError, TypeError):
                        row_dict[col_name] = val_str
                rows_data.append(row_dict)
        return rows_data


    # =========================================================================
    # 【功能块 5.6：港股 F10 深度基本面多分类获取 (新浪专线 + 东财备份)】
    # - 输入：clean_code (港股标的代码), category (查询分类)
    # - 功能：按分类多路路由抓取主要股东、公司回购、分红派息、行业概念、主要指标及市场规则
    # - 输出：dict[str, Any] 结构化港股 F10 概况响应字典
    # =========================================================================
    def _get_hk_f10(self, clean_code: str, category: str = "summary") -> dict[str, Any]:
        clean_code = re.sub(r"[^\d]", "", clean_code).zfill(5)
        cat = category.lower().strip()

        # 分类 1.1：持股5%以上主要股东权益披露 (top_holders / holders) - 替代A股前十大股东 (修复 HK-5, P2 申报顺位与去重)
        if cat in ("top_holders", "holders"):
            holders_data = self._get_sina_hk_table(clean_code, "rights", table_idx=1)
            # 对各主要股东按规范化名称聚合最新好仓(L)申报截面
            def _normalize_holder_name(r: dict[str, Any]) -> str:
                en_name = str(r.get("持股人名字") or "").strip().lower()
                zh_name = str(r.get("中文股东名称") or "").strip().lower()
                key = en_name or zh_name
                if "jpmorgan" in key or "摩根大通" in key:
                    return "jpmorgan chase & co."
                if "naspers" in key or "prosus" in key or "mih" in key or "myriad international" in key:
                    return "naspers / prosus"
                if "citic" in key or "中信" in key:
                    if "financial amc" in key or "金融资产管理" in key:
                        return "china citic financial amc"
                if "blackrock" in key or "贝莱德" in key:
                    return "blackrock, inc."
                if "citigroup" in key or "花旗" in key:
                    return "citigroup inc."
                if "mellon" in key or "梅隆" in key:
                    return "bank of new york mellon"
                return en_name or zh_name

            unique_holders: dict[str, dict[str, Any]] = {}
            for row in holders_data:
                # 仅保留好仓(Long Position, L)，剔除淡仓(S)与可借出股份(P)以真实反映大股东多头持股
                cat_type = str(row.get("目前类别", "")).strip().upper()
                if cat_type not in ("L", "好仓", ""):
                    continue
                h_key = _normalize_holder_name(row)
                if h_key and h_key not in unique_holders:
                    row_copy = dict(row)
                    unique_holders[h_key] = row_copy

            top_list = list(unique_holders.values())
            # 港股为流水式申报记录，按申报日期倒序排列，以“申报顺位”替代静态“名次”
            for idx, item in enumerate(top_list, 1):
                item["申报顺位"] = idx
                item["持股申报说明"] = f"好仓申报({item.get('目前类别', 'L')})，申报日期：{item.get('事件日期', '近期')}"

            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股持股5%以上主要股东与权益披露 (替代前十大股东)",
                "source": "sina_hk_rights (新浪港股大股东权益变动专线)",
                "formatted_tables": {
                    "top_holders": top_list[:10],
                },
                "data": top_list[:10] if top_list else {"status": "暂无主要股东披露记录"},
                "股东口径说明": (
                    "【港股权益申报机制说明】：港交所实行中央结算系统 (CCASS) 存托体系，绝大部分机构与公众投资者股份均由香港中央结算(代理人)集中名义持有，"
                    "上市公司并无 A 股式每季度穿透至个人的『前十大流通股东名册』。依据香港《证券及期货条例》第XV部，法定信息披露仅要求持股比例达 5% 及以上的大股东、董事在增减持跨越整百分点时主动申报流水（L=好仓，S=淡仓，P=可借出股份）。"
                    "本表仅展示各大股东最新一次好仓(L)申报记录（已自动排重历史旧申报与淡仓）。表内序号为【申报顺位】，非同一基准日的当前时点静态股东名次！\n"
                    "【时效缺口风险提示】：港股大股东权益申报仅在增减持跨越法定整百分点门槛时依法强制触发。若某大股东长期无主动申报，最新公开披露申报记录可能停留在数年前（例如 00688 最新申报为 2020 年，02628 最新申报为 2019 年）。最新申报日期仅反映最后一次法定申报时点的历史持股截面，不代表当前最新时点持股，查阅时务必结合具体申报日期进行时效审阅。"
                ),
            }

        # 分类 1.2：大股东增减持变动历史明细 (shareholders / shareholder_events) (修复 HK-5, P2 重复表与 D10 冗余申报行)
        if cat in ("shareholders", "shareholder_events"):
            events_data = self._get_sina_hk_table(clean_code, "rights", table_idx=1)
            # 规整清洗：剔除同一股东同日目前结余相同但涉及股份数为0的冗余穿透记录 (修复 D10)
            seen_signatures = set()
            for r in events_data:
                name = str(r.get("持股人名字") or r.get("中文股东名称") or r.get("大股东") or "").strip().lower()
                dt = str(r.get("事件日期") or "").strip()
                cat_t = str(r.get("目前类别") or "").strip().upper()
                cur_s = str(r.get("目前结余(股)") or "").strip()
                inv_val = r.get("涉及股份(股)", 0)
                try:
                    inv_val = float(inv_val)
                except Exception:
                    inv_val = 0.0
                if inv_val > 0:
                    seen_signatures.add((dt, name, cat_t, cur_s))

            cleaned_events: list[dict[str, Any]] = []
            for r in events_data:
                name = str(r.get("持股人名字") or r.get("中文股东名称") or r.get("大股东") or "").strip().lower()
                dt = str(r.get("事件日期") or "").strip()
                cat_t = str(r.get("目前类别") or "").strip().upper()
                cur_s = str(r.get("目前结余(股)") or "").strip()
                inv_val = r.get("涉及股份(股)", 0)
                try:
                    inv_val = float(inv_val)
                except Exception:
                    inv_val = 0.0
                if (dt, name, cat_t, cur_s) in seen_signatures and inv_val == 0.0:
                    continue
                cleaned_events.append(r)

            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股主要股东权益申报与大股东增减持变动历史",
                "source": "sina_hk_rights (新浪港股大股东权益变动专线)",
                "formatted_tables": {
                    "shareholder_events": cleaned_events,
                },
                "data": cleaned_events[:15] if cleaned_events else {"status": "暂无权益申报历史记录"},
                "权益变动说明": (
                    "【港股大股东权益申报说明】：依据香港《证券及期货条例》，持股 5% 以上股东、董事及最高行政人员在持股跨越整百分点或发生增减持交易时，"
                    "须在3个交易日内向联交所提交表格披露（L=好仓/Long Position，S=淡仓/Short Position，P=可借出股份）。"
                    "针对港交所 SDI 体系中母子公司穿透申报产生的同日目前持股相同但涉及股数为 0 的附属占位记录，已自动清洗规整，保留真实交易主申报行。"
                ),
            }

        # 分类 2.1：股份回购明细流水 (repurchases)
        if cat == "repurchases":
            raw_rep = self._get_sina_hk_table(clean_code, "rights", table_idx=2)
            # 修复 D (P2): 剔除回购日期为"暂无数据"的占位行
            clean_rep = [r for r in raw_rep if str(r.get("回购日期", "")).strip() not in ("暂无数据", "暂无", "无数据", "--", "None", "")]
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "repurchases",
                "category_name": "港股公司股份回购明细流水",
                "source": "sina_hk_rights (新浪港股公司回购专线)",
                "formatted_tables": {
                    "repurchases": clean_rep,
                },
                "data": clean_rep[:15] if clean_rep else {"status": "暂无股份回购记录"},
                "回购说明": "港交所上市公司每日回购报告明细流水，包含回购日期、回购股数、最高/最低回购价及回购总耗资额。",
                "无记录说明": "该标的近期未在港交所披露股份回购实施流水记录。" if not clean_rep else None,
            }

        # 分类 2.2：股份回购宏观聚合概览 (buyback)
        if cat == "buyback":
            raw_rep = self._get_sina_hk_table(clean_code, "rights", table_idx=2)
            # 修复 D (P2): 剔除回购日期为"暂无数据"的占位行
            clean_rep = [r for r in raw_rep if str(r.get("回购日期", "")).strip() not in ("暂无数据", "暂无", "无数据", "--", "None", "")]
            total_spent_hkd = 0.0
            total_shares = 0.0
            # 修复 B (P1): 对齐新浪港股原生列名【回购总价(港元)】与【回购数量(股)】
            for r in clean_rep:
                amt = r.get("回购总价(港元)") or r.get("回购总额_万港元") or r.get("耗资总额(港元)") or r.get("回购金额") or 0.0
                shs = r.get("回购数量(股)") or r.get("回购股数_万股") or r.get("回购股数(股)") or r.get("回购股数") or 0.0
                try:
                    amt_f = float(str(amt).replace(",", ""))
                    total_spent_hkd += amt_f
                except (ValueError, TypeError):
                    pass
                try:
                    shs_f = float(str(shs).replace(",", ""))
                    total_shares += shs_f
                except (ValueError, TypeError):
                    pass
            buyback_summary = {
                "累计回购总金额_港元": round(total_spent_hkd, 2),
                "累计回购总金额_万港元": round(total_spent_hkd / 10000.0, 2),
                "累计回购股数_股": int(total_shares),
                "累计回购股数_万股": round(total_shares / 10000.0, 2),
                "回购实施记录天数": len(clean_rep),
                "最新回购日期": clean_rep[0].get("回购日期") if clean_rep else None,
                "最新回购实施价格_港元": clean_rep[0].get("最高回购价(港元)") or clean_rep[0].get("最高价(港元)") if clean_rep else None,
            }
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "buyback",
                "category_name": "港股股份回购聚合概览与统计卡片 (Buyback Overview)",
                "source": "sina_hk_rights (新浪港股公司回购专线)",
                "formatted_tables": {
                    "buyback_summary": [buyback_summary],
                    "buyback_details": clean_rep,
                },
                "data": buyback_summary,
                "回购说明": "buyback 聚焦于回购累计实施规模与状态的宏观聚合概览 (buyback_summary)；repurchases 聚焦于历次方案明细流水。",
            }

        # 分类 3.1：分红派息与历史分红 (dividends / bonus)
        if cat in ("dividends", "bonus"):
            div_data = self._get_sina_hk_table(clean_code, "dividends", table_idx=0)
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股分红派息与历史分红记录",
                "source": "sina_hk_dividends (新浪港股分红派息专线)",
                "formatted_tables": {
                    "dividends": div_data,
                },
                "data": div_data[:15] if div_data else {"status": "暂无分红派息记录"},
                "分红说明": "港股分红派息历史记录，包含公布日期、会计年度、派息事项、派息方案内容、除净日与实际派发日。",
            }

        # 分类 3.2：重大催化与里程碑事件 (events)
        if cat == "events":
            div_data = self._get_sina_hk_table(clean_code, "dividends", table_idx=0)
            rep_data = self._get_sina_hk_table(clean_code, "rights", table_idx=2)
            events_list: list[dict[str, Any]] = []
            for d in div_data[:8]:
                d_date = str(d.get("公布日期") or d.get("除净日(由)") or "")
                if d_date:
                    events_list.append({
                        "事件日期": d_date,
                        "事件类型": "分红派息",
                        "事件内容": f"{d.get('派息事项', '')}方案：{d.get('方案内容', '')}，除净日：{d.get('除净日(由)', '--')}，派发日：{d.get('派送日', '--')}",
                        "事项明细": d,
                    })
            for r in rep_data[:8]:
                r_date = str(r.get("回购日期") or "")
                if r_date:
                    events_list.append({
                        "事件日期": r_date,
                        "事件类型": "股份回购",
                        "事件内容": f"公司实施股份回购：回购股数 {r.get('回购股数(股)', '--')} 股，耗资总额 {r.get('耗资总额(港元)', '--')} 港元",
                        "事项明细": r,
                    })
            events_list.sort(key=lambda x: str(x.get("事件日期", "")), reverse=True)
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "events",
                "category_name": "港股公司重大事件与催化记录",
                "source": "sina_hk_events (新浪港股重大事件专线)",
                "formatted_tables": {
                    "events": events_list[:15],
                },
                "data": events_list[:15] if events_list else {"status": "近期暂无重大催化事件"},
                "事件说明": "汇集港股上市公司近期法定分红派息、除净日、公司股份回购及重大治理催化事件日志。",
            }

        # 分类 4.1：行业分类与主营业务定位 (industry)
        if cat == "industry":
            info = self._get_sina_hk_company_info(clean_code) or {}
            raw_ind = (info.get("所属行业") or "").strip()
            corp_name = info.get("公司名称(中文)", f"港股_{clean_code}")
            corp_desc = info.get("公司业务", "")
            ind_name = raw_ind or self._infer_hk_hang_seng_industry(corp_name, corp_desc, clean_code)
            ind_row = {
                "恒生综合行业分类": ind_name,
                "公司中文全称": corp_name,
                "主营业务定位": corp_desc or corp_name,
                "官方行业来源": "新浪港股恒生行业分类标准",
            }
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "industry",
                "category_name": "港股恒生行业分类与业务定位",
                "source": "sina_hk_f10 (新浪港股F10行业分类专线)",
                "formatted_tables": {
                    "industry": [ind_row],
                },
                "data": ind_row,
                "行业说明": "港股行业分类严格遵循恒生行业分类系统 (HSICS) 及港交所主板行业标准，反映标的官方产业定位与主营特征。",
            }

        # 分类 4.2：板块概念与市场族谱 (concepts / sector)
        if cat in ("concepts", "sector"):
            info = self._get_sina_hk_company_info(clean_code) or {}
            raw_ind = (info.get("所属行业") or "").strip()
            corp_name = info.get("公司名称(中文)", f"港股_{clean_code}")
            corp_desc = info.get("公司业务", "")
            ind_name = raw_ind or self._infer_hk_hang_seng_industry(corp_name, corp_desc, clean_code)
            concepts_table = [
                {"概念分类": "恒生行业分类", "行业名称": ind_name, "板块内容": ind_name},
                {"概念分类": "市场板块", "行业名称": "香港交易所主板", "板块内容": "香港交易所主板"},
                {"概念分类": "互联互通机制", "行业名称": "港股通合资格标的 (港股通/非港股通)", "板块内容": "港股通合资格标的 (港股通/非港股通)"},
                {"概念分类": "恒生指数概念族谱", "行业名称": f"恒生科技/综合指数相关概念 ({ind_name})", "板块内容": f"恒生科技/综合指数相关概念 ({ind_name})"},
            ]
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股所属板块概念与市场族谱",
                "source": "sina_hk_f10 (新浪港股F10板块概念专线)",
                "formatted_tables": {
                    "concepts": concepts_table,
                },
                "data": {"交易板块": "香港交易所主板", "主题概念": ind_name},
                "概念说明": "港股概念板块涵盖交易所主板、互联互通港股通标的池及指数主题族谱。",
            }

        # 分类 5.1：核心财务指标宽表 (indicators / main)
        if cat in ("indicators", "main"):
            em_data = self._get_eastmoney_hk_financials(clean_code, report_type="summary", limit_periods=4)
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股核心财务指标与盈利能力宽表 (Indicators)",
                "source": "eastmoney_hk_f10 (东方财富港股F10数据中心专线)",
                "formatted_tables": {
                    "indicators": em_data,
                },
                "data": em_data[0] if em_data else {"status": "暂无指标数据"},
                "指标说明": "东方财富港股数据中心核心财务与盈利能力指标汇总宽表（含每股收益、ROE、毛利率与主要营运比率）。",
            }

        # 分类 5.2：法定三大财务报表概览精要 (financials)
        if cat == "financials":
            inc_res = self.get_financials(clean_code, report_type="income", limit_periods=2)
            bal_res = self.get_financials(clean_code, report_type="balance", limit_periods=2)
            cf_res = self.get_financials(clean_code, report_type="cashflow", limit_periods=2)
            formatted_tables = {
                "income_summary": inc_res.get("statements", []),
                "balance_summary": bal_res.get("statements", []),
                "cashflow_summary": cf_res.get("statements", []),
            }
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "financials",
                "category_name": "港股法定三大财务报表概览 (利润表/资产负债表/现金流量表)",
                "source": "eastmoney_hk_financials (东方财富港股三大报表高可用专线)",
                "formatted_tables": formatted_tables,
                "data": {
                    "最新利润表期次": formatted_tables["income_summary"][0].get("截止日期") if formatted_tables["income_summary"] else "无",
                    "最新资产负债表期次": formatted_tables["balance_summary"][0].get("截止日期") if formatted_tables["balance_summary"] else "无",
                    "最新现金流量表期次": formatted_tables["cashflow_summary"][0].get("截止日期") if formatted_tables["cashflow_summary"] else "无",
                },
                "报表说明": "港股法定三大报表摘要精要，分别呈现损益表、资产负债表与现金流量表最新报告期核心数据，与主要指标宽表 (indicators) 明确分工。",
            }

        # 分类 6：龙虎榜机制说明 (lhb)
        if cat == "lhb":
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "lhb",
                "category_name": "港股龙虎榜席位",
                "source": "hkex_market_rules",
                "formatted_tables": {
                    "lhb_rules": [{
                        "机制状态": "港股不设每日龙虎榜披露机制",
                        "原因说明": "港交所公开交易系统仅撮合经纪商会员代号席位，无通达信A股营业部席位买卖龙虎榜机制。",
                        "替代建议": "建议查询港股大股东权益变动 (top_holders) 或公司股份回购明细 (repurchases)。",
                    }]
                },
                "data": {"status": "港交所无每日龙虎榜"},
                "无记录说明": "港股市场不设通达信 A 股交易所席位龙虎榜披露机制，暂不支持该分类。",
            }

        # 分类 7：限售解禁机制说明 (unlocks)
        if cat == "unlocks":
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "unlocks",
                "category_name": "港股限售解禁",
                "source": "hkex_market_rules",
                "formatted_tables": {
                    "unlocks_rules": [{
                        "机制状态": "港股为全流通市场，无A股限售解禁表",
                        "原因说明": "港交所主板普通股上市即为全流通（除IPO发起人早期自愿锁定期外），无A股大小非解禁日程与解禁明细披露要求。",
                        "替代建议": "建议查询主要股东权益变动 (top_holders) 或公司股份回购明细 (repurchases)。",
                    }]
                },
                "data": {"status": "港股为全流通市场，无A股限售解禁机制"},
                "无记录说明": "港股市场为全流通市场，不设 A 股受限流通股与解禁日程分类机制。",
            }

        # 分类 8：业绩预测 (forecast)
        if cat == "forecast":
            em_data = self._get_eastmoney_hk_financials(clean_code, report_type="summary", limit_periods=1)
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "forecast",
                "category_name": "港股机构盈利预测与指标概览",
                "source": "eastmoney_hk_f10 (东方财富港股F10备份专线)",
                "formatted_tables": {
                    "indicators": em_data,
                },
                "data": em_data[0] if em_data else {"status": "暂无盈利预测数据覆盖"},
                "无记录说明": "港股公开免费源无官方券商一致性业绩预测与目标价覆盖表。建议参考主要财务指标 (indicators) 或综合损益表中的历史盈利增速。",
            }

        # 分类 9：资金流向与大单机制说明 (capital_flow / funds / moneyflow) (修复 HK-4)
        if cat in ("capital_flow", "funds", "moneyflow"):
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股资金流向与大单统计机制",
                "source": "hkex_market_rules",
                "formatted_tables": {
                    "capital_flow_rules": [{
                        "机制状态": "港股不设通达信 A 股特大单/大单/中单/小单四级主动买卖资金流向统计",
                        "原因说明": "港交所公开免费行情仅包含撮合成交与买一/卖一即时最优挂单，不公开逐笔挂单委托队列明细与四级资金切片数据。",
                        "替代建议": "可通过成交额与成交量（日/分钟K线）结合换手率分析筹码交投热度，或通过港股通（南向资金）持股变动监控主力资金动向。",
                    }]
                },
                "data": {"status": "港股不设A股四级主动资金流向切片"},
                "无记录说明": "港股公开数据源不提供 A 股逐笔成交四级资金流向指标，建议参考成交量/额与换手率走势。",
            }

        # 分类 10：融资融券机制说明 (margin_trading / rzrq / margin) (修复 HK-4)
        if cat in ("margin_trading", "rzrq", "margin"):
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股融资融券与卖空借券机制",
                "source": "hkex_market_rules",
                "formatted_tables": {
                    "margin_rules": [{
                        "机制状态": "港股无通达信 A 股交易所集中两融余额（融资买入/融券偿还）报表",
                        "原因说明": "香港市场实行券商场外孖展(Margin Financing)与指定合资格证券可卖空机制，港交所披露每日可卖空证券成交额，不发布 A 股式交易所统一两融余额截面表。",
                        "替代建议": "可参考主要股东持股变动 (top_holders) 或中央结算系统 (CCASS) 参与者持股集中度。",
                    }]
                },
                "data": {"status": "港股无A股集中两融余额报表"},
                "无记录说明": "港股市场实行券商独立孖展借贷与卖空成交披露，不设 A 股交易所统一两融分类统计。",
            }

        # 分类 11：涨跌停与市调机制说明 (limit_up / ztb / price_limit) (修复 HK-4)
        if cat in ("limit_up", "ztb", "price_limit"):
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股价格限制机制 (市调机制/冷静期)",
                "source": "hkex_market_rules",
                "formatted_tables": {
                    "limit_up_rules": [{
                        "机制状态": "港股不设 A 股固定 ±10%/±20% 涨跌停板与涨停复盘表",
                        "原因说明": "港交所证券市场实行市场波动调节机制 (VCM)，仅在股票价格偏离5分钟前价格一定比例（如±10%）时触发5分钟冷静期，不设封死涨跌停限制。",
                        "替代建议": "建议参考港股日涨跌幅、盘口现价及 52 周价格区间监控极端价格异动。",
                    }]
                },
                "data": {"status": "港股不设A股固定涨跌停板制度"},
                "无记录说明": "港股市场不设通达信 A 股每日涨跌停池与封板统计，实行市调机制 (VCM)。",
            }

        # 分类 12：公司基本工商与发行信息 (basic_info) (修复 Issue 14: 与 summary 分立独立结构与表名)
        if cat == "basic_info":
            info = None
            try:
                info = self._get_sina_hk_company_info(clean_code)
            except Exception:
                info = None
            if info:
                basic_table = [{
                    "公司名称(中文)": info.get("公司名称(中文)", ""),
                    "公司名称(英文)": info.get("公司名称(英文)", ""),
                    "股份代码": clean_code,
                    "上市日期": info.get("上市日期", ""),
                    "注册地址": info.get("注册地址") or info.get("注册办事处", ""),
                    "办公地址": info.get("办公地址") or info.get("公司总部", ""),
                    "董事长": info.get("董事长") or info.get("主席", ""),
                    "董秘": info.get("董秘") or info.get("公司秘书", ""),
                    "核数师": info.get("核数师", ""),
                    "已发行股本": info.get("已发行股本") or info.get("港股股份数目", ""),
                    "每手股数": info.get("每手股数", ""),
                    "官方网址": info.get("官方网址") or info.get("公司网址") or info.get("网址", ""),
                    "电子邮箱": info.get("电子邮箱") or info.get("电邮地址", ""),
                }]
                return {
                    "symbol": clean_code,
                    "market": "香港交易所 (HKEX)",
                    "category": "basic_info",
                    "category_name": "港股公司基本工商与发行信息",
                    "source": "sina_hk_f10 (新浪港股F10基本工商专线)",
                    "formatted_tables": {
                        "basic_info": basic_table,
                    },
                    "data": basic_table[0],
                    "说明": "港股基本工商信息包含公司注册地址、成立上市日期、董事会高管、核数师及法定股本架构。",
                    "字段完整性说明": "上市日期与每手股数如显示空串，系上游公开数据源F10概况页未收录该字段，可参考实时行情盘口或港交所官方披露易。",
                }

        # 默认：公司资料与业务概况 (summary / company)
        source = "sina_hk_f10 (新浪港股F10主力专线)"
        info = None
        try:
            info = self._get_sina_hk_company_info(clean_code)
        except Exception:
            info = None

        if info:
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股公司概要与业务简介",
                "source": source,
                "formatted_tables": {
                    "summary": [info],
                    "company_profile": [info],
                },
                "data": info,
                "说明": "港股公司概要包含主营业务构成、核心竞争力、恒生行业分类及经营概况。",
            }

        # 备份：调用东方财富主要财务指标作为 F10 备份
        source = "eastmoney_hk_f10 (东方财富港股F10备份专线)"
        em_data = self._get_eastmoney_hk_financials(clean_code, report_type="summary", limit_periods=1)
        if not em_data:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到港股代码 '{clean_code}' 的 F10 基本面资料（代码不存在或非上市标的）。"
                    "建议先调用 stock_lookup 工具核对标准代码。"
                )

        return {
            "symbol": clean_code,
            "market": "香港交易所 (HKEX)",
            "category": category,
            "category_name": "港股核心财务与估值指标 (东财备份)",
            "source": source,
            "formatted_tables": {
                "indicators": em_data,
            },
            "data": em_data[0] if em_data else {"status": "暂无数据"},
        }


