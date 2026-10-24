"""MCPHub Finance A股独立处理引擎 (AStockEngine)。
基于通达信官方专线与新浪/东财高可用备份矩阵，提供中国 A 股全市场（沪深北）
的实体解析、实时盘口、多周期 K 线、CAS 工商业与银行三大财务报表、
F10 深度题材与筹码、以及自然语言智能选股。
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

from ..core.constants import *
from ..core.indicators import calculate_indicators
from ..core.market_utils import infer_market_setcode
from ..core.network import _SECURE_OPENER
from .base import BaseMarketEngine


class AStockEngine(BaseMarketEngine):
    """A 股通达信专线独立处理引擎"""

    def __init__(
        self,
        token: str = "",
        api_endpoint: str = "http://tdxhub.icfqs.com:7615/TQLEX",
        rag_endpoint: str = "https://ai.icfqs.com:8965/v1/rag-entity-retrieve",
        timeout: int = 15,
        logger: logging.Logger | None = None,
        post_fn: Callable[[str, dict[str, Any] | list[Any]], Any] | None = None,
    ):
        self.token = token.strip()
        self.api_endpoint = api_endpoint.rstrip("/")
        self.rag_endpoint = rag_endpoint
        self.timeout = timeout
        self.logger = logger or logging.getLogger("mcp_gateway.finance.a_engine")
        self._entity_cache: dict[str, dict[str, Any] | None] = {}
        self._post_fn = post_fn

    def resolve_entity(self, clean_code: str) -> dict[str, Any] | None:
        """A 股 6 位代码三轨反查正主实体"""
        clean_code = clean_code.strip()
        if clean_code in self._entity_cache:
            return self._entity_cache[clean_code]
        res = self._do_resolve_entity(clean_code)
        self._entity_cache[clean_code] = res
        return res

    def _do_resolve_entity(self, clean_code: str) -> dict[str, Any] | None:
        if clean_code.isdigit() and len(clean_code) == 6:
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
                raw_data = f10_res.get("data", {})
                for rs in raw_data.get("ResultSets", []):
                    if rs.get("ResultSetKey") == "table8":
                        cols = rs.get("ColName", [])
                        contents = rs.get("Content", [])
                        if "name" in cols and contents:
                            name_idx = cols.index("name")
                            t8_name = contents[0][name_idx]
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

        return None

    def _resolve_exact_security_entity(self, clean_code: str) -> dict[str, Any] | None:
        return self.resolve_entity(clean_code)

    def _post(self, url: str, body: dict[str, Any] | list[Any]) -> Any:
        if self._post_fn is not None:
            return self._post_fn(url, body)
        return self._default_post(url, body)

    def _default_post(self, url: str, body: dict[str, Any] | list[Any]) -> Any:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "MCPHub-Finance-Gateway/1.0",
        }
        if self.token:
            headers["token"] = self.token

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=payload, headers=headers, method="POST")

        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                cur_timeout = 8.0 if attempt < max_retries else self.timeout
                with _SECURE_OPENER.open(req, timeout=cur_timeout) as resp:
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
            except (URLError, TimeoutError, socket.timeout) as exc:
                if attempt < max_retries:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                reason = getattr(exc, "reason", str(exc))
                raise RuntimeError(f"TDX API network connection error ({reason})") from exc
            except Exception as exc:
                if attempt < max_retries and ("timeout" in str(exc).lower() or "handshake" in str(exc).lower()):
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise


    # =========================================================================
    # 【功能块 5.55：A股历年分红派息与送配明细抓取器 (新浪财经分红专线)】
    # - 输入：clean_code (6位A股代码)
    # - 功能：抓取A股公司历年分红派息、送股转增、股权登记日及除权除息日明细
    # - 输出：list[dict[str, Any]] 结构化分红派息记录列表
    # =========================================================================
    def _get_ashare_dividends(self, clean_code: str) -> list[dict[str, Any]]:
        clean_code = re.sub(r"[^\d]", "", clean_code).zfill(6)
        url = f"http://vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/{clean_code}.phtml"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                html = raw_bytes.decode("gbk", errors="ignore")
        except Exception:
            return []

        m = re.search(r'<table id="sharebonus_1"[^>]*>(.*?)</table>', html, re.S)
        if not m:
            return []

        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S)
        results = []
        for r in rows:
            cells = [
                re.sub(r"<[^>]+>", "", c).strip()
                for c in re.findall(r"<t[dh][^>]*>(.*?)</(?:td|th)>", r, re.S)
            ]
            if len(cells) >= 8 and cells[0] not in ("公告日期", ""):
                def _parse_div_num(val_str: str) -> float:
                    if not val_str or val_str.strip() in ("--", "-", "null", "None"):
                        return 0.0
                    try:
                        return round(float(val_str.strip()), 4)
                    except (ValueError, TypeError):
                        return 0.0

                results.append({
                    "公告日期": cells[0],
                    "送股_每10股": _parse_div_num(cells[1]),
                    "转增_每10股": _parse_div_num(cells[2]),
                    "派息_每10股_含税": _parse_div_num(cells[3]),
                    "进度": cells[4],
                    "除权除息日": cells[5],
                    "股权登记日": cells[6],
                    "红股上市日": cells[7] if len(cells) > 7 else "--",
                })
        return results

    # =========================================================================
    # 【功能块 5.5.1：A股官方龙虎榜异动与营业部买卖席位数据提取器】
    # - 输入：clean_code (str): 6位A股代码（如 "600875", "002594"）
    # - 功能：调用东方财富数据中心龙虎榜官方数据专线 (RPT_DAILYBILLBOARD_DETAILS 与 RPT_BILLBOARD_DAILYDETAILSBUY)，
    #         提取标的近期上榜日期、收盘价、偏离值幅度、异动原因及前五大买卖营业部席位金额与净买额。
    # - 输出：dict[str, Any]: 结构化龙虎榜记录与席位明细字典
    # =========================================================================
    def _get_ashare_lhb(self, clean_code: str) -> dict[str, Any]:
        clean_code = re.sub(r"[^\d]", "", clean_code).zfill(6)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://data.eastmoney.com/"}

        billboard_records = []
        try:
            url_board = (
                f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
                f"reportName=RPT_DAILYBILLBOARD_DETAILS&columns=ALL&"
                f"filter=(SECURITY_CODE%3D%22{clean_code}%22)&sortColumns=TRADE_DATE&sortTypes=-1&pageSize=10"
            )
            req = Request(url_board, headers=headers)
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                d = json.loads(resp.read().decode("utf-8", errors="replace"))
                if d.get("result") and d["result"].get("data"):
                    for row in d["result"]["data"]:
                        t_date = str(row.get("TRADE_DATE", ""))[:10]
                        billboard_records.append({
                            "交易日期": t_date,
                            "收盘价": row.get("CLOSE_PRICE"),
                            "涨跌幅_pct": row.get("CHANGE_RATE"),
                            "龙虎榜买入总额_万元": round(float(row.get("BILLBOARD_BUY_AMT") or 0.0), 2),
                            "龙虎榜卖出总额_万元": round(float(row.get("BILLBOARD_SELL_AMT") or 0.0), 2),
                            "龙虎榜净买入额_万元": round(float(row.get("BILLBOARD_NET_AMT") or 0.0), 2),
                            "市场总成交额_万元": round(float(row.get("ACCUM_AMOUNT") or 0.0) / 10000.0, 2) if row.get("ACCUM_AMOUNT") else None,
                            "上榜原因": row.get("EXPLANATION") or row.get("EXPLAIN"),
                        })
        except Exception:
            pass

        seat_records = []
        try:
            url_seats = (
                f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
                f"reportName=RPT_BILLBOARD_DAILYDETAILSBUY&columns=ALL&"
                f"filter=(SECURITY_CODE%3D%22{clean_code}%22)&sortColumns=TRADE_DATE&sortTypes=-1&pageSize=20"
            )
            req = Request(url_seats, headers=headers)
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                d = json.loads(resp.read().decode("utf-8", errors="replace"))
                if d.get("result") and d["result"].get("data"):
                    for row in d["result"]["data"]:
                        t_date = str(row.get("TRADE_DATE", ""))[:10]
                        seat_records.append({
                            "交易日期": t_date,
                            "营业部席位名称": row.get("OPERATEDEPT_NAME"),
                            "买入金额_万元": round(float(row.get("BUY") or 0.0) / 10000.0, 2),
                            "卖出金额_万元": round(float(row.get("SELL") or 0.0) / 10000.0, 2),
                            "净买入额_万元": round(float(row.get("NET") or 0.0) / 10000.0, 2),
                            "占总成交比例_pct": round(float(row.get("TOTAL_BUYRIO") or 0.0) * 100.0, 2) if row.get("TOTAL_BUYRIO") else None,
                            "上榜异动说明": row.get("EXPLANATION"),
                        })
        except Exception:
            pass

        return {
            "symbol": clean_code,
            "category": "lhb",
            "category_name": "龙虎榜异动与营业部席位追踪",
            "source": "eastmoney_billboard (东方财富龙虎榜官方数据专线)",
            "formatted_tables": {
                "billboard_records": billboard_records,
                "seat_details": seat_records,
            },
            "无记录说明": "该标的近期未触发交易所龙虎榜公开信息披露标准（近期无严重异动/偏离值未达标），暂无营业部席位数据。" if not billboard_records else None,
        }

    # =========================================================================
    # 【功能块 5.5.2：A股官方股份回购方案与实施进度提取器】
    # - 输入：clean_code (str): 6位A股代码（如 "600875", "000333"）
    # - 功能：调用东方财富数据中心股票回购专线 (RPTA_WEB_GETHGLIST_NEW)，
    #         提取上市公司回购方案类型、回购目的、进展状态、拟回购金额上下限、已回购金额及公告日程。
    # - 输出：dict[str, Any]: 结构化股份回购方案与明细字典
    # =========================================================================
    def _get_ashare_repurchases(self, clean_code: str) -> dict[str, Any]:
        clean_code = re.sub(r"[^\d]", "", clean_code).zfill(6)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://data.eastmoney.com/"}
        url = (
            f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
            f"reportName=RPTA_WEB_GETHGLIST_NEW&columns=ALL&"
            f"filter=(DIM_SCODE%3D%22{clean_code}%22)&sortColumns=NOTICEDATE&sortTypes=-1&pageSize=20"
        )
        records = []
        try:
            req = Request(url, headers=headers)
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                d = json.loads(resp.read().decode("utf-8", errors="replace"))
                if d.get("result") and d["result"].get("data"):
                    for row in d["result"]["data"]:
                        n_date = str(row.get("NOTICEDATE", ""))[:10] if row.get("NOTICEDATE") else None
                        s_date = str(row.get("REPURSTARTDATE", ""))[:10] if row.get("REPURSTARTDATE") else None
                        e_date = str(row.get("REPURENDDATE", ""))[:10] if row.get("REPURENDDATE") else None
                        f_date = str(row.get("FINISHDATE", ""))[:10] if row.get("FINISHDATE") else None
                        amt_lower = round(float(row.get("REPURAMOUNTLOWER") or 0.0) / 10000.0, 2) if row.get("REPURAMOUNTLOWER") else None
                        amt_limit = round(float(row.get("REPURAMOUNTLIMIT") or 0.0) / 10000.0, 2) if row.get("REPURAMOUNTLIMIT") else None
                        amt_done = round(float(row.get("REPURAMOUNT") or 0.0) / 10000.0, 2) if row.get("REPURAMOUNT") else None
                        records.append({
                            "公告日期": n_date,
                            "回购方案类型": row.get("REPUROBJECTIVE"),
                            "回购进展状态": row.get("REPURPROGRESS_NAME") or "实施中",
                            "拟回购金额下限_万元": amt_lower,
                            "拟回购金额上限_万元": amt_limit,
                            "已实施回购金额_万元": amt_done,
                            "回购价格上限_元": row.get("REPURPRICECAP"),
                            "回购价格下限_元": row.get("REPURPRICELOWER"),
                            "回购股数上限_万股": round(float(row.get("REPURNUMCAP") or 0.0) / 10000.0, 2) if row.get("REPURNUMCAP") else None,
                            "回购实施起始日": s_date,
                            "回购实施截止日": e_date,
                            "方案完成日期": f_date,
                        })
        except Exception:
            pass

        return {
            "symbol": clean_code,
            "category": "repurchases",
            "category_name": "股份回购方案与实施明细",
            "source": "eastmoney_repurchases (东方财富官方回购数据专线)",
            "formatted_tables": {
                "repurchases": records,
            },
            "无记录说明": "该标的历史无公开披露的股份回购实施记录（或近期未发布股份回购方案）。如需获取公司最新重大公告，请调用 stock_news 工具。" if not records else None,
        }


    def get_quotes(self, symbol: str, setcode: str | None = None) -> dict[str, Any]:
        """获取实时盘口、深度估值指标与五档买卖盘（支持 A 股与港股）"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        if not clean_code:
            raise ValueError(f"无效的证券代码 '{symbol}'，证券标的代码必须包含有效数字编号（如 A股'600519'、港股'00700'）。若仅知道公司名称，请先调用 stock_lookup 工具查找对应代码。")

        if len(clean_code) == 5 or symbol.strip().upper().startswith("HK"):
            raise ValueError(f"标的代码 '{symbol}' 为港股标的，请调用港股引擎。")

        inferred = infer_market_setcode(clean_code)
        if setcode is not None:
            sc_str = str(setcode).strip().lower()
            sc_map = {"sh": "1", "sse": "1", "1": "1", "sz": "0", "szse": "0", "0": "0", "bj": "2", "bse": "2", "2": "2"}
            normalized_sc = sc_map.get(sc_str, sc_str)
            if normalized_sc != inferred:
                m_name = "上海证券交易所 (SSE, setcode=1)" if inferred == "1" else ("深圳证券交易所 (SZSE, setcode=0)" if inferred == "0" else "北京证券交易所 (BSE, setcode=2)")
                raise ValueError(
                    f"证券代码 '{clean_code}' 所属交易所市场为 {m_name}，"
                    f"与传入的 setcode='{setcode}' 冲突。禁止跨市场错误指定 setcode，建议省略 setcode 参数由系统自动推断。"
                )
            m_setcode = normalized_sc
        else:
            m_setcode = inferred

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

        base_info = raw.get("BaseInfo", {}) if isinstance(raw, dict) else {}
        stock_name = base_info.get("Name", "") if isinstance(base_info, dict) else ""
        if not stock_name:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity and entity.get("entity_name"):
                stock_name = entity.get("entity_name")

        market_desc = (
            "上海证券交易所 (SSE)"
            if str(m_setcode) == "1"
            else (
                "深圳证券交易所 (SZSE)"
                if str(m_setcode) == "0"
                else (
                    "北京证券交易所 (BSE)"
                    if str(m_setcode) == "2"
                    else f"市场代码_{m_setcode}"
                )
            )
        )

        result: dict[str, Any] = {
            "symbol": clean_code,
            "name": stock_name,
            "setcode": m_setcode,
            "market": market_desc,
            "source": "tdx_official_专线",
        }

        # 1. 核心行情字段中文化映射
        hq_raw = raw.get("HQInfo", {})
        ext_raw = raw.get("ExtInfo", {})
        now_p = float(hq_raw.get("Now", 0.0) or 0.0) if isinstance(hq_raw, dict) else 0.0
        close_p = float(hq_raw.get("Close", 0.0) or 0.0) if isinstance(hq_raw, dict) else 0.0
        zgb_wan = float(ext_raw.get("ZGB", 0.0) or 0.0) if isinstance(ext_raw, dict) else 0.0

        # 防空壳/无效标的/跨市场静默错标拦截：若通达信返回空盘口或现价/昨收/股本均无效，进行正主实体核验
        if not hq_raw or (now_p <= 0 and close_p <= 0 and zgb_wan <= 0):
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到证券代码 '{symbol}' 的有效盘口行情（代码不存在或非有效 A 股/港股标的）。"
                    "若不确定代码，请先调用 stock_lookup 工具查询标准证券名称与代码。"
                )
            else:
                raise ValueError(
                    f"未查询到证券代码 '{symbol}' 在指定市场 (setcode={m_setcode}) 的有效盘口行情（代码在该市场无有效交易盘口）。"
                    "请核对市场归属，或省略 setcode 参数由系统自动推断。"
                )

        if isinstance(hq_raw, dict):
            mapped_hq = {HQ_FIELD_MAP.get(k, k): v for k, v in hq_raw.items()}
            if stock_name:
                mapped_hq["股票名称"] = stock_name

            # 彻底拦截上游将流通股本串入 Yield 与 Jjjz 的缺陷 (P1)
            mapped_hq.pop("Yield", None)
            mapped_hq.pop("收益率_Yield", None)
            is_fund = clean_code.startswith(("51", "15", "16", "50", "58"))
            if not is_fund:
                mapped_hq.pop("Jjjz", None)
                mapped_hq.pop("基金净值", None)
            else:
                if "Jjjz" in mapped_hq:
                    try:
                        mapped_hq["基金净值"] = round(float(mapped_hq.pop("Jjjz")), 4)
                    except (ValueError, TypeError):
                        pass

            if "ItemNum" in mapped_hq:
                mapped_hq["明细笔数"] = mapped_hq.pop("ItemNum")

            # 修复 P3: 盘后成交量与剩余委托量保底规整（若标的当日未参与盘后固定价格交易或无收盘撮合残单，上游缺省不返回，补齐保底为 0 手）
            if mapped_hq.get("盘后成交量") in (None, ""):
                mapped_hq["盘后成交量"] = 0
            if mapped_hq.get("剩余委托量") in (None, ""):
                mapped_hq["剩余委托量"] = 0

            # 数值类型严格对齐：消除跨标的 str/int/float 漂移 (P1)
            int_keys = ("成交量_手", "内盘_手", "外盘_手", "成交笔数", "剩余委托量", "明细笔数", "内外盘强弱标志", "盘后成交量")
            for ik in int_keys:
                if ik in mapped_hq and mapped_hq[ik] not in (None, ""):
                    try:
                        mapped_hq[ik] = int(float(mapped_hq[ik]))
                    except (ValueError, TypeError):
                        pass

            float_keys = ("现价", "昨收", "今开", "最高价", "最低价", "最高", "最低", "成交均价", "成交额_元", "量比", "换手率_pct")
            for fk in float_keys:
                if fk in mapped_hq and mapped_hq[fk] not in (None, ""):
                    try:
                        mapped_hq[fk] = round(float(mapped_hq[fk]), 2 if "额" in fk or "均价" in fk or "价" in fk else 4)
                    except (ValueError, TypeError):
                        pass

            # 自动计算涨跌幅与日内真实振幅（遵循交易所标准公式：(最高-最低)/昨收*100，彻底避免通达信Yield字段污染）
            high_p = float(hq_raw.get("MaxP", 0.0) or 0.0)
            low_p = float(hq_raw.get("MinP", 0.0) or 0.0)
            if close_p > 0 and now_p > 0:
                mapped_hq["涨跌幅_pct"] = round(((now_p - close_p) / close_p) * 100.0, 2)
                mapped_hq["涨跌额"] = round(now_p - close_p, 3)
            if close_p > 0 and high_p > 0 and low_p > 0:
                amp_val = round(((high_p - low_p) / close_p) * 100.0, 2)
                mapped_hq["日内振幅_pct"] = amp_val
                mapped_hq["振幅_pct"] = amp_val
            if "最高价" in mapped_hq and "最高" not in mapped_hq:
                mapped_hq["最高"] = mapped_hq["最高价"]
            if "最低价" in mapped_hq and "最低" not in mapped_hq:
                mapped_hq["最低"] = mapped_hq["最低价"]
            if "涨跌额" in mapped_hq:
                mapped_hq["涨跌额_元"] = mapped_hq["涨跌额"]
            if "行情时间" in mapped_hq:
                mapped_hq["更新时间"] = mapped_hq["行情时间"]
            result["盘口核心行情"] = mapped_hq

        # 2. 估值与股本指标中文化映射与年化口径对齐
        ext_raw = raw.get("ExtInfo", {})
        pro_raw = raw.get("ProInfo", {})
        if isinstance(ext_raw, dict):
            mapped_ext = {EXT_FIELD_MAP.get(k, k): v for k, v in ext_raw.items()}
            # 规范化整型标志位与浮点数
            bool_tag_keys = ("是否属于融资融券标的", "是否属于沪股通标的", "是否属于深股通标的", "是否属于沪深300标的", "是否含权证或转债")
            for bk in bool_tag_keys:
                if bk in mapped_ext and mapped_ext[bk] not in (None, ""):
                    try:
                        mapped_ext[bk] = int(float(mapped_ext[bk]))
                    except (ValueError, TypeError):
                        pass
            if "自由流通股本_万股" in mapped_ext and mapped_ext["自由流通股本_万股"] not in (None, ""):
                try:
                    mapped_ext["自由流通股本_万股"] = round(float(mapped_ext["自由流通股本_万股"]), 2)
                except (ValueError, TypeError):
                    pass
            if "行业涨跌幅_pct" in mapped_ext and mapped_ext["行业涨跌幅_pct"] not in (None, ""):
                try:
                    mapped_ext["行业涨跌幅_pct"] = round(float(mapped_ext["行业涨跌幅_pct"]), 2)
                except (ValueError, TypeError):
                    pass
            # 计算市净率 PB、年化每股收益，并附加口径防幻觉说明
            now_p = float(hq_raw.get("Now", 0.0) or 0.0) if isinstance(hq_raw, dict) else 0.0
            mgjzc = float(ext_raw.get("MGJZC", 0.0) or 0.0)
            syl = float(ext_raw.get("SYL", 0.0) or 0.0)
            mgsy = float(ext_raw.get("MGSY", 0.0) or 0.0)
            zgb_wan = float(ext_raw.get("ZGB", 0.0) or 0.0)
            ltgb_wan = float(ext_raw.get("LTGB", 0.0) or 0.0)
            if zgb_wan > 0:
                mapped_ext["总股本_股"] = int(round(zgb_wan * 10000))
            if ltgb_wan > 0:
                mapped_ext["流通股本_股"] = int(round(ltgb_wan * 10000))

            # 交叉比对 F10 summary table4 最新财报，防止上游行情专线 ExtInfo 字段滞留上一期 (修复 P1-3 与 P2-1)
            clean_code = re.sub(r"[^\d]", "", symbol)
            if clean_code:
                try:
                    f10_sum = self.get_f10(clean_code, category="summary")
                    t4_row = None
                    t6_row = None
                    for t in f10_sum.get("data", {}).get("ResultSets", []):
                        rk = t.get("ResultSetKey")
                        if rk == "table4" and t.get("Content"):
                            t4_row = dict(zip(t.get("ColName", []), t["Content"][0]))
                        elif rk == "table6" and t.get("Content"):
                            t6_row = dict(zip(t.get("ColName", []), t["Content"][0]))
                    
                    # 修复 P3-6：若 F10 table6 存在精确到个位的真实法定总股本与流通A股，优先覆盖万股截断值
                    if t6_row:
                        t6_zgb = int(float(t6_row.get("zgb") or 0))
                        t6_ltgb = int(float(t6_row.get("ltgb") or 0))
                        if t6_zgb > 0:
                            mapped_ext["总股本_股"] = t6_zgb
                        if t6_ltgb > 0:
                            mapped_ext["流通股本_股"] = t6_ltgb

                    if t4_row and t4_row.get("bgq"):
                        f10_bgq = str(t4_row.get("bgq"))
                        f10_mgsy = float(t4_row.get("mgsy") or 0.0)
                        f10_mgjzc = float(t4_row.get("mgjzc") or 0.0)
                        mapped_ext["最新财报报告期"] = f10_bgq
                        is_stale = False
                        if f10_mgsy > 0 and (abs(f10_mgsy - mgsy) > 0.001 or (f10_mgjzc > 0 and abs(f10_mgjzc - mgjzc) > 0.01)):
                            is_stale = True

                        if is_stale:
                            mgsy = f10_mgsy
                            mgjzc = f10_mgjzc
                            mapped_ext["报告期每股收益_EPS"] = mgsy
                            mapped_ext["每股净资产_BPS"] = mgjzc
                            ann_factor = 4.0 if f10_bgq.endswith("-03-31") else (2.0 if f10_bgq.endswith("-06-30") else (4.0 / 3.0 if f10_bgq.endswith("-09-30") else 1.0))
                            ann_eps_raw = mgsy * ann_factor
                            # 修复 P2-1：换期校准双侧严格代数闭环，确保 [现价 ÷ 年化每股收益_EPS = 动态市盈率_PE] 绝对互逆
                            if ann_eps_raw > 0 and now_p > 0:
                                syl = round(now_p / ann_eps_raw, 2)
                                mapped_ext["动态市盈率_PE"] = syl
                                mapped_ext["年化每股收益_EPS"] = round(now_p / syl, 4) if syl > 0 else round(ann_eps_raw, 4)
                            else:
                                mapped_ext["年化每股收益_EPS"] = round(ann_eps_raw, 4)
                            mapped_ext["派生指标换期说明"] = f"检测到上游行情盘口指标滞留于旧期次，已自动自算对齐至最新披露的 {f10_bgq} 财报口径 (EPS={f10_mgsy}元, BPS={f10_mgjzc}元)。"
                        else:
                            mapped_ext["派生指标换期说明"] = f"上游行情盘口指标与最新披露的 {f10_bgq} 财报完全同步对齐，无需换期重算。"
                except Exception:
                    pass

            if mgjzc > 0:
                if now_p > 0 and "市净率_PB" not in mapped_ext:
                    mapped_ext["市净率_PB"] = round(now_p / mgjzc, 2)
            if syl != 0:
                mapped_ext["动态市盈率_PE"] = round(syl, 2)
                if now_p > 0 and "年化每股收益_EPS" not in mapped_ext:
                    mapped_ext["年化每股收益_EPS"] = round(now_p / syl, 4)

            # 从 ProInfo (或 ExtInfo 备用源) 中提取真正滚动过去 4 季度的 TTM 滚动市盈率与静态市盈率
            pe_ttm = None
            pe_static = None
            if isinstance(pro_raw, dict):
                pe_ttm = pro_raw.get("PETTM")
                pe_static = pro_raw.get("PEStatic")
            if pe_ttm is None and isinstance(ext_raw, dict):
                pe_ttm = ext_raw.get("PETTM") or ext_raw.get("SYL_TTM")
            if pe_static is None and isinstance(ext_raw, dict):
                pe_static = ext_raw.get("PEStatic") or ext_raw.get("SYL_Static")

            if pe_ttm is not None and str(pe_ttm).strip() != "":
                try:
                    mapped_ext["滚动市盈率_PETTM"] = round(float(pe_ttm), 2)
                except (ValueError, TypeError):
                    pass
            if pe_static is not None and str(pe_static).strip() != "":
                try:
                    mapped_ext["静态市盈率_PEStatic"] = round(float(pe_static), 2)
                    mapped_ext["静态市盈率_PE_LYR"] = round(float(pe_static), 2)
                except (ValueError, TypeError):
                    pass

            ann_eps_val = mapped_ext.get("年化每股收益_EPS")
            ann_eps_str = f"年化每股收益≈{ann_eps_val}元，现价/年化EPS=动态市盈率" if ann_eps_val is not None else "当前亏损或暂无年化每股收益"
            ttm_val = mapped_ext.get("滚动市盈率_PETTM")
            ttm_note = f"滚动市盈率_PETTM({ttm_val}倍)按过去4季度滚动归母净利测算；" if ttm_val is not None else ""
            pb_val = mapped_ext.get("市净率_PB")
            pb_note = (
                f"市净率_PB({pb_val}倍)由[现价÷每股净资产_BPS]派生；若标的近期发生送转增发导致总股本变动，"
                "上游每股净资产若停留在报告期旧股本口径将导致PB低估，精确估值建议结合资产负债表[总市值÷归母所有者权益]进行核验；"
            ) if pb_val is not None else ""
            mapped_ext["估值口径说明"] = (
                f"{ttm_note}{pb_note}动态市盈率_PE({mapped_ext.get('动态市盈率_PE')}倍)采用最新报告期年化折算口径({ann_eps_str})；"
                f"报告期每股收益_EPS({mgsy}元)为非年化单期累计值，两者口径不同，禁止直接互乘推导价格或市值。"
            )
            mapped_ext["PE使用纪律说明"] = (
                "【PE口径防误导铁律】报告与回答中提及市盈率必须严格指明是哪一个，严禁混称：\n"
                "1.【滚动市盈率_PETTM】(价值投资推荐口径)：基于过去4个季度滚动归母净利润(真实TTM)计算，能平滑季节性波动；\n"
                "2.【动态市盈率_PE】：现价÷最新单季年化每股收益(年化口径)，一季度净利×4、半年度×2，易受单季突发收支和淡旺季严重扰动；\n"
                "3.【静态市盈率_PEStatic】：现价÷上一完整会计年度每股收益。报告中必须明确注明所用PE类型；\n"
                "4.【财报披露期刷新时差警示】：财报密集披露期，上游专线不同估值字段存在刷新时差（如PETTM已按最新季报更新，但动态PE或BPS仍停留在上一期），请结合 stock_financials 交叉核对报告期；\n"
                "5.【商业银行市净率特别提示】：银行标的（如邮储银行 601658、工商银行）包含庞大永续债/优先股等【其他权益工具】。银行官方每股净资产 BPS 依法严格扣除了其他权益工具（普通股净资产 = 归母净资产 − 其他权益工具），其 PB(如邮储银行 0.63倍) 与工业股直接按【市值÷归母总权益】(0.550倍) 存在准则差异，不可直接横向混比；\n"
                "6.【银行股动态PE与年化EPS永续债利息简化偏差】：商业银行发行有大额永续债/优先股，上游行情源（通达信/腾讯等）动态 PE 普遍采用【归母净利×年化倍数÷总股本】简化公式，未剔除永续债分派利息，相对官方扣除永续债利息后的真实基本 EPS（如 601658 官方中报 EPS 0.40元，年化0.80元）存在年化EPS偏高约7%、动态PE偏低约7%的行情源标准简化口径偏差；精确估值建议结合财报官方基本每股收益进行自算；\n"
                "7.【负市盈率警示】：当标的滚动过去4季累计净利为负时，PE呈现负值（如 -940倍），此为亏损代号，绝非低估值！"
            )
            # 整理并派生涨跌停价指标，彻底消除休市后 2% 隔日口径偏差
            zt_curr = mapped_ext.get("涨停价")
            dt_curr = mapped_ext.get("跌停价")
            if zt_curr is not None:
                mapped_ext["当日涨停价"] = zt_curr
            if dt_curr is not None:
                mapped_ext["当日跌停价"] = dt_curr

            clean_sym = re.sub(r"[^\d]", "", symbol)
            name_str = str(raw.get("Name", "") or "")
            is_st = "ST" in name_str.upper() or "*ST" in name_str.upper()
            if clean_sym.startswith(("688", "300", "301")):
                r_limit = 0.20
            elif clean_sym.startswith(("8", "4", "920")):
                r_limit = 0.30
            elif is_st:
                r_limit = 0.05
            else:
                r_limit = 0.10

            ref_price = now_p if now_p > 0 else close_p
            if ref_price > 0:
                next_zt = round(ref_price * (1.0 + r_limit) + 1e-6, 2)
                next_dt = round(ref_price * (1.0 - r_limit) + 1e-6, 2)
                mapped_ext["次日参考涨停价"] = next_zt
                mapped_ext["次日参考跌停价"] = next_dt
                mapped_ext["次日涨跌幅限制_pct"] = round(r_limit * 100.0, 1)
                mapped_ext["涨跌停价时效说明"] = (
                    f"【涨跌停价时效与次日限价说明】表中【涨停价】/【当日涨停价】({zt_curr}元)为上游交易所针对当日交易日（基于昨收价）设定的法定限价；"
                    f"盘后休市期间，上游字段尚未更新至下一个交易日（如 600875 盘后字段 24.15元 仍为当日限价）。"
                    f"网关已依据当日最新基准现价({ref_price}元)及标的板块涨跌幅限制(±{int(r_limit*100)}%)自算生成【次日参考涨停价】({next_zt}元)与【次日参考跌停价】({next_dt}元)供隔夜推演参考；"
                    "次日官方实际涨跌停价请以交易所早间开盘清算价为准。"
                )

            result["估值与基本盘面"] = mapped_ext

        # 3. 五档深度买卖盘口整理（通达信官方 BspInfo 返回 5 项明细数组或极速备用字典）
        bsp_raw = raw.get("BspInfo", [])
        bsp_dict = {}
        if isinstance(bsp_raw, list) and bsp_raw:
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

        if bsp_dict:
            result["五档深度盘口"] = bsp_dict
            # 检查是否处于盘后休市时段（交易所停止连续竞价撮合，盘口仅剩少量静态残量挂单）
            valid_tiers = sum(1 for v in bsp_dict.values() if isinstance(v, dict) and v.get("价格") is not None)
            clean_time = str(hq_raw.get("HQTime", "")).replace(":", "")[:6]
            is_after_market = False
            if clean_time.isdigit() and len(clean_time) >= 6:
                if clean_time >= "150000" or clean_time < "091500":
                    is_after_market = True

            total_hand = sum(
                int(v.get("委托量_手", 0) or 0) for v in bsp_dict.values()
                if isinstance(v, dict) and isinstance(v.get("委托量_手"), (int, float))
            )

            if is_after_market or valid_tiers <= 1 or (total_hand > 0 and total_hand < 200 and clean_time >= "145700"):
                result["盘口深度说明"] = (
                    f"【收盘残量防误导警示】当前处于休市/盘后时段（行情时间: {hq_raw.get('HQTime')}），交易所撮合系统已停止连续竞价撮合。"
                    f"五档当前累计委托量仅约 {total_hand} 手，纯属收盘集合竞价未撤单的极少量静态残量挂单！"
                    "【铁律】严禁将休市后的微量残单当作真实买卖盘口深度、买卖支撑阻力或多空博弈力量强弱进行分析与推断。"
                    "真实有效的盘口买卖深度仅在交易日连续竞价撮合期间（9:30-11:30、13:00-14:57）动态全量呈现。"
                )

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
        """获取多周期历史 K 线走势，并可选附加 MACD, KDJ, BOLL 等技术指标计算结果（支持 A 股与港股）"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        if not clean_code:
            raise ValueError(f"无效的证券代码 '{symbol}'，证券标的代码必须包含有效数字编号（如 A股'600519'、港股'00700'）。若仅知道公司名称，请先调用 stock_lookup 工具查找对应代码。")

        if len(clean_code) == 5 or symbol.strip().upper().startswith("HK"):
            raise ValueError(f"标的代码 '{symbol}' 为港股标的，请调用港股引擎。")

        inferred = infer_market_setcode(clean_code)
        if setcode is not None:
            sc_str = str(setcode).strip().lower()
            sc_map = {"sh": "1", "sse": "1", "1": "1", "sz": "0", "szse": "0", "0": "0", "bj": "2", "bse": "2", "2": "2"}
            normalized_sc = sc_map.get(sc_str, sc_str)
            if normalized_sc != inferred:
                m_name = "上海证券交易所 (SSE, setcode=1)" if inferred == "1" else ("深圳证券交易所 (SZSE, setcode=0)" if inferred == "0" else "北京证券交易所 (BSE, setcode=2)")
                raise ValueError(
                    f"证券代码 '{clean_code}' 所属交易所市场为 {m_name}，"
                    f"与传入的 setcode='{setcode}' 冲突。禁止跨市场错误指定 setcode，建议省略 setcode 参数由系统自动推断。"
                )
            m_setcode = normalized_sc
        else:
            m_setcode = inferred

        # 周期映射: day->4, week->5, month->6, 60m->3, 30m->2, 15m->1, 5m->0, 1m->7
        period_map = {"5m": 0, "15m": 1, "30m": 2, "60m": 3, "day": 4, "week": 5, "month": 6, "1m": 7}
        p_val = period_map.get(period.lower(), 4)

        # 复权映射: qfq->1, hfq->2, none->0 (通达信官方 PBFXT 底层协议规范)
        adjust_map = {"none": 0, "qfq": 1, "hfq": 2}
        tq_val = adjust_map.get(adjust.lower(), 1)

        # 预热缓冲区机制：针对计算技术指标 (MACD/EMA/BOLL/KDJ) 的场景，自动多拉取 200 根历史预热缓冲 (Warm-up buffer)
        # 消除递归指数滤波在截断窗口内的初值漂移缺陷 (解决 P1-2)
        req_count = max(1, min(int(count), 300))
        warmup_buffer = 200 if with_indicators else 0
        want_num = min(800, req_count + warmup_buffer)

        req_body = {
            "Head": {"Target": 0, "CharSet": "UTF8"},
            "Code": clean_code,
            "Setcode": int(m_setcode),
            "Period": p_val,
            "Startxh": 0,
            "WantNum": want_num,
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
            # 通达信 ListItem Item 结构: [日期, 时间(自午夜秒数), 今开, 最高, 最低, 收盘, 成交额, 成交额, 成交量...]
            raw_date = str(raw_row[0])
            if period.lower() in ("1m", "5m", "15m", "30m", "60m") and len(raw_row) > 1 and str(raw_row[1]).replace(".", "").isdigit():
                sec = int(float(raw_row[1]))
                hh = sec // 3600
                mm = (sec % 3600) // 60
                # 纠正通达信底层将上午收市 (11:30) 错标为 13:00 (46800秒) 的历史协议缺陷 (P2)
                if hh == 13 and mm == 0:
                    hh, mm = 11, 30
                dt_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]} {hh:02d}:{mm:02d}"
            elif len(raw_date) == 8 and raw_date.isdigit():
                dt_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            else:
                dt_str = raw_date

            # 价格规范化归一（股票取 2 位小数，基金/债券/可转债取 3 位），消除底层 float32 导致的单精度浮点漂移 (如 1309.689941 -> 1309.69)
            price_dec = 3 if clean_code.startswith(("51", "15", "16", "58", "11", "12")) else 2
            open_p = round(float(raw_row[2]), price_dec)
            high_p = round(float(raw_row[3]), price_dec)
            low_p = round(float(raw_row[4]), price_dec)
            close_p = round(float(raw_row[5]), price_dec)
            amt_val = round(float(raw_row[6]), 2) if len(raw_row) > 6 else 0.0
            vol_val = round(float(raw_row[8]), 2) if len(raw_row) > 8 else (round(float(raw_row[7]), 2) if len(raw_row) > 7 else 0.0)
            # 周线与月线通达信底层上报单位为手 (1手=100股)，日线与分钟线底层为股。统一跨周期成交量单位为【股】消除漂移
            if period.lower() in ("week", "month"):
                vol_val = round(vol_val * 100.0, 2)

            parsed_bars.append({
                "time": dt_str,
                "datetime": dt_str,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": vol_val,
                "amount": amt_val,
            })

        # 按时间正序排列后在包含预热缓冲区的序列上计算技术指标，再裁剪保留用户请求的最新 count 根 (解决 P1-2)
        if with_indicators and parsed_bars:
            parsed_bars = calculate_indicators(parsed_bars)
        parsed_bars = parsed_bars[-req_count:]

        if not parsed_bars:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到证券代码 '{symbol}' 的历史 K 线数据（代码不存在或非有效 A 股/港股标的）。"
                    "建议先调用 stock_lookup 工具核对标准证券名称与代码。"
                )

        return {
            "symbol": clean_code,
            "name": stock_name,
            "setcode": m_setcode,
            "period": period,
            "adjust": adjust,
            "total_bars": len(parsed_bars),
            "indicators_included": bool(with_indicators),
            "klines": parsed_bars,
        }


    def get_financials(
        self,
        symbol: str,
        report_type: str = "balance",
        period_type: str = "report",
        limit_periods: int = 4,
    ) -> dict[str, Any]:
        """BaseMarketEngine 统一财务报表抽象契约实现"""
        return self.get_financial_statements(
            symbol=symbol,
            report_type=report_type,
            period_type=period_type,
            limit_periods=limit_periods,
        )

    def get_financial_statements(
        self,
        symbol: str,
        report_type: str = "balance",
        period_type: str = "report",
        limit_periods: int = 4,
    ) -> dict[str, Any]:
        clean_code = re.sub(r"[^\d]", "", symbol)
        if len(clean_code) == 5 or symbol.strip().upper().startswith("HK"):
            raise ValueError(f"标的代码 '{symbol}' 为港股标的，请调用港股引擎。")
        if not clean_code or len(clean_code) != 6:
            raise ValueError(f"无效的证券代码 '{symbol}'，A 股证券标的代码必须为 6 位数字（如 '600519'）。建议先调用 stock_lookup 工具核对标准代码。")

        # =========================================================================
        # 【功能块 7.1：A股财报统一前置正主有效性拦截守卫 (修复 P0·本轮新发现)】
        # - 输入：clean_code (6位A股代码)
        # - 功能：无论请求何种报表类型与单季/累计口径，统一在发起底层 TQLEX 报表查询前
        #         核验标的存在性。彻底杜绝无效/停牌退市代码在 period_type='quarter'
        #         等分支下因上游通达信服务端会话游标缓存残留而泄漏上一只股票财报数据的致命缺陷
        # - 输出：合法标的放行，非法/无效代码直接显式抛出标准明确的 ValueError
        # =========================================================================
        entity = self.resolve_entity(clean_code)
        if entity is None:
            raise ValueError(
                f"未查询到证券代码 '{symbol}' 的财务报表数据（代码不存在或非有效 A 股/港股标的，无财报披露信息）。"
                "建议先调用 stock_lookup 工具核对标准证券名称与代码。"
            )

        rtype = report_type.lower()
        fixed_tag = "00102" if period_type.lower() == "quarter" else "00101"

        if rtype in ("balance", "balance_sheet", "zcfzb"):
            entry = "TdxShareCW.ph_agf10_cw_zcfzb"
            params = [clean_code]
            field_map_default = BALANCE_FIELDS_NON_FINANCE
            type_name = "资产负债表"
        elif rtype in ("income", "income_statement", "lyb"):
            entry = "TdxShareCW.ph_agf10_cw_lyb"
            params = [fixed_tag, clean_code]
            field_map_default = INCOME_FIELDS_NON_FINANCE
            type_name = f"利润表({ '单季度' if period_type == 'quarter' else '报告期' })"
        elif rtype in ("cashflow", "cashflow_statement", "xjllb"):
            entry = "TdxShareCW.ph_agf10_cw_xjllb"
            params = [fixed_tag, clean_code]
            field_map_default = CASHFLOW_FIELDS_NON_FINANCE
            type_name = f"现金流量表({ '单季度' if period_type == 'quarter' else '报告期' })"
        else:
            raise ValueError(f"Unsupported report_type: {report_type}. Available: balance, income, cashflow")

        url = f"{self.api_endpoint}?Entry={entry}"
        raw = self._post(url, {"Params": params})

        result_periods: list[dict[str, Any]] = []
        result_sets = raw.get("ResultSets", [])

        # 核心：自动嗅探通达信返回的 isjr 金融属性标志位 (1=金融类：银行/保险/证券；0=工商业实体) (修复 P0)
        is_financial = False
        if isinstance(result_sets, list):
            for rs in result_sets:
                if rs.get("ColName") == ["isjr"] and rs.get("Content"):
                    val = str(rs["Content"][0][0]).strip()
                    if val in ("1", "True", "true"):
                        is_financial = True
                    break

        if is_financial:
            standard_name = "金融企业会计准则 (银行 / 保险 / 证券 / 金融控股)"
            if rtype in ("balance", "balance_sheet", "zcfzb"):
                field_map = BALANCE_FIELDS_FINANCE
            elif rtype in ("income", "income_statement", "lyb"):
                field_map = INCOME_FIELDS_FINANCE
            else:
                field_map = CASHFLOW_FIELDS_FINANCE
        else:
            standard_name = "一般工商业企业会计准则 (非金融实体企业)"
            field_map = field_map_default

        if isinstance(result_sets, list):
            for rs in result_sets:
                col_names = rs.get("ColName", [])
                contents = rs.get("Content", [])
                if not col_names or not contents or col_names == ["isjr"]:
                    continue
                # 遍历历史财报期
                for row in contents[:limit_periods]:
                    period_dict: dict[str, Any] = {}
                    supp_dict: dict[str, Any] = {}
                    for col_code, val in zip(col_names, row):
                        val_str = str(val).strip()
                        if not val_str:
                            continue
                        clean_col_name = field_map.get(col_code, col_code)
                        # 数值型字段友好且一致的转换（日期类保留原始文本，其余统一转为规范 float，杜绝跨股票 str/int/float 漂移）
                        if clean_col_name in ("截止日期", "报告期日期") or re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", val_str) or "报" in val_str:
                            parsed_val: Any = val_str
                        else:
                            try:
                                parsed_val = round(float(val_str), 2)
                            except (ValueError, TypeError):
                                parsed_val = val_str

                        # 现金流量表：将间接法与现金净变动的补充资料与主表直接法科目做物理隔离，彻底杜绝下游遍历加总时双计
                        if rtype in ("cashflow", "cashflow_statement", "xjllb") and clean_col_name.startswith("补充资料_"):
                            supp_dict[clean_col_name] = parsed_val
                        elif rtype in ("income", "income_statement", "lyb") and clean_col_name == "重复核对_净利润":
                            # 过滤底层重复核对字段 N027，防止下游将其作为独立科目加总造成双计
                            continue
                        else:
                            period_dict[clean_col_name] = parsed_val

                    # 针对库存股，若存在正数值，显式挂载抵减项别名标注，杜绝加总时误当正项累加导致权益高估 2x 库存股
                    if "库存股" in period_dict and period_dict["库存股"] is not None:
                        period_dict["库存股_抵减项"] = period_dict["库存股"]
                        period_dict["减_库存股"] = period_dict["库存股"]

                    # =========================================================================
                    # 【功能块 7.1：财报报告期与机器可读日期标准归一化 (修复 P2-6)】
                    # - 输入：period_dict 中原始'截止日期'（常为文本如 2026中报）与'报告期日期' (Tdate)
                    # - 功能：精准提取或推导 ISO-8601 标准日期 (YYYY-MM-DD)，写入'截止日期'与'报告期日期'，
                    #         并将文本标签保留至'报告期'、'报表类型'与'报告期标签'，杜绝日期列文字化退化
                    # - 输出：原地规范化填充 period_dict 日期与报告期相关字段
                    # =========================================================================
                    iso_date = ""
                    # 1. 优先提取 Tdate (映射为报告期日期) 中原生的机器可读标准 ISO 日期
                    cand_tdate = str(period_dict.get("报告期日期") or "").strip()
                    if re.match(r"^\d{4}-\d{2}-\d{2}", cand_tdate):
                        iso_date = cand_tdate[:10]
                    elif re.match(r"^\d{8}$", cand_tdate):
                        iso_date = f"{cand_tdate[:4]}-{cand_tdate[4:6]}-{cand_tdate[6:8]}"

                    # 2. 若无原生 ISO 日期，尝试从截止日期解析 (兼容 YYYYMMDD、YYYY-MM-DD 或 文字标签如 '2026中报')
                    raw_end = str(period_dict.get("截止日期") or "").strip()
                    if not iso_date:
                        if re.match(r"^\d{4}-\d{2}-\d{2}", raw_end):
                            iso_date = raw_end[:10]
                        elif re.match(r"^\d{8}$", raw_end):
                            iso_date = f"{raw_end[:4]}-{raw_end[4:6]}-{raw_end[6:8]}"
                        else:
                            m_lbl = re.match(r"^(\d{4})\s*(一季报|中报|三季报|年报)", raw_end)
                            if m_lbl:
                                y_str, rpt_txt = m_lbl.group(1), m_lbl.group(2)
                                md_map = {"一季报": "03-31", "中报": "06-30", "三季报": "09-30", "年报": "12-31"}
                                iso_date = f"{y_str}-{md_map.get(rpt_txt, '12-31')}"

                    if iso_date:
                        period_dict["截止日期"] = iso_date
                        period_dict["报告期日期"] = iso_date
                        month = iso_date[5:7] if len(iso_date) >= 7 else ""
                        rpt_name = "一季报" if month == "03" else ("中报" if month == "06" else ("三季报" if month == "09" else ("年报" if month == "12" else "")))
                        if rpt_name:
                            period_dict["报表类型"] = rpt_name
                            period_dict["报告期"] = f"{iso_date[:4]}{rpt_name}"
                            period_dict["报告期标签"] = f"{iso_date[:4]}{rpt_name}"
                    elif raw_end:
                        period_dict["报告期"] = raw_end
                        period_dict["报告期标签"] = raw_end

                    # 修复 G (P3): 统一对齐挂载 币种 与 金额单位
                    period_dict["币种"] = "CNY"
                    period_dict["金额单位"] = "元"

                    # 针对现金流量表：将补充资料独立收纳于二级字典，主表仅保留三大活动直接法科目与期初期末现金净增加额
                    if supp_dict:
                        # 间接法补充资料现金净增加额回填（若底层上游漏值为0或None，自动取主表现金净增加额或期初期末差额，修复 B8）
                        if supp_dict.get("补充资料_现金及现金等价物净增加额") in (None, 0, 0.0):
                            main_cash_net = period_dict.get("现金及现金等价物净增加额")
                            if main_cash_net is not None and isinstance(main_cash_net, (int, float)) and main_cash_net != 0:
                                supp_dict["补充资料_现金及现金等价物净增加额"] = main_cash_net
                        period_dict["补充资料_间接法"] = supp_dict

                    if rtype in ("cashflow", "cashflow_statement", "xjllb"):
                        if is_financial:
                            period_dict["现金流量表加总纪律说明"] = (
                                "【金融企业现金流量表核算规范】本表遵循《金融企业财务报表格式》（银行/保险/证券）。"
                                "因底层行情专线未采集保险原保费收入、证券代理买卖等特殊金融科目明细，流入/流出细项之和与小计可能存在差额，"
                                "差额已自动自陈至不可加总差额科目。财务分析必须且只能引用三大活动净额（经营净额、投资净额、筹资净额）"
                                "与期末现金及现金等价物余额。\n"
                                "【间接法补充资料披露规则】依据《企业会计准则第31号》，上市公司季报（一季报03-31/三季报09-30）依法无需披露间接法附表，故季报期【补充资料_间接法】为空，仅中报(06-30)与年报(12-31)完整披露。"
                            )
                            # 金融企业经营活动现金流入不可加总差额 (原保险保费、代理买卖证券、拆入回购等缺项，修复 D2)
                            jy_in = period_dict.get("经营活动现金流入小计")
                            if jy_in is not None and isinstance(jy_in, (int, float)):
                                known_fin_jy_in = [
                                    "客户存款和同业存放款项净增加额", "向中央银行借款净增加额", "向其他金融机构拆入资金净增加额",
                                    "收取利息、手续费及佣金的现金", "处置以公允价值计量且变动计入当期损益的金融资产净增加额",
                                    "拆入资金净增加额", "回购业务资金净增加额", "代理买卖证券收到的现金净额", "收到的税费返还",
                                    "收到原保险合同保费取得的现金", "收到再保业务现金净额", "保户储金及投资款净增加额",
                                    "处置金融资产净增加额", "拆入资金净增加额_备用", "收到其他与经营活动有关的现金"
                                ]
                                diff_fin_in = round(float(jy_in) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_jy_in), 2)
                                if abs(diff_fin_in) > 0.01:
                                    period_dict["经营活动现金流入不可加总差额_原保险保费及拆入回购等金融科目缺项"] = diff_fin_in

                            # 金融企业经营活动现金流出不可加总差额 (原保险赔付、返售拆出等缺项，修复 D2)
                            jy_out = period_dict.get("经营活动现金流出小计")
                            if jy_out is not None and isinstance(jy_out, (int, float)):
                                known_fin_jy_out = [
                                    "客户贷款及垫款净增加额", "存放中央银行和同业款项净增加额", "支付原保险合同赔付款项的现金",
                                    "拆出资金净增加额", "返售业务资金净增加额", "支付利息、手续费及佣金的现金",
                                    "支付保单红利的现金", "支付再保业务现金净额", "支付原保险合同款项现金",
                                    "支付给职工以及为职工支付的现金", "支付的各项税费", "支付其他与经营活动有关的现金"
                                ]
                                diff_fin_out = round(float(jy_out) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_jy_out), 2)
                                if abs(diff_fin_out) > 0.01:
                                    period_dict["经营活动现金流出不可加总差额_保险赔付及拆出返售等金融科目缺项"] = diff_fin_out

                            # 金融企业投资活动现金流入/流出不可加总差额 (修复 P2-3)
                            tz_in = period_dict.get("投资活动现金流入小计")
                            if tz_in is not None and isinstance(tz_in, (int, float)):
                                known_fin_tz_in = [
                                    "收回投资收到的现金", "取得投资收益收到的现金",
                                    "处置固定资产、无形资产和其他长期资产收回的现金净额", "收到其他与投资活动有关的现金"
                                ]
                                diff_fin_tz_in = round(float(tz_in) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_tz_in), 2)
                                if abs(diff_fin_tz_in) > 0.01:
                                    period_dict["投资活动现金流入不可加总差额_金融投资细项缺项"] = diff_fin_tz_in

                            tz_out = period_dict.get("投资活动现金流出小计")
                            if tz_out is not None and isinstance(tz_out, (int, float)):
                                known_fin_tz_out = [
                                    "投资支付的现金", "质押贷款净增加额",
                                    "购建固定资产、无形资产和其他长期资产支付的现金", "支付其他与投资活动有关的现金"
                                ]
                                diff_fin_tz_out = round(float(tz_out) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_tz_out), 2)
                                if abs(diff_fin_tz_out) > 0.01:
                                    period_dict["投资活动现金流出不可加总差额_金融投资细项缺项"] = diff_fin_tz_out

                            # 金融机构筹资活动现金流入小计缺失推算与自陈 (修复 P2-2)
                            cz_in = period_dict.get("筹资活动现金流入小计")
                            cz_out = period_dict.get("筹资活动现金流出小计")
                            cz_net = period_dict.get("筹资活动现金流量净额") or period_dict.get("筹资活动产生的现金流量净额")
                            if cz_in is None:
                                known_fin_cz_in_sum = sum(float(period_dict.get(k, 0.0) or 0.0) for k in ("吸收投资收到的现金", "发行债券收到的现金", "取得借款收到的现金", "收到其他与筹资活动有关的现金"))
                                if known_fin_cz_in_sum > 0:
                                    period_dict["筹资活动现金流入小计"] = known_fin_cz_in_sum
                                elif cz_out is not None and cz_net is not None:
                                    inferred_in = round(float(cz_net) + float(cz_out), 2)
                                    period_dict["筹资活动现金流入小计"] = max(0.0, inferred_in)
                                    if abs(inferred_in) < 1.0:
                                        period_dict["筹资流入说明"] = "当期金融机构未发生新增债券发行或权益融资筹资流入(0.00元)，筹资活动净额等于流出之负值。"
                                    else:
                                        period_dict["筹资流入说明"] = f"上游报表缺漏筹资流入列，根据【净额({cz_net}) + 流出({cz_out})】严密倒轧推算补齐。"
                                else:
                                    period_dict["筹资活动现金流入小计"] = 0.0
                                    period_dict["筹资流入说明"] = "当期金融机构未发生新增筹资流入，规范填报为 0.00元。"
                                cz_in = period_dict["筹资活动现金流入小计"]

                            if cz_in is not None and isinstance(cz_in, (int, float)):
                                known_fin_cz_in = ["吸收投资收到的现金", "发行债券收到的现金", "取得借款收到的现金", "收到其他与筹资活动有关的现金"]
                                diff_fin_cz_in = round(float(cz_in) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_cz_in), 2)
                                if abs(diff_fin_cz_in) > 0.01:
                                    period_dict["筹资活动现金流入不可加总差额_金融借款及发债细项等"] = diff_fin_cz_in

                            cz_out = period_dict.get("筹资活动现金流出小计")
                            if cz_out is not None and isinstance(cz_out, (int, float)):
                                known_fin_cz_out = ["偿还债务支付的现金", "分配股利、利润或偿付利息支付的现金", "支付其他与筹资活动有关的现金"]
                                diff_fin_cz_out = round(float(cz_out) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_cz_out), 2)
                                if abs(diff_fin_cz_out) > 0.01:
                                    period_dict["筹资活动现金流出不可加总差额_金融分红及利息细项等"] = diff_fin_cz_out

                            # 间接法补充资料缺失声明 (修复 D2)
                            if "补充资料_间接法" not in period_dict:
                                period_dict["补充资料_间接法说明"] = "上游专线未收录该金融机构间接法编制细项，分析现金流请直接引用主表现金流量净额与期末现金余额。"
                        else:
                            period_dict["现金流量表加总纪律说明"] = (
                                "因上游数据模板中缺失'收到的税费返还'等流入细分项，流入/流出细项之和与小计可能存在不可闭合差额。"
                                "量化与财报分析时禁止机械累加细项计算小计，必须且只能引用三大活动净额（经营净额、投资净额、筹资净额）"
                                "与期末现金余额。\n"
                                "【间接法补充资料披露规则】依据《企业会计准则第31号》，上市公司季报（一季报03-31/三季报09-30）依法无需披露间接法附表，故季报期【补充资料_间接法】为空，仅中报(06-30)与年报(12-31)完整披露。"
                            )
                            if "补充资料_间接法" not in period_dict:
                                period_dict["补充资料_间接法说明"] = "上游专线未收录该期次间接法编制细项（季报依法无需编制附表），分析现金流请直接引用主表现金流量净额与期末现金余额。"
                            # 经营活动现金流入/流出不可加总差额自陈 (财务公司拆借/存贷款/税费返还等科目缺项)
                            jy_in = period_dict.get("经营活动现金流入小计")
                            if jy_in is not None and isinstance(jy_in, (int, float)):
                                known_jy_in = ["销售商品提供劳务收到的现金", "收到的税费返还", "收到其他与经营活动有关的现金"]
                                diff_jy_in = round(float(jy_in) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_jy_in), 2)
                                if abs(diff_jy_in) > 0.01:
                                    period_dict["经营活动现金流入不可加总差额_收到的税费返还及金融拆借等"] = diff_jy_in

                            jy_out = period_dict.get("经营活动现金流出小计")
                            if jy_out is not None and isinstance(jy_out, (int, float)):
                                known_jy_out = [
                                    "购买商品接受劳务支付的现金", "支付给职工以及为职工支付的现金",
                                    "支付的各项税费", "支付其他与经营活动有关的现金"
                                ]
                                diff_jy_out = round(float(jy_out) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_jy_out), 2)
                                if abs(diff_jy_out) > 0.01:
                                    period_dict["经营活动现金流出不可加总差额_金融存贷款及细项等"] = diff_jy_out

                            # 筹资活动现金流入小计自动补全与说明 (修复 D7 静默缺列)
                            cz_in = period_dict.get("筹资活动现金流入小计")
                            cz_out = period_dict.get("筹资活动现金流出小计")
                            cz_net = period_dict.get("筹资活动现金流量净额") or period_dict.get("筹资活动产生的现金流量净额")
                            if cz_in is None:
                                known_cz_sub_in = sum(float(period_dict.get(k, 0.0) or 0.0) for k in ("吸收投资收到的现金", "取得借款收到的现金", "发行债券收到的现金", "收到其他与筹资活动有关的现金"))
                                if known_cz_sub_in > 0:
                                    period_dict["筹资活动现金流入小计"] = known_cz_sub_in
                                elif cz_out is not None and cz_net is not None:
                                    period_dict["筹资活动现金流入小计"] = round(float(cz_out) + float(cz_net), 2)
                                else:
                                    period_dict["筹资活动现金流入小计"] = 0.0
                                period_dict["筹资活动现金流入小计说明"] = "该报告期无借款发债或股权融资等筹资流入，上游报表略去该列，已按会计准则规范填报为 0.0 或由净额推算补齐。"
                                cz_in = period_dict["筹资活动现金流入小计"]

                            if cz_in is not None and isinstance(cz_in, (int, float)):
                                # 修复 B2：严格补齐已映射科目【发行债券收到的现金】，杜绝自陈差额系统性虚高
                                known_cz_in = ["吸收投资收到的现金", "取得借款收到的现金", "发行债券收到的现金", "收到其他与筹资活动有关的现金"]
                                diff_cz_in = round(float(cz_in) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_cz_in), 2)
                                if abs(diff_cz_in) > 0.01:
                                    period_dict["筹资活动现金流入不可加总差额_金融借款及发债细项等"] = diff_cz_in

                            cz_out = period_dict.get("筹资活动现金流出小计")
                            if cz_out is not None and isinstance(cz_out, (int, float)):
                                known_cz_out = ["偿还债务支付的现金", "分配股利利润或偿付利息支付的现金", "支付其他与筹资活动有关的现金"]
                                diff_cz_out = round(float(cz_out) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_cz_out), 2)
                                if abs(diff_cz_out) > 0.01:
                                    period_dict["筹资活动现金流出不可加总差额_金融分红及利息细项等"] = diff_cz_out

                        # 现金流量表法定豁免声明 (修复 P3-4: 一、三季度季报免于披露间接法补充资料)
                        rpt_t = period_dict.get("报表类型")
                        if rpt_t in ("一季报", "三季报") or not period_dict.get("补充资料_间接法"):
                            period_dict["间接法补充资料披露规则说明"] = (
                                "【法定豁免声明】根据中国证监会《公开发行证券的公司信息披露内容与格式准则第13号/第15号》，"
                                "上市公司第一季度和第三季度财务会计报告（季报）免于披露【将净利润调节为经营活动现金流量】等现金流量表补充资料（间接法）。"
                                "非上市公司漏报或未填报，分析现金流请直接引用主表现金流量净额与期末现金余额。"
                            )

                        # 现金流量表法定标准会计键序重排 (修复 P2-6 & Issue 14: 确保筹资流入项 -> 流入小计 -> 流出项 -> 流出小计 -> 净额)
                        canonical_cf_order = [
                            "截止日期", "报告期", "报告期日期", "报表类型",
                            "销售商品提供劳务收到的现金", "收到的税费返还", "收到其他与经营活动有关的现金",
                            "客户存款和同业存放款项净增加额", "向中央银行借款净增加额", "向其他金融机构拆入资金净增加额",
                            "收取利息、手续费及佣金的现金", "处置以公允价值计量且变动计入当期损益的金融资产净增加额",
                            "拆入资金净增加额", "回购业务资金净增加额", "代理买卖证券收到的现金净额",
                            "收到原保险合同保费取得的现金", "收到再保业务现金净额", "保户储金及投资款净增加额",
                            "处置金融资产净增加额", "拆入资金净增加额_备用",
                            "经营活动现金流入小计", "经营活动现金流入不可加总差额_收到的税费返还及金融拆借等",
                            "经营活动现金流入不可加总差额_原保险保费及拆入回购等金融科目缺项",
                            "购买商品接受劳务支付的现金", "支付给职工以及为职工支付的现金", "支付的各项税费",
                            "支付其他与经营活动有关的现金", "客户贷款及垫款净增加额", "存放中央银行和同业款项净增加额",
                            "支付原保险合同赔付款项的现金", "拆出资金净增加额", "返售业务资金净增加额",
                            "支付利息、手续费及佣金的现金", "支付保单红利的现金", "支付再保业务现金净额",
                            "支付原保险合同款项现金",
                            "经营活动现金流出小计", "经营活动现金流出不可加总差额_金融存贷款及细项等",
                            "经营活动现金流出不可加总差额_保险赔付及拆出返售等金融科目缺项",
                            "经营活动现金流量净额", "经营活动产生的现金流量净额",
                            "收回投资收到的现金", "取得投资收益收到的现金",
                            "处置固定资产无形资产收回的现金净额", "处置固定资产、无形资产和其他长期资产收回的现金净额",
                            "处置子公司及其他营业单位收到的现金净额", "收到其他与投资活动有关的现金",
                            "投资活动现金流入小计", "投资活动现金流入不可加总差额_金融投资细项缺项",
                            "购建固定资产无形资产支付的现金", "购建固定资产、无形资产和其他长期资产支付的现金",
                            "投资支付的现金", "取得子公司及其他营业单位支付的现金净额", "质押贷款净增加额",
                            "支付其他与投资活动有关的现金",
                            "投资活动现金流出小计", "投资活动现金流出不可加总差额_金融投资细项缺项",
                            "投资活动现金流量净额",
                            "吸收投资收到的现金", "发行债券收到的现金", "取得借款收到的现金", "收到其他与筹资活动有关的现金",
                            "筹资活动现金流入小计", "筹资活动现金流入小计说明", "筹资活动现金流入不可加总差额_金融借款及发债细项等",
                            "偿还债务支付的现金", "分配股利利润或偿付利息支付的现金", "分配股利、利润或偿付利息支付的现金",
                            "支付其他与筹资活动有关的现金",
                            "筹资活动现金流出小计", "筹资活动现金流出不可加总差额_金融分红及利息细项等",
                            "筹资活动现金流量净额", "筹资活动产生的现金流量净额",
                            "汇率变动对现金及现金等价物的影响", "现金及现金等价物净增加额",
                            "期初现金及现金等价物余额", "期末现金及现金等价物余额",
                            "现金流量表加总纪律说明", "间接法补充资料披露规则说明", "补充资料_间接法", "补充资料_间接法说明",
                        ]
                        ordered_keys = sorted(period_dict.keys(), key=lambda x: canonical_cf_order.index(x) if x in canonical_cf_order else 999)
                        period_dict = {k: period_dict[k] for k in ordered_keys}

                    # =========================================================================
                    # 【功能块 7.2：利润表会计勾稽核算与加总说明 (修复 P2-4, P2-5, P3-5)】
                    # - 输入：利润表各科目数值
                    # - 功能：
                    #   1. 综合收益勾稽 (P2-4): 归母其他综合收益 = 归母综合收益总额 − 归母净利润
                    #   2. 扣非净利润勾稽 (P2-5): 扣除非经常性损益调整项 = 归母净利润 − 扣非净利润
                    #   3. 营业总成本构成核算说明 (P3-5): 营业成本+税金附加+期间费用与总成本闭合核验
                    #   4. 营业利润不可加总差额自陈 (工商业未采集信用与资产减值损失科目)
                    # - 输出：原地注入规范会计勾稽项与审计说明
                    # =========================================================================
                    if rtype in ("income", "income_statement", "lyb"):
                        if is_financial:
                            period_dict["利润表加总纪律说明"] = (
                                "【金融企业利润表核算规范】利息净收入、手续费及佣金净收入与投资收益为金融机构主要核心收益来源。"
                                "营业利润 = 营业收入 − 营业支出（注意：投资收益、公允价值变动收益、汇兑收益、其他业务收入已包含在营业收入中，切勿重复累加）。"
                                "财务建模与分析请直接引用官方公布的【营业收入】、【营业利润】、【利润总额】与【归属于母公司股东的净利润】。"
                            )
                            # 金融企业营业收入不可加总差额自陈 (保险公司新准则下保险服务收入/已赚保费上游缺项等，修复 P2-1)
                            yysr = period_dict.get("营业收入")
                            if yysr is not None and isinstance(yysr, (int, float)):
                                known_fin_yysr = [
                                    "利息净收入", "手续费及佣金净收入", "加_投资收益", "加_公允价值变动收益",
                                    "加_汇兑收益", "加_其他业务收入", "已赚保费"
                                ]
                                sub_fin_yysr = sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_yysr)
                                diff_yysr = round(float(yysr) - sub_fin_yysr, 2)
                                if abs(diff_yysr) > 0.01:
                                    period_dict["营业收入不可加总差额_保险服务收入及金融专有科目缺项"] = diff_yysr
                                    period_dict["保险企业收入说明"] = (
                                        "该标的执行新保险合同准则(CAS 25/IFRS 17)，上游专线未单独收录【保险服务收入/已赚保费】细项，"
                                        f"导致营业收入与已知金融收益项之和存在客观缺口({round(diff_yysr / 100000000.0, 2)}亿元)，财务建模请直接引用官方【营业收入】整体。"
                                    )

                            # 金融企业综合收益与扣非净利润勾稽 (P2-4, P2-5)
                            g_total_ci = period_dict.get("归属于母公司股东的综合收益总额")
                            g_np = period_dict.get("归属于母公司股东的净利润")
                            if g_total_ci is not None and g_np is not None and isinstance(g_total_ci, (int, float)) and isinstance(g_np, (int, float)):
                                calc_oci = round(float(g_total_ci) - float(g_np), 2)
                                period_dict["归属于母公司股东的其他综合收益"] = calc_oci
                                period_dict["综合收益勾稽说明"] = (
                                    "归属于母公司股东的综合收益总额 = 归属于母公司股东的净利润 + 归属于母公司股东的其他综合收益 "
                                    f"({float(g_np):,.2f} + {calc_oci:,.2f} = {float(g_total_ci):,.2f}元)，严格遵循企业会计准则第30号勾稽闭合。"
                                )
                            kf_np = period_dict.get("扣非净利润")
                            if g_np is not None and kf_np is not None and isinstance(g_np, (int, float)) and isinstance(kf_np, (int, float)):
                                adj_item = round(float(g_np) - float(kf_np), 2)
                                period_dict["扣除非经常性损益调整项"] = adj_item
                                period_dict["扣非净利润勾稽说明"] = (
                                    "扣非净利润 = 归属于母公司股东的净利润 − 扣除非经常性损益调整项 "
                                    f"({float(g_np):,.2f} − {adj_item:,.2f} = {float(kf_np):,.2f}元)，遵循公开发行证券的公司信息披露解释性公告第1号。"
                                )
                        else:
                            yylr = period_dict.get("营业利润")
                            if yylr is not None and isinstance(yylr, (int, float)):
                                yysr = float(period_dict.get("营业总收入", 0.0) or 0.0)
                                yycb = float(period_dict.get("营业总成本", 0.0) or 0.0)
                                tzsy = float(period_dict.get("投资收益", 0.0) or 0.0)
                                qtsy = float(period_dict.get("其他收益", 0.0) or 0.0)
                                gysy = float(period_dict.get("公允价值变动净收益", 0.0) or 0.0)
                                hdsy = float(period_dict.get("汇兑收益", 0.0) or 0.0)
                                zccz = float(period_dict.get("资产处置收益", 0.0) or 0.0)
                                calc_yylr = yysr - yycb + tzsy + qtsy + gysy + hdsy + zccz
                                diff_yylr = round(float(yylr) - calc_yylr, 2)
                                if abs(diff_yylr) > 0.01:
                                    period_dict["营业利润不可加总差额_信用减值与资产减值损失及其他"] = diff_yylr

                            # 营业总成本构成核算说明 (P3-5)
                            yyzcb = period_dict.get("营业总成本")
                            if yyzcb is not None and isinstance(yyzcb, (int, float)):
                                known_costs = ["营业成本", "税金及附加", "销售费用", "管理费用", "研发费用", "财务费用"]
                                cost_sum = sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_costs)
                                cost_diff = round(float(yyzcb) - cost_sum, 2)
                                if abs(cost_diff) > 0.01:
                                    period_dict["营业总成本构成差额说明"] = (
                                        f"营业总成本({float(yyzcb):,.2f}元)与已列明期间与主营成本项(营业成本+税金及附加+销售+管理+研发+财务费用，合计{cost_sum:,.2f}元)存在差额({cost_diff:,.2f}元)。"
                                        "该差额通常由其他业务成本、资产减值损失准备或合并报表内部抵销细项构成。"
                                    )
                                else:
                                    period_dict["营业总成本构成说明"] = "营业总成本 = 营业成本 + 税金及附加 + 销售费用 + 管理费用 + 研发费用 + 财务费用，科目完全闭合。"

                            # 工商业企业综合收益与扣非净利润勾稽 (P2-4, P2-5)
                            g_total_ci = period_dict.get("归属于母公司所有者的综合收益总额")
                            g_np = period_dict.get("归母净利润")
                            if g_total_ci is not None and g_np is not None and isinstance(g_total_ci, (int, float)) and isinstance(g_np, (int, float)):
                                calc_oci = round(float(g_total_ci) - float(g_np), 2)
                                period_dict["归属于母公司股东的其他综合收益"] = calc_oci
                                period_dict["综合收益勾稽说明"] = (
                                    "归属于母公司股东的综合收益总额 = 归母净利润 + 归属于母公司股东的其他综合收益 "
                                    f"({float(g_np):,.2f} + {calc_oci:,.2f} = {float(g_total_ci):,.2f}元)，严格遵循企业会计准则第30号勾稽闭合。"
                                )
                            kf_np = period_dict.get("扣非净利润")
                            if g_np is not None and kf_np is not None and isinstance(g_np, (int, float)) and isinstance(kf_np, (int, float)):
                                adj_item = round(float(g_np) - float(kf_np), 2)
                                period_dict["扣除非经常性损益调整项"] = adj_item
                                period_dict["扣非净利润勾稽说明"] = (
                                    "扣非净利润 = 归母净利润 − 扣除非经常性损益调整项 "
                                    f"({float(g_np):,.2f} − {adj_item:,.2f} = {float(kf_np):,.2f}元)，遵循公开发行证券的公司信息披露解释性公告第1号。"
                                )

                            period_dict["利润表加总纪律说明"] = (
                                "【营业利润不可直接复算自陈】通达信38列利润表工商业模板未收录'信用减值损失'与'资产减值损失'等细项；"
                                "因此：营业总收入 − 营业总成本 + 投资收益 + 资产处置 + 汇兑 + 其他收益 + 公允价值变动净收益 ≠ 营业利润。"
                                "差额已自动核算至【营业利润不可加总差额_信用减值与资产减值损失及其他】。"
                                "财务建模与分析时请务必直接取用官方公布的【营业利润】、【利润总额】与【净利润】，严禁自行通过加减科目拼凑营业利润！\n"
                                "【归母净利润双键同值同源提示】为兼容不同行业模板取词习惯，表中【归母净利润】与【归属于母公司股东的净利润】互为同值别名（逐位同值）。若对单期科目进行整表遍历求和或统计，切勿将二者重复累加！"
                            )

                        # 修复 H (P3): 消除金融(银行/证券/保险)与工商业模板归母净利润及综合收益键名分裂
                        if "归属于母公司股东的净利润" in period_dict and "归母净利润" not in period_dict:
                            period_dict["归母净利润"] = period_dict["归属于母公司股东的净利润"]
                        elif "归母净利润" in period_dict and "归属于母公司股东的净利润" not in period_dict:
                            period_dict["归属于母公司股东的净利润"] = period_dict["归母净利润"]

                        if "归属于母公司股东的综合收益总额" in period_dict and "归属于母公司所有者的综合收益总额" not in period_dict:
                            period_dict["归属于母公司所有者的综合收益总额"] = period_dict["归属于母公司股东的综合收益总额"]
                        elif "归属于母公司所有者的综合收益总额" in period_dict and "归属于母公司股东的综合收益总额" not in period_dict:
                            period_dict["归属于母公司股东的综合收益总额"] = period_dict["归属于母公司所有者的综合收益总额"]

                    # 针对资产负债表：自动核算明细与小计的不可加总差额
                    if rtype in ("balance", "balance_sheet", "zcfzb"):
                        if is_financial:
                            zchj = period_dict.get("资产总计")
                            fzhj = period_dict.get("负债合计")
                            qyhj = period_dict.get("所有者权益合计")
                            if zchj is not None and fzhj is not None and qyhj is not None and isinstance(zchj, (int, float)) and isinstance(fzhj, (int, float)) and isinstance(qyhj, (int, float)):
                                diff = round(float(zchj) - (float(fzhj) + float(qyhj)), 2)
                                if abs(diff) > 0.01:
                                    period_dict["金融资产负债不平衡自陈差额"] = diff
                            period_dict["资产负债表核算说明"] = (
                                "【金融企业资产负债表核算规范】本表遵循《金融企业财务报表格式》（银行业/保险业/证券业）。"
                                "金融企业资产负债表不区分流动与非流动资产/负债，以资产流动性从高到低排列（现金及存放央行款项排在最前）。"
                                "会计基本恒等式为：【资产总计 = 负债合计 + 所有者权益合计】。"
                            )
                        else:
                            # 1. 非流动负债不可加总差额（底层数据源缺失新租赁准则【租赁负债】、【长期应付职工薪酬】等科目）
                            non_curr_total = period_dict.get("非流动负债合计")
                            if non_curr_total is not None and isinstance(non_curr_total, (int, float)):
                                known_items = [
                                    "长期借款", "应付债券", "长期应付款", "专项应付款",
                                    "预计负债", "递延所得税负债", "递延收益", "其他非流动负债"
                                ]
                                sub_sum = sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_items)
                                diff = round(float(non_curr_total) - sub_sum, 2)
                                if abs(diff) > 0.01:
                                    period_dict["非流动负债不可加总差额_新租赁准则租赁负债等"] = diff

                            # 2. 流动资产不可加总差额 (拆出资金/金融衍生资产/买入返售金融资产等)
                            curr_assets_total = period_dict.get("流动资产合计")
                            if curr_assets_total is not None and isinstance(curr_assets_total, (int, float)):
                                # 修复 B1：兼顾已映射的【其他应收款】与【其他应收款合计】，杜绝漏算已映射科目致差额虚高
                                known_curr_assets = [
                                    "货币资金", "交易性金融资产", "应收票据", "应收账款", "预付款项",
                                    "应收利息", "应收股利", "存货", "合同资产",
                                    "持有待售资产", "一年内到期的非流动资产", "其他流动资产"
                                ]
                                other_recv = float(period_dict.get("其他应收款") if period_dict.get("其他应收款") is not None else (period_dict.get("其他应收款合计") or 0.0))
                                sub_ca = sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_curr_assets) + other_recv
                                diff_ca = round(float(curr_assets_total) - sub_ca, 2)
                                if abs(diff_ca) > 0.01:
                                    period_dict["流动资产不可加总差额_拆出资金及金融细项等"] = diff_ca

                            # 3. 流动负债不可加总差额 (吸收存款及同业存放/卖出回购金融资产款等)
                            curr_liab_total = period_dict.get("流动负债合计")
                            if curr_liab_total is not None and isinstance(curr_liab_total, (int, float)):
                                # 修复：补齐已映射科目【预计流动负债】、【待转销项税额】、【衍生金融负债】等，杜绝流动负债自陈差额虚高
                                known_curr_liab = [
                                    "短期借款", "交易性金融负债", "衍生金融负债", "衍生金融负债_备用", "应付票据", "应付账款", "预收款项",
                                    "合同负债", "应付职工薪酬", "应交税费", "其他应付款合计",
                                    "持有待售负债", "一年内到期的非流动负债", "预计流动负债", "待转销项税额", "其他流动负债"
                                ]
                                ap_sum = float(period_dict.get("应付票据") or 0.0) + float(period_dict.get("应付账款") or 0.0)
                                ap_total = float(period_dict.get("应付票据及应付账款合计") or 0.0) if ap_sum == 0.0 else 0.0

                                sub_cl = sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_curr_liab) + ap_total
                                diff_cl = round(float(curr_liab_total) - sub_cl, 2)
                                if abs(diff_cl) > 0.01:
                                    period_dict["流动负债不可加总差额_吸收存款及金融细项等"] = diff_cl

                            # 4. 非流动资产不可加总差额 (底层数据源缺失新租赁准则【使用权资产】等科目，修复 Issue 9)
                            non_curr_assets_total = period_dict.get("非流动资产合计")
                            if non_curr_assets_total is not None and isinstance(non_curr_assets_total, (int, float)):
                                known_non_curr_assets = [
                                    "可供出售金融资产", "债权投资", "其他债权投资", "其他权益工具投资", "其他非流动金融资产",
                                    "持有至到期投资", "长期应收款", "长期股权投资", "投资性房地产", "固定资产",
                                    "固定资产清理", "在建工程", "工程物资", "生产性生物资产", "油气资产",
                                    "无形资产", "开发支出", "商誉", "长期待摊费用", "递延所得税资产", "其他非流动资产"
                                ]
                                sub_nca = sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_non_curr_assets)
                                diff_nca = round(float(non_curr_assets_total) - sub_nca, 2)
                                if abs(diff_nca) > 0.01:
                                    period_dict["非流动资产不可加总差额_新租赁准则使用权资产等"] = diff_nca

                            # 5. 归母所有者权益不可加总差额 (底层数据源缺失【专项储备】等细项，以及未收录的其他权益工具；库存股法定抵减)
                            parent_eq_total = period_dict.get("归母所有者权益合计")
                            if parent_eq_total is not None and isinstance(parent_eq_total, (int, float)):
                                known_equity_items = [
                                    "实收资本_股本", "资本公积", "盈余公积", "未分配利润",
                                    "其他综合收益", "专项储备", "一般风险准备", "外币报表折算差额",
                                    "其他权益工具"
                                ]
                                # 依据企业会计准则，库存股为所有者权益的法定借方抵减项，计算权益和时必须扣除
                                treasury_stock = float(period_dict.get("库存股") or 0.0)
                                sub_eq = sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_equity_items if k in period_dict) - treasury_stock
                                diff_eq = round(float(parent_eq_total) - sub_eq, 2)
                                if abs(diff_eq) > 0.01:
                                    period_dict["归母所有者权益不可加总差额_专项储备及其他权益项等"] = diff_eq

                    if period_dict:
                        result_periods.append(period_dict)
                break

        if not result_periods:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到证券代码 '{symbol}' 的财务报表数据（代码不存在或非有效 A 股/港股标的，无财报披露信息）。"
                    "建议先调用 stock_lookup 工具核对标准证券名称与代码。"
                )

        return {
            "symbol": clean_code,
            "statement_type": type_name,
            "period_type": period_type,
            "period_count": len(result_periods),
            "currency": "CNY",
            "unit": "元",
            "币种": "CNY",
            "金额单位": "元",
            "is_financial": is_financial,
            "accounting_standard": standard_name,
            "statements": result_periods,
            "会计口径与说明": (
                "1.【金融与工商业准则双轨制】：本接口底层内置工商业与金融企业（银行/保险/证券）双套专业映射体系，"
                "当查询金融企业(isjr=1)时自动无缝切换金融专用会计科目（覆盖吸收存款、货币资金、拆出资金、备付金等专有科目；"
                "执行新保险准则CAS25/IFRS17的保险公司如中国平安，由于上游专线未收录保险服务收入/已赚保费细项，"
                "营业收入与已知金融细项存在客观差额已自陈标注，营业利润严格遵循【营业收入 − 营业支出】）；"
                "查询普通实体企业(isjr=0)时对齐工商业企业准则。\n"
                "2.【流动负债二级明细防双计】：‘其他应付款合计’(N051)已按新准则口径合并包含‘其中_应付利息’(N049)与‘其中_应付股利’(N050)。明细科目加总时，"
                "切勿将‘其中_应付股利/应付利息’与‘其他应付款合计’重复累加，否则将导致流动负债总额双计虚增。\n"
                "3.【新租赁准则等科目缺失标注】：受底层工商业模板限制，工商业未收录新租赁准则科目（【租赁负债】、【使用权资产】）及【长期应付职工薪酬】。"
                "当期‘非流动负债合计’中已包含上述准则科目，故工商业非流动负债明细直接累加与小计存在残差。\n"
                "4.【所有者权益加总纪律与库存股抵减项】：依据财政部《企业会计准则》，‘库存股’是所有者权益的法定抵减科目（借方余额）。在计算归母所有者权益时，必须从各项权益总和中扣除库存股：【归母所有者权益 = 实收资本 + 资本公积 + 盈余公积 + 未分配利润 + 其他综合收益 + 专项储备 − 库存股】。严禁将库存股作为正数加计，否则会导致权益和高估 2×库存股。网关已自动挂载‘库存股_抵减项’与‘减_库存股’别名，并在底层权益自陈差额核算中严格执行扣减准则。\n"
                "5.【权益分区专项储备与不可加总差额】：针对工商业底层数据源未收录【专项储备】（如高危行业安全生产费储备）或特殊权益工具导致的明细与归母权益总额客观差额，网关已自动核算并自陈至【归母所有者权益不可加总差额_专项储备及其他权益项等】。\n"
                "6.【现金流量表结构规范】：现金流量表现已将主表（直接法三大活动流向）与间接法调节表物理隔离，间接法补充资料独立归集于‘补充资料_间接法’字典中，彻底避免科目遍历时的静默双计。\n"
                "7.【披露期三表期次同步说明】：在上市公司定期报告披露季（如三季报、年报发布期），上游数据库各报表库更新存在时间差（如资产负债表库通常优先录入，利润表与现金流量表随后入库），若发现三表最新期次不一致，属于上游数据源入库批次时差，调用时请核对每期'截止日期'或'报告期日期'。"
            ),
            "会计口径与合并说明": (
                "1.【金融与工商业准则双轨制】：本接口底层内置工商业与金融企业（银行/保险/证券）双套专业映射体系，"
                "当查询金融企业(isjr=1)时自动无缝切换金融专用会计科目（覆盖吸收存款、货币资金、拆出资金、备付金等专有科目；"
                "执行新保险准则CAS25/IFRS17的保险公司如中国平安，由于上游专线未收录保险服务收入/已赚保费细项，"
                "营业收入与已知金融细项存在客观差额已自陈标注，营业利润严格遵循【营业收入 − 营业支出】）；"
                "查询普通实体企业(isjr=0)时对齐工商业企业准则。\n"
                "2.【流动负债二级明细防双计】：‘其他应付款合计’(N051)已按新准则口径合并包含‘其中_应付利息’(N049)与‘其中_应付股利’(N050)。明细科目加总时，"
                "切勿将‘其中_应付股利/应付利息’与‘其他应付款合计’重复累加，否则将导致流动负债总额双计虚增。\n"
                "3.【新租赁准则等科目缺失标注】：受底层工商业模板限制，工商业未收录新租赁准则科目（【租赁负债】、【使用权资产】）及【长期应付职工薪酬】。"
                "当期‘非流动负债合计’中已包含上述准则科目，故工商业非流动负债明细直接累加与小计存在残差。\n"
                "4.【所有者权益加总纪律与库存股抵减项】：依据财政部《企业会计准则》，‘库存股’是所有者权益的法定抵减科目（借方余额）。在计算归母所有者权益时，必须从各项权益总和中扣除库存股：【归母所有者权益 = 实收资本 + 资本公积 + 盈余公积 + 未分配利润 + 其他综合收益 + 专项储备 − 库存股】。严禁将库存股作为正数加计，否则会导致权益和高估 2×库存股。网关已自动挂载‘库存股_抵减项’与‘减_库存股’别名，并在底层权益自陈差额核算中严格执行扣减准则。\n"
                "5.【权益分区专项储备与不可加总差额】：针对工商业底层数据源未收录【专项储备】（如高危行业安全生产费储备）或特殊权益工具导致的明细与归母权益总额客观差额，网关已自动核算并自陈至【归母所有者权益不可加总差额_专项储备及其他权益项等】。\n"
                "6.【现金流量表结构规范】：现金流量表现已将主表（直接法三大活动流向）与间接法调节表物理隔离，间接法补充资料独立归集于‘补充资料_间接法’字典中，彻底避免科目遍历时的静默双计。\n"
                "7.【披露期三表期次同步说明】：在上市公司定期报告披露季（如三季报、年报发布期），上游数据库各报表库更新存在时间差（如资产负债表库通常优先录入，利润表与现金流量表随后入库），若发现三表最新期次不一致，属于上游数据源入库批次时差，调用时请核对每期'截止日期'或'报告期日期'。"
            ),
        }

    def get_f10(self, symbol: str, category: str = "concepts") -> dict[str, Any]:
        """获取 F10 深度基本面（热点题材板块族谱、机构盈利预测、股东与主力持仓、龙虎榜异动等）"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        category_alias_map = {
            "industry": "concepts",
            "sector": "concepts",
            "dividends": "events",
            "bonus": "events",
            "lhb": "limit_up",
            "buyback": "repurchases",
            "indicators": "indicators",
            "financials": "financials",
        }
        cat = category_alias_map.get(category.lower().strip(), category.lower().strip())

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
        # - indicators/financials (核心财务与主要指标): [code, "gsgy", ""]
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
            "indicators": ("TdxSharePCCW.tdxf10_gg_zxts", [clean_code, "gsgy", ""], "核心财务与经营指标概览 (公司概要与主要指标)"),
            "financials": ("TdxSharePCCW.tdxf10_gg_zxts", [clean_code, "gsgy", ""], "核心财务与经营指标概览 (公司概要与主要指标)"),
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

    def screener(self, message: str, limit: int = 10, page: int = 1, filter_st: bool = True, query: str | None = None, condition: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """通达信自然语言智能选股 (输入中文自然语言条件，如'主板 绩优 储能 连板')"""
        # 修复 Issue 8: 识别查询条件中的港股/香港意图 (包含常见知名港股巨头与港股关键词)
        is_hk_query = bool(re.search(r"港股|香港|HKEX|\bHK\b|恒生|H股|腾讯|美团|阿里|中芯国际港股|小米", message, re.I))
        # 修复 P1: 分页请求与上游切片严格保持 1:1 对齐 (pageSize = limit)
        # 严禁人为虚增 pageSize，避免上游以 (pageNo - 1) * pageSize 计算偏移量时产生翻倍偏移、跳过整页标的
        fetch_size = max(1, min(limit, 100))
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
        filtered_st_count = 0
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
                    filtered_st_count += 1
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
                    "symbol": sec_code,
                    "name": sec_name,
                    "market": market_name,
                    "price": now_price,
                    "change_pct": chg_pct,
                    "is_st": is_st,
                    "indicators": dynamic_indicators,
                    "matched_indicators": dynamic_indicators,
                })

                if len(results) >= limit:
                    break

        # 从 Row 0 元信息 [0, '', 命中总数, session_id, count] 中精确提取全量命中总数（非单页返回数）
        total_hits = len(results)
        if isinstance(raw, list) and raw and isinstance(raw[0], list) and len(raw[0]) > 2:
            try:
                total_hits = int(raw[0][2])
            except (ValueError, TypeError):
                total_hits = len(results)

        total_pages = math.ceil(total_hits / limit) if limit > 0 else 1

        res_dict = {
            "query": message,
            "message": message,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "filter_st": filter_st,
            "matched_count": total_hits,
            "returned_count": len(results),
            "filtered_st_count": filtered_st_count,
            "filter_st_note": "默认已过滤 ST/*ST 股（若当页切片包含 ST 则过滤并记录 filtered_st_count）；若需获取全部股票（含风险警示股）请将 filter_st 设为 False。",
            "分页与过滤说明": "网关分页机制与上游切片严格保持 1:1 对齐（第 p 页对应上游切片范围 [(p-1)*limit .. p*limit-1]），杜绝跨页遍历时偏移倍增与漏标的；开启 filter_st 时在该页切片内精准剔除 ST/*ST 标的并记录 filtered_st_count，逐页并集与剔除 ST 之和严格守恒等于 matched_count。",
            "market_scope": "A股 (中国大陆交易所：沪市/深市/北交所)",
            "is_hk_intent": is_hk_query,
            "stocks": results,
            "raw_summary": raw[0] if (isinstance(raw, list) and raw) else None,
        }
        if is_hk_query:
            res_dict["warning"] = (
                "【市场范围警示】：当前自然语言选股引擎仅覆盖 A 股（中国大陆交易所：沪深北），暂不支持港股标的自动筛选。"
                "检测到查询条件中包含'港股/香港'相关意图，已默认按 A 股相关概念标的返回。"
                "如需查询具体港股（如腾讯 00700、美团 03690），请直接使用 stock_lookup 查找代码后调用 stock_quotes/stock_financials/stock_f10。"
            )
        return res_dict

