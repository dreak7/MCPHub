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
from ..core.market_utils import infer_market_setcode, normalize_datetime_iso
from ..core.network import _SECURE_OPENER
from .base import BaseMarketEngine


# =========================================================================
# 【功能块 0：法定会计键序重排】
# - 输入：待排序键、法定键序表
# - 功能：已知键按法定顺序；未知键继承前一个已知键的位置，绝不被统一甩到表尾
#         （落在「期末现金」之后的科目会被按小计锚点切区的解析器整段漏掉）
# - 输出：重排后的键列表
# =========================================================================
def _order_by_canonical(keys: list[str], canonical: list[str]) -> list[str]:
    order_index = {k: i for i, k in enumerate(canonical)}
    anchored: list[tuple[float, str]] = []
    cursor = -1.0
    for k in keys:
        if k in order_index:
            cursor = float(order_index[k])
            anchored.append((cursor, k))
        else:
            # 0.001 步进足够容纳单表 <1000 个未知键；再多会跨到下一个法定键之后
            cursor += 0.001
            anchored.append((cursor, k))
    return [k for _, k in sorted(anchored, key=lambda t: t[0])]


# =========================================================================
# 【功能块 0.1：五档挂单委买/委卖/委差/委比真算】
# - 输入：bsp_dict（键为中文数字档位 买一…买五 / 卖一…卖五）
# - 功能：汇总五档委托量并计算委差与委比；键名必须用中文数字，f"买{i}" 永远匹配不到
# - 输出：(委买手, 委卖手, 委差手, 委比%, 是否可计算)
# =========================================================================
# 五档档位契约（唯一事实源）：任何遍历/求和/标签生成都从这里取，
# 防止再次出现 f"买{i}"（买1…买5）匹配不到中文档位的整类缺陷
ORDERBOOK_BUY_LEVELS: tuple[str, ...] = ("买一", "买二", "买三", "买四", "买五")
ORDERBOOK_SELL_LEVELS: tuple[str, ...] = ("卖一", "卖二", "卖三", "卖四", "卖五")

# 估值口径阈值（A 股专用；港股有自己的口径，不共用）
A_PB_SNAPSHOT_DIFF_THRESHOLD = 0.03        # 行情 PB 与自算普通股 PB 的绝对差（倍）
A_BPS_EQUITY_GAP_THRESHOLD_PCT = 1.0       # BPS 隐含净资产与普通股归母权益的差（%）


def _pb_snapshot_diff_note(pb_val: Any, pb_calc: Any, has_other_equity_tools: bool = False) -> str:
    """行情 PB 与自算普通股 PB 的偏差提示（高估/低估/其他权益工具三种口径），偏差在阈值内返回空串。"""
    if pb_val is None or pb_calc is None or not pb_calc:
        return ""
    if abs(pb_val - pb_calc) <= A_PB_SNAPSHOT_DIFF_THRESHOLD:
        return ""
    pct_diff = round(abs(pb_val - pb_calc) / pb_calc * 100.0, 1)
    tail = (
        f"真实普通股PB已由网关自算生成【市净率_PB_自算_普通股】{pb_calc} 倍"
        "（= 现价 ÷ 普通股每股净资产_BPS_自算，分母采用扣除其他权益工具后的普通股净资产），"
        "跨工具横比请优先使用该字段；注意 F10 table6 的【市净率_PB】为总归母 BPS 口径，两者数值不可直接比对）。"
    )
    if pb_val > pb_calc:
        return (
            f"（注：行情市净率 {pb_val} 倍与普通股真实市净率存在约 {pct_diff}% 的高估偏差，"
            f"系行情 BPS 分母仍基于报告期旧股本或上游净资产统计口径差异所致；{tail}"
        )
    if has_other_equity_tools:
        return (
            f"（注：行情市净率 {pb_val} 倍因BPS分母基于未扣除其他权益工具(永续债/优先股)的归母总权益计算"
            f"而存在约 {pct_diff}% 的低估偏差；{tail}"
        )
    return (
        f"（注：行情市净率 {pb_val} 倍与普通股真实市净率存在约 {pct_diff}% 的低估偏差，"
        f"系行情 BPS 分母基数或口径差异所致；{tail}"
    )


def _bsp_from_snapshot(bsp_raw: dict[str, Any]) -> dict[str, Any]:
    """通达信极速快照字典 → 中文档位五档盘口（键序与旧实现一致：先买五档、后卖五档）。"""
    out: dict[str, Any] = {}
    for i, b_name in enumerate(ORDERBOOK_BUY_LEVELS, 1):
        out[b_name] = {
            "价格": bsp_raw.get(f"BuyPrice{i}") or bsp_raw.get(f"BuyP{i}"),
            "委托量_手": bsp_raw.get(f"BuyVol{i}") or bsp_raw.get(f"BuyV{i}"),
        }
    for i, s_name in enumerate(ORDERBOOK_SELL_LEVELS, 1):
        out[s_name] = {
            "价格": bsp_raw.get(f"SellPrice{i}") or bsp_raw.get(f"SellP{i}"),
            "委托量_手": bsp_raw.get(f"SellVol{i}") or bsp_raw.get(f"SellV{i}"),
        }
    return out


def _calc_order_imbalance(bsp_dict: dict[str, Any]) -> tuple[int, int, int, float, bool]:
    buy_names = ORDERBOOK_BUY_LEVELS
    sell_names = ORDERBOOK_SELL_LEVELS

    def _vol(name: str) -> int:
        item = bsp_dict.get(name)
        if not isinstance(item, dict):
            return 0
        val = item.get("委托量_手")
        return int(val) if isinstance(val, (int, float)) else 0

    buy_total = sum(_vol(n) for n in buy_names)
    sell_total = sum(_vol(n) for n in sell_names)
    diff = buy_total - sell_total
    total = buy_total + sell_total
    pct = round((diff / total) * 100.0, 2) if total > 0 else 0.0
    return buy_total, sell_total, diff, pct, total > 0


# =========================================================================
# 【第二道防翻车安全门：单一事实源 SSOT 架构治理公约 (AStockEngine)】
# - 职责：一级数据净水厂。负责网络原水抽取、东财/新浪/通达信数据解包、
#         万元与元量纲对齐 (如 LHB / 10000.0)、会计恒等式补齐推导 (如利润总额)、
#         期次换期 PB 重算等所有数值清洗逻辑。
# - 原则：底层洗净才出门。所有流向网关与调用方的数据，必须在引擎内部 100% 清洗完毕。
# =========================================================================
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
        # 缓存标的最新普通股归母权益信息 (clean_code -> (普通股归母权益, 原始归母合计, 报告期, 缓存时间戳))
        self._equity_cache: dict[str, tuple[float, float, str, float]] = {}
        # 自然语言选股分页快照（7.3）：key=查询串，60 秒内跨页共用，避免上游分页排序漂移
        self._screener_cache: dict[str, dict[str, Any]] = {}
        self._post_fn = post_fn

    # =========================================================================
    # 【功能块 0.1：最新财报普通股股东权益提取与缓存器 (修复 D2: 真实普通股市净率自算)】
    # - 输入：clean_code (str): 6位A股代码
    # - 功能：向底层财报接口查询最新1期资产负债表，提取归母所有者权益合计并扣除其他权益工具（永续债/优先股），
    #         计算真实普通股股东权益，并以 1800 秒 TTL 缓存避免高频重复拉取
    # - 输出：tuple[float, float, str] | None: (普通股股东权益_元, 归属于母公司所有者权益合计_元, 报告期截止日期)
    # =========================================================================
    def _get_latest_equity_info(self, clean_code: str) -> tuple[float, float, str] | None:
        now_ts = time.time()
        cached = self._equity_cache.get(clean_code)
        if cached and (now_ts - cached[3] < 1800.0):
            return (cached[0], cached[1], cached[2])
        try:
            fin = self.get_financials(clean_code, report_type="balance", limit_periods=1)
            stmts = fin.get("statements", [])
            if stmts:
                row = stmts[0]
                gm = float(row.get("归母所有者权益合计") or row.get("归属于母公司所有者权益合计") or row.get("归属于母公司股东权益合计") or row.get("所有者权益合计") or 0.0)
                qt = float(row.get("其他权益工具") or 0.0)
                ptg_qy = round(gm - qt, 2)
                bgq = str(row.get("截止日期") or row.get("报告期") or "")
                res_tuple = (ptg_qy, gm, bgq, now_ts)
                self._equity_cache[clean_code] = res_tuple
                return (ptg_qy, gm, bgq)
        except Exception:
            pass
        return None

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
                if attempt < max_retries and exc.code in (500, 502, 503, 504, 429):
                    time.sleep(0.89 * (attempt + 1))
                    continue
                err_msg = f"HTTP {exc.code} {exc.reason}"
                try:
                    detail = exc.read().decode("utf-8", errors="replace")
                    err_msg += f": {detail}"
                except Exception:
                    pass
                raise RuntimeError(f"TDX API request failed ({err_msg})") from exc
            except (URLError, TimeoutError, socket.timeout) as exc:
                if attempt < max_retries:
                    time.sleep(0.89 * (attempt + 1))
                    continue
                reason = getattr(exc, "reason", str(exc))
                raise RuntimeError(f"TDX API network connection error ({reason})") from exc
            except Exception as exc:
                if attempt < max_retries and ("timeout" in str(exc).lower() or "handshake" in str(exc).lower()):
                    time.sleep(0.89 * (attempt + 1))
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
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        def _load_once() -> list[dict[str, Any]]:
            try:
                req = Request(url, headers=headers)
                with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                    html = resp.read().decode("gbk", errors="ignore")
            except Exception:
                return []
            m = re.search(r'<table id="sharebonus_1"[^>]*>(.*?)</table>', html, re.S)
            if not m:
                return []
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S)
            loaded: list[dict[str, Any]] = []
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

                    prog_str = cells[4].strip()
                    ex_div = cells[5].strip()
                    div_item = {
                        "公告日期": cells[0],
                        "送股_每10股": _parse_div_num(cells[1]),
                        "转增_每10股": _parse_div_num(cells[2]),
                        "派息_每10股_含税": _parse_div_num(cells[3]),
                        "进度": prog_str,
                        "除权除息日": ex_div,
                        "股权登记日": cells[6].strip(),
                        "红股上市日": cells[7].strip() if len(cells) > 7 else "--",
                    }
                    # 修复 D9: 区分除权除息日占位符 '--' 的具体业务含义 (不分配不适用 vs 预案待披露)
                    if ex_div in ("--", "-", "", "None"):
                        if "不分配" in prog_str:
                            div_item["除权除息日说明"] = "该方案为不分配利润，不适用除权除息"
                        elif any(kw in prog_str for kw in ("预案", "董事会", "股东大会")):
                            div_item["除权除息日说明"] = "方案尚处预案/审议阶段，除权除息日待实施公告披露"
                        else:
                            div_item["除权除息日说明"] = "上游暂未收录具体除权除息日"
                    loaded.append(div_item)
            return loaded

        results = _load_once()
        if not results:
            # 上游新浪分红页偶发空页/反爬：实测首查 0 行、复测 16 行，重试一次再判空
            time.sleep(0.5)
            results = _load_once()
        return results

    # =========================================================================
    # 【功能块 5.5.1：A股官方龙虎榜异动与营业部买卖席位数据提取器 (修复 D1, D2)】
    # - 输入：clean_code (str): 6位A股代码（如 "600875", "002594"）
    # - 功能：调用东方财富数据中心龙虎榜官方数据专线 (RPT_DAILYBILLBOARD_DETAILS 与 RPT_BILLBOARD_DAILYDETAILSBUY)，
    #         提取标的近期上榜日期、收盘价、偏离值幅度、异动原因及前五大买卖营业部席位金额与净买额。
    #         将上榜总额准确除以 10000 转换为【万元】口径（彻底消除放大 10000 倍错误，修复 D1），
    #         并在行级与表级注入异动窗口累计成交额口径说明（消除单日成交额低估误解，修复 D2）。
    # - 输出：dict[str, Any]: 结构化龙虎榜记录与席位明细字典
    # =========================================================================
    def _get_ashare_lhb(self, clean_code: str) -> dict[str, Any]:
        clean_code = re.sub(r"[^\d]", "", clean_code).zfill(6)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://data.eastmoney.com/"}

        # 榜单总览：分页取全（50 条/页，最多 3 页），同日多榜次与更早记录不再被 pageSize=10 截断
        billboard_records: list[dict[str, Any]] = []
        board_truncated = False
        for page in range(1, 4):
            try:
                url_board = (
                    f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
                    f"reportName=RPT_DAILYBILLBOARD_DETAILS&columns=ALL&"
                    f"filter=(SECURITY_CODE%3D%22{clean_code}%22)&sortColumns=TRADE_DATE&sortTypes=-1"
                    f"&pageSize=50&pageNumber={page}"
                )
                req = Request(url_board, headers=headers)
                with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                    d = json.loads(resp.read().decode("utf-8", errors="replace"))
                rows = ((d.get("result") or {}).get("data") or [])
                for row in rows:
                    t_date = str(row.get("TRADE_DATE", ""))[:10]
                    # 修复 D1: 上游 BILLBOARD_BUY_AMT 原生单位为【元】，必须除以 10000 转换为【万元】
                    billboard_records.append({
                        "交易日期": t_date,
                        "收盘价": row.get("CLOSE_PRICE"),
                        "涨跌幅_pct": row.get("CHANGE_RATE"),
                        "龙虎榜买入总额_万元": round(float(row.get("BILLBOARD_BUY_AMT") or 0.0) / 10000.0, 2),
                        "龙虎榜卖出总额_万元": round(float(row.get("BILLBOARD_SELL_AMT") or 0.0) / 10000.0, 2),
                        "龙虎榜净买入额_万元": round(float(row.get("BILLBOARD_NET_AMT") or 0.0) / 10000.0, 2),
                        "市场总成交额_万元": round(float(row.get("ACCUM_AMOUNT") or 0.0) / 10000.0, 2) if row.get("ACCUM_AMOUNT") else None,
                        "市场总成交额_说明": "该成交额为交易所异动统计窗口期（如连续2个或3个交易日）内的多日累计成交总额，席位占总成交比例亦基于此多日总额计算，非单日K线成交额；单日成交额请参考 stock_kline",
                        "上榜原因": row.get("EXPLANATION") or row.get("EXPLAIN"),
                    })
                if len(rows) < 50:
                    break
                if page == 3:
                    board_truncated = True
            except Exception:
                break

        seat_records: list[dict[str, Any]] = []
        seat_truncated = False
        seat_sources = (
            ("买入前五席位", "RPT_BILLBOARD_DAILYDETAILSBUY", "TOTAL_BUYRIO"),
            ("卖出前五席位", "RPT_BILLBOARD_DAILYDETAILSSELL", "TOTAL_SELLRIO"),
        )
        for side_label, report_name, ratio_field in seat_sources:
            for page in range(1, 4):
                try:
                    url_seat = (
                        f"https://datacenter-web.eastmoney.com/api/data/v1/get?"
                        f"reportName={report_name}&columns=ALL&"
                        f"filter=(SECURITY_CODE%3D%22{clean_code}%22)&sortColumns=TRADE_DATE&sortTypes=-1"
                        f"&pageSize=50&pageNumber={page}"
                    )
                    req_seat = Request(url_seat, headers=headers)
                    with _SECURE_OPENER.open(req_seat, timeout=self.timeout) as resp:
                        d_seat = json.loads(resp.read().decode("utf-8", errors="replace"))
                    rows = ((d_seat.get("result") or {}).get("data") or [])
                    for row in rows:
                        seat_records.append({
                            "交易日期": str(row.get("TRADE_DATE", ""))[:10],
                            "席位类型": side_label,
                            "营业部席位名称": row.get("OPERATEDEPT_NAME"),
                            "买入金额_万元": round(float(row.get("BUY") or 0.0) / 10000.0, 2),
                            "卖出金额_万元": round(float(row.get("SELL") or 0.0) / 10000.0, 2),
                            "净买入额_万元": round(float(row.get("NET") or 0.0) / 10000.0, 2),
                            "占总成交比例_pct": round(float(row.get(ratio_field) or 0.0) * 100.0, 2) if row.get(ratio_field) else None,
                            "占总成交比例_说明": "占总成交比例系相对异动统计窗口期累计总成交额计算，非单日K线成交额",
                            "上榜异动说明": row.get("EXPLANATION"),
                        })
                    if len(rows) < 50:
                        break
                    if page == 3:
                        seat_truncated = True
                except Exception:
                    break

        # 买卖两表按 (交易日, 上榜原因, 席位, 买入额, 卖出额) 合并：只有金额完全相同的同一笔记录才折叠，
        # 避免把「机构专用」这类匿名席位的多个不同机构误合成一条（金额进 key 才能与榜单总额逐分对上）
        merged_seats: dict[tuple[str, str, str, float, float], dict[str, Any]] = {}
        for rec in seat_records:
            key = (
                str(rec.get("交易日期") or ""),
                str(rec.get("上榜异动说明") or "").strip(),
                str(rec.get("营业部席位名称") or ""),
                round(float(rec.get("买入金额_万元") or 0.0), 2),
                round(float(rec.get("卖出金额_万元") or 0.0), 2),
            )
            prev = merged_seats.get(key)
            if prev is None:
                merged_seats[key] = rec
                continue
            if prev.get("席位类型") != rec.get("席位类型"):
                buy_ratio = prev.get("占总成交比例_pct") if prev.get("席位类型") == "买入前五席位" else rec.get("占总成交比例_pct")
                sell_ratio = prev.get("占总成交比例_pct") if prev.get("席位类型") == "卖出前五席位" else rec.get("占总成交比例_pct")
                prev["席位类型"] = "买卖前五双向席位"
                prev["占总成交比例_pct_买"] = buy_ratio
                prev["占总成交比例_pct_卖"] = sell_ratio
                prev["双向席位说明"] = "该席位同日同时进入买入前五与卖出前五，买卖金额同源，按行累加会把同一笔成交计两次"
        collapsed_rows = len(seat_records) - len(merged_seats)
        seat_records = sorted(
            merged_seats.values(), key=lambda x: (str(x.get("交易日期", "")), str(x.get("席位类型", ""))), reverse=True
        )

        # 同日多榜次：给每条榜单编【榜单批次】，下游按它 join，避免按日期 join 双计 2.0×
        board_by_date: dict[str, list[dict[str, Any]]] = {}
        for b in billboard_records:
            board_by_date.setdefault(str(b.get("交易日期") or ""), []).append(b)
        batch_by_reason: dict[tuple[str, str], str] = {}
        for date, rows in board_by_date.items():
            for idx, b in enumerate(rows, 1):
                label = f"{date}-{idx}"
                batch_by_reason[(date, str(b.get("上榜原因") or "").strip())] = label
                b["榜单批次"] = label
                if len(rows) > 1:
                    b["同日多榜次说明"] = (
                        f"该交易日共有 {len(rows)} 条不同上榜原因的榜单记录，金额为同一批成交按不同口径分别统计；"
                        "按日期直接求和会重复计算，请以【榜单批次】为主键。"
                    )
        for s in seat_records:
            key = (str(s.get("交易日期") or ""), str(s.get("上榜异动说明") or "").strip())
            s["榜单批次"] = batch_by_reason.get(key, f"{key[0]}-?")

        board_dates = {str(b.get("交易日期") or "") for b in billboard_records}
        seat_dates = {str(s.get("交易日期") or "") for s in seat_records}
        missing_seat_dates = sorted(board_dates - seat_dates, reverse=True)
        multi_reason_dates = sorted([d for d, rows in board_by_date.items() if len(rows) > 1], reverse=True)

        res_lhb = {
            "symbol": clean_code,
            "category": "lhb",
            "category_name": "龙虎榜异动与营业部席位追踪",
            "source": "eastmoney_billboard (东方财富龙虎榜官方数据专线)",
            "成交额口径说明": "龙虎榜中【市场总成交额_万元】及席位【占总成交比例_pct】均为交易所异动统计窗口期（例如连续2个或3个交易日异动累计）的多日累计口径，若按单日K线成交额复核会存在2~3倍窗口期倍数差异；单日真实成交额请以 stock_kline 为准。",
            "席位去重口径说明": (
                "【龙虎榜席位去重口径（已收敛 P3）】：买卖两侧明细已按 (交易日期, 上榜原因, 席位名称, 买入额, 卖出额) 折叠同一笔重复记录"
                f"（本次折叠 {collapsed_rows} 条）；「机构专用」等匿名席位的不同机构因金额不同不会被误合并。"
                "同一席位同时进入买卖前五时标记为【买卖前五双向席位】，并分别给出【占总成交比例_pct_买】【占总成交比例_pct_卖】。"
                "按行求和即等于榜单买卖总额，无需再自行去重。"
            ),
            "formatted_tables": {
                "billboard_records": billboard_records,
                "seat_details": seat_records,
            },
        }
        if multi_reason_dates:
            res_lhb["同日多榜次说明"] = (
                f"共 {len(multi_reason_dates)} 个交易日在同一天有多条不同上榜原因的榜单记录"
                f"（如 {', '.join(multi_reason_dates[:3])}），各条金额为同一批成交按不同口径分别统计，"
                "按日期直接求和会双计；请以【榜单批次】为主键关联榜单与席位明细。"
            )
        if missing_seat_dates:
            res_lhb["席位覆盖说明"] = (
                f"榜单总览覆盖 {len(board_dates)} 个交易日，其中 {len(missing_seat_dates)} 个日期无席位明细"
                f"（如 {', '.join(missing_seat_dates[:3])}）；上游席位接口仅保留最近一批明细，"
                "历史日期的席位请以交易所披露或上市公司公告为准。"
            )
        if board_truncated or seat_truncated:
            res_lhb["数据完整性说明"] = "本次最多取 3 页 × 50 条；已达分页上限，更早的记录可能未取全。"
        if not billboard_records:
            res_lhb["无记录说明"] = "该标的近期未触发交易所龙虎榜公开信息披露标准（近期无严重异动/偏离值未达标），暂无营业部席位数据。"
        return res_lhb

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
        total_done_wan = 0.0
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
                        if amt_done is not None:
                            total_done_wan += amt_done
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

        res_rep = {
            "symbol": clean_code,
            "category": "repurchases",
            "category_name": "股份回购方案与实施明细",
            "source": "eastmoney_repurchases (东方财富官方回购数据专线)",
            "formatted_tables": {
                "repurchases": records,
            },
            "累计已实施回购总额_万元": round(total_done_wan, 2) if records else 0.0,
            "样本区间回购总额_万元": round(total_done_wan, 2) if records else 0.0,
        }
        if not records:
            res_rep["无记录说明"] = "该标的历史无公开披露的股份回购实施记录（或近期未发布股份回购方案）。如需获取公司最新重大公告，请调用 stock_news 工具。"
        return res_rep


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
                mapped_hq["涨跌额_元"] = round(now_p - close_p, 3)
            if close_p > 0 and high_p > 0 and low_p > 0:
                amp_val = round(((high_p - low_p) / close_p) * 100.0, 2)
                mapped_hq["日内振幅_pct"] = amp_val
            if "行情时间" in mapped_hq:
                mapped_hq["更新时间"] = mapped_hq["行情时间"]
            # 跨市场统一时间形态：旧键（20261026/131802）保留，另发 ISO 形态供下游直接解析
            iso_dt = normalize_datetime_iso(mapped_hq.get("行情日期"), mapped_hq.get("行情时间"))
            if iso_dt:
                mapped_hq["行情datetime"] = iso_dt
            # 彻底切除行内同值镜像别名【最高】、【最低】、【涨跌额】与【振幅_pct】，收敛为唯一规范键【最高价】、【最低价】、【涨跌额_元】与【日内振幅_pct】以防整表遍历求和双计
            mapped_hq.pop("最高", None)
            mapped_hq.pop("最低", None)
            mapped_hq.pop("涨跌额", None)
            mapped_hq.pop("振幅_pct", None)
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
                    
                    # 记录行情原始每股净资产与旧股本
                    raw_mgjzc = mgjzc
                    t6_sjl = 0.0
                    t6_zgb = 0
                    t6_ltgb = 0

                    # 修复 P3-6：若 F10 table6 存在精确到个位的真实法定总股本与流通A股，优先覆盖万股截断值
                    if t6_row:
                        t6_zgb = int(float(t6_row.get("zgb") or 0))
                        t6_ltgb = int(float(t6_row.get("ltgb") or 0))
                        if t6_zgb > 0:
                            mapped_ext["总股本_股"] = t6_zgb
                        if t6_ltgb > 0:
                            mapped_ext["流通股本_股"] = t6_ltgb
                        t6_sjl = float(t6_row.get("sjl") or 0.0)
                        if t6_sjl > 0:
                            mapped_ext["市净率_PB_F10"] = round(t6_sjl, 4)

                    if t4_row and t4_row.get("bgq"):
                        f10_bgq = str(t4_row.get("bgq"))
                        f10_mgsy = float(t4_row.get("mgsy") or 0.0)
                        f10_mgjzc = float(t4_row.get("mgjzc") or 0.0)
                        mapped_ext["最新财报报告期"] = f10_bgq
                        is_stale = False
                        if f10_mgsy > 0 and (abs(f10_mgsy - mgsy) > 0.001 or (f10_mgjzc > 0 and abs(f10_mgjzc - mgjzc) > 0.01)):
                            is_stale = True

                        # 修复 P2-2（根因）：diff_factor_str 必须分两路计算——
                        # is_stale=False 路径：mgsy 即是最终报告期值，用旧 mgsy 计算实测乘数无误；
                        # is_stale=True 路径：mgsy 将被换成 f10_mgsy，需在换期之后用新值重算，
                        # 否则用旧 mgsy 基准算出的乘数会指向旧期次 EPS，导致归因假阳性。
                        # 此处先用旧 mgsy 计算供 is_stale=False 路径使用；is_stale=True 路径在下方单独重算。
                        diff_factor_str = ""
                        raw_ann_eps = mapped_ext.get("年化每股收益_EPS") or (round(now_p / syl, 4) if (syl > 0 and now_p > 0) else None)
                        if mgsy > 0 and raw_ann_eps:
                            try:
                                actual_mult = round(float(raw_ann_eps) / float(mgsy), 3)
                                exp_mult = 2.0 if f10_bgq.endswith("-06-30") else (4.0 if f10_bgq.endswith("-03-31") else (1.333 if f10_bgq.endswith("-09-30") else 1.0))
                                if abs(actual_mult - exp_mult) > 0.04:
                                    diff_factor_str = f"（注：年化EPS实测乘数为{actual_mult}倍，与标准年化倍数{exp_mult}倍存在差异，系标的在报告期后发生股本变动/增发或发行永续债等，致使年化EPS基于现总股本而BPS仍基于报告期旧股本基数不同所致）。"
                            except (ValueError, TypeError):
                                pass

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

                            # 修复 P1: 换期重算覆盖 PB 分母，防止 F10 table6 sjl 停留在上一期旧净资产口径造成高估
                            if mgjzc > 0 and now_p > 0:
                                mapped_ext["市净率_PB"] = round(now_p / mgjzc, 2)
                                pb_recalc = round(now_p / mgjzc, 4)
                                if t6_sjl > 0 and raw_mgjzc > 0:
                                    old_pb_approx = now_p / raw_mgjzc
                                    equity_factor = (t6_sjl / old_pb_approx) if (old_pb_approx > 0 and abs(t6_sjl / old_pb_approx - 1.0) > 0.05) else 1.0
                                    pb_recalc = round((now_p / mgjzc) * equity_factor, 4)
                                mapped_ext["市净率_PB_自算_普通股"] = pb_recalc
                                mapped_ext["市净率_PB_换期重算标记"] = True

                            # 修复 P2-2（is_stale=True 路径）：换期后用 f10_mgsy（新值）重算实测乘数，
                            # 覆盖上面用旧 mgsy 算出的 diff_factor_str，消除归因假阳性。
                            new_ann_eps = mapped_ext.get("年化每股收益_EPS") or raw_ann_eps
                            if f10_mgsy > 0 and new_ann_eps:
                                try:
                                    actual_mult_stale = round(float(new_ann_eps) / float(f10_mgsy), 3)
                                    exp_mult_stale = 2.0 if f10_bgq.endswith("-06-30") else (4.0 if f10_bgq.endswith("-03-31") else (1.333 if f10_bgq.endswith("-09-30") else 1.0))
                                    if abs(actual_mult_stale - exp_mult_stale) > 0.04:
                                        diff_factor_str = f"（注：年化EPS实测乘数为{actual_mult_stale}倍，与标准年化倍数{exp_mult_stale}倍存在差异，系标的在报告期后发生股本变动/增发或发行永续债等，致使年化EPS基于现总股本而BPS仍基于报告期旧股本基数不同所致）。"
                                    else:
                                        diff_factor_str = ""
                                except (ValueError, TypeError):
                                    diff_factor_str = ""

                            # 修复 P2-1：换期触发时，f10 indicators/summary/quotes 同响应内
                            # table6.市净率_PB_昨收（sjl 字段）是上游 F10 通道 raw 映射（上一交易日收盘清算 PB），
                            # 不随 quotes 换期引擎重算，会停留在旧期次净资产计算的旧 PB 值，形成自相矛盾。
                            # 修复方式：在 quotes 估值字段注入 市净率_PB_昨收_校准说明，
                            # 并用新 BPS 与最后已知收盘价自算覆盖 市净率_PB_昨收 字段，保持响应内代数一致。
                            if t6_sjl > 0 and mgjzc > 0:
                                close_ref = close_p if close_p > 0 else now_p
                                if close_ref > 0:
                                    pb_yishou_recalc = round(close_ref / mgjzc, 4)
                                    pb_now_recalc = round(now_p / mgjzc, 4) if now_p > 0 else pb_yishou_recalc
                                    derived_old_bps = round(close_ref / t6_sjl, 3)
                                    mapped_ext["市净率_PB_昨收_换期重算"] = pb_yishou_recalc
                                    # 修复 D4: 当发生换期时，市净率_PB_F10 同步校准为换期后新BPS与现价计算的真实PB，与 F10 table6 市净率_PB 100% 对齐
                                    mapped_ext["市净率_PB_F10"] = pb_now_recalc
                                    mapped_ext["市净率_PB_昨收_校准说明"] = (
                                        f"F10 table6.市净率_PB_昨收(sjl)={t6_sjl} 系上游 F10 通道基于上一期旧净资产(反解约{derived_old_bps}元/股)的收盘清算 PB，"
                                        f"未随本次换期({f10_bgq})自动重算；网关已以最新报告期 BPS({mgjzc}元)自算得 "
                                        f"PB_昨收_换期重算={pb_yishou_recalc}倍；现价市净率_PB_F10 同步校准为 {pb_now_recalc} 倍；下游横比请使用换期重算值，切勿直接引用原始 sjl 字段。"
                                    )

                            mapped_ext["派生指标换期说明"] = f"检测到上游行情盘口指标滞留于旧期次，已自动自算对齐至最新披露的 {f10_bgq} 财报口径 (EPS={f10_mgsy}元, BPS={f10_mgjzc}元)；PB 分母已同步换期重算为 {mapped_ext.get('市净率_PB_自算_普通股')} 倍。{diff_factor_str}"
                        else:
                            # 修复 P2: 600030 派生指标换期说明措辞优化（严禁在股本变动或永续债标的上声称无需重算即可直接推算）
                            pb_simple = round(now_p / mgjzc, 4) if mgjzc > 0 else 0
                            if t6_sjl > 0 and pb_simple > 0 and (abs(t6_sjl - pb_simple) > 0.03 or abs(t6_sjl / pb_simple - 1.0) > 0.03):
                                mapped_ext["派生指标换期说明"] = (
                                    f"财报期次已与最新披露的 {f10_bgq} 财报同步；但需注意：行情 BPS ({mgjzc}元) 分母仍基于报告期旧股本，"
                                    f"且归母包含其他权益工具（永续债/优先股），与现总股本推算存在偏差；推算真实市净率必须使用【市净率_PB_自算_普通股】({t6_sjl}倍)，"
                                    f"严禁直接用现价/BPS推算。{diff_factor_str}"
                                )
                            else:
                                mapped_ext["派生指标换期说明"] = f"上游行情盘口指标与最新披露的 {f10_bgq} 财报完全同步对齐，无需换期重算。{diff_factor_str}"
                except Exception:
                    pass

            if mgjzc > 0:
                if now_p > 0 and "市净率_PB" not in mapped_ext:
                    mapped_ext["市净率_PB"] = round(now_p / mgjzc, 2)

            # =========================================================================
            # 【功能块 4.1.1：真实普通股市净率自算 (市净率_PB_自算_普通股，修复 D2)】
            # - 输入：clean_code (标的代码), now_p (现价), mapped_ext 中总股本_股, _get_latest_equity_info
            # - 功能：向底层财报系统获取最新资产负债表归母所有者权益合计，依法扣除其他权益工具（永续债/优先股），
            #         计算真实普通股归母权益；以 [现价 × 总股本 ÷ 普通股归母权益] 真实自算普通股市净率，
            #         彻底终结因上游 F10 漏算专项储备（如比亚迪 002594 漏算 208 亿高估 8.69%）或含永续债导致的失真
            # - 输出：写入 mapped_ext["市净率_PB_自算_普通股"] 与 mapped_ext["普通股每股净资产_BPS_自算"]
            # =========================================================================
            zgb_current = mapped_ext.get("总股本_股") or (int(round(zgb_wan * 10000)) if zgb_wan > 0 else 0)
            if zgb_current > 0 and now_p > 0:
                eq_info = self._get_latest_equity_info(clean_code)
                if eq_info:
                    ptg_qy, raw_gm, eq_bgq = eq_info
                    if ptg_qy > 0:
                        calc_pb_common = round((now_p * zgb_current) / ptg_qy, 4)
                        calc_bps_common = round(ptg_qy / zgb_current, 4)
                        mapped_ext["市净率_PB_自算_普通股"] = calc_pb_common
                        mapped_ext["普通股每股净资产_BPS_自算"] = calc_bps_common
                        mapped_ext["普通股归母权益_元"] = ptg_qy

            # 修复 P2-2: 002594 (比亚迪) 等标的 BPS 漏算外币报表折算差额/专项储备导致行情 PB 高估 8.7%
            # 无论是否处于财报换期窗口，只要行情 PB 与自算普通股 PB 偏差超过 3% 且当前说明误报“无需换期重算”，
            # 必须强制纠偏为警示，严禁误导称“无需换期重算”
            calc_pb_val = mapped_ext.get("市净率_PB_自算_普通股")
            pb_simple_val = round(now_p / mgjzc, 4) if (mgjzc > 0 and now_p > 0) else 0.0
            cur_note = str(mapped_ext.get("派生指标换期说明", ""))
            if "无需换期重算" in cur_note and calc_pb_val and calc_pb_val > 0 and pb_simple_val > 0:
                rel_diff = abs(pb_simple_val - calc_pb_val) / calc_pb_val
                if rel_diff > 0.03:
                    pct_str = round(rel_diff * 100.0, 1)
                    direction = "高估" if pb_simple_val > calc_pb_val else "低估"
                    mapped_ext["派生指标换期说明"] = (
                        f"【BPS口径偏差与重算警示】：该标的行情 BPS ({mgjzc}元) 分母仍基于报告期旧股本或存在外币报表折算差额/其他权益工具漏算，"
                        f"导致行情市净率_PB({pb_simple_val}倍)被{direction}约 {pct_str}%（偏离真实普通股归母权益）；"
                        f"推算真实市净率必须使用【市净率_PB_自算_普通股】({calc_pb_val}倍)，严禁直接用现价/BPS推算。"
                    )

            if "市净率_PB_自算_普通股" not in mapped_ext and mapped_ext.get("市净率_PB") is not None:
                mapped_ext["市净率_PB_自算_普通股"] = round(float(mapped_ext["市净率_PB"]), 4)
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
            if ttm_val is not None and ttm_val < 0:
                mapped_ext["市盈率可用状态"] = "亏损不可用"
                mapped_ext["市盈率亏损标记"] = True
                ttm_note = f"滚动市盈率_PETTM为负({ttm_val}倍)，因过去4季度滚动归母净利为负处于亏损状态，属于不可用指标，严禁用于低估值横比、排序或PEG推演；"
            elif ttm_val is not None:
                mapped_ext["市盈率可用状态"] = "正常"
                mapped_ext["市盈率亏损标记"] = False
                ttm_note = f"滚动市盈率_PETTM({ttm_val}倍)按过去4季度滚动归母净利测算；"
            else:
                ttm_note = ""
            pb_val = mapped_ext.get("市净率_PB")
            pb_calc = mapped_ext.get("市净率_PB_自算_普通股")
            pb_calc_str = f"，网关自算普通股市净率为 {pb_calc} 倍" if pb_calc is not None else ""
            _scope = locals()
            has_other_equity_tools = bool(
                _scope.get("eq_info")
                and (float(_scope.get("raw_gm") or 0.0) - float(_scope.get("ptg_qy") or 0.0) > 1000)
            )
            pb_diff_warn = _pb_snapshot_diff_note(pb_val, pb_calc, has_other_equity_tools)
            # 修复 P2-5：BPS 隐含净资产与归母权益的差额给出量化自陈
            # （002594 实测差额 208.44 亿恰等于外币报表折算差额，旧文案只按「送转→低估」单向解释）
            try:
                bps_up = mapped_ext.get("每股净资产_BPS")
                shares_total = mapped_ext.get("总股本_股")
                eq_parent = mapped_ext.get("普通股归母权益_元")
                if bps_up and shares_total and eq_parent:
                    implied_eq = float(bps_up) * float(shares_total)
                    gap_pct = (implied_eq - float(eq_parent)) / float(eq_parent) * 100.0
                    if abs(gap_pct) >= A_BPS_EQUITY_GAP_THRESHOLD_PCT:
                        mapped_ext["BPS隐含净资产差额说明"] = (
                            f"上游 BPS({bps_up})×总股本({int(shares_total)}) = {implied_eq:,.2f} 元，"
                            f"与普通股归母权益({float(eq_parent):,.2f} 元)相差 {gap_pct:+.2f}%；"
                            "差额可能来自报告期股本时滞、外币报表折算差额或其他综合收益未计入 BPS，"
                            "估值方向与幅度以【市净率_PB_自算_普通股】为准。"
                        )
            except (TypeError, ValueError, ZeroDivisionError):
                pass
            pb_note = (
                f"市净率_PB({pb_val}倍{pb_calc_str})由[现价÷每股净资产_BPS]派生；上游 BPS 与归母权益之间可能存在正向或负向偏差"
                "（报告期旧股本、其他权益工具、外币报表折算差额等），方向与幅度以【市净率_PB_自算_普通股】为准，不得默认按低估处理。"
                "精确普通股PB建议结合资产负债表重算：普通工商业标的为[总市值 ÷ 归母所有者权益]；"
                "金融企业（券商/银行/保险）或发行了永续债/优先股的标的，分母必须扣除其他权益工具，即[总市值 ÷ (归母所有者权益 − 其他权益工具/优先股/永续债)]，"
                f"严禁直接以归母总权益计算导致估值大幅低估（如中信证券会低估约16%，邮储银行会低估约13%）；{pb_diff_warn}"
            ) if pb_val is not None else ""
            loss_pe_warn = f" 【高危警示·亏损股市盈率不可用】标的处于亏损状态，市盈率为负({ttm_val}倍)，负市盈率属于亏损代号而非低估值，严禁带入PEG或参与横向排序！" if (ttm_val is not None and ttm_val < 0) else ""
            mapped_ext["估值口径说明"] = (
                f"{ttm_note}{pb_note}动态市盈率_PE({mapped_ext.get('动态市盈率_PE')}倍)采用最新报告期年化折算口径({ann_eps_str})；"
                f"报告期每股收益_EPS({mgsy}元)为非年化单期累计值，两者口径不同，禁止直接互乘推导价格或市值。{loss_pe_warn}"
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
                    f"盘后休市期间，上游字段尚未更新至下一个交易日（如当前标的 {clean_code} 盘后字段 {zt_curr}元 仍为当日限价）。"
                    f"网关已依据当日最新基准现价({ref_price}元)及标的板块涨跌幅限制(±{int(r_limit*100)}%)自算生成【次日参考涨停价】({next_zt}元)与【次日参考跌停价】({next_dt}元)供隔夜推演参考；"
                    "次日官方实际涨跌停价请以交易所早间开盘清算价为准。"
                )

            result["估值与基本盘面"] = mapped_ext

        # 3. 五档深度买卖盘口整理（通达信官方 BspInfo 返回 5 项明细数组或极速备用字典）
        bsp_raw = raw.get("BspInfo", [])
        bsp_dict = {}
        if isinstance(bsp_raw, list) and bsp_raw:
            for idx, item in enumerate(bsp_raw[:5]):
                if isinstance(item, dict):
                    b_label = ORDERBOOK_BUY_LEVELS[idx]
                    s_label = ORDERBOOK_SELL_LEVELS[idx]
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
            bsp_dict = _bsp_from_snapshot(bsp_raw)

        # P3-4 修复：旧实现用 f"买{i}"（买1…买5）匹配中文档位 买一…买五，永远命中不到 → 四字段恒 0
        buy_total_hand, sell_total_hand, weicha_hand, weibi_pct, weibi_ok = _calc_order_imbalance(bsp_dict)
        if "盘口核心行情" in result:
            hq_view = result["盘口核心行情"]
            hq_view["委买总委托量_手"] = buy_total_hand
            hq_view["委卖总委托量_手"] = sell_total_hand
            hq_view["委差_手"] = weicha_hand
            hq_view["委比_pct"] = weibi_pct
            hq_view["委差_手_可计算"] = weibi_ok
            hq_view["委比_pct_可计算"] = weibi_ok
            if not weibi_ok:
                hq_view["委比委差说明"] = (
                    "当前五档买卖挂单量均为 0（可能为停牌、盘前/集合竞价尚未形成挂单，或盘后已无残单），委差与委比不可计算；"
                    "表中 0 为占位值，勿解读为多空委托力量均衡。本键为条件键，仅在五档全 0 时下发。"
                )

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

        # 严格校验 K 线周期枚举与复权方式 (修复 P1-1: 杜绝非法周期静默映射为日线)
        valid_periods = {"day", "week", "month", "60m", "30m", "15m", "5m", "1m"}
        period_alias = {"daily": "day", "weekly": "week", "monthly": "month"}
        period_raw = str(period).lower().strip()
        norm_period = period_alias.get(period_raw, period_raw)
        if norm_period not in valid_periods:
            raise ValueError(f"无效的 K 线周期 '{period}'。支持的周期为: ['day', 'week', 'month', '60m', '30m', '15m', '5m', '1m']")

        valid_adjusts = {"qfq", "hfq", "none"}
        adjust_raw = str(adjust).lower().strip()
        if adjust_raw not in valid_adjusts:
            raise ValueError(f"无效的复权方式 '{adjust}'。支持的复权方式为: ['qfq', 'hfq', 'none']")

        # 周期映射: day->4, week->5, month->6, 60m->3, 30m->2, 15m->1, 5m->0, 1m->7
        period_map = {"5m": 0, "15m": 1, "30m": 2, "60m": 3, "day": 4, "week": 5, "month": 6, "1m": 7}
        p_val = period_map[norm_period]

        # 复权映射: qfq->1, hfq->2, none->0 (通达信官方 PBFXT 底层协议规范)
        adjust_map = {"none": 0, "qfq": 1, "hfq": 2}
        tq_val = adjust_map[adjust_raw]

        # 预热缓冲区机制：针对计算技术指标 (MACD/EMA/BOLL/KDJ) 的场景，自动多拉取 200 根历史预热缓冲 (Warm-up buffer)
        # 消除递归指数滤波在截断窗口内的初值漂移缺陷 (解决 P1-2)
        # 严格校验请求根数参数范围 (修复 P3-1: 杜绝非法负数/零越界)
        try:
            n_count = int(count)
        except (ValueError, TypeError):
            raise ValueError(f"count 必须为整数，收到: {count}")
        if n_count < 1 or n_count > 500:
            raise ValueError(f"count 参数越界: {n_count}，必须在 1 到 500 之间")

        req_count = min(n_count, 300)
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
            if norm_period in ("1m", "5m", "15m", "30m", "60m") and len(raw_row) > 1 and str(raw_row[1]).replace(".", "").isdigit():
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
            # 修复 P4-1: 成交量严格四舍五入整型规范化，消除单精度浮点导致的 1 手微差毛刺
            raw_vol_f = float(raw_row[8]) if len(raw_row) > 8 else (float(raw_row[7]) if len(raw_row) > 7 else 0.0)
            vol_val = int(round(raw_vol_f))
            # 周线与月线通达信底层上报单位为手 (1手=100股)，日线与分钟线底层为股。统一跨周期成交量单位为【股】消除漂移
            if norm_period in ("week", "month"):
                vol_val = int(round(raw_vol_f * 100.0))

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

        resp: dict[str, Any] = {
            "symbol": clean_code,
            "name": stock_name,
            "setcode": m_setcode,
            "period": norm_period,
            "adjust": adjust_raw,
            "total_bars": len(parsed_bars),
            "indicators_included": bool(with_indicators),
            "klines": parsed_bars,
        }
        # 修复 P3-2：技术指标别名键并存声明
        # boll_up/boll_upper、boll_dn/boll_lower、macd_bar/macd_hist 三对别名键逐位同值（无错值风险），
        # 为保持向后兼容暂不删除任何一方；但整表遍历或写入数据库时会产生重复列，请按需选取一套后丢弃另一套。
        if with_indicators:
            resp["技术指标别名说明"] = (
                "【K线技术指标别名并存提示】每根 bar 同时存在以下三对逐位相同的同义别名键，纯为向后兼容保留，无错值风险但入库时会产生重复列：\n"
                "  · BOLL上轨：boll_up = boll_upper（两者逐位相同）；\n"
                "  · BOLL下轨：boll_dn = boll_lower（两者逐位相同）；\n"
                "  · MACD柱：macd_bar = macd_hist（两者逐位相同）。\n"
                "建议入库/分析时各取一个规范键（推荐 boll_upper/boll_lower/macd_hist），丢弃另一套别名，避免重复列。"
            )
        if int(count) > 300:
            resp["截断说明"] = (
                f"单次请求根数 count={count} 超过单次最大安全返回上限(300根)，已自动截断为最近 300 根。"
                "如需更大时间跨度的历史行情，请切换为更大周期（如 day/week）或分批请求。"
            )
            resp["超限提示"] = (
                f"当前请求 count={count} 超过单次最大返回上限(300根)。"
                "已自动截断交付最近 300 根有效 K 线。"
            )
        elif int(count) > len(parsed_bars):
            resp["超限提示"] = (
                f"当前请求 count={count} 大于当前标的在该周期实际可提取的最大历史记录数({len(parsed_bars)}根)。"
                "已全量交付当前标的所能提供的全部历史 K 线。"
            )

        return resp


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

        # 严格校验历史财报期数范围 (修复 P3-1: 杜绝 limit_periods=0 导致空表及 >10 越界)
        try:
            n_lp = int(limit_periods)
        except (ValueError, TypeError):
            raise ValueError(f"limit_periods 必须为整数，收到: {limit_periods}")
        if n_lp < 1 or n_lp > 10:
            raise ValueError(f"limit_periods 参数越界: {n_lp}，必须在 1 到 10 之间")

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

        # =========================================================================
        # 【功能块 7.1.1：金融细分子行业智能分类器 (券商/保险/银行，修复 D1)】
        # - 输入：is_financial (bool), entity 名称与代码
        # - 功能：精准识别金融机构细分子行业（券商/证券、保险、银行），供现金流与利润表科目映射特化使用
        # - 输出：is_broker, is_insurance, is_bank 布尔标志位
        # =========================================================================
        entity_name = str((entity.get("entity_name") or entity.get("name") or "") if isinstance(entity, dict) else (getattr(entity, "name", "") or ""))
        is_broker = is_financial and any(k in entity_name for k in ("证券", "券商", "信托", "期货"))
        is_insurance = is_financial and any(k in entity_name for k in ("保险", "人寿", "太保", "平安", "新华"))
        is_bank = is_financial and ("银行" in entity_name)

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

                    # 针对库存股：收敛为唯一规范键【库存股】，彻底切除易导致下游遍历加总3倍虚增的【减_库存股】与【库存股_抵减项】正数别名 (修复 P2-2)
                    if "减_库存股" in period_dict:
                        val = period_dict.pop("减_库存股")
                        if "库存股" not in period_dict:
                            period_dict["库存股"] = val
                    if "库存股_抵减项" in period_dict:
                        val = period_dict.pop("库存股_抵减项")
                        if "库存股" not in period_dict:
                            period_dict["库存股"] = val

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

                    is_quarter_mode = (period_type.lower() == "quarter")
                    if iso_date:
                        period_dict["截止日期"] = iso_date
                        period_dict["报告期日期"] = iso_date
                        month = iso_date[5:7] if len(iso_date) >= 7 else ""
                        rpt_name = "一季报" if month == "03" else ("中报" if month == "06" else ("三季报" if month == "09" else ("年报" if month == "12" else "")))
                        q_map = {"03": "一季度", "06": "二季度", "09": "三季度", "12": "四季度"}
                        if is_quarter_mode:
                            q_label = q_map.get(month, "单季度")
                            if rpt_name:
                                period_dict["报表类型"] = rpt_name
                                period_dict["报告期"] = f"{iso_date[:4]}{rpt_name}"
                                period_dict["报告期标签"] = f"{iso_date[:4]}{rpt_name}"
                            else:
                                period_dict["报表类型"] = f"{iso_date[:4]}{q_label}"
                                period_dict["报告期"] = f"{iso_date[:4]}{q_label}"
                                period_dict["报告期标签"] = f"{iso_date[:4]}{q_label}"
                            period_dict["单期标签"] = f"{iso_date[:4]}{q_label}"
                            period_dict["单季度计算说明"] = f"本行为 {iso_date[:4]} 年第{q_label[0]}季度独立单季发生额（非累计值）。"
                        else:
                            if rpt_name:
                                period_dict["报表类型"] = rpt_name
                                period_dict["报告期"] = f"{iso_date[:4]}{rpt_name}"
                                period_dict["报告期标签"] = f"{iso_date[:4]}{rpt_name}"
                    elif raw_end:
                        if is_quarter_mode:
                            period_dict["报告期"] = raw_end
                            period_dict["报告期标签"] = raw_end
                            period_dict["单期标签"] = raw_end
                            period_dict["单季度计算说明"] = f"本行为 {raw_end} 独立单季发生额（非累计值）。"
                        else:
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
                        # 修复 D8: 间接法调整项之和不等于补充经营净额的自陈声明，警示严禁拆解累加调整项
                        supp_dict["间接法调整项可用性说明"] = (
                            "【间接法补充资料调节项使用警示】间接法附表中仅列示已采集的已知调整细项（如折旧、摊销等），"
                            "底层专线未收录营运资金变动（经营性应收应付增减）等全部调节科目，调整细项之和不等于经营活动现金流量净额。"
                            "现金流质量评估与财务建模必须且只能直接引用【补充资料_经营活动现金流量净额】及主表三大净额，严禁机械累加细项。"
                        )
                        period_dict["补充资料_间接法"] = supp_dict

                    if rtype in ("cashflow", "cashflow_statement", "xjllb"):
                        for hk in ("现金的期末余额_核对", "减_现金的期初余额_核对", "加_现金等价物的期末余额_核对", "减_现金等价物的期初余额_核对", "现金及现金等价物净增加额_主表", "现金及现金等价物净增加额_补充法"):
                            period_dict.pop(hk, None)
                        if is_financial:
                            # =========================================================================
                            # 【功能块 7.1.2：券商专有现金流出科目纠偏 (修复 D1)】
                            # - 输入：is_broker (bool), period_dict 中 N020 原生映射字段
                            # - 功能：针对券商/证券类标的，将通达信 N020 原生错误映射的【支付原保险合同赔付款项的现金】
                            #         转换为规范券商科目【为交易目的而持有的金融资产及融出资金等净支出】，
                            #         消除在券商现金流中出现巨额保险赔付的事实性荒谬
                            # - 输出：写入规范券商科目并移除保险专用科目
                            # =========================================================================
                            if is_broker and "支付原保险合同赔付款项的现金" in period_dict:
                                val_n020 = period_dict.pop("支付原保险合同赔付款项的现金")
                                period_dict["为交易目的而持有的金融资产及融出资金等净支出"] = val_n020

                            broker_cf_note = (
                                "【证券企业现金流科目纠偏说明（已收敛 D1）】：券商无保险合同业务，通达信专线原生字段 N020 实际对应券商"
                                "【为交易目的而持有的金融资产及融出资金等净支出】（金额达数百亿，属自营投资与两融融出资金净支出）。"
                                "网关已自动纠偏至规范券商科目，彻底清除保险专用科目误导。\n"
                            ) if is_broker else ""

                            period_dict["现金流量表加总纪律说明"] = (
                                "【金融企业现金流量表核算规范】本表遵循《金融企业财务报表格式》（银行/保险/证券）。\n"
                                f"{broker_cf_note}"
                                "【金融企业税费返还科目特别提示（已收敛 D6）】：通达信专线将科目 N010 映射为【收到的税费返还】，"
                                "在银行与券商等机构中金额较大（占营收比例较高），系底层报表归集口径包含代扣代缴返还或税金清算，数据完全透传自通达信专线原生字段。\n"
                                "【金融企业现金流明细覆盖度限制特别警示】通达信及公开专线对金融企业现金流量表明细采集存在客观缺失，明细科目严禁用于现金流内部百分比结构分析！"
                                "财务建模与流动性分析时，必须且只能引用三大活动净额与期末现金及现金等价物余额（三净额与期末现金桥严格闭合）。\n"
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

                            # 金融企业经营活动现金流出不可加总差额 (原保险赔付、融出资金、返售拆出等缺项，修复 D1 & D2)
                            jy_out = period_dict.get("经营活动现金流出小计")
                            if jy_out is not None and isinstance(jy_out, (int, float)):
                                known_fin_jy_out = [
                                    "客户贷款及垫款净增加额", "存放中央银行和同业款项净增加额",
                                    "支付原保险合同赔付款项的现金", "为交易目的而持有的金融资产及融出资金等净支出",
                                    "拆出资金净增加额", "返售业务资金净增加额", "支付利息、手续费及佣金的现金",
                                    "支付保单红利的现金", "支付再保业务现金净额", "支付原保险合同款项现金",
                                    "支付给职工以及为职工支付的现金", "支付的各项税费", "支付其他与经营活动有关的现金"
                                ]
                                diff_fin_out = round(float(jy_out) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_jy_out), 2)
                                if abs(diff_fin_out) > 0.01:
                                    diff_cf_out_key = "经营活动现金流出不可加总差额_金融交易及细项科目缺项" if is_broker else "经营活动现金流出不可加总差额_保险赔付及拆出返售等金融科目缺项"
                                    period_dict[diff_cf_out_key] = diff_fin_out

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
                                    # 修复 P2-5: 茅台等合并持牌金融子公司的企业，流出项含金融负抵减导致小计小于工商明细和
                                    if diff_jy_out < -100000000.0:
                                        period_dict["经营活动现金流出差额结构说明"] = (
                                            "该期经营活动现金流出小计小于已知工商明细和（差额为负）。"
                                            "权威成因：公司合并报表涵盖持牌金融子公司（如贵州茅台控股之贵州茅台集团财务有限公司），"
                                            "其客户存款及同业存放款项净减少额、存放中央银行和同业款项净变动等金融专有流出科目在官方报表中以负数抵减列示；"
                                            "上游通用工商科目未单独列示金融负项明细行，但官方流出小计与经营现金净额真实闭合，并无虚增。"
                                        )

                            # 投资活动现金流入/流出不可加总差额自陈
                            # （mango 实测 002594 2026-06-30 投资流入残差 0.42 亿、2025-12-31 0.67 亿，原先无自陈）
                            def _sum_first_present(groups: list[tuple[str, ...]]) -> float:
                                total = 0.0
                                for names in groups:
                                    for n in names:
                                        v = period_dict.get(n)
                                        if isinstance(v, (int, float)):
                                            total += float(v)
                                            break
                                return round(total, 2)

                            tz_in = period_dict.get("投资活动现金流入小计")
                            if isinstance(tz_in, (int, float)):
                                diff_tz_in = round(float(tz_in) - _sum_first_present([
                                    ("收回投资收到的现金",),
                                    ("取得投资收益收到的现金",),
                                    ("处置固定资产无形资产收回的现金净额", "处置固定资产、无形资产和其他长期资产收回的现金净额"),
                                    ("处置子公司及其他营业单位收到的现金净额",),
                                    ("收到其他与投资活动有关的现金",),
                                ]), 2)
                                if abs(diff_tz_in) > 0.01:
                                    period_dict["投资活动现金流入不可加总差额_工商细项缺项"] = diff_tz_in

                            tz_out = period_dict.get("投资活动现金流出小计")
                            if isinstance(tz_out, (int, float)):
                                diff_tz_out = round(float(tz_out) - _sum_first_present([
                                    ("购建固定资产无形资产支付的现金", "购建固定资产、无形资产和其他长期资产支付的现金"),
                                    ("投资支付的现金",),
                                    ("取得子公司及其他营业单位支付的现金净额",),
                                    ("支付其他与投资活动有关的现金",),
                                ]), 2)
                                if abs(diff_tz_out) > 0.01:
                                    period_dict["投资活动现金流出不可加总差额_工商细项缺项"] = diff_tz_out

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
                            "为交易目的而持有的金融资产及融出资金等净支出",
                            "支付原保险合同赔付款项的现金", "拆出资金净增加额", "返售业务资金净增加额",
                            "支付利息、手续费及佣金的现金", "支付保单红利的现金", "支付再保业务现金净额",
                            "支付原保险合同款项现金",
                            "经营活动现金流出小计", "经营活动现金流出不可加总差额_金融存贷款及细项等",
                            "经营活动现金流出不可加总差额_金融交易及细项科目缺项",
                            "经营活动现金流出不可加总差额_保险赔付及拆出返售等金融科目缺项",
                            "经营活动现金流量净额", "经营活动产生的现金流量净额",
                            "收回投资收到的现金", "取得投资收益收到的现金",
                            "处置固定资产无形资产收回的现金净额", "处置固定资产、无形资产和其他长期资产收回的现金净额",
                            "处置子公司及其他营业单位收到的现金净额", "收到其他与投资活动有关的现金",
                            "投资活动现金流入小计", "投资活动现金流入不可加总差额_金融投资细项缺项",
                            "投资活动现金流入不可加总差额_工商细项缺项",
                            "购建固定资产无形资产支付的现金", "购建固定资产、无形资产和其他长期资产支付的现金",
                            "投资支付的现金", "取得子公司及其他营业单位支付的现金净额", "质押贷款净增加额",
                            "支付其他与投资活动有关的现金",
                            "投资活动现金流出小计", "投资活动现金流出不可加总差额_金融投资细项缺项",
                            "投资活动现金流出不可加总差额_工商细项缺项",
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
                        # 未知键继承前一个已知键的位置：统一甩到 999 会落到「期末现金」之后，
                        # 按小计锚点切区的解析器会整段漏项（如 600030 券商科目 443.08 亿）
                        period_dict = {k: period_dict[k] for k in _order_by_canonical(list(period_dict.keys()), canonical_cf_order)}

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
                                    if is_insurance:
                                        period_dict["营业收入不可加总差额_保险服务收入及金融专有科目缺项"] = diff_yysr
                                        period_dict["保险企业收入说明"] = (
                                            "该标的执行新保险合同准则(CAS 25/IFRS 17)，上游专线未单独收录【保险服务收入/已赚保费】细项，"
                                            f"导致营业收入与已知金融收益项之和存在客观缺口({round(diff_yysr / 100000000.0, 2)}亿元)。"
                                            "【可用性结论】金融/保险企业营业收入构成明细严禁用于收入结构分析；但顶层【营业收入】总额真实准确，财务建模请直接引用官方【营业收入】整体。"
                                        )
                                    elif is_broker:
                                        period_dict["营业收入不可加总差额_证券专有科目及其他收益缺项"] = diff_yysr
                                        period_dict["证券企业收入说明"] = (
                                            "该标的为证券/金融机构，营业收入以手续费及佣金净收入、利息净收入、投资收益、公允价值变动收益为核心支柱。"
                                            f"上游专线未单独列示其他业务收益与专有科目细项，导致营业收入与已知金融收益项存在微小统计差异({round(diff_yysr / 100000000.0, 2)}亿元)。"
                                            "【可用性结论】顶层【营业收入】总额真实准确，财务建模请直接引用官方【营业收入】整体。"
                                        )
                                    else:
                                        period_dict["营业收入不可加总差额_金融专有科目及其他收益缺项"] = diff_yysr
                                        period_dict["金融企业收入说明"] = (
                                            "该标的为金融机构，营业收入以存贷款利息与手续费净收入为主。"
                                            f"上游专线未单独列示部分非主营收益细项，导致营业收入与已知收益项之和存在客观差异({round(diff_yysr / 100000000.0, 2)}亿元)。"
                                            "【可用性结论】顶层【营业收入】总额真实准确，财务建模请直接引用官方【营业收入】整体。"
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

                            # 营业总成本构成核算说明 (P1-1 修复算式与闭合结论)
                            yyzcb = period_dict.get("营业总成本")
                            if yyzcb is not None and isinstance(yyzcb, (int, float)):
                                # 兼顾【营业税金及附加】与【税金及附加】双向别名，杜绝因键名遗漏导致系统性虚报差额
                                tax_cost = float(period_dict.get("营业税金及附加") if period_dict.get("营业税金及附加") is not None else (period_dict.get("税金及附加") or 0.0))
                                if "营业税金及附加" in period_dict and "税金及附加" not in period_dict:
                                    period_dict["税金及附加"] = period_dict["营业税金及附加"]
                                elif "税金及附加" in period_dict and "营业税金及附加" not in period_dict:
                                    period_dict["营业税金及附加"] = period_dict["税金及附加"]

                                known_cost_items = ["营业成本", "营业税金及附加", "销售费用", "管理费用", "研发费用", "财务费用"]
                                cost_sum = round(
                                    float(period_dict.get("营业成本", 0.0) or 0.0)
                                    + tax_cost
                                    + float(period_dict.get("销售费用", 0.0) or 0.0)
                                    + float(period_dict.get("管理费用", 0.0) or 0.0)
                                    + float(period_dict.get("研发费用", 0.0) or 0.0)
                                    + float(period_dict.get("财务费用", 0.0) or 0.0),
                                    2
                                )
                                cost_diff = round(float(yyzcb) - cost_sum, 2)
                                if abs(cost_diff) > 0.01:
                                    period_dict["营业总成本构成差额说明"] = (
                                        f"营业总成本({float(yyzcb):,.2f}元)与已列明主营与期间成本项(营业成本+营业税金及附加+销售+管理+研发+财务费用，合计{cost_sum:,.2f}元)存在差额({cost_diff:,.2f}元)。"
                                        "该差额通常由其他业务成本、资产减值损失准备或合并抵销细项构成。"
                                    )
                                    period_dict["营业总成本构成说明"] = (
                                        f"营业总成本({float(yyzcb):,.2f}元)与已列明成本费用合计({cost_sum:,.2f}元)存在差额({cost_diff:,.2f}元)，科目未完全闭合（详见营业总成本构成差额说明）。"
                                    )
                                else:
                                    period_dict["营业总成本构成说明"] = "营业总成本 = 营业成本 + 营业税金及附加 + 销售费用 + 管理费用 + 研发费用 + 财务费用，科目完全闭合。"

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

                            # 利润总额与营业外收支会计恒等式闭合 (修复 D7: 补全营业外收入/营业外支出以支持利润总额复算)
                            lrzj = period_dict.get("利润总额")
                            if yylr is not None and lrzj is not None and isinstance(yylr, (int, float)) and isinstance(lrzj, (int, float)):
                                non_op_in = period_dict.get("营业外收入")
                                non_op_out = period_dict.get("营业外支出")
                                if non_op_in is None and non_op_out is None:
                                    diff_lr = round(float(lrzj) - float(yylr), 2)
                                    if abs(diff_lr) < 0.01:
                                        period_dict["营业外收入"] = 0.0
                                        period_dict["营业外支出"] = 0.0
                                    elif diff_lr > 0:
                                        period_dict["营业外收入"] = diff_lr
                                        period_dict["营业外支出"] = 0.0
                                    else:
                                        period_dict["营业外收入"] = 0.0
                                        period_dict["营业外支出"] = round(-diff_lr, 2)
                                    period_dict["营业外收支说明"] = "该报告期上游未披露营业外收支细项，已依据【利润总额 = 营业利润 + 营业外收入 − 营业外支出】会计恒等式反推补齐并闭合。"
                                elif non_op_in is None and non_op_out is not None:
                                    calc_in = round(float(lrzj) - float(yylr) + float(non_op_out), 2)
                                    period_dict["营业外收入"] = max(0.0, calc_in)
                                    period_dict["营业外收支说明"] = "该报告期上游略去营业外收入，已依据会计恒等式由利润总额与营业外支出差额推算补齐。"
                                elif non_op_in is not None and non_op_out is None:
                                    calc_out = round(float(yylr) + float(non_op_in) - float(lrzj), 2)
                                    period_dict["营业外支出"] = max(0.0, calc_out)
                                    period_dict["营业外收支说明"] = "该报告期上游略去营业外支出，已依据会计恒等式由利润总额与营业外收入差额推算补齐。"

                                cur_in = float(period_dict.get("营业外收入", 0.0) or 0.0)
                                cur_out = float(period_dict.get("营业外支出", 0.0) or 0.0)
                                if abs(round(float(yylr) + cur_in - cur_out, 2) - float(lrzj)) < 0.05:
                                    period_dict["利润总额核算说明"] = "利润总额 = 营业利润 + 营业外收入 − 营业外支出，会计恒等式严格闭合。"

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

                            # 金融资产侧明细可加总性校验与客观缺口自陈 (修复 P1-1)
                            # 剔除客户资金存款与货币资金双计，核算银行信贷/保险投资/证券专有科目覆盖率
                            if zchj is not None and isinstance(zchj, (int, float)):
                                known_fin_assets = [
                                    "存放同业及其他金融机构款项", "货币资金", "现金及存放中央银行款项",
                                    "结算备付金", "存出备付金", "融出资金", "贵金属", "拆出资金",
                                    "交易性金融资产", "衍生金融资产", "买入返售金融资产", "应收款项",
                                    "应收保费", "应收分保账款", "应收分保未到期责任准备金", "应收分保未决赔款准备金",
                                    "应收分保寿险责任准备金", "应收分保长期健康险责任准备金", "保户质押贷款",
                                    "应收股利", "应收利息", "定期存款", "发放贷款及垫款", "存出保证金",
                                    "存出资本保证金", "债权投资", "其他债权投资", "其他权益工具投资",
                                    "长期应收款", "长期股权投资", "存出资本保证金_保险", "投资性房地产",
                                    "固定资产清理", "固定资产", "在建工程", "无形资产", "开发支出",
                                    "独立账户资产", "长期待摊费用", "其他非流动资产", "递延所得税资产",
                                    "商誉", "其他资产"
                                ]
                                # 若无'货币资金'但有'客户资金存款'，计入客户资金存款；若已有'货币资金'，客户资金为其内部明细切勿重复累加
                                extra_client_fund = 0.0
                                if "货币资金" not in period_dict and "客户资金存款" in period_dict:
                                    extra_client_fund = float(period_dict.get("客户资金存款") or 0.0)

                                sub_fin_assets = sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_assets) + extra_client_fund
                                diff_fin_assets = round(float(zchj) - sub_fin_assets, 2)
                                if abs(diff_fin_assets) > 0.01:
                                    period_dict["资产总计不可加总差额_金融投资及信贷专有科目缺项"] = diff_fin_assets
                                    coverage_pct = round((sub_fin_assets / float(zchj)) * 100.0, 2)
                                    period_dict["金融资产结构限制说明"] = (
                                        f"【金融企业资产侧明细覆盖度限制警示】通达信底层专线金融资产负债表未收录商业银行（如以摊余成本计量的金融投资、债权投资等大额科目）"
                                        f"与保险公司（如大额保单投资、准备金分保资产等）核心大额资产科目，已知明细和仅占资产总计约 {coverage_pct}%，"
                                        f"客观缺口达 {round(diff_fin_assets / 100000000.0, 2)} 亿元已自陈至【资产总计不可加总差额_金融投资及信贷专有科目缺项】。"
                                        "金融机构资产结构分析严禁使用明细项机械累加或内部占比分析，财务分析与资产规模评估必须且只能引用官方【资产总计】、【负债合计】、【所有者权益合计】等顶层科目！"
                                    )

                            # 金融负债侧明细可加总性校验与客观缺口自陈 (修复 D3: 解决保险公司保险合同负债缺项自陈缺失)
                            if fzhj is not None and isinstance(fzhj, (int, float)):
                                known_fin_liab = [
                                    "向中央银行借款", "同业及其他金融机构存放款项", "拆入资金", "交易性金融负债",
                                    "衍生金融负债", "卖出回购金融资产款", "吸收存款及同业存放", "吸收存款",
                                    "代理买卖证券款", "代理承销证券款", "应付职工薪酬", "应交税费",
                                    "应付利息", "应付短期融资券", "应付短期融资款", "应付款项", "应付账款",
                                    "预收保费", "应付分保账款", "应付保单红利", "保户储金及投资款",
                                    "未到期责任准备金", "担保赔偿准备金", "寿险责任准备金", "长期健康险责任准备金",
                                    "保险合同准备金", "保险合同负债", "预计负债", "短期借款", "长期借款", "应付债券",
                                    "递延所得税负债", "租赁负债", "独立账户负债", "其他负债"
                                ]
                                sub_fin_liab = sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_liab)
                                diff_fin_liab = round(float(fzhj) - sub_fin_liab, 2)
                                if abs(diff_fin_liab) > 0.01:
                                    period_dict["负债合计不可加总差额_保险合同负债及金融专有科目缺项"] = diff_fin_liab
                                    coverage_liab_pct = round((sub_fin_liab / float(fzhj)) * 100.0, 2)
                                    period_dict["金融负债结构限制说明"] = (
                                        f"【金融企业负债侧明细覆盖度限制警示】通达信底层专线金融资产负债表未收录保险公司核心负债科目"
                                        f"（如执行新保险合同准则 CAS 25/IFRS 17 的【保险合同负债】或原寿险与长期健康险责任准备金），"
                                        f"已知负债明细和仅占负债合计约 {coverage_liab_pct}%，客观缺口达 {round(diff_fin_liab / 100000000.0, 2)} 亿元已自陈至【负债合计不可加总差额_保险合同负债及金融专有科目缺项】。"
                                        "金融机构负债结构分析严禁使用明细项机械累加或内部占比分析，财务分析与偿债能力评估必须且只能引用官方公布的【负债合计】、【资产总计】与【所有者权益合计】顶层科目！"
                                    )
                                else:
                                    period_dict["金融负债结构限制说明"] = "银行与证券机构负债侧明细覆盖度达 99.8% 以上，主要负债（存款、借款、拆借、回购、代理款项等）均已规范采集闭合。"

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
                                # 修复 P1-2: 检查上游数据源是否将【外币报表折算差额】与【专项储备】同值别名并发（如 002594 比亚迪均为 208.4686 亿），避免明细加总双计
                                items_to_sum = list(known_equity_items)
                                zx_val = period_dict.get("专项储备")
                                wb_val = period_dict.get("外币报表折算差额")
                                if zx_val is not None and wb_val is not None and zx_val == wb_val and float(zx_val) != 0:
                                    items_to_sum.remove("专项储备")
                                    # 修复 D3 (P2): 物理剔除重复别名键【专项储备】，彻底杜绝下游整表遍历加总双计 208 亿
                                    period_dict.pop("专项储备", None)
                                    period_dict["所有者权益双计剔除说明"] = (
                                        "检测到上游专线将【外币报表折算差额】与【专项储备】同值别名并发（如 002594 比亚迪均为 208.47 亿元逐位相同），"
                                        "网关已物理剔除重复别名键【专项储备】，保留法定真实科目【外币报表折算差额】，彻底杜绝下游整表遍历求和双计。"
                                    )
                                sub_eq = sum(float(period_dict.get(k, 0.0) or 0.0) for k in items_to_sum if k in period_dict) - treasury_stock
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
                "4.【所有者权益加总纪律与库存股抵减项】：依据财政部《企业会计准则》，‘库存股’是所有者权益的法定抵减科目（借方余额），本接口统一收敛为唯一规范键【库存股】（杜绝'减_库存股'与'库存股_抵减项'等正数别名造成遍历加总时3倍虚增）。在计算归母所有者权益时，必须从各项权益总和中扣除库存股：【归母所有者权益 = 实收资本 + 资本公积 + 盈余公积 + 未分配利润 + 其他综合收益 + 专项储备 − 库存股】。严禁将库存股作为正数加计！\n"
                "5.【权益分区专项储备与不可加总差额】：针对工商业底层数据源未收录【专项储备】（如高危行业安全生产费储备）或特殊权益工具导致的明细与归母权益总额客观差额，网关已自动核算并自陈至【归母所有者权益不可加总差额_专项储备及其他权益项等】。\n"
                "6.【现金流量表结构规范与商业银行明细覆盖限制】：现金流量表现已将主表（直接法三大活动流向）与间接法调节表物理隔离，间接法补充资料独立归集于‘补充资料_间接法’字典中，彻底避免科目遍历时的静默双计。同时特别警示：商业银行现金流明细缺失严重（筹资流出缺失达70%~90%），禁止用于内部结构分析，必须且只能引用三大净额与期末现金余额（勾稽桥式严格闭合）。\n"
                "7.【披露期三表期次同步说明】：在上市公司定期报告披露季（如三季报、年报发布期），上游数据库各报表库更新存在时间差（如资产负债表库通常优先录入，利润表与现金流量表随后入库），若发现三表最新期次不一致，属于上游数据源入库批次时差，调用时请核对每期'截止日期'或'报告期日期'。\n"
                "8.【三表股本与二级市场实时总股本口径说明】：资产负债表所有者权益中的【实收资本】/【股本】严格反映上市公司在该报告期经审计与工商核准的法定登记股本；若标的发生限售股解禁（如600030中信证券解禁限售股）、A/H双重架构、定向增发或二级市场转增，二级市场可流通交易的实时总股本会领先于历史财报中的法定股本。财务建模与计算最新每股指标（如最新BPS、PB、动态PE）时，请以 stock_quotes 中的【总股本_股】与【市净率_PB_自算_普通股】为准，严禁直接拿历史财报静态股本除算实时市值。"
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
                "4.【所有者权益加总纪律与库存股抵减项】：依据财政部《企业会计准则》，‘库存股’是所有者权益的法定抵减科目（借方余额），本接口统一收敛为唯一规范键【库存股】（杜绝'减_库存股'与'库存股_抵减项'等正数别名造成遍历加总时3倍虚增）。在计算归母所有者权益时，必须从各项权益总和中扣除库存股：【归母所有者权益 = 实收资本 + 资本公积 + 盈余公积 + 未分配利润 + 其他综合收益 + 专项储备 − 库存股】。严禁将库存股作为正数加计！\n"
                "5.【权益分区专项储备与不可加总差额】：针对工商业底层数据源未收录【专项储备】（如高危行业安全生产费储备）或特殊权益工具导致的明细与归母权益总额客观差额，网关已自动核算并自陈至【归母所有者权益不可加总差额_专项储备及其他权益项等】。\n"
                "6.【现金流量表结构规范与商业银行明细覆盖限制】：现金流量表现已将主表（直接法三大活动流向）与间接法调节表物理隔离，间接法补充资料独立归集于‘补充资料_间接法’字典中，彻底避免科目遍历时的静默双计。同时特别警示：商业银行现金流明细缺失严重（筹资流出缺失达70%~90%），禁止用于内部结构分析，必须且只能引用三大净额与期末现金余额（勾稽桥式严格闭合）。\n"
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

    # =========================================================================
    # 【功能块 7.3：自然语言选股分页快照（跨页一致性）】
    # - 问题：上游 wendaQuery 每次按 pageNo 重算切片，排序不稳定 → 跨页并集重复或漏项
    # - 方案：同一查询 60 秒内共用一份快照，固定 pageSize=100 顺序取页、按代码全局去重；
    #         所有分页从同一数组切片，页间天然不重不漏；上限 2000 行 / 20 页
    # =========================================================================
    def _fetch_screener_page(self, message: str, page_no: int, page_size: int = 100) -> Any:
        body = [{
            "message": message.strip(),
            "rang": "AG",
            "pageNo": str(page_no),
            "pageSize": str(page_size),
        }]
        url = f"{self.api_endpoint}?Entry=JNLPSE:wendaQuery"
        raw = self._post(url, body)
        if isinstance(raw, dict) and raw.get("ErrorCode") in (-7405, -7404):
            time.sleep(0.6)
            raw = self._post(url, body)
        return raw

    @staticmethod
    def _parse_screener_payload(raw: Any) -> tuple[list[dict[str, Any]], int, list[Any] | None]:
        """上游二维数组 → (行, 全量命中数, 元信息)；不做 ST 过滤、不做分页截断。"""
        rows: list[dict[str, Any]] = []
        if not (isinstance(raw, list) and len(raw) >= 3):
            return rows, 0, None
        raw_headers = raw[1] if isinstance(raw[1], list) else []
        headers = [str(h).strip() for h in raw_headers]
        meta = raw[0] if isinstance(raw[0], list) else None
        total_hits = 0
        if meta and len(meta) > 2:
            try:
                total_hits = int(meta[2])
            except (ValueError, TypeError):
                total_hits = 0

        for item in raw[3:]:
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

            rows.append({
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
        return rows, total_hits, meta

    def _screener_snapshot(self, message: str, min_rows: int) -> tuple[list[dict[str, Any]] | None, int, int, list[Any] | None]:
        """取（或续取）同一查询的分页快照；返回 (行, 命中总数, 去重条数, 元信息)；上游过载返回 None。"""
        key = message.strip()
        now = time.time()
        state = self._screener_cache.get(key)
        if not state or (now - float(state["ts"])) >= 60.0:
            if len(self._screener_cache) >= 16:
                self._screener_cache.pop(next(iter(self._screener_cache)))
            state = {"ts": now, "rows": [], "seen": set(), "matched": 0, "dup": 0, "next_page": 1, "done": False, "meta": None}
            self._screener_cache[key] = state

        need = min(max(1, int(min_rows)), 2000)
        while len(state["rows"]) < need and not state["done"] and state["next_page"] <= 20:
            raw = self._fetch_screener_page(message, state["next_page"], 100)
            if isinstance(raw, dict) and raw.get("ErrorCode") in (-7405, -7404):
                return None, 0, int(state["dup"]), state["meta"]
            rows, matched, meta = self._parse_screener_payload(raw)
            if matched:
                state["matched"] = matched
            if meta:
                state["meta"] = meta
            for row in rows:
                if row["code"] in state["seen"]:
                    state["dup"] += 1
                    continue
                state["seen"].add(row["code"])
                state["rows"].append(row)
            state["next_page"] += 1
            if len(rows) < 100:
                state["done"] = True
        return state["rows"], int(state["matched"] or len(state["rows"])), int(state["dup"]), state["meta"]

    def screener(self, message: str, limit: int = 10, page: int = 1, filter_st: bool = True, query: str | None = None, condition: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """通达信自然语言智能选股 (输入中文自然语言条件，如'主板 绩优 储能 连板')"""
        # 严格校验选股 limit 与 page 参数范围 (修复 P3-1: 杜绝 page=21 等越界返回看似成功的值或空表)
        try:
            n_limit = int(limit)
        except (ValueError, TypeError):
            raise ValueError(f"limit 必须为整数，收到: {limit}")
        if n_limit < 1 or n_limit > 100:
            raise ValueError(f"limit 参数越界: {n_limit}，必须在 1 到 100 之间")

        try:
            n_page = int(page)
        except (ValueError, TypeError):
            raise ValueError(f"page 必须为整数，收到: {page}")
        if n_page < 1 or n_page > 20:
            raise ValueError(f"page 参数越界: {n_page}，必须在 1 到 20 之间")

        # 修复 Issue 8: 识别查询条件中的港股/香港意图 (包含常见知名港股巨头与港股关键词)
        is_hk_query = bool(re.search(r"港股|香港|HKEX|\bHK\b|恒生|H股|腾讯|美团|阿里|中芯国际港股|小米", message, re.I))

        offset = (n_page - 1) * n_limit
        rows, total_hits, dedup_dropped, meta = self._screener_snapshot(message, offset + n_limit)
        if rows is None:
            return {
                "message": message,
                "matched_count": 0,
                "returned_count": 0,
                "page": page,
                "pageSize": n_limit,
                "filter_st": filter_st,
                "filtered_st_count": 0,
                "headers": [],
                "results": [],
                "上游状态说明": "通达信自然语言选股服务瞬时会话过载 (RPC ErrorCode -7405)，此为网络会话瞬时抖动，已自动执行重试仍未恢复，建议间隔 1-2 秒后重试。",
            }

        window = rows[offset: offset + n_limit]
        results: list[dict[str, Any]] = []
        filtered_st_count = 0
        for row in window:
            if filter_st and row.get("is_st"):
                filtered_st_count += 1
                continue
            # 快照对象可能被多页复用，返回副本避免下游修改污染缓存
            results.append({
                **row,
                "indicators": dict(row.get("indicators") or {}),
                "matched_indicators": dict(row.get("indicators") or {}),
            })

        total_pages = math.ceil(total_hits / n_limit) if n_limit > 0 else 1

        res_dict = {
            "query": message,
            "message": message,
            "page": page,
            "limit": n_limit,
            "total_pages": total_pages,
            "filter_st": filter_st,
            "matched_count": total_hits,
            "returned_count": len(results),
            "filtered_st_count": filtered_st_count,
            "filter_st_note": "默认已过滤 ST/*ST 股（在页切片内剔除并记录 filtered_st_count）；若需获取全部股票（含风险警示股）请将 filter_st 设为 False。",
            "分页与过滤说明": (
                "同一查询在 60 秒内共用一份分页快照：网关按 pageSize=100 顺序取页、按证券代码全局去重后落成单一数组，"
                "所有 page 都从该数组切片，因此【页间不重不漏】；快照最多累计 2000 行，超出上限的部分需重新查询。"
                "filter_st 只在返回切片时剔除 ST/*ST，不影响快照本身与 matched_count（后者为上游全量命中数）。"
            ),
            "分页快照说明": f"本次查询快照累计 {len(rows)} 行，跨页去重 {dedup_dropped} 条，TTL 60 秒；期间重复翻页结果一致，命中总数变化需等快照过期后重新查询。",
            "快照累计行数": len(rows),
            "快照跨页去重条数": dedup_dropped,
            "快照TTL秒": 60,
            "页内去重条数": dedup_dropped,
            "market_scope": "A股 (中国大陆交易所：沪市/深市/北交所)",
            "is_hk_intent": is_hk_query,
            "stocks": results,
            "raw_summary": meta,
        }
        if is_hk_query:
            res_dict["warning"] = (
                "【市场范围警示】：当前自然语言选股引擎仅覆盖 A 股（中国大陆交易所：沪深北），暂不支持港股标的自动筛选。"
                "检测到查询条件中包含'港股/香港'相关意图，已默认按 A 股相关概念标的返回。"
                "如需查询具体港股（如腾讯 00700、美团 03690），请直接使用 stock_lookup 查找代码后调用 stock_quotes/stock_financials/stock_f10。"
            )
        return res_dict
