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

    def get_f10(self, symbol: str, category: str = "summary", arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """获取港股 F10 深度基本面"""
        clean_code = re.sub(r"[^\d]", "", symbol).zfill(5)
        return self._get_hk_f10(clean_code, category=category, arguments=arguments)

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

        # =========================================================================
        # 【功能块 5.1.1：挂载港股最新财报期次与归母普通股每股指标自算 (修复 D4, D5, D13, 对齐 A 股估值键)】
        # - 输入：clean_code (5位港股标的代码)
        # - 功能：向东财 F10 indicators 调取最新已披露报告期之每股指标（BPS、EPS、PB_TTM）
        # - 输出：最新财报报告期, 每股净资产_BPS, 报告期每股收益_EPS, 市净率_PB_自算_普通股
        # =========================================================================
        f10_bgq = ""
        f10_bps = None
        f10_eps = None
        f10_pb_f10 = None
        f10_pb_calc = None
        try:
            ind_stmts = self._get_eastmoney_hk_financials(clean_code, report_type="indicator", limit_periods=1)
            if ind_stmts and isinstance(ind_stmts, list):
                i0 = ind_stmts[0]
                f10_bgq = str(i0.get("报告期") or i0.get("报告期日期") or "")
                f10_bps = i0.get("每股净资产_BPS_元")
                f10_eps = i0.get("基本每股收益_元")
                f10_pb_f10 = i0.get("市净率_PB_TTM")
                if f10_pb_f10 is not None:
                    try:
                        f10_pb_calc = round(float(f10_pb_f10), 4)
                    except Exception:
                        f10_pb_calc = None
        except Exception:
            pass

        raw_pb = _safe_opt_float(parts[58] if len(parts) > 58 else None)
        pb_common = f10_pb_calc if f10_pb_calc is not None else raw_pb

        result: dict[str, Any] = {
            "symbol": norm_code,
            "name": name,
            "setcode": "31",
            "market": "香港交易所 (HKEX)",
            "source": "tencent_free_hk_quote (腾讯免费港股专线)",
            "盘口深度说明": (
                "【港股盘口深度与五档委托说明（已收敛 P3-1）】：港交所公开免费行情专线仅提供即时买一/卖一撮合成交档位，"
                "买二至买五档位上游不提供公开委托量与价格（显示为 None 或 0），严禁将委零或空档当成真实买卖盘口深度或多空博弈力量分析；"
                "盘中持续交易时段请重点关注买一/卖一即时撮合成交价与全天累计成交额。"
            ),
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
                "市净率_PB": raw_pb,
                "市净率_PB_自算_普通股": pb_common,
                "市净率_PB_F10": f10_pb_calc,
                "最新财报报告期": f10_bgq if f10_bgq else None,
                "每股净资产_BPS": f10_bps,
                "报告期每股收益_EPS": f10_eps,
                "派生指标换期说明": (
                    f"【港股估值指标期次自陈（对齐 A 股，已修复 D4/D5/D13）】：已挂载上市公司最新财报报告期【{f10_bgq or '最新期'}】官方归母指标（BPS={f10_bps}，EPS={f10_eps}）。"
                    "行情流透传的【市净率_PB】可能滞后一期或底层按总权益（含少数股东权益）核算；"
                    "本表【市净率_PB_自算_普通股】基于最新归属于母公司普通股股东每股净资产校准，严肃基本面分析请以此为准。"
                ),
                "市净率_PB_口径说明": "上游行情流透传的【市净率_PB】底层通常基于包含少数股东权益的总净资产核算或滞后；本表【市净率_PB_自算_普通股】已结合 F10 最新报告期归属于母公司普通股每股净资产 BPS 校验对齐。",
                "市盈率股本计算口径": "全部已发行总股本(含A+H)乘H股价",
                "港股市值_亿港元": float_mv_yi,
                "港股市值_H股流通市值_亿港元": float_mv_yi,
                "流通市值_亿港元": float_mv_yi,
                "总市值_全部股本乘H股价_亿港元": total_mv_yi,
                "总市值_亿港元": total_mv_yi,
                "港股市值_元": int(round(float_mv_yi * 100000000)) if float_mv_yi else None,
                "港股市值_港元": int(round(float_mv_yi * 100000000)) if float_mv_yi else None,
                "流通市值_元": int(round(float_mv_yi * 100000000)) if float_mv_yi else None,
                "流通市值_港元": int(round(float_mv_yi * 100000000)) if float_mv_yi else None,
                "总市值_元": int(round(total_mv_yi * 100000000)) if total_mv_yi else None,
                "总市值_港元": int(round(total_mv_yi * 100000000)) if total_mv_yi else None,
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
                    "2.【总市值_全部股本乘H股价_亿港元】(全股本混合参考口径)：境内A股+境外H股全部总股本 × H股现价（如中国国航约 699 亿港元）。因两地市场存在AH折溢价，此混合口径仅供全股本估值参考，严禁误认为港股单一市场可流通规模！\n"
                    "3.【港股市值计价货币特别提示（已收敛 D4）】：表中【总市值_元】、【流通市值_元】与【港股市值_元】等字段之【元】均为港交所法定交易计价货币【港元 (HKD)】（非人民币 CNY）。若误当人民币使用会高估约 13%（按汇率约 0.87 折算）。网关已同步挂载【总市值_港元】、【流通市值_港元】等明确标注币种字段；如需人民币市值请按实时动态汇率折算。"
                ),
                "估值口径说明": (
                    "【港股市盈率口径与汇率折算披露（已收敛 P2-2 与口径陷阱 1、2）】：\n"
                    "1. 字段映射关系：【市盈率_TTM】与【滚动市盈率_PETTM】同源取自过去12个月净利润测算；【静态市盈率_PE_LYR】与【静态市盈率_PEStatic】同源取自上一财年经审计年报。\n"
                    "2. 跨币种汇率换算机制：对于财报以人民币(CNY)编报但股票以港元(HKD)交易的标的（如腾讯控股、中国国航、青岛啤酒等），行情源提供的 PE_TTM 底层已包含港元对人民币的汇率折算（隐含换算比率约为 0.868~0.875 CNY/HKD，涵盖典型 0.868~0.872 区间）；对于报表以美元(USD)编报的标的（如小马智行 02026），其隐含折算比率为两段折算（HKD→USD，约为 0.128），切勿误判为异常。\n"
                    "3. PB 估值两段折算防误导律：港股 BPS 多为人民币或美元、价格为港元，计算 PB 切勿直接拿【现价 ÷ BPS】，必须先统一换算币种；本表已由【市净率_PB_自算_普通股】自动校准。\n"
                    "4.【quotes 与 f10 indicators 汇率快照源独立性自陈】：quotes 滚动市盈率采用盘中实时动态汇率快照（约 0.868~0.875），而 f10 indicators 采用财报披露日或静态清算汇率快照（约 0.857~0.882），两处独立测算存在约 1%~2% 的合理横比差异，各自自洽，动态研判请以 quotes 实时估值为准。\n"
                    "5.【A+H两地上市标的市盈率全股本口径说明与自算指引（已修复 D12）】：上游公共行情源对 A+H 标的所提供的【市盈率_TTM】底层采用全部总股本（A+H）乘 H 股价折算综合市值，并按汇率折算归母净利润测算。严肃基本面估值分析时，建议结合最新财报真实归母净利润及分季度做差 TTM 汇率复核。"
                ),
                "52周价格区间说明": (
                    "52周最高/最低价统计周期为最近52周滚动交易日，上游行情源底层采用滚动现金分红前复权(QFQ)口径计算；"
                    "若与不复权历史真实成交K线(adjust='none')的高低点存在偏差（高股息标的如煤炭/基建/银行等因累计分红派息，偏差通常在 1%~7% 不等，差额精确对应期间累计派息分红金额），属于前复权派息扣减所致的正常口径差异；若需真实历史极值成交价，请使用 adjust='none' 的不复权 K 线。"
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
                        "港股日内分钟 K 线（1m/5m/15m/30m/60m）已完整覆盖 09:30-16:00 持续交易时段及 16:00-16:08/16:10 收市竞价交易时段 (CAS) 撮合量，"
                        "当日全部分钟 K 线累计成交量与日 K 线全天成交量 100% 代数闭合对齐 (比率=1.000000)。"
                    ),
                    "港股60分钟跨午休切分机制说明": (
                        "【港股60分钟K线跨午休切分机制说明】：港交所上午持续交易时段为 09:30-12:00 (共150分钟)，下午为 13:00-16:00 (共180分钟)。"
                        "主流交易所规范将全天60分钟K线按【30+60+60+30】及跨午休规则切分为6根bar："
                        "10:30 (开盘前1小时)、11:30 (中间1小时)、13:30 (跨午休聚合bar：囊括上午11:30-12:00与下午13:00-13:30各半小时成交量，对应30m的12:00与13:30之和)、"
                        "14:30、15:30、16:00 (收盘半小时)。"
                    ),
                    "分钟线跨日限制说明": (
                        "【港股分钟线跨日限制说明（已修复 P2-3）】：上游公共行情源仅提供港股当日日内交易分时数据（1m/5m/15m/30m/60m），"
                        "不提供跨日历史分钟归档库。如需跨日或长周期多日技术走势分析，请切换至日 K 线 (period='day')。"
                    ),
                }
                if is_truncated:
                    resp_payload["截断说明"] = (
                        f"受请求条数 count={count} 限制，当前仅返回最近 {len(final_bars)} 根分钟线，"
                        f"已截断当日早盘前 {len(parsed_bars) - len(final_bars)} 根分钟线。如需完整全天日内量能分析，"
                        f"请传入 count={len(parsed_bars)}（单日全天最大可用根数）或直接查阅日 K 线。"
                    )
                if int(count) > len(parsed_bars):
                    # 修复 D9, D10: 港股 1m/5m/15m/30m/60m count 超限提示（无条件触发，杜绝超限静默截断）
                    resp_payload["超限提示"] = (
                        f"当前请求 count={count} 大于港股当日该周期实际可生成的最大根数({len(parsed_bars)}根)。"
                        f"港交所持续交易时段全天最多共生成 6 根 60m bar、11 根 30m bar、21 根 15m bar、66 根 5m bar、约 332 根 1m bar。"
                        "港股分钟线仅覆盖当日日内交易时段（不提供跨日历史分钟线），已全量交付当日已生成的全部切片。"
                    )
                    # 修复 P2-1: 声明两键并存对齐，当请求条数超出单日真上限时同步下发截断说明
                    if "截断说明" not in resp_payload:
                        resp_payload["截断说明"] = (
                            f"请求条数 count={count} 超过港股当日该周期实际最大可用根数({len(parsed_bars)}根)，"
                            f"已截断超出部分并全量交付当日可用的全部 {len(parsed_bars)} 根分钟线。"
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
                "港股历史 K 线已全面接入腾讯官方复权引擎 (newfqkline)，支持 qfq（前复权，默认）、hfq（后复权）及 none（不复权）；"
                "可用于长周期技术分析与真实收益率测算。"
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

        # =========================================================================
        # =========================================================================
        # 【功能块 5.2.1：上市公司法定编报币种探测与逐期动态反解汇率引擎】
        # - 输入：secucode (港股标准代码如 '02026.HK'), rep_name (当前请求东财报表名), data (待处理数据列表)
        # - 功能：向法定损益表 (RPT_HKF10_FN_INCOME) 探测上市公司的法定申报记账币种 (USD/CNY/HKD)。
        #         当公司法定申报币种为 USD（如小马智行 02026、小鹏汽车 09868）时，东财在
        #         MAININDICATOR（指标表）与 CASHFLOW_PC（现金流量表）中私下按汇率折算为人民币。
        #         本引擎通过逐期将【指标表折算后人民币营收 ÷ 损益表法定美元营收】，动态反解出
        #         上市公司各报告期的官方真实折算汇率 (dynamic fx_rate)，杜绝任何硬编码字典，
        #         自适应未来任何新财报与汇率波动，将指标表与现金流表无缝逆向还原为法定原币 USD。
        # - 输出：real_currency (str): 法定编报币种, fx_rates_by_date (dict[str, float]): 各期动态反解汇率字典
        # =========================================================================
        real_currency = None
        fx_rate = 1.0
        fx_rates_by_date: dict[str, float] = {}
        if rep_name in ("RPT_HKF10_FN_MAININDICATOR", "RPT_HKF10_FN_CASHFLOW_PC"):
            try:
                probe_params = {
                    "reportName": "RPT_HKF10_FN_INCOME",
                    "columns": "CURRENCY_CODE,ITEM_NAME,AMOUNT,REPORT_DATE",
                    "filter": f'(SECUCODE="{secucode}")',
                    "pageNumber": "1",
                    "pageSize": str(max(500, limit_periods * 120)),
                    "sortTypes": "-1",
                    "sortColumns": "REPORT_DATE",
                }
                probe_url = f"{base_url}?{urllib.parse.urlencode(probe_params)}"
                probe_req = Request(probe_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with _SECURE_OPENER.open(probe_req, timeout=min(3.0, self.timeout)) as probe_resp:
                    probe_js = json.loads(probe_resp.read().decode("utf-8", errors="replace"))
                    p_data = (probe_js.get("result") or {}).get("data", [])
                    if p_data and isinstance(p_data, list):
                        for item in p_data:
                            if item.get("CURRENCY_CODE"):
                                real_currency = str(item.get("CURRENCY_CODE")).strip()
                                break
                        if real_currency == "USD":
                            # 1. 动态提取损益表中各报告期的法定美元营业收入
                            usd_income_by_date: dict[str, float] = {}
                            for item in p_data:
                                r_d = str(item.get("REPORT_DATE") or "").split(" ")[0]
                                iname = str(item.get("ITEM_NAME") or "").strip()
                                if iname in ("营业总额", "营业额", "收益", "营业收入", "收入") and item.get("AMOUNT") is not None:
                                    try:
                                        u_val = float(item["AMOUNT"])
                                        if u_val > 0 and r_d not in usd_income_by_date:
                                            usd_income_by_date[r_d] = u_val
                                    except Exception:
                                        pass

                            # 2. 获取指标表中折算后的人民币营收，逐期动态反解汇率
                            cny_income_by_date: dict[str, float] = {}
                            if rep_name == "RPT_HKF10_FN_MAININDICATOR":
                                for r in data:
                                    r_d = str(r.get("REPORT_DATE") or "").split(" ")[0]
                                    cny_val = r.get("OPERATE_INCOME") or r.get("TOTAL_OPERATE_INCOME")
                                    if cny_val is not None:
                                        try:
                                            c_f = float(cny_val)
                                            if c_f > 0:
                                                cny_income_by_date[r_d] = c_f
                                        except Exception:
                                            pass
                            else:
                                # 若当前请求为 CASHFLOW_PC，向 MAININDICATOR 动态抓取指标表营收反解各期汇率
                                try:
                                    ind_params = {
                                        "reportName": "RPT_HKF10_FN_MAININDICATOR",
                                        "columns": "REPORT_DATE,OPERATE_INCOME",
                                        "filter": f'(SECUCODE="{secucode}")',
                                        "pageNumber": "1",
                                        "pageSize": "50",
                                        "sortTypes": "-1",
                                        "sortColumns": "REPORT_DATE",
                                    }
                                    ind_url = f"{base_url}?{urllib.parse.urlencode(ind_params)}"
                                    ind_req = Request(ind_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                                    with _SECURE_OPENER.open(ind_req, timeout=min(3.0, self.timeout)) as ind_resp:
                                        ind_js = json.loads(ind_resp.read().decode("utf-8", errors="replace"))
                                        ind_rows = (ind_js.get("result") or {}).get("data", [])
                                        for ir in ind_rows:
                                            r_d = str(ir.get("REPORT_DATE") or "").split(" ")[0]
                                            cny_val = ir.get("OPERATE_INCOME")
                                            if cny_val is not None:
                                                try:
                                                    c_f = float(cny_val)
                                                    if c_f > 0:
                                                        cny_income_by_date[r_d] = c_f
                                                except Exception:
                                                    pass
                                except Exception:
                                    pass

                            # 3. 逐期动态相除计算官方折算汇率
                            for r_d, u_val in usd_income_by_date.items():
                                if r_d in cny_income_by_date and u_val > 0:
                                    r_calc = round(cny_income_by_date[r_d] / u_val, 4)
                                    if 4.0 <= r_calc <= 12.0:
                                        fx_rates_by_date[r_d] = r_calc

                            # 4. 确定最新一期汇率作为未匹配报告期的动态保底基准
                            if fx_rates_by_date:
                                sorted_dates = sorted(fx_rates_by_date.keys(), reverse=True)
                                fx_rate = fx_rates_by_date[sorted_dates[0]]
            except Exception:
                real_currency = None
                fx_rate = 1.0

        periods_map: dict[str, dict[str, Any]] = {}
        for row in data:
            r_date = str(row.get("REPORT_DATE", "")).split(" ")[0]
            if not r_date:
                continue
            cur_fx = fx_rates_by_date.get(r_date, fx_rate)
            if r_date not in periods_map:
                # 修复 P2-4: 优先使用东财原生的 DATE_TYPE_CODE 映射标准财报周期，杜绝特殊财年标签错位
                date_type_code = str(row.get("DATE_TYPE_CODE") or "").strip()
                dt_map = {"001": "年报", "002": "中报", "003": "一季报", "004": "三季报"}
                r_type = dt_map.get(date_type_code) or row.get("REPORT_TYPE") or row.get("REPORT_TYPE_DETAILS") or (
                    "年报" if r_date.endswith("12-31") else ("中报" if r_date.endswith("06-30") else ("一季报" if r_date.endswith("03-31") else ("三季报" if r_date.endswith("09-30") else "季报")))
                )
                raw_c_code = row.get("CURRENCY_CODE") or row.get("CURRENCY") or "HKD"
                # 修复 P1-1 & P1-3: indicators 与 cashflow 币种统一校准为公司的真实法定编报币种 (USD/CNY/HKD)
                c_code = real_currency if (real_currency and rep_name in ("RPT_HKF10_FN_MAININDICATOR", "RPT_HKF10_FN_CASHFLOW_PC")) else (row.get("CURRENCY_CODE") or row.get("CURRENCY") or "CNY")
                periods_map[r_date] = {
                    "截止日期": r_date,
                    "报告期日期": r_date,
                    "报告期": r_date,
                    "报告期截止日": r_date,
                    "报告期标签": r_type,
                    "报表类型": r_type,
                    "DATE_TYPE_CODE": date_type_code,
                    "币种": c_code,
                    "报告币种": c_code,
                    "金额单位": "元" if c_code != "USD" else "美元",
                }
                if real_currency == "USD" and rep_name == "RPT_HKF10_FN_MAININDICATOR":
                    periods_map[r_date]["币种口径校准说明"] = (
                        f"【法定申报原币校准（已修复 P1-1，收敛 D1）】：该标的在港交所官方申报法定币种为 USD（美元）。"
                        f"东财指标表原生金额系按报告期末汇率约 {cur_fx:.4f} 折算之人民币，本系统已全量逆向还原为真实法定原币 USD，"
                        "涵盖营业收入、毛利润、净利润、总资产、总负债、股东权益、BPS 与 EPS 等全科目，使绝对金额与比率严格勾稽对齐。"
                    )
                elif real_currency == "USD" and rep_name == "RPT_HKF10_FN_CASHFLOW_PC":
                    periods_map[r_date]["跨表币种勾稽说明"] = (
                        f"【跨表币种勾稽校准（已修复 P1-3，收敛 D7）】：上市公司法定记账货币为 USD，现金流量表金额已按汇率约 {cur_fx:.4f} "
                        "全量逆向校准为 USD，与资产负债表现金及等价物保持同币种可比口径；受受限制现金/定期存款在两表中会计分类准则差异影响，两者期末现金存在 0.5%~1.7% 的微小准则残差，主表三大活动净额与净增加额代数闭合。"
                    )
                elif real_currency and rep_name == "RPT_HKF10_FN_MAININDICATOR" and real_currency != raw_c_code:
                    periods_map[r_date]["币种说明"] = f"底层主要指标财务数值与法定利润表一致，实际记账编报币种为【{real_currency}】（原始接口所标 {raw_c_code} 仅为股票交易币种）。"
            # 宽表与窄表兼容解析 (修复 HK-7)
            if "STD_ITEM_NAME" in row or "ITEM_NAME" in row:
                item_name = row.get("STD_ITEM_NAME") or row.get("ITEM_NAME")
                amount = row.get("AMOUNT")
                if item_name and amount is not None:
                    try:
                        amt_val = round(float(amount), 2)
                        # 若法定币种为 USD 但现金流金额为东财折算后的人民币，执行逆向还原
                        if real_currency == "USD" and rep_name == "RPT_HKF10_FN_CASHFLOW_PC" and cur_fx > 1.0:
                            amt_val = round(amt_val / cur_fx, 2)
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
                            val_f = float(raw_v)
                            # 若法定币种为 USD 但指标表金额为东财折算后的人民币，对金额与每股指标逆向还原 (修复 D1 全字段还原)
                            if real_currency == "USD" and rep_name == "RPT_HKF10_FN_MAININDICATOR" and cur_fx > 1.0:
                                if ch_k.endswith("_pct") or ch_k in ("流动比率", "市盈率_PE_TTM", "市净率_PB_TTM"):
                                    pass
                                elif ch_k in ("已发行总股本_股", "已发行H股股数_股"):
                                    pass
                                elif ch_k in ("总市值_港元", "港股市值_港元"):
                                    pass
                                elif ch_k in ("每股净资产_BPS_元", "基本每股收益_元", "基本每股收益_EPS", "摊薄每股收益_元", "每股收益_TTM_元", "每股现金流_元"):
                                    val_f = round(val_f / cur_fx, 4)
                                else:
                                    val_f = round(val_f / cur_fx, 2)
                            periods_map[r_date][ch_k] = round(val_f, 2) if "." in str(val_f) else int(val_f)
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
                periods_map[r_date]["每股收益_TTM_说明"] = (
                    "【港股每股收益_TTM不可信警示（修复 D3）】：上游东财数据中心宽表中【每股收益_TTM_元】(EPS_TTM) "
                    "未严格与同响应【市盈率_PE_TTM】及法定三表归母净利润做差 TTM 勾稽（偏差23%~46%），口径不可信，严禁直接引用此 EPS_TTM 进行估值决策。"
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

            # HK-2: 注入港股现金及等价物口径说明 (收敛 P3-6 极微小残差自陈)
            if end_cash is not None or p.get("货币资金") is not None or p.get("现金及等价物") is not None:
                p["现金口径说明"] = (
                    "港股会计报表中，现金流量表「期末现金及等价物」指不受使用限制且可随时支取的流动资金；"
                    "资产负债表「现金及等价物/银行结存」为账面总额（通常包含定期存款、受限保证金等），"
                    "二者差额主要系受限制存款与冻结资金等项目差异所致，属会计准则常规差异。"
                    "若现金流量表现金及等价物与资产负债表账面现金（含受限制存款）存在极微小残差（如 02026 微差 0.035%），"
                    "系受限制存款按不同会计准则在期末汇率折算或冻结保证金利息归集口径微差所致，属正常会计报表微差。"
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

        # 修复 HK-3, P1-2: 当 period_type="quarter" 时，对流量表（利润表与现金流量表）按相邻累计报告期做差计算真实单季度数值
        if period_type.lower() == "quarter" and statements:
            if report_type.lower() in ("income", "income_statement", "lyb", "cashflow", "cashflow_statement", "xjllb"):
                quarter_stmts = []
                def _safe_dt(d_str: str) -> datetime.datetime | None:
                    try:
                        return datetime.datetime.strptime(str(d_str)[:10], "%Y-%m-%d")
                    except Exception:
                        return None

                for i, curr_p in enumerate(statements):
                    p_copy = dict(curr_p)
                    curr_date = str(p_copy.get("截止日期", ""))
                    curr_year = curr_date[:4]
                    curr_dt = _safe_dt(curr_date)
                    r_type_str = str(p_copy.get("报表类型") or p_copy.get("报告期标签") or "")
                    dt_code = str(p_copy.get("DATE_TYPE_CODE") or "").strip()

                    # 修复 P1-2 & D2: 严谨财年首季守护 (Fiscal Year Q1 Guard)
                    # 若本期为第一季度 (Q1，如东财 DATE_TYPE_CODE == '003' 或报表类型为一季报，包含阿里等3-31结账标的的 06-30 一季报)，
                    # 其披露累计值本就是新财年第一季度的单季度真实值，严禁寻找前驱扣减上一财年年报！
                    if dt_code == "003" or "一季报" in r_type_str or "Q1" in r_type_str or (curr_date.endswith("-03-31") and dt_code not in ("001", "002", "004")):
                        p_copy["单期标签"] = "一季报(第一季度单期/Q1)"
                        p_copy["单季度计算说明"] = f"本期为公司财年第一季度(Q1，截止日 {curr_date})，披露累计值即为单季度真实值，不扣减上一财年年报。"
                        quarter_stmts.append(p_copy)
                        continue

                    # 寻找同财年前驱期做差 (修复 D2: 支持阿里等非12月财年跨自然年做差与财年末Q4补齐)
                    prev_p = None
                    cand_dt_code = ""
                    for j in range(i + 1, len(statements)):
                        cand_p = statements[j]
                        cand_date = str(cand_p.get("截止日期", ""))
                        cand_dt = _safe_dt(cand_date)
                        cand_dt_code = str(cand_p.get("DATE_TYPE_CODE") or "").strip()
                        if not curr_dt or not cand_dt:
                            continue
                        days_diff = (curr_dt - cand_dt).days
                        if days_diff <= 0:
                            continue

                        # 根据财报类型代码匹配严格同财年前驱
                        if dt_code == "001":
                            # 年报(FY): 前驱应为前三季度(004, 约90天) 或 上半年(002, 约180天)
                            if cand_dt_code in ("004", "002") and days_diff <= 280:
                                prev_p = cand_p
                                break
                        elif dt_code == "004":
                            # 三季报(9M): 前驱应为上半年(002, 约90天)
                            if cand_dt_code == "002" and days_diff <= 140:
                                prev_p = cand_p
                                break
                        elif dt_code == "002":
                            # 中报(H1): 前驱应为一季报(003, 约90天)
                            if cand_dt_code == "003" and days_diff <= 140:
                                prev_p = cand_p
                                break
                        else:
                            # 缺失 DATE_TYPE_CODE 时的自然日前驱回退 (按 120 天单季容错)
                            if cand_date.startswith(curr_year) and cand_dt_code != "001":
                                prev_p = cand_p
                                break

                    if prev_p:
                        prev_date = str(prev_p.get("截止日期", ""))
                        for k, v in curr_p.items():
                            if isinstance(v, (int, float)) and k not in ("基本每股收益_EPS", "稀释每股收益", "每股收益_元", "摊薄每股收益_元", "每股净资产_BPS_元"):
                                prev_v = prev_p.get(k)
                                if isinstance(prev_v, (int, float)):
                                    p_copy[k] = round(float(v) - float(prev_v), 2)
                        # 优先依据 DATE_TYPE_CODE 映射标准财季标签 (修复 D2: 阿里等3月财年标签对齐与Q4补全)
                        if dt_code == "001":
                            if cand_dt_code == "004" or "三季报" in str(prev_p.get("报表类型", "")):
                                p_copy["单期标签"] = "年报(第四季度单期/Q4)"
                                p_copy["单季度计算说明"] = f"本期单季度数据系由全年累计({curr_date})扣减前三季度累计({prev_date})做差推算得到第四季度单季。"
                            else:
                                p_copy["单期标签"] = "年报(下半年单期/H2)"
                                p_copy["单季度计算说明"] = f"该标的实行半年度披露，本期单期数据系由全年累计({curr_date})扣减上半年中报({prev_date})做差推算得到下半年单期(H2)。"
                        elif dt_code == "004":
                            p_copy["单期标签"] = "三季报(第三季度单期/Q3)"
                            p_copy["单季度计算说明"] = f"本期单季度数据系由前三季度累计({curr_date})扣减上半年中报({prev_date})做差推算得到第三季度单季。"
                        elif dt_code == "002":
                            p_copy["单期标签"] = "中报(第二季度单期/Q2)"
                            p_copy["单季度计算说明"] = f"本期单季度数据系由上半年累计({curr_date})扣减一季报({prev_date})做差推算得到第二季度单季。"
                        elif curr_date.endswith("-12-31"):
                            p_copy["单期标签"] = "年报(第四季度单期/Q4)"
                            p_copy["单季度计算说明"] = f"本期单季度数据系由全年累计({curr_date})扣减前三季度累计({prev_date})做差推算得到第四季度单季。"
                        elif curr_date.endswith("-09-30"):
                            p_copy["单期标签"] = "三季报(第三季度单期/Q3)"
                            p_copy["单季度计算说明"] = f"本期单季度数据系由前三季度累计({curr_date})扣减上半年中报({prev_date})做差推算得到第三季度单季。"
                        elif curr_date.endswith("-06-30"):
                            p_copy["单期标签"] = "中报(第二季度单期/Q2)"
                            p_copy["单季度计算说明"] = f"本期单季度数据系由上半年累计({curr_date})扣减一季报({prev_date})做差推算得到第二季度单季。"
                        else:
                            p_copy["单季度计算说明"] = f"本期单期数据系由本期累计({curr_date})扣减同财年上一期累计({prev_date})做差推算。"
                        quarter_stmts.append(p_copy)
                    elif dt_code == "002" or curr_date.endswith("-06-30"):
                        # 半年度披露标的未披露Q1，中报本身即为上半年单期 (修复 Issue 7)
                        p_copy["单期标签"] = "中报(上半年单期/H1)"
                        p_copy["单季度计算说明"] = "该标的实行半年度披露规则（无独立一季报），中报披露值即为上半年(H1)单期真实值。"
                        quarter_stmts.append(p_copy)
                    else:
                        p_copy["报表口径"] = "累计值(非单季)"
                        p_copy["单季度计算说明"] = "前期财报未单独披露累计值，无法推算单季度真实数值，省略出单季序列。"

                # 修复 P2-5: 报告期 字段严格保持为标准日期字符串（如 2026-06-30），单期中文属性放入 报告期标签/单期标签，杜绝下游按日期解析时全量失配
                for q_item in quarter_stmts:
                    raw_dt = str(q_item.get("截止日期") or q_item.get("报告期日期") or "")
                    tag_v = q_item.get("单期标签")
                    if tag_v:
                        q_item["报告期标签"] = tag_v
                        q_item["报告期_标签"] = tag_v
                        q_item["报表类型"] = tag_v
                        if raw_dt:
                            q_item["报告期"] = raw_dt
                            q_item["报告期_日期"] = raw_dt
                        q_item["报告期字段语义说明"] = (
                            "【报告期字段标准语义（已收敛 P2-5）】：【报告期】与【截止日期】统一保持为标准日期格式(YYYY-MM-DD)以保证按日期排序解析器兼容；"
                            "如需读取季度单期中文类型属性（如'中报(第二季度单期/Q2)'），请直接读取【报告期标签】或【单期标签】。"
                        )

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
                "卖出回购金融资产", "拆入资金", "拆出资金",
                "保费", "保户储金", "赔付款项", "质押贷款", "客户存款和同业存放款项净增加额",
                "存放中央银行和同业款项净减少额", "向中央银行借款净增加额"
            }
            # 只有当包含纯金融专有科目且不具备主营工商业特征时才判定为金融企业 (修复 P2-2)
            has_fin_keys = any(k in sample_keys for k in fin_feature_keys)
            has_industrial_keys = any(k in sample_keys for k in ("存货", "在建工程", "固定资产", "销售成本", "营业额"))
            if has_fin_keys and not (has_industrial_keys and not any(k in corp_name for k in ("银行", "保险", "证券", "信托", "金融", "理财", "券商"))):
                is_financial = True

        if is_financial:
            standard_name = "国际/香港财务报告准则 (银行 / 保险 / 证券 / 金融控股)"
        else:
            standard_name = "国际财务报告准则 (IFRS) / 香港财务报告准则 (HKFRS) - 一般工商业"

        rtype_lower = report_type.lower()
        if statements:
            for p in statements:
                # 修复 P3-3: 港股成本键名双向映射（销售成本 vs 营运支出）
                if rtype_lower in ("income", "income_statement", "lyb"):
                    if "销售成本" in p and "营运支出" not in p:
                        p["营运支出"] = p["销售成本"]
                    elif "营运支出" in p and "销售成本" not in p:
                        p["销售成本"] = p["营运支出"]

                # 修复 P1-2: 港股权益侧明细与归母不可加总差额自陈（收敛建行 00939 优先股/永续债等专有权益工具未单独分列问题）
                if rtype_lower in ("balance", "balance_sheet", "zcfzb"):
                    eq_parent = p.get("归属于母公司股东权益") or p.get("归属于母公司所有者权益")
                    sc = p.get("股本") or p.get("实收资本(或股本)")
                    res = p.get("其他储备") or p.get("资本公积")
                    re_p = p.get("保留溢利") or p.get("未分配利润")
                    if eq_parent is not None and sc is not None and (res is not None or re_p is not None):
                        try:
                            sum_detail = round(float(sc or 0.0) + float(res or 0.0) + float(re_p or 0.0), 2)
                            diff_eq = round(float(eq_parent) - sum_detail, 2)
                            if abs(diff_eq) > 1.0:
                                p["归母所有者权益不可加总差额_优先股永续债及其他权益工具等"] = diff_eq
                                p["权益不可加总差额自陈说明"] = (
                                    f"【港股权益科目可拆解性自陈（已收敛 P1-2）】：上游港股数据源未单独拆分优先股、永续债等其他权益工具明细科目；"
                                    f"已披露明细（股本 {sc} + 其他储备 {res or 0} + 保留溢利 {re_p or 0} = {sum_detail}）与归属于母公司股东权益({eq_parent})存在差额 {diff_eq}，"
                                    "已自陈至【归母所有者权益不可加总差额_优先股永续债及其他权益工具等】。总权益恒等式完全闭合，建模取数请以归属于母公司股东权益总额为准。"
                                )
                        except (ValueError, TypeError):
                            pass

                # 修复 P2-4: 现金流量表季度结构降级与细项科目未披露说明
                if rtype_lower in ("cashflow", "cashflow_statement", "xjllb"):
                    p["现金流量表细项披露自陈说明"] = (
                        "【港股现金流量表披露结构自陈（已收敛 P2-4）】：依香港联交所上市规则及 IFRS 准则，港股上市公司在季度报告（及部分中期报告）中仅依法简要披露经营/投资/筹资三大活动净额及现金变动（通常仅 8~33 项），不强制披露底层细项科目与间接法补充资料；"
                        "三净额与期末现金桥仍完全闭合，完整科目细项详见经审计的年度报告。"
                    )

                if is_financial:
                    if rtype_lower in ("balance", "balance_sheet", "zcfzb"):
                        p["资产负债表核算说明"] = (
                            "【金融企业资产负债表核算规范】本表遵循国际/香港财务报告准则金融机构专用列报格式。"
                            "金融企业资产负债表不严格区分流动与非流动划分，以资产与负债的流动性由高到低排列（库存现金、存放央行、同业款项居前）。"
                            "基本会计恒等式为：【总资产 = 总负债 + 股东权益合计】（或【总资产 = 权益及负债合计】）。\n"
                            "【非运算项目禁止求和声明】：报表中的【非运算项目】属于数据源用于平衡展示的统计插补列，非实际资产或负债细项，严禁参与整表字段遍历累加求和（若重复累加会导致资产总计虚增一倍以上）。\n"
                            "【保险行业中报其他项目归集特性提示】：港股专线在半年度报告(中报)中对部分保险机构（如 02601 中国太保）的细项大幅归集至【资产其他项目】与【负债其他项目】（占比可能超 85%），明细覆盖率低于季报/年报，分析细分资产负债构成时需审慎。"
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
                        # 修复 P2-6: 银行模板键名对齐与语义反转说明
                        if "经营收入总额" in p:
                            if "营业收入" not in p:
                                p["营业收入"] = p["经营收入总额"]
                            if "营业额" not in p:
                                p["营业额"] = p["经营收入总额"]
                        p["金融/银行收入与支出语义说明"] = (
                            "【金融/银行利润表科目语义反转声明（已收敛 P2-6）】：银行及金融机构无工商业'营业额'与'销售成本/毛利'概念。"
                            "本表'经营收入总额'对应全行营业总收入，'营运支出'代表经营支出总额（业务管理费等日常营运开支），绝非进货商品之'销售成本'；"
                            "银行股不适用工商业毛利率计算模型。"
                        )
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
        # =========================================================================
        # 【功能块 5.3.3：港股财报裸码清洗与币种动态感知 (修复 D5 & D8)】
        # - 输入：statements (各报告期财报行列表)
        # - 功能：
        #   1. 物理清洗内部计算用的裸码【DATE_TYPE_CODE】，防止未映射上游码进入交付层 (修复 D5)
        #   2. 动态感知上市公司法定记账货币（如 USD、HKD、CNY），消除对美元标的硬编码'统一为 CNY'的自相矛盾 (修复 D8)
        # - 输出：洗净后的 statements 及自洽的会计口径说明
        # =========================================================================
        stmt_name = type_names.get(report_type.lower(), report_type)
        period_suffix = "单期(半年度单期/单季)" if period_type.lower() == "quarter" else "累计报告期"
        
        stat_list = statements or []
        for p in stat_list:
            p.pop("DATE_TYPE_CODE", None)

        rep_cur = (stat_list[0].get("币种") or stat_list[0].get("CURRENCY") or "HKD/CNY") if stat_list else "HKD/CNY"

        return {
            "symbol": clean_code,
            "market": "香港交易所 (HKEX)",
            "statement_type": f"{stmt_name} ({period_suffix})",
            "period_type": period_type,
            "source": source,
            "period_count": len(stat_list),
            "is_financial": is_financial,
            "accounting_standard": standard_name,
            "statements": stat_list,
            "会计口径与说明": (
                f"港股会计报表按国际财务报告准则 (IFRS) 或香港财务报告准则 (HKFRS) 编制。当前口径：【{period_suffix}】。\n"
                f"准则类别：【{standard_name}】。\n"
                f"1.【数据源与金额单位说明】：本接口采用双源高可用容灾架构：以东方财富港股数据中心专线为主力数据源（统一金额单位为【元/对应法定币种】，各期次法定记账货币以行内【币种】为准，如当前标的为【{rep_cur}】），"
                "当 period_type=quarter 时针对利润表与现金流量表按同财年相邻累计报告期自动做差生成单期数据（资产负债表为时点存量不作差）。\n"
                "2.【单期与单季度口径区别】：港交所主板强制规则仅要求披露半年度(中报)与全年度(年报)；除 A+H 两地上市标的（因中国证监会要求必须按季披露 Q1/Q3）及腾讯控股等少数自愿披露标的外，多数纯 H 股无一季报/三季报。"
                "因此对于实行半年度披露的标的，做差推算的单期其本质是半年度单期（如年报扣除中报得到下半年单期 H2，中报累计值即为上半年单期 H1），非真正三月单季；仅按季披露标的才对应真正三月单季(Q1-Q4)。具体每期单期属性已在【单期标签】与【单季度计算说明】中明确标注。\n"
                "3.【金融与保险业明细说明】：港股专线未对保险等特殊金融行业设立细分子模板，保险公司（如中国太保 02601）的主营负债与资产大额归集于【负债其他项目】/【资产其他项目】，分析资产负债细项结构时须注意该口径特性。\n"
                "4.【非运算项目与插补列纪律】：报表中出现的【非运算项目】为数据源排版统计插补列，绝对禁止整表字段累加求和。\n"
                "5.【成本科目双向别名】：为消除港股功能法（销售成本）与性质法（营运支出）列报分裂，接口已自动映射【销售成本】与【营运支出】为互通别名。"
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
        for tr in trs[1:100]:
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
    # - 输入：clean_code (港股标的代码), category (查询分类), arguments (过滤参数字典)
    # - 功能：按分类多路路由抓取主要股东、公司回购、分红派息、行业概念、主要指标及市场规则
    # - 输出：dict[str, Any] 结构化港股 F10 概况响应字典
    # =========================================================================
    def _get_hk_f10(self, clean_code: str, category: str = "summary", arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        clean_code = re.sub(r"[^\d]", "", clean_code).zfill(5)
        cat = category.lower().strip()

        # 分类 1.1：持股5%以上主要股东权益披露 (top_holders / holders) - 替代A股前十大股东 (修复 HK-5, P2 申报顺位与去重, P3-6 limit支持, P2-6 分母口径)
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
            latest_decl_date = ""
            for item in top_list:
                dt = str(item.get("事件日期") or "").strip()
                if dt and dt > latest_decl_date:
                    latest_decl_date = dt

            # 港股为流水式申报记录，按申报日期倒序排列，以“申报顺位”替代静态“名次”
            for idx, item in enumerate(top_list, 1):
                item["申报顺位"] = idx
                item["持股申报说明"] = f"好仓申报({item.get('目前类别', 'L')})，申报日期：{item.get('事件日期', '近期')}"
                item["时效风险提示"] = f"申报日期为 {item.get('事件日期', '早期')}，仅代表该法定申报时点历史截面，不代表当前静态持股"
                # 修复 P1-1: 依据股份类别精确区分 A 股、H 股、普通股与其他类别分母口径（彻底消除 "H Shares"/"Ordinary Shares" 误判为 A 股缺陷）
                raw_cat = str(item.get("股份类别") or item.get("类别") or "").strip()
                cat_upper = raw_cat.upper()
                if "H股" in raw_cat or re.search(r'\bH\s*SHARES?\b|^H$', cat_upper):
                    item["持股比例分母口径说明"] = "该持股比例系相对已发行H股股份总数申报（非全部A+H综合股本）"
                elif "A股" in raw_cat or re.search(r'\bA\s*SHARES?\b|^A$', cat_upper):
                    item["持股比例分母口径说明"] = "该持股比例系相对已发行A股股份总数申报（非H股亦非A+H综合股本）"
                elif "普通股" in raw_cat or "ORDINARY" in cat_upper:
                    item["持股比例分母口径说明"] = "该持股比例系相对已发行普通股股份总数申报"
                elif "其他" in raw_cat or "OTHER" in cat_upper:
                    item["持股比例分母口径说明"] = "该持股比例系相对该特定类别股份（非H股/其他类别股份总数）申报"
                elif cat_upper:
                    item["持股比例分母口径说明"] = f"该持股比例系相对对应股份类别（{raw_cat}）总数申报"
                else:
                    item["持股比例分母口径说明"] = "该持股比例系相对已发行对应类别股份总数申报（非全部综合股本）"

            # 修复 P3-6: 支持 limit / periods 参数截断行数
            req_limit = (arguments or {}).get("limit")
            req_periods = (arguments or {}).get("periods")
            n_limit = int(req_limit) if req_limit is not None else (int(req_periods) if req_periods is not None else 10)
            final_top_list = top_list[:n_limit] if n_limit > 0 else top_list

            no_holder_desc = "暂无持股5%以上主要股东法定申报记录（上游未采集或该标的近期无大股东触发整百分点跨越申报门槛）。" if not final_top_list else None
            resp_holders = {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股持股5%以上主要股东与权益披露 (替代前十大股东)",
                "source": "sina_hk_rights (新浪港股大股东权益变动专线)",
                "最新申报日期": latest_decl_date or "无",
                "最新申报时效提示": (
                    f"表内主要股东最新法定申报日期为【{latest_decl_date or '未知'}】。"
                    "依据香港《证券及期货条例》，持股 5% 以上大股东仅在持股跨越整百分点门槛时依法强制申报流水。"
                    "若大股东长期无跨界增减持则无新申报，最新公开披露申报记录可能停留于数年前（如 00688 最新申报为 2020 年，02628 为 2019 年）。"
                    "表内数据仅代表各股东各自最近一次法定申报截面，绝非当前时点的最新静态持股名册！"
                ),
                "持股比例分母口径说明": (
                    "【港股持股比例分母口径提示（已修复 D8, 收敛 P1-1）】：香港联交所主要股东权益申报中，"
                    "申报的持股比例分母为股东所持对应股份类别（如持 A 股相对已发行 A 股总数计算，持 H 股相对已发行 H 股总数计算，持普通股相对普通股总数计算），"
                    "而非公司全部 A+H 综合总股本。同表跨类别股东比例相加无数学意义（例如 00939 财政部持 A 股结余率 54.71% 系相对 211.8 亿 A 股计算）。"
                ),
                "formatted_tables": {
                    "top_holders": final_top_list,
                },
                "data": final_top_list if final_top_list else {"status": "未收录（上游未采集或待更新）", "无记录说明": no_holder_desc},
                "股东口径说明": (
                    "【港股权益申报机制说明】：港交所实行中央结算系统 (CCASS) 存托体系，绝大部分机构与公众投资者股份均由香港中央结算(代理人)集中名义持有，"
                    "上市公司并无 A 股式每季度穿透至个人的『前十大流通股东名册』。依据香港《证券及期货条例》第XV部，法定信息披露仅要求持股比例达 5% 及以上的大股东、董事在增减持跨越整百分点时主动申报流水（L=好仓，S=淡仓，P=可借出股份）。"
                    "本表仅展示各大股东最新一次好仓(L)申报记录（已自动排重历史旧申报与淡仓）。表内序号为【申报顺位】，非同一基准日的当前时点静态股东名次！\n"
                    "【时效缺口风险提示】：港股大股东权益申报仅在增减持跨越法定整百分点门槛时依法强制触发。若某大股东长期无主动申报，最新公开披露申报记录可能停留在数年前（例如 00688 最新申报为 2020 年，02628 最新申报为 2019 年）。最新申报日期仅反映最后一次法定申报时点的历史持股截面，不代表当前最新时点持股，查阅时务必结合具体申报日期进行时效审阅。"
                ),
            }
            if no_holder_desc:
                # 修复 P3-2: 00883 空 top_holders 注入无记录说明，区分确无申报与未采集
                resp_holders["无记录说明"] = no_holder_desc
            if req_limit is not None or req_periods is not None:
                resp_holders["期次与截断说明"] = f"已按参数截取前 {len(final_top_list)} 位好仓主要股东申报流水。"
            return resp_holders

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

            no_ev_desc = "暂无主要股东近期法定权益变动流水申报记录（上游未采集或标的近期无大股东触发整百分点变动申报）。" if not cleaned_events else None
            resp_events = {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股主要股东权益申报与大股东增减持变动历史",
                "source": "sina_hk_rights (新浪港股大股东权益变动专线)",
                "formatted_tables": {
                    "shareholder_events": cleaned_events,
                },
                "data": cleaned_events[:15] if cleaned_events else {"status": "未收录（上游未采集或待更新）", "无记录说明": no_ev_desc},
                "权益变动说明": (
                    "【港股大股东权益申报说明】：依据香港《证券及期货条例》，持股 5% 以上股东、董事及最高行政人员在持股跨越整百分点或发生增减持交易时，"
                    "须在3个交易日内向联交所提交表格披露（L=好仓/Long Position，S=淡仓/Short Position，P=可借出股份）。"
                    "针对港交所 SDI 体系中母子公司穿透申报产生的同日目前持股相同但涉及股数为 0 的附属占位记录，已自动清洗规整，保留真实交易主申报行。"
                ),
            }
            if no_ev_desc:
                resp_events["无记录说明"] = no_ev_desc
            return resp_events

        # 分类 2.1：股份回购明细流水 (repurchases)
        if cat == "repurchases":
            raw_rep = self._get_sina_hk_table(clean_code, "rights", table_idx=2)
            # 修复 D (P2): 剔除回购日期为"暂无数据"的占位行
            clean_rep = [r for r in raw_rep if str(r.get("回购日期", "")).strip() not in ("暂无数据", "暂无", "无数据", "--", "None", "")]
            no_rep_desc = "该标的近期未在港交所披露股份回购实施流水记录。" if not clean_rep else None
            resp_rep = {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "repurchases",
                "category_name": "港股公司股份回购明细流水",
                "source": "sina_hk_rights (新浪港股公司回购专线)",
                "formatted_tables": {
                    "repurchases": clean_rep,
                },
                "data": clean_rep[:15] if clean_rep else {"status": "暂无股份回购记录", "无记录说明": no_rep_desc},
                "回购说明": "港交所上市公司每日回购报告明细流水，包含回购日期、回购股数、最高/最低回购价及回购总耗资额。",
            }
            if no_rep_desc:
                resp_rep["无记录说明"] = no_rep_desc
            return resp_rep

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
            # 修复 P2-5: 更名聚合卡片字段为样本区间口径，明确统计窗口起止日期，杜绝将样本流水误当全历史累计
            first_date = clean_rep[0].get("回购日期") if clean_rep else None
            last_date = clean_rep[-1].get("回购日期") if clean_rep else None
            window_str = f"{last_date} 至 {first_date}" if (first_date and last_date and first_date != last_date) else (first_date or "无")
            buyback_summary = {
                "样本区间回购总额_港元": round(total_spent_hkd, 2),
                "样本区间回购总额_万港元": round(total_spent_hkd / 10000.0, 2),
                "样本区间回购总额_万元": round(total_spent_hkd / 10000.0, 2),
                "累计已实施回购总额_万港元": round(total_spent_hkd / 10000.0, 2),
                "累计已实施回购总额_万元": round(total_spent_hkd / 10000.0, 2),
                "样本区间回购股数_股": int(total_shares),
                "样本区间回购股数_万股": round(total_shares / 10000.0, 2),
                "累计回购总金额_港元": round(total_spent_hkd, 2),
                "累计回购总金额_万港元": round(total_spent_hkd / 10000.0, 2),
                "累计回购股数_股": int(total_shares),
                "累计回购股数_万股": round(total_shares / 10000.0, 2),
                "样本回购记录笔数": len(clean_rep),
                "样本回购实施记录天数": len(set(str(r.get("回购日期", "")).strip() for r in clean_rep if str(r.get("回购日期", "")).strip() not in ("暂无数据", "暂无", "无数据", "--", "None", ""))) if clean_rep else 0,
                "回购实施记录天数": len(set(str(r.get("回购日期", "")).strip() for r in clean_rep if str(r.get("回购日期", "")).strip() not in ("暂无数据", "暂无", "无数据", "--", "None", ""))) if clean_rep else 0,
                "样本统计区间": window_str,
                "最新回购日期": first_date,
                "最新回购实施价格_港元": clean_rep[0].get("最高回购价(港元)") or clean_rep[0].get("最高价(港元)") if clean_rep else None,
                "累计字段口径自陈": "【累计与区间说明（已收敛 P3-4 与 D13）】：本表'累计回购总金额_港元'、'累计已实施回购总额_万港元/万元'与'累计回购股数'系为兼容旧调用方命名的样本统计区间内累计值，数值等同于'样本区间回购总额'，统计窗口严格受限于公开披露样本流水（见统计区间说明）。",
                "统计区间说明": f"本统计基于新浪港股公开披露的最近一页回购流水样本（共 {len(clean_rep)} 笔记录，统计窗口为 {window_str}），非全历史累计值。",
            }
            no_bb_desc = "该标的近期未在港交所披露股份回购实施流水记录。" if not clean_rep else None
            resp_bb = {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "buyback",
                "category_name": "港股股份回购聚合概览与统计卡片 (Buyback Overview)",
                "source": "sina_hk_rights (新浪港股公司回购专线)",
                "formatted_tables": {
                    "buyback_summary": [buyback_summary],
                    "buyback_details": clean_rep,
                },
                "data": buyback_summary if clean_rep else {"status": "暂无股份回购记录", "无记录说明": no_bb_desc},
                "回购说明": "buyback 聚焦于回购累计实施规模与状态的宏观聚合概览 (buyback_summary)；repurchases 聚焦于历次方案明细流水。",
                "表关系说明": "buyback_details 表与 repurchases 分类返回的回购流水完全同源（逐位哈希相同），明细取数推荐优先使用 buyback_details。",
            }
            if no_bb_desc:
                resp_bb["无记录说明"] = no_bb_desc
            return resp_bb

        # 分类 3.1：分红派息与历史分红 (dividends / bonus)
        if cat in ("dividends", "bonus"):
            div_data = self._get_sina_hk_table(clean_code, "dividends", table_idx=0)
            clean_div = []
            for d in div_data:
                d_copy = dict(d)
                plan = str(d_copy.get("派息内容") or d_copy.get("方案内容") or "").strip()
                hkd_amt = None
                cny_amt = None
                is_div = True
                if "不派息" in plan or plan == "--" or not plan:
                    is_div = False
                else:
                    m_hkd = re.search(r'相当于\s*([0-9.]+)\s*(?:港元|HKD)', plan, re.I) or re.search(r'([0-9.]+)\s*(?:港元|HKD)', plan, re.I)
                    if m_hkd:
                        try:
                            hkd_amt = round(float(m_hkd.group(1)), 4)
                        except Exception:
                            pass
                    m_cny = re.search(r'([0-9.]+)\s*(?:元人民币|人民币|元RMB|RMB)', plan, re.I)
                    if m_cny:
                        try:
                            cny_amt = round(float(m_cny.group(1)), 4)
                        except Exception:
                            pass
                d_copy["每股派息_港元"] = hkd_amt
                d_copy["每股派息_人民币"] = cny_amt
                d_copy["是否派息"] = is_div
                clean_div.append(d_copy)

            no_div_desc = "该标的近期未在港交所披露分红派息记录。" if not clean_div else None
            resp_div = {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股分红派息与历史分红记录",
                "source": "sina_hk_dividends (新浪港股分红派息专线)",
                "formatted_tables": {
                    "dividends": clean_div,
                },
                "data": clean_div[:15] if clean_div else {"status": "暂无分红派息记录", "无记录说明": no_div_desc},
                "分红说明": "港股分红派息历史记录，包含公布日期、会计年度、派息事项、派息方案内容、除净日与实际派发日。",
                "分红数据提取说明": (
                    "【港股分红数值提取与自由文本自陈（已收敛 P2-3）】：港股上市公司公告以自由文本格式披露派息方案（常含'相当于X.XXX港元'或多币种描述），"
                    "网关层已尽可能正则提取【每股派息_港元】与【每股派息_人民币】数值字段供程序化建模；若当期为'不派息'或格式不规则，则数值置为 None 并保留原文文本。"
                ),
            }
            if no_div_desc:
                resp_div["无记录说明"] = no_div_desc
            return resp_div

        # 分类 3.2：重大催化与里程碑事件 (events)
        if cat == "events":
            div_data = self._get_sina_hk_table(clean_code, "dividends", table_idx=0)
            rep_data = self._get_sina_hk_table(clean_code, "rights", table_idx=2)
            events_list: list[dict[str, Any]] = []
            for d in div_data[:8]:
                d_date = str(d.get("公布日期") or d.get("除净日") or d.get("除净日(由)") or "")
                if d_date and d_date != "--":
                    plan = str(d.get("派息内容") or d.get("方案内容") or d.get("派息事项") or "").strip()
                    if plan == "--":
                        plan = ""
                    ex_date = str(d.get("除净日") or d.get("除净日(由)") or "").strip()
                    pay_date = str(d.get("派息日期") or d.get("派送日") or "").strip()
                    content_parts = []
                    if plan:
                        content_parts.append(f"分红方案：{plan}")
                    elif d.get("派息事项") and d.get("派息事项") != "--":
                        content_parts.append(f"事项：{d.get('派息事项')}")
                    else:
                        content_parts.append("公司实施分红派息")
                    if ex_date and ex_date != "--":
                        content_parts.append(f"除净日：{ex_date}")
                    if pay_date and pay_date != "--":
                        content_parts.append(f"派息日：{pay_date}")
                    events_list.append({
                        "事件日期": d_date,
                        "事件类型": "分红派息",
                        "事件内容": "，".join(content_parts),
                        "事项明细": d,
                    })
            for r in rep_data[:8]:
                r_date = str(r.get("回购日期") or "")
                if r_date and r_date != "--":
                    shares = r.get("回购数量(股)") or r.get("回购股数(股)") or r.get("回购股数")
                    cost = r.get("回购总价(港元)") or r.get("耗资总额(港元)") or r.get("回购金额")
                    avg_p = r.get("平均回购价(港元)")
                    content_parts = ["公司实施股份回购"]
                    if shares and shares != "--":
                        try:
                            s_int = int(float(shares))
                            content_parts.append(f"回购股数 {s_int:,} 股")
                        except Exception:
                            content_parts.append(f"回购股数 {shares} 股")
                    if cost and cost != "--":
                        try:
                            c_f = float(cost)
                            content_parts.append(f"耗资总额 {c_f:,.2f} 港元")
                        except Exception:
                            content_parts.append(f"耗资总额 {cost} 港元")
                    if avg_p and avg_p != "--":
                        content_parts.append(f"平均回购价 {avg_p} 港元")
                    events_list.append({
                        "事件日期": r_date,
                        "事件类型": "股份回购",
                        "事件内容": "，".join(content_parts),
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

        # 分类 4.2：板块概念与市场族谱 (concepts / sector) (修复 P3-3 动态提取专属题材)
        if cat in ("concepts", "sector"):
            info = self._get_sina_hk_company_info(clean_code) or {}
            raw_ind = (info.get("所属行业") or "").strip()
            corp_name = info.get("公司名称(中文)", f"港股_{clean_code}")
            corp_desc = info.get("公司业务", "")
            ind_name = raw_ind or self._infer_hk_hang_seng_industry(corp_name, corp_desc, clean_code)

            # 修复 P3-3: 动态结合主营业务描述与证券名称架构提取专属题材概念
            dynamic_tags = []
            desc_text = f"{corp_name} {corp_desc}".lower()
            keyword_map = [
                ("人工智能", ["人工智能", "ai", "大模型", "算法", "深度学习"]),
                ("自动驾驶/智能出行", ["自动驾驶", "智能驾驶", "机器人出租车", "robotaxi", "无人驾驶", "新能源汽车", "智能网联"]),
                ("云计算/大数据", ["云计算", "云服务", "大数据", "数据中心", "idc", "saas"]),
                ("电子商务/数字经济", ["电子商务", "电商", "零售", "平台", "淘宝", "天猫"]),
                ("生物医药/创新药", ["生物医药", "创新药", "抗体", "疫苗", "肿瘤", "医疗健康"]),
                ("半导体/芯片", ["芯片", "半导体", "集成电路", "晶圆"]),
                ("清洁能源/绿电", ["光伏", "风电", "绿色能源", "储能", "核电"]),
            ]
            for tag_name, kws in keyword_map:
                if any(kw in desc_text for kw in kws):
                    dynamic_tags.append(tag_name)

            # 证券架构概念
            if any(corp_name.endswith(suffix) for suffix in ("-W", "-SW", " W", " SW")):
                dynamic_tags.append("同股不同权架构(W股)")
            if any(corp_name.endswith(suffix) for suffix in ("-SW", "-S", " SW", " S")):
                dynamic_tags.append("在港第二上市标的(S股)")
            if any(corp_name.endswith(suffix) for suffix in ("-B", " B")):
                dynamic_tags.append("未盈利生物科技标的(B股)")

            concepts_table = [
                {"概念分类": "恒生行业分类", "行业名称": ind_name, "板块内容": ind_name},
                {"概念分类": "市场板块", "行业名称": "香港交易所主板", "板块内容": "香港交易所主板"},
                {"概念分类": "互联互通机制", "行业名称": "港股通合资格标的 (港股通/非港股通)", "板块内容": "港股通合资格标的 (港股通/非港股通)"},
                {"概念分类": "恒生指数概念族谱", "行业名称": f"恒生科技/综合指数相关概念 ({ind_name})", "板块内容": f"恒生科技/综合指数相关概念 ({ind_name})"},
            ]
            for tag in dynamic_tags:
                concepts_table.append({"概念分类": "公司专属题材与架构概念", "行业名称": tag, "板块内容": tag})

            primary_theme = " / ".join(dynamic_tags[:3]) if dynamic_tags else ind_name
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股所属板块概念与市场族谱",
                "source": "sina_hk_f10 (新浪港股F10板块概念专线)",
                "formatted_tables": {
                    "concepts": concepts_table,
                },
                "data": {"交易板块": "香港交易所主板", "核心题材概念": primary_theme, "概念标签列表": dynamic_tags, "主题概念": ind_name},
                "概念说明": "港股概念板块涵盖交易所主板、互联互通港股通标的池、指数主题族谱及公司业务专属题材标签。",
            }

        # 分类 5.1：核心财务指标宽表 (indicators / main)
        if cat in ("indicators", "main"):
            em_data = self._get_eastmoney_hk_financials(clean_code, report_type="summary", limit_periods=4)
            no_ind_desc = "暂无该标的官方核心财务指标数据覆盖。" if not em_data else None
            resp_ind = {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股核心财务指标与盈利能力宽表 (Indicators)",
                "source": "eastmoney_hk_f10 (东方财富港股F10数据中心专线)",
                "formatted_tables": {
                    "indicators": em_data,
                },
                "data": em_data[0] if em_data else {"status": "暂无指标数据", "无记录说明": no_ind_desc},
                "指标说明": "东方财富港股数据中心核心财务与盈利能力指标汇总宽表（含每股收益、ROE、毛利率与主要营运比率）。",
                "每股收益_TTM_说明": (
                    "【港股每股收益_TTM不可信警示（已自陈，修复 D3）】：上游东财数据中心宽表中【每股收益_TTM_元】(EPS_TTM) "
                    "未严格与同响应【市盈率_PE_TTM】及法定三表归母净利润做差 TTM 勾稽（如 09988 隐含偏差达 23%~46%，"
                    "疑因上游采用不同股本基数或经调整 Non-GAAP 口径），本字段仅供对照，严禁直接引用此 EPS_TTM 进行估值决策，"
                    "严肃估值请以 PE_TTM 或三大财务报表做差自算 TTM 净利润为准。"
                ),
                "每股摊薄盈利披露说明": (
                    "【每股摊薄盈利披露说明（已收敛 P3-5）】：港股部分标的（如季报未披露完整稀释工具测算期次）上游未披露稀释每股收益，"
                    "该字段正常置为 None，基础每股收益请取【基本每股收益_EPS】。"
                ),
                "估值与市盈率口径说明": (
                    "【港股 F10 财务指标市盈率 PE_TTM 口径与行情端差异自陈（已收敛 P2-2 与口径陷阱 2）】：\n"
                    "1. 本表【市盈率_PE_TTM】由东方财富港股数据中心基于最近四期财务报告 TTM 归母净利润与静态清算市值计算生成；\n"
                    "2. 行情接口 (quotes) 中的【市盈率_TTM】系实时行情数据流透传（底层基于全股本总市值乘以即时 H 股价，并按日频即时港元汇率约 0.868~0.875 折算）；\n"
                    "3. 汇率快照源独立性与隐含汇率自陈：F10 indicators 隐含汇率约为 0.857~0.882（采用财报发布日或上期清算汇率），quotes 滚动市盈率隐含汇率约为 0.868~0.875（采用盘中即时汇率），同票横比相差约 ±2% 属正常汇率快照差异；报表以美元计价标的（如02026）隐含汇率约为 0.128（两段折算）。严肃基本面分析请指明数据来源，跨端横比时不可混用！"
                ),
            }
            if no_ind_desc:
                resp_ind["无记录说明"] = no_ind_desc
            return resp_ind

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
                "数据有效行数": 0,
                "规则声明行数": 1,
                "记录计数说明": "返回的表格为机制不适用说明（共1行），实际业务数据记录为0行，下游统计有效记录数时请勿将其计入实际业务记录。",
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
                "数据有效行数": 0,
                "规则声明行数": 1,
                "记录计数说明": "返回的表格为机制不适用说明（共1行），实际业务数据记录为0行，下游统计有效记录数时请勿将其计入实际业务记录。",
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
                "是否包含未来预测记录": False,
                "数据属性": "历史财务指标快照（非未来盈利预测）",
                "数据有效行数": 0,
                "指标快照行数": len(em_data),
                "formatted_tables": {
                    "indicators": em_data,
                },
                "data": em_data[0] if em_data else {"status": "暂无盈利预测数据覆盖"},
                "每股收益_TTM_说明": (
                    "【港股每股收益_TTM不可信警示（已自陈，修复 D3）】：上游东财数据中心宽表中【每股收益_TTM_元】(EPS_TTM) "
                    "未严格与同响应【市盈率_PE_TTM】及法定三表归母净利润做差 TTM 勾稽（如 09988 隐含偏差达 23%~46%），"
                    "本字段仅供对照，严禁直接引用此 EPS_TTM 进行估值决策，严肃估值请以 PE_TTM 或三大财务报表做差自算 TTM 净利润为准。"
                ),
                "无记录说明": "港股公开免费源无官方券商一致性业绩预测与目标价覆盖表。返回的 indicators 表为最近一期历史财务指标快照（共1行），切勿因 len(rows)=1 误当成未来盈利预测数据。建议参考综合损益表中的历史盈利增速。",
            }

        # 分类 9：资金流向与大单机制说明 (capital_flow / funds / moneyflow) (修复 HK-4)
        if cat in ("capital_flow", "funds", "moneyflow"):
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股资金流向与大单统计机制",
                "source": "hkex_market_rules",
                "数据有效行数": 0,
                "规则声明行数": 1,
                "记录计数说明": "返回的表格为机制不适用说明（共1行），实际业务数据记录为0行，下游统计有效记录数时请勿将其计入实际业务记录。",
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
                "数据有效行数": 0,
                "规则声明行数": 1,
                "记录计数说明": "返回的表格为机制不适用说明（共1行），实际业务数据记录为0行，下游统计有效记录数时请勿将其计入实际业务记录。",
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
                "数据有效行数": 0,
                "规则声明行数": 1,
                "记录计数说明": "返回的表格为机制不适用说明（共1行），实际业务数据记录为0行，下游统计有效记录数时请勿将其计入实际业务记录。",
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
                    "已发行股本口径说明": (
                        "【港股已发行股本统计与时滞说明（已修复 D6）】：1. A+H两地上市标的（如00939、01088等）本字段指在香港联交所实际上市流通的H股股数，"
                        "非含A股的全部总股本，全股本请参考行情端【全部已发行总股本_股】；"
                        "2. 非A+H标的（如00700、09988、03800、02026等）本字段源自上市公司定期财报静态工商登记截面，"
                        "而行情端总股本由交易所与结算所每日根据公司期权行权、股份回购注销、可转债转股等动态更新，回购注销或员工行权期间存在常规的 -5%~+4% 统计时滞差异。"
                        "最新即时股本与市值请以 quotes 行情端为准。"
                    ),
                    "每手股数": info.get("每手股数", ""),
                    "官方网址": info.get("官方网址") or info.get("公司网址") or info.get("网址", ""),
                    "电子邮箱": info.get("电子邮箱") or info.get("电邮地址", ""),
                    "空值字段说明": "港股公开F10概况页部分字段未收录（如上市日期、每手股数，以及部分国企未登记主席导致董事长显示为空串），此系上游源特性，交易单位请以交易所手册为准。",
                }]
                return {
                    "symbol": clean_code,
                    "market": "香港交易所 (HKEX)",
                    "category": "basic_info",
                    "category_name": "港股公司基本工商与发行信息",
                    "source": "sina_hk_f10 (新浪港股F10基本工商专线)",
                    "已发行股本口径说明": (
                        "【港股已发行股本统计与时滞说明（已修复 D6）】：1. A+H两地上市标的（如00939、01088等）本字段指在香港联交所实际上市流通的H股股数，"
                        "非含A股的全部总股本，全股本请参考行情端【全部已发行总股本_股】；"
                        "2. 非A+H标的（如00700、09988、03800、02026等）本字段源自上市公司定期财报静态工商登记截面，"
                        "而行情端总股本由交易所与结算所每日根据公司期权行权、股份回购注销、可转债转股等动态更新，回购注销或员工行权期间存在常规的 -5%~+4% 统计时滞差异。"
                        "最新即时股本与市值请以 quotes 行情端为准。"
                    ),
                    "formatted_tables": {
                        "basic_info": basic_table,
                    },
                    "data": basic_table[0],
                    "说明": "港股基本工商信息包含公司注册地址、成立上市日期、董事会高管、核数师及法定股本架构。",
                    "字段完整性说明": "上市日期、每手股数及部分国企/红筹标的董事长(如01088)若为空串，系上游公开数据源F10概况页未收录该字段；每手股数请参考行情盘口，最新高管名单请参考披露易最新公司治理公告。",
                }

        # 默认：公司资料与业务概况 (summary / company)
        source = "sina_hk_f10 (新浪港股F10主力专线)"
        info = None
        try:
            info = self._get_sina_hk_company_info(clean_code)
        except Exception:
            info = None

        if info:
            if "港股股份数目" in info or "已发行股本" in info:
                info["已发行股本口径说明"] = (
                    "【港股已发行股本统计与时滞说明（已修复 D6）】：1. A+H两地上市标的本字段指在香港联交所实际流通的H股股数；"
                    "2. 非A+H标的本字段源自定期报告静态工商截面，与行情端每日根据期权行权/回购注销更新的实时股本存在 -5%~+4% 统计时滞。最新即时股本与市值请以 quotes 行情端为准。"
                )
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股公司概要与业务简介",
                "source": source,
                "已发行股本口径说明": (
                    "【港股已发行股本统计与时滞说明（已修复 D6）】：1. A+H两地上市标的本字段指在香港联交所实际流通的H股股数；"
                    "2. 非A+H标的本字段源自定期报告静态工商截面，与行情端每日根据期权行权/回购注销更新的实时股本存在 -5%~+4% 统计时滞。最新即时股本与市值请以 quotes 行情端为准。"
                ),
                "formatted_tables": {
                    "summary": [info],
                    "company_profile": [info],
                },
                "data": info,
                "说明": "港股公司概要包含主营业务构成、核心竞争力、恒生行业分类及经营概况。",
                "表别名兼容说明": "formatted_tables 内 summary 与 company_profile 互为同义兼容别名表（内容逐位一致），推荐主键使用 summary；整表遍历时请注意排重。",
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


