"""通达信金融数据专线与多源高可用金融服务适配器 (Finance Gateway)
提供 A 股/港股/美股/ETF 的实体检索、实时盘口、多周期K线（含内置技术指标计算）、
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
import socket
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


# =========================================================================
# 【功能块 0：安全 HTTP 传输层与重定向拦截处理器导入】
# - 输入：mcp_gateway.core.network 模块
# - 功能：导入 NoRedirectHandler 与 _SECURE_OPENER，保持 100% 向后兼容
# - 输出：NoRedirectHandler, _SECURE_OPENER 在当前命名空间可用
# =========================================================================
from .core.network import NoRedirectHandler, _SECURE_OPENER
from .engines.hk_stock import HKStockEngine
from .engines.a_stock import AStockEngine


# =========================================================================
# 【功能块 1：核心常量与字段映射表导入】
# - 输入：mcp_gateway.core.constants 模块
# - 功能：导入所有规范化中文字段映射字典（盘口/K线/F10各子表/三大财务报表等）
# - 输出：各常量在当前命名空间可用，100% 保持向后兼容
# =========================================================================
from .core.constants import *



# =========================================================================
# 【功能块 2：核心纯函数与市场工具导入】
# - 输入：mcp_gateway.core.indicators / market_utils 模块
# - 功能：导入技术指标纯算库、市场代码智能推导与交易上下文感知纯函数
# - 输出：calculate_indicators, infer_market_setcode, get_market_context 向后兼容可用
# =========================================================================
from .core.indicators import calculate_indicators
from .core.market_utils import get_market_context, infer_market_setcode


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
        self._entity_cache: dict[str, dict[str, Any] | None] = {}
        self.hk_engine = HKStockEngine(timeout=self.timeout, logger=self.logger)
        self.a_engine = AStockEngine(token=self.token, api_endpoint=self.api_endpoint, rag_endpoint=self.rag_endpoint, timeout=self.timeout, logger=self.logger, post_fn=lambda url, body: self._post(url, body))

    def _post(self, url: str, body: dict[str, Any] | list[Any]) -> Any:
        return self.a_engine._default_post(url, body)

    def _unused_old_post(self, url: str, body: dict[str, Any] | list[Any]) -> Any:
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
        if clean_code in self._entity_cache:
            return self._entity_cache[clean_code]
        res = self._do_resolve_exact_security_entity(clean_code)
        self._entity_cache[clean_code] = res
        return res

    def _do_resolve_exact_security_entity(self, clean_code: str) -> dict[str, Any] | None:
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
                        raw_name = parts[1].strip()
                        # 清理交易手数与股票类型后缀（如 "金斯瑞生物-一千" -> "金斯瑞生物", "-W", "-S", "-SW", "-B"）
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

    def lookup_stock(self, query: str, market_range: str = "AG") -> list[dict[str, Any]]:
        """AI RAG 语义代码检索 (支持 A股, 港股 HK-GP, 美股 MG-GP, 基金 JJ, 指数 ZS)"""
        raw_query = query.strip()
        # 清洗可能带有的交易所前缀或后缀（如 sh600519 -> 600519, 002698.sz -> 002698）
        clean_code = re.sub(r"^(sh|sz|bj|hk)", "", raw_query, flags=re.I)
        clean_code = re.sub(r"\.(sh|sz|bj|hk|ss)$", "", clean_code, flags=re.I)

        req_body = {"query": raw_query, "range": market_range}
        try:
            resp = self._post(self.rag_endpoint, req_body)
        except Exception:
            # 应对偶发 RAG 网络超时/握手抖动，若为 5-6 位代码且正主能独立反查，优雅降级至独立反查 (解决 P2-1)
            resp = {}
        entities: list[dict[str, Any]] = []
        if isinstance(resp, dict) and "retrieved_entities" in resp:
            raw_entities = resp["retrieved_entities"]
            if isinstance(raw_entities, list):
                entities = raw_entities

        # 智能全市场双重嗅探：仅当用户进行纯文本语义搜索（非特定长度纯数字）且默认搜 AG 返回为空时，才尝试 HK-GP
        if not entities and market_range == "AG" and not (clean_code.isdigit() and len(clean_code) in (5, 6)):
            try:
                hk_resp = self._post(self.rag_endpoint, {"query": raw_query, "range": "HK-GP"})
                if isinstance(hk_resp, dict) and "retrieved_entities" in hk_resp:
                    hk_entities = hk_resp["retrieved_entities"]
                    if isinstance(hk_entities, list) and hk_entities:
                        entities = hk_entities
            except Exception:
                pass

        # 纯数字代码正主保全与首位置顶逻辑（解决基金/债券等同名代码挤占或遗漏股票正主的问题）
        # 严格遵守市场范围约束：AG 范围仅置顶 6 位 A 股；HK/HK-GP 范围仅置顶 5 位港股 (修复 Issue 2)
        is_ag_range = market_range in ("AG", "A", "")
        is_hk_range = market_range in ("HK", "HK-GP")
        should_resolve = False
        exact_target = None
        if clean_code.isdigit():
            if len(clean_code) == 6 and (is_ag_range or market_range in ("all", "ALL")):
                should_resolve = True
            elif len(clean_code) == 5 and (is_hk_range or market_range in ("all", "ALL")):
                should_resolve = True

        if should_resolve:
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

        for ent in entities:
            if isinstance(ent, dict):
                e_code = ent.get("entity_code") or ent.get("code") or ""
                e_name = ent.get("entity_name") or ent.get("name") or ""
                e_setcode = ent.get("entity_setcode") if ent.get("entity_setcode") is not None else ent.get("setcode")
                ent["code"] = e_code
                ent["name"] = e_name
                ent["setcode"] = str(e_setcode) if e_setcode is not None else ""
                if "market" not in ent:
                    ent["market"] = "SZ" if str(e_setcode) == "0" else ("SH" if str(e_setcode) == "1" else ("BJ" if str(e_setcode) == "2" else ("HK" if str(e_setcode) == "31" else str(e_setcode))))

        # 严格遵守市场范围过滤：排除港股、美股与异市纯英文 ticker，保留境内6位代码证券 (解决 P3-1)
        if market_range in ("AG", "A"):
            valid_entities = []
            for e in entities:
                c_str = str(e.get("code") or "")
                m_str = str(e.get("market") or "").upper()
                s_str = str(e.get("setcode") or "")
                # 排除港股 (HK, 31, 48)、美股 (US, 105, 106, 107) 与纯英文美股 ticker (如 EGHT, SBUX, AIFC, JEM)
                if m_str in ("HK", "US") or s_str in ("31", "48", "105", "106", "107"):
                    continue
                if not (c_str.isdigit() and len(c_str) == 6):
                    continue
                valid_entities.append(e)

            # 若用户查询为 6 位纯数字且该代码在全市场不存在（如 610617、601617），过滤掉无公共前缀的无关 RAG 幻觉推荐 (修复 P3-1)
            if clean_code.isdigit() and len(clean_code) == 6 and not exact_target:
                filtered_candidates = []
                for e in valid_entities:
                    cand_code = str(e.get("code") or "")
                    common_prefix_len = 0
                    for c1, c2 in zip(clean_code, cand_code):
                        if c1 == c2:
                            common_prefix_len += 1
                        else:
                            break
                    if common_prefix_len >= 3:
                        filtered_candidates.append(e)
                valid_entities = filtered_candidates

            entities = valid_entities
        elif market_range in ("HK", "HK-GP"):
            valid_entities = []
            for e in entities:
                c_str = str(e.get("code") or "")
                m_str = str(e.get("market") or "").upper()
                s_str = str(e.get("setcode") or "")
                if m_str in ("SH", "SZ", "BJ", "US") or s_str in ("0", "1", "2", "105", "106", "107"):
                    continue
                if not (c_str.isdigit() and len(c_str) == 5):
                    continue
                valid_entities.append(e)
            entities = valid_entities

        return entities

    # =========================================================================
    # 【功能块：港股处理方法向后兼容委托桩 (转发至 HKStockEngine)】
    # =========================================================================
    def _get_hk_quotes(self, clean_code: str) -> dict[str, Any]:
        return self.hk_engine.get_quotes(clean_code)

    def _get_hk_kline(self, clean_code: str, period: str = "day", count: int = 30, adjust: str = "qfq", with_indicators: bool = True) -> dict[str, Any]:
        return self.hk_engine.get_kline(clean_code, period=period, count=count, adjust=adjust, with_indicators=with_indicators)

    def _parse_sina_hk_financials(self, clean_code: str, report_type: str = "income", limit_periods: int = 4) -> list[dict[str, Any]] | None:
        return self.hk_engine._parse_sina_hk_financials(clean_code, report_type=report_type, limit_periods=limit_periods)

    def _get_eastmoney_hk_financials(self, clean_code: str, report_type: str = "balance", limit_periods: int = 4) -> list[dict[str, Any]]:
        return self.hk_engine._get_eastmoney_hk_financials(clean_code, report_type=report_type, limit_periods=limit_periods)

    def _get_hk_financials(self, symbol: str, report_type: str = "income", period_type: str = "report", limit_periods: int = 4) -> dict[str, Any]:
        return self.hk_engine.get_financials(symbol, report_type=report_type, period_type=period_type, limit_periods=limit_periods)

    def _infer_hk_hang_seng_industry(self, main_biz: str, corp_name: str) -> str:
        return self.hk_engine._infer_hk_hang_seng_industry(main_biz, corp_name)

    def _get_sina_hk_company_info(self, clean_code: str) -> dict[str, Any] | None:
        return self.hk_engine._get_sina_hk_company_info(clean_code)

    def _get_sina_hk_table(self, clean_code: str, path: str, table_idx: int = 0) -> list[dict[str, Any]]:
        return self.hk_engine._get_sina_hk_table(clean_code, path=path, table_idx=table_idx)

    def _get_ashare_dividends(self, clean_code: str) -> list[dict[str, Any]]:
        return self.a_engine._get_ashare_dividends(clean_code)

    def _get_ashare_lhb(self, clean_code: str) -> list[dict[str, Any]]:
        return self.a_engine._get_ashare_lhb(clean_code)

    def _get_ashare_repurchases(self, clean_code: str) -> dict[str, Any]:
        return self.a_engine._get_ashare_repurchases(clean_code)


    # =========================================================================
    # 【功能块：港股 F10 委托桩 (转发至 HKStockEngine)】
    # =========================================================================
    def _get_hk_f10(self, clean_code: str, category: str = 'summary') -> dict[str, Any]:
        return self.hk_engine.get_f10(clean_code, category=category)

    # =========================================================================
    # 【功能块：A股与港股双引擎统一路由派发】
    # =========================================================================
    def get_quotes(self, symbol: str, setcode: str | None = None) -> dict[str, Any]:
        clean_code = re.sub(r'[^\d]', '', symbol)
        if not clean_code:
            raise ValueError(f"无效的证券代码 '{symbol}'，证券标的代码必须包含有效数字编号（如 A股'600519'、港股'00700'）。若仅知道公司名称，请先调用 stock_lookup 工具查找对应代码。")
        is_hk = (
            (setcode is not None and str(setcode).upper() in ('31', '116', 'HK'))
            or symbol.strip().upper().startswith('HK')
            or len(clean_code) == 5
        )
        if is_hk:
            return self.hk_engine.get_quotes(clean_code)
        return self.a_engine.get_quotes(symbol=clean_code, setcode=setcode)

    def get_kline(self, symbol: str, period: str = 'day', count: int = 30, adjust: str = 'qfq', setcode: str | None = None, with_indicators: bool = True) -> dict[str, Any]:
        clean_code = re.sub(r'[^\d]', '', symbol)
        if not clean_code:
            raise ValueError(f"无效的证券代码 '{symbol}'，证券标的代码必须包含有效数字编号（如 A股'600519'、港股'00700'）。若仅知道公司名称，请先调用 stock_lookup 工具查找对应代码。")
        is_hk = (
            (setcode is not None and str(setcode).upper() in ('31', '116', 'HK'))
            or symbol.strip().upper().startswith('HK')
            or len(clean_code) == 5
        )
        if is_hk:
            return self.hk_engine.get_kline(clean_code, period=period, count=count, adjust=adjust, with_indicators=with_indicators)
        return self.a_engine.get_kline(clean_code, period=period, count=count, adjust=adjust, setcode=setcode, with_indicators=with_indicators)

    def get_financial_statements(self, symbol: str, report_type: str = 'balance', period_type: str = 'report', limit_periods: int = 4) -> dict[str, Any]:
        clean_code = re.sub(r'[^\d]', '', symbol)
        if infer_market_setcode(symbol) == '31' or len(clean_code) == 5:
            norm_code = clean_code.zfill(5)
            return self.hk_engine.get_financials(norm_code, report_type=report_type, period_type=period_type, limit_periods=limit_periods)
        return self.a_engine.get_financials(symbol=clean_code, report_type=report_type, period_type=period_type, limit_periods=limit_periods)

    def get_f10(self, symbol: str, category: str = 'concepts') -> dict[str, Any]:
        clean_code = re.sub(r'[^\d]', '', symbol)
        if infer_market_setcode(symbol) == '31' or len(clean_code) == 5:
            norm_code = clean_code.zfill(5)
            return self.hk_engine.get_f10(norm_code, category=category)
        return self.a_engine.get_f10(symbol=clean_code, category=category)

    def screener(self, message: str, limit: int = 10, page: int = 1, filter_st: bool = True, query: str | None = None, condition: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.a_engine.screener(message=message, limit=limit, page=page, filter_st=filter_st, query=query, condition=condition, **kwargs)



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
    def _normalize_time_str(self, raw_time: str) -> str:
        """清洗与规范化时间字符串为统一的标准 ISO 格式 (YYYY-MM-DD HH:MM:SS)
        解决新浪/东财接口混用'4小时前'、'2小时前'、'昨天 15:30'相对时间串的缺陷 (修复 B7)
        """
        if not raw_time or not isinstance(raw_time, str):
            return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        s = raw_time.strip()
        now = datetime.datetime.now()

        # 1. 'X小时前'
        m_h = re.search(r"(\d+)\s*小时前", s)
        if m_h:
            dt = now - datetime.timedelta(hours=int(m_h.group(1)))
            return dt.strftime("%Y-%m-%d %H:%M:%S")

        # 2. 'X分钟前'
        m_m = re.search(r"(\d+)\s*分钟前", s)
        if m_m:
            dt = now - datetime.timedelta(minutes=int(m_m.group(1)))
            return dt.strftime("%Y-%m-%d %H:%M:%S")

        # 3. '刚刚'
        if "刚刚" in s:
            return now.strftime("%Y-%m-%d %H:%M:%S")

        # 4. '昨天 HH:MM' / '前天 HH:MM'
        m_yt = re.search(r"昨天\s*(\d{1,2}:\d{2}(?::\d{2})?)", s)
        if m_yt:
            t_str = m_yt.group(1)
            if len(t_str.split(":")) == 2:
                t_str += ":00"
            y_day = now - datetime.timedelta(days=1)
            return f"{y_day.strftime('%Y-%m-%d')} {t_str}"

        m_bf = re.search(r"前天\s*(\d{1,2}:\d{2}(?::\d{2})?)", s)
        if m_bf:
            t_str = m_bf.group(1)
            if len(t_str.split(":")) == 2:
                t_str += ":00"
            bf_day = now - datetime.timedelta(days=2)
            return f"{bf_day.strftime('%Y-%m-%d')} {t_str}"

        # 5. 'MM-DD HH:MM' (缺少年份)
        m_md = re.match(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}:\d{2}(?::\d{2})?)$", s)
        if m_md:
            mon, day, tim = m_md.group(1), m_md.group(2), m_md.group(3)
            if len(tim.split(":")) == 2:
                tim += ":00"
            return f"{now.year}-{int(mon):02d}-{int(day):02d} {tim}"

        # 6. 标准时间截取（如 '2026-10-22 08:30:00.000' -> '2026-10-22 08:30:00'）
        if len(s) >= 19 and re.match(r"^\d{4}-\d{2}-\d{2}", s):
            return s[:19]
        if len(s) == 10 and re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return f"{s} 00:00:00"

        return s

    def get_news(self, symbol: str, limit: int = 5, entity_name: str | None = None) -> list[dict[str, Any]]:
        raw_query = symbol.strip()
        clean_code = re.sub(r"[^\d]", "", raw_query)
        req_limit = max(1, min(limit, 10))

        # 智能确定检索词与标的名称组合（确保精准命中正主个股，杜绝泛化模糊数字匹配噪声）
        if entity_name:
            search_term = f"{entity_name} {clean_code}" if clean_code else entity_name
        elif infer_market_setcode(raw_query) == "31" or (len(clean_code) == 5 and not raw_query.startswith(("60", "68"))):
            norm_code = clean_code.zfill(5)
            try:
                hk_req = Request(f"http://qt.gtimg.cn/q=r_hk{norm_code}", headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(hk_req, timeout=3) as hk_resp:
                    hk_raw = hk_resp.read().decode("gbk", errors="replace")
                    hk_parts = hk_raw.split("~")
                    if len(hk_parts) > 1 and hk_parts[1].strip():
                        entity_name = hk_parts[1].strip()
                        search_term = f"{entity_name} {norm_code}"
                    else:
                        search_term = f"港股 {norm_code}"
            except Exception:
                search_term = f"港股 {norm_code}"
        else:
            search_term = clean_code if clean_code else raw_query

        lottery_noise = (
            "双色球", "大乐透", "福彩", "体彩", "开奖号码", "杀号", "预测奖号",
            "排列三", "排列四", "排列五", "七星彩", "六合彩", "中奖", "彩票", "幸运飞艇",
        )

        def _is_relevant(title_text: str, content_text: str) -> bool:
            comb = (title_text + " " + content_text).lower()
            if clean_code and clean_code in comb:
                return True
            if entity_name:
                core_name = re.sub(r"(股份|科技|集团|控股|发展|实业)$", "", entity_name).strip()
                if (core_name and core_name.lower() in comb) or (entity_name.lower() in comb):
                    return True
            if not clean_code and not entity_name:
                return True
            return False

        # 1. 主力通道：新浪财经新闻搜索接口（风控宽松、响应极速、原生标准 JSON）
        try:
            encoded_q = urllib.parse.quote(search_term)
            sina_url = f"https://search.sina.com.cn/api/news?q={encoded_q}&size={req_limit + 5}&page=1"
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
                    for item in items:
                        title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                        if any(k in title for k in lottery_noise):
                            continue
                        snippet = re.sub(r"<[^>]+>", "", item.get("intro", "")).strip()
                        if not _is_relevant(title, snippet):
                            continue
                        # 过滤他股上市挂牌快讯（标题明确为其他个股 IPO，且标题未包含当前查询标的名称或代码时剔除）
                        ipo_keywords = ("登陆创业板", "挂牌创业板", "今日登陆", "正式挂牌", "今日上市", "敲钟上市", "今日申购", "上市首日")
                        if any(kw in title for kw in ipo_keywords):
                            has_target = False
                            if clean_code and clean_code in title:
                                has_target = True
                            if entity_name:
                                core_name = re.sub(r"(股份|科技|集团|控股|发展|实业)$", "", entity_name).strip()
                                if (core_name and core_name in title) or (entity_name in title):
                                    has_target = True
                            if not has_target:
                                continue
                        media_name = item.get("media", "").strip() or "新浪财经"
                        norm_t = self._normalize_time_str(item.get("dataTime", ""))
                        is_broker_pub = bool(re.match(r"^(中信证券|海通证券|广发证券|招商证券|华泰证券|国泰君安|光大证券|申万宏源|中金公司|银河证券|方正证券|天风证券|长江证券|安信证券|国信证券|中泰证券|兴业证券|东吴证券|浙商证券)[：:]", title))
                        item_obj = {
                            "title": title,
                            "source": media_name,
                            "time": norm_t,
                            "datetime": norm_t,
                            "url": item.get("url", ""),
                            "snippet": snippet[:150] + "..." if len(snippet) > 150 else snippet,
                            "summary": snippet[:150] + "..." if len(snippet) > 150 else snippet,
                        }
                        if is_broker_pub:
                            item_obj["内容属性说明"] = "券商行业研报观点(标题格式为'券商名：观点'，系该券商研究团队对外发表的行业分析，非本公司自身日常经营公告)"
                        news_list.append(item_obj)
                        if len(news_list) >= req_limit:
                            break
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
                    "pagesize": req_limit + 5,
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
                    for item in articles:
                        title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                        if any(k in title for k in lottery_noise):
                            continue
                        snippet = re.sub(r"<[^>]+>", "", item.get("content", "")).strip()
                        if not _is_relevant(title, snippet):
                            continue
                        media_name = item.get("mediaName", "").strip() or "东方财富"
                        norm_t = self._normalize_time_str(item.get("date", ""))
                        is_broker_pub = bool(re.match(r"^(中信证券|海通证券|广发证券|招商证券|华泰证券|国泰君安|光大证券|申万宏源|中金公司|银河证券|方正证券|天风证券|长江证券|安信证券|国信证券|中泰证券|兴业证券|东吴证券|浙商证券)[：:]", title))
                        item_obj = {
                            "title": title,
                            "source": media_name,
                            "time": norm_t,
                            "datetime": norm_t,
                            "url": item.get("url", ""),
                            "snippet": snippet[:150] + "..." if len(snippet) > 150 else snippet,
                            "summary": snippet[:150] + "..." if len(snippet) > 150 else snippet,
                        }
                        if is_broker_pub:
                            item_obj["内容属性说明"] = "券商行业研报观点(标题格式为'券商名：观点'，系该券商研究团队对外发表的行业分析，非本公司自身日常经营公告)"
                        news_list.append(item_obj)
                        if len(news_list) >= req_limit:
                            break
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
                            norm_t = self._normalize_time_str(item.get("notice_date") or "")
                            ann_list.append({
                                "title": ann_title,
                                "source": "上市公司官方公告",
                                "time": norm_t,
                                "datetime": norm_t,
                                "url": ann_url_link,
                                "snippet": f"【上市公司法定公告】{ann_title}",
                                "summary": f"【上市公司法定公告】{ann_title}",
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
        market_range = arguments.get("range") or arguments.get("market_range") or "AG"
        if not query:
            raise ValueError("query 不能为空，请输入股票简称或代码（如'贵州茅台'、'腾讯'）")
        entities = self.tdx_client.lookup_stock(query=query, market_range=market_range)

        # 判定是否存在代码或全称的精准命中 (修复 P0 缺陷)
        clean_q = re.sub(r"^(sh|sz|bj|hk)", "", query.strip(), flags=re.I)
        clean_q = re.sub(r"\.(sh|sz|bj|hk|ss)$", "", clean_q, flags=re.I)
        exact_match = False
        matched_code = None
        for ent in entities:
            e_code = str(ent.get("code") or ent.get("entity_code") or "")
            e_name = str(ent.get("name") or ent.get("entity_name") or "")
            e_market = str(ent.get("market") or "")
            e_setcode = str(ent.get("setcode") or "")
            aliases = [str(a) for a in ent.get("aliases", [])]

            # 严格防范跨市场误命中：若显式指定了 range，标的必须隶属于目标市场 (修复 Issue 2)
            if market_range in ("AG", "A") and (e_market == "HK" or e_setcode == "31" or len(e_code) == 5):
                continue
            if market_range in ("HK", "HK-GP") and (e_market in ("SH", "SZ", "BJ") or e_setcode in ("0", "1", "2") or len(e_code) == 6):
                continue

            if (clean_q and e_code == clean_q) or query.strip() in ([e_name] + aliases):
                exact_match = True
                matched_code = e_code
                break

        res: dict[str, Any] = {
            "query": query,
            "range": market_range,
            "exact_match": exact_match,
            "matched_code": matched_code,
            "entity_count": len(entities),
            "entities": entities,
        }
        if exact_match:
            res["命中说明"] = f"已精确匹配到目标证券标的 (代码: {matched_code})"
        elif clean_q.isdigit() and len(clean_q) in (5, 6):
            res["未命中说明"] = (
                f"在指定市场范围 ({market_range}) 中未检索到代码为 '{query}' 的上市证券标的；"
                "下方返回列表为语义模型相关度最近的关联候选（非正主标的），请核对代码后再行查询。"
            )
        return res

    def stock_quotes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 2：实时盘口与完整证券行情"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空，请输入证券代码（如'600519'）")
        setcode = arguments.get("setcode")
        return self.tdx_client.get_quotes(symbol=symbol, setcode=setcode)

    def stock_kline(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 3：多周期历史 K 线走势 + 内置技术指标计算 (MACD/KDJ/BOLL/MA)"""
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
        clean_code = re.sub(r"[^\d]", "", symbol)
        category = arguments.get("category", "concepts")
        cat_lower = category.lower().strip()
        category_alias_map = {
            "sector": "industry",
            "dividends": "dividends",
            "bonus": "dividends",
            "lhb": "lhb",
            "buyback": "buyback",
            "repurchases": "repurchases",
            "indicators": "indicators",
            "financials": "financials",
        }
        norm_cat = category_alias_map.get(cat_lower, cat_lower)

        # 港股标的（5位代码）：启用新浪港股F10主力专线与东方财富高可用备份专线
        if infer_market_setcode(symbol) == "31" or len(clean_code) == 5:
            norm_code = clean_code.zfill(5)
            return self.tdx_client._get_hk_f10(norm_code, category=cat_lower)

        # =========================================================================
        # 【功能块 8.1：A股全分类统一前置有效代码拦截守卫 (修复 P1-1 契约分裂)】
        # - 输入：clean_code (标的代码)
        # - 功能：统一在执行任何具体分类分支前校验标的存在性，杜绝无效代码进入
        #         dividends/repurchases/margin_trading 等分支导致契约分裂
        # - 输出：合法标的通过，非法代码直接抛出标准明确的 ValueError
        # =========================================================================
        entity = self.tdx_client._resolve_exact_security_entity(clean_code)
        if entity is None:
            raise ValueError(
                f"未查询到证券代码 '{symbol}' 的 F10 基本面资料（代码不存在或非有效 A 股/港股标的）。"
                "建议先调用 stock_lookup 工具核对标准证券名称与代码。"
            )

        # =========================================================================
        # 【功能块 8.2：A股官方权威行业分类专线 (修复 P2-1)】
        # - 输入：clean_code
        # - 功能：提取上市公司中国证监会官方门类大类行业与申万一、二级细分行业
        # - 输出：结构化返回官方行业分类字典
        # =========================================================================
        if norm_cat == "industry":
            sum_res = self.tdx_client.get_f10(clean_code, category="summary")
            csrc_ind, sw_ind_1, sw_ind_2, main_biz = "", "", "", ""
            for t in sum_res.get("data", {}).get("ResultSets", []):
                if t.get("ResultSetKey") == "table8" and t.get("Content"):
                    r = t["Content"][0]
                    cols = t.get("ColName", [])
                    if "zzhy" in cols:
                        csrc_ind = str(r[cols.index("zzhy")] or "")
                elif t.get("ResultSetKey") == "table1" and t.get("Content"):
                    r = t["Content"][0]
                    cols = t.get("ColName", [])
                    if "hy1" in cols:
                        sw_ind_1 = str(r[cols.index("hy1")] or "")
                    if "hy2" in cols:
                        sw_ind_2 = str(r[cols.index("hy2")] or "")
                    if "T017" in cols:
                        main_biz = str(r[cols.index("T017")] or "")
            if not csrc_ind or not sw_ind_1:
                b_res = self.tdx_client.get_f10(clean_code, category="basic_info")
                for t in b_res.get("data", {}).get("ResultSets", []):
                    if t.get("ResultSetKey") == "table0" and t.get("Content"):
                        r = t["Content"][0]
                        cols = t.get("ColName", [])
                        if "T040" in cols and not csrc_ind:
                            csrc_ind = str(r[cols.index("T040")] or "")
                        if "yjhy" in cols and not sw_ind_1:
                            sw_ind_1 = str(r[cols.index("yjhy")] or "")
                        if "T017" in cols and not main_biz:
                            main_biz = str(r[cols.index("T017")] or "")
            ind_record = {
                "证监会行业": csrc_ind or "未知",
                "申万一级行业": sw_ind_1 or "未知",
                "申万二级行业": sw_ind_2 or "未知",
                "主营业务简述": main_biz or "暂无主营简述",
            }
            return {
                "symbol": clean_code,
                "category": "industry",
                "category_name": "官方行业分类 (证监会行业与申万行业)",
                "source": "tdx_official_vip (通达信官方专线)",
                "formatted_tables": {
                    "industry": [ind_record],
                },
                "data": ind_record,
                "行业说明": "A股上市公司所属中国证监会官方门类大类行业与申万一、二级细分行业分类。",
            }

        # =========================================================================
        # 【功能块 8.3：A股权威龙虎榜异动与席位明细专线 (修复 P2-2)】
        # - 输入：clean_code
        # - 功能：调用东方财富权威龙虎榜接口获取登榜记录与买卖营业部席位金额占比
        # - 输出：结构化龙虎榜明细字典
        # =========================================================================
        if norm_cat == "lhb":
            lhb_data = self.tdx_client._get_ashare_lhb(clean_code)
            has_records = len(lhb_data.get("lhb_records", [])) > 0
            res_obj = {
                "symbol": clean_code,
                "category": "lhb",
                "category_name": "龙虎榜每日席位明细与游资机构博弈",
                "source": "eastmoney_lhb_vip (东方财富龙虎榜权威专线)",
                "formatted_tables": {
                    "lhb_daily_details": lhb_data.get("lhb_records", []),
                },
                "data": lhb_data,
                "龙虎榜说明": "收录股票近期异动登榜的营业部席位买卖金额、机构专用席位博弈与成交占比。",
                "盘后入库说明": (
                    "【龙虎榜披露时效与规则（修复 P3-2）】交易所官方龙虎榜异动及席位明细由上交所/深交所于交易日 16:30–17:30 盘后清算后统一公布入库。"
                    "盘中交易时段或清算完成前查询无当日记录；若收盘清算后仍无记录，表明该标的当日价格振幅、换手率或涨跌幅未触发交易所法定披露标准"
                    "（如非ST主板单日偏离值达7%、换手率达20%等；可结合 limit_up 工具交叉核验涨跌停及异动池）。"
                ),
            }
            if not has_records:
                res_obj["无记录说明"] = f"标的 {symbol} 近期无公开披露的龙虎榜异动登榜记录（低波动或未触发交易所异动披露机制）。"
            return res_obj

        # =========================================================================
        # 【功能块 8.4：A股股份回购方案与实施明细专线 (修复 P2-7 与 P2-3)】
        # - 输入：clean_code
        # - 功能：调用东方财富权威回购专线获取上市公司回购进展与规模，精准区分 repurchases 与 buyback
        # - 输出：结构化回购明细字典
        # =========================================================================
        if norm_cat in ("repurchases", "buyback"):
            rep_data = self.tdx_client._get_ashare_repurchases(clean_code)
            rep_list = rep_data.get("formatted_tables", {}).get("repurchases", [])
            has_records = len(rep_list) > 0
            cat_name = "股份回购方案与实施明细" if norm_cat == "repurchases" else "股份回购与注销实施进度 (Buyback 专线)"
            tbl_key = "repurchases" if norm_cat == "repurchases" else "buyback_details"
            res_obj = {
                "symbol": clean_code,
                "category": norm_cat,
                "category_name": cat_name,
                "source": "eastmoney_repurchases (东方财富A股回购权威专线)",
                "formatted_tables": {
                    tbl_key: rep_list,
                },
                "data": rep_data,
                "回购说明": "A股上市公司实施股份回购的最新进展、回购金额上限/下限、已回购股数与方案进度。",
            }
            if norm_cat == "buyback":
                res_obj["参数说明"] = "buyback 为 repurchases 的英文专业术语别名，统一由东方财富权威回购专线直通驱动。"
            if not has_records:
                res_obj["无记录说明"] = f"标的 {symbol} 近期无公开披露的股份回购方案或实施记录。"
            return res_obj

        # A 股标的：若请求 dividends/bonus，路由至真实分红派息专线 (修复 Issue 4)
        if norm_cat == "dividends":
            div_rows = self.tdx_client._get_ashare_dividends(clean_code)
            return {
                "symbol": clean_code,
                "category": "dividends",
                "category_name": "A股历年分红派息与送转明细",
                "source": "sina_ashare_dividends (新浪财经分红送配专线)",
                "formatted_tables": {
                    "dividends": div_rows,
                },
                "data": div_rows if div_rows else {"status": "暂无分红派息记录"},
                "分红说明": "A股历年权益分派方案、派息送股及除权除息日明细。",
            }

        # 1. 首先尝试通达信官方专线获取（优先官方授权渠道）
        try:
            tdx_res = self.tdx_client.get_f10(symbol=symbol, category=norm_cat)
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
                    # 仅当首表为异常占位列名 ["0"] 且内容为 [[0]] 时才判定为底层接口故障需要降级；有效列结构但无记录行时属于正常空记录
                    if first_table.get("ColName") == ["0"] and (not first_table.get("Content") or first_table.get("Content") == [[0]]):
                        need_fallback = True
            if not need_fallback:
                tdx_res["source"] = "tdx_official_vip (通达信官方授权专线)"
                # 将 ResultSets 中的各表结构化转换为行对象字典列表，便于大模型与前端直观阅读
                formatted_tables = {}
                holder_total_shares = 0.0
                zgb_total_shares = 0.0
                if category == "top_holders":
                    for table in raw_data.get("ResultSets", []):
                        if table.get("ResultSetKey") == "table0":
                            t0_rows = table.get("Content", [])
                            t0_cols = table.get("ColName", [])
                            if t0_rows and "T004" in t0_cols:
                                t0_idx = t0_cols.index("T004")
                                try:
                                    holder_total_shares = float(t0_rows[0][t0_idx] or 0.0)
                                except (ValueError, TypeError):
                                    pass
                            break
                    # 获取标的真实法定总股本 (优先从 summary.table6.zgb 快速读取)
                    try:
                        sum_chk = self.tdx_client.get_f10(clean_code, category="summary")
                        for t in sum_chk.get("data", {}).get("ResultSets", []):
                            if t.get("ResultSetKey") == "table6" and t.get("Content"):
                                c_names = t.get("ColName", [])
                                if "zgb" in c_names:
                                    zgb_idx = c_names.index("zgb")
                                    zgb_total_shares = float(t["Content"][0][zgb_idx] or 0.0)
                                    break
                    except Exception:
                        pass
                    if zgb_total_shares <= 0.0:
                        zgb_total_shares = holder_total_shares

                for idx, table in enumerate(raw_data.get("ResultSets", [])):
                    t_key = table.get("ResultSetKey", f"table{idx}")
                    cols = table.get("ColName", [])
                    rows = table.get("Content", [])

                    # 严格过滤与业务无关的三类底层系统表（复权因子表 AdjustingFactor/RatioFactor 与纯历史收盘价表）
                    col_str = " ".join(cols)
                    if "AdjustingFactor" in col_str or "RatioFactor" in col_str or cols == ["TradingDay", "ClosePrice"]:
                        continue
                    if norm_cat == "capital_flow" and t_key != "table0":
                        continue
                    if norm_cat == "unlocks" and t_key != "table0":
                        continue

                    table_rows = []
                    for row in rows:
                        row_dict = {}
                        for c_idx, val in enumerate(row):
                            col_raw = cols[c_idx] if c_idx < len(cols) else f"col_{c_idx}"
                            if norm_cat == "concepts":
                                col_title = CONCEPTS_FIELDS.get(col_raw, col_raw)
                            elif norm_cat == "margin_trading":
                                col_title = MARGIN_TRADING_FIELDS.get(col_raw, col_raw)
                            elif norm_cat == "capital_flow":
                                col_title = CAPITAL_FLOW_FIELDS.get(col_raw, col_raw)
                            elif norm_cat == "events":
                                col_title = EVENTS_FIELDS.get(col_raw, col_raw)
                            elif norm_cat == "limit_up":
                                col_title = LIMIT_UP_FIELDS.get(col_raw, col_raw)
                            elif norm_cat == "unlocks":
                                col_title = UNLOCKS_FIELDS.get(col_raw, col_raw)
                            elif norm_cat == "shareholders":
                                col_title = SHAREHOLDERS_FIELDS.get(col_raw, col_raw)
                            elif norm_cat == "forecast":
                                col_title = FORECAST_FIELDS.get(col_raw, col_raw)
                            elif norm_cat == "summary":
                                col_title = SUMMARY_FIELDS.get(col_raw, col_raw)
                            elif norm_cat == "top_holders" and t_key == "table0":
                                col_title = TOP_HOLDERS_TOTALS_FIELDS.get(col_raw, col_raw)
                            elif norm_cat == "top_holders" and t_key == "table1":
                                col_title = TOP_HOLDERS_FIELDS.get(col_raw, col_raw)
                            elif norm_cat == "basic_info":
                                if t_key == "table0":
                                    col_title = BASIC_INFO_TABLE0_FIELDS.get(col_raw, col_raw)
                                elif t_key == "table1":
                                    col_title = BASIC_INFO_TABLE1_FIELDS.get(col_raw, col_raw)
                                elif t_key == "table2":
                                    col_title = BASIC_INFO_TABLE2_FIELDS.get(col_raw, col_raw)
                                else:
                                    col_title = col_raw
                            else:
                                col_title = col_raw
                            row_dict[col_title] = val

                        # 针对 margin_trading：清洗数字字符串类型漂移，统一规范为 float (修复 B3)
                        if norm_cat == "margin_trading":
                            for num_k in ("融资余额_元", "融资买入额_元", "融资偿还额_元", "融券余量_股", "融券卖出量_股", "融券偿还量_股", "成交量_股", "成交金额_元", "收盘价", "流通股本_万股", "总股本_万股"):
                                if num_k in row_dict and row_dict[num_k] is not None:
                                    try:
                                        row_dict[num_k] = float(row_dict[num_k])
                                    except (ValueError, TypeError):
                                        pass

                        # 针对 forecast：清洗一致预期调整数字字符串类型漂移，统一规范为 float (修复 B5)
                        if norm_cat == "forecast":
                            for num_k in ("一致预期变动_短期_pct", "一致预期变动_中期_pct", "一致预期变动_长期_pct", "预测每股收益_当年_元", "预测每股收益_次年_元", "预测每股收益_后年_元", "预测PEG", "机构目标价_元"):
                                if num_k in row_dict and row_dict[num_k] is not None:
                                    try:
                                        row_dict[num_k] = float(row_dict[num_k])
                                    except (ValueError, TypeError):
                                        pass

                        # 针对 top_holders table0：补齐真实总股本并反推限售流通股 (修复 P2-4)
                        if norm_cat == "top_holders" and t_key == "table0":
                            if zgb_total_shares > 0:
                                row_dict["总股本_股"] = int(zgb_total_shares)
                                circ_total = float(row_dict.get("统计流通总额_股") or row_dict.get("流通A股合计_股") or 0.0)
                                if row_dict.get("限售流通股合计_股") is None and circ_total > 0:
                                    row_dict["限售流通股合计_股"] = max(0, int(zgb_total_shares - circ_total))

                        # 自动补齐 top_holders table1 中持股比例，并严密校准分母为真实总股本 (修复 P1-1)
                        if norm_cat == "top_holders" and t_key == "table1":
                            cgs_val = float(row_dict.get("持股数量_股", 0.0) or 0.0)
                            if cgs_val > 0:
                                if zgb_total_shares > 0:
                                    row_dict["持股占总股本比例_pct"] = round((cgs_val / zgb_total_shares) * 100.0, 3)
                                if holder_total_shares > 0:
                                    row_dict["持股占流通A股比例_pct"] = round((cgs_val / holder_total_shares) * 100.0, 3)
                                if "持股占总股本比例_pct" in row_dict:
                                    row_dict["持股比例_pct"] = row_dict["持股占总股本比例_pct"]
                                elif "持股占流通A股比例_pct" in row_dict:
                                    row_dict["持股比例_pct"] = row_dict["持股占流通A股比例_pct"]

                        table_rows.append(row_dict)
                    if norm_cat == "events" and table_rows:
                        table_rows = list(reversed(table_rows))
                        for r in table_rows:
                            for dk in ("催化日期", "cjrq"):
                                if dk in r and re.match(r"^\d{8}$", str(r[dk])):
                                    ds = str(r[dk])
                                    r[dk] = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
                    formatted_tables[t_key] = table_rows
                tdx_res["formatted_tables"] = formatted_tables
                if norm_cat == "events":
                    has_records = any(len(rows) > 0 for rows in formatted_tables.values())
                    if not has_records:
                        tdx_res["无记录说明"] = "该标的近期事件库无记录（近期无重大行业题材或宏观催化事件）。如需查询公司最新日常经营与法定公告，请调用 stock_news 工具。"
                    tdx_res["事件库属性说明"] = (
                        "本表为通达信收录的历史重大行业题材与宏观里程碑事件库（已按发生日期倒序排列，非实时个股公告）；"
                        "如需获取上市公司最新分钟级法定披露公告与突发新闻，请调用 stock_news 工具。"
                    )
                elif norm_cat == "top_holders":
                    tdx_res["持股比例分母口径说明"] = (
                        "【持股比例口径校准（已修复 P1-1）】："
                        "1.【持股占总股本比例_pct / 持股比例_pct】：分母为公司真实法定总股本（如东方电气 34.58亿股、邮储银行 1200亿股），严密反映股东对上市公司的真实权益占比；"
                        "2.【持股占流通A股比例_pct】：分母为境内流通A股股本（如 A+H 标的或含受限股标的），仅反映其在 A 股流通盘中的集中度；"
                        "3. 表内【是否定期报告】字段中，1 代表季末/半年末/年末定期法定财报披露，0 代表非季末重大股权变动、大宗交易或增减持等临时变动日。"
                    )
                    tdx_res["股本口径说明"] = (
                        "【A+H及跨市场股本口径警示】十大流通股东表中的股本字段仅为定期报告历史流通股口径（按季度披露）。"
                        "对于 A+H 两地上市标的（如比亚迪 002594/01211、中国平安、宁德时代等），表中流通总额通常为境内A股流通股与境外H股流通股的历史合并加总（例如比亚迪历史定期报告71.70亿股=A股流通34.87亿+H股流通36.83亿）；"
                        "而 quotes 工具返回的【总股本_万股】为交易所实时最新法定全部已发行股本（如比亚迪最新91.17亿股，包含最新送转增发）。"
                        "两者属于【历史定期报告流通额 vs 实时法定总股本】的不同口径，严禁混用，测算总市值或AH溢价时必须区分各市场实际上市股数！"
                    )
                    tdx_res["限售股与股本结构核算指南"] = (
                        "通达信底层 table0 中的【限售流通股合计_股(T013)】为预留扩展列（历史多期未直接填列）。"
                        "已自动通过【真实基准总股本 − 统计流通总额_股】精确反推补全（如东方电气 34.58 亿股 − 26.71 亿流通总额 = 7.87 亿限售流通股）。"
                    )
                elif norm_cat == "forecast":
                    has_records = any(len(rows) > 0 for rows in formatted_tables.values())
                    if not has_records:
                        tdx_res["无记录说明"] = "该标的近期无卖方机构发布盈利预测报告或未覆盖业绩一致预期数据。"
                    else:
                        tdx_res["一致预期指标说明"] = (
                            "表内【一致预期变动_短期/中期/长期_pct】为卖方分析师对该标的盈利预测与目标价在近1月/3月/6月的动态调整幅度（%），非预测区间上下限。"
                            "正值代表卖方分析师综合调高预期，负值代表调低预期。"
                        )
                elif norm_cat == "limit_up":
                    has_records = any(len(rows) > 0 for rows in formatted_tables.values())
                    if not has_records:
                        tdx_res["无记录说明"] = "该股票在近期统计周期内无涨停或异动记录（白马蓝筹股/低波动标的未触发涨停机制属于常态）。"
                    else:
                        t0_data = formatted_tables.get("table0", [])
                        # 修复 B6：通达信官方专线对'超涨10%'类异动记录的成交金额恒返回0，智能对齐日K线成交额全量补齐
                        need_kline = any(r.get("当日成交金额_万元") in (0, 0.0, "0", None) for r in t0_data)
                        kline_map = {}
                        if need_kline:
                            try:
                                kline_res = self.tdx_client.get_kline(clean_code, period="day", count=300)
                                for b in (kline_res.get("klines") or kline_res.get("bars") or []):
                                    b_time = str(b.get("time", ""))[:10]
                                    b_amt = b.get("amount")
                                    if b_time and b_amt and float(b_amt) > 0:
                                        kline_map[b_time] = round(float(b_amt) / 10000.0, 2)
                            except Exception:
                                pass

                        for row in t0_data:
                            # 预设统一列名，确保所有标的与行的列结构完全一致，杜绝 8 列与 10 列漂移 (修复 P3)
                            row.setdefault("成交额补充说明", None)
                            row.setdefault("封板说明", None)
                            if row.get("当日成交金额_万元") in (0, 0.0, "0", None):
                                trade_d = str(row.get("涨停日期", ""))[:10]
                                if trade_d in kline_map:
                                    row["当日成交金额_万元"] = kline_map[trade_d]
                                    row["成交额补充说明"] = "由日K线历史成交额精确补全(解决上游超涨10%缺值)"
                                elif row.get("当日标志") == "1" or str(row.get("currq")) == "1":
                                    try:
                                        q = self.tdx_client.get_quotes(clean_code)
                                        amt = q.get("盘口核心行情", {}).get("成交额_元")
                                        if amt and float(amt) > 0:
                                            row["当日成交金额_万元"] = round(float(amt) / 10000.0, 2)
                                            row["成交额补充说明"] = "盘口实时成交额补充(盘中清算前)"
                                    except Exception:
                                        pass
                            if row.get("封板时间") == "--":
                                row["封板说明"] = "盘中超涨10%异动，未触发封死涨停板"
                elif category == "unlocks":
                    t0_data = formatted_tables.get("table0", [])
                    if t0_data:
                        latest_date_str = str(t0_data[0].get("解禁日期", ""))
                        today_str = datetime.date.today().strftime("%Y%m%d")
                        if latest_date_str and latest_date_str < today_str:
                            tdx_res["解禁状态说明"] = (
                                f"该标的最近一次限售解禁已于 {latest_date_str} 实施完成，当前股票处于全流通状态或近期无待解禁日程。"
                            )
                    try:
                        fb_unlocks = self.fallback_client.get_f10(symbol=clean_code, category="unlocks")
                        if fb_unlocks and isinstance(fb_unlocks, list) and len(fb_unlocks) > 1:
                            tdx_res["多阶段解禁明细"] = fb_unlocks
                    except Exception:
                        pass
                elif norm_cat == "summary":
                    t5_rows = formatted_tables.get("table5", [])
                    # 检查整张 F10 摘要表是否具备任何实质非空非 None 业务数据
                    summary_has_real_data = any(
                        any(v is not None and str(v).strip() not in ("", "--", "None", "null") for v in row.values())
                        for t_rows in formatted_tables.values() for row in t_rows
                    )
                    # 修复 P2-5：严谨区分“确无质押”与“未采集”，空表时客观说明无记录，有明细时准确呈现
                    if not t5_rows:
                        if summary_has_real_data:
                            tdx_res["质押状态说明"] = "F10 摘要未收录当前股东股权质押明细（暂无质押披露记录或数据源未采集，不可直接等同于已核实零质押，请以中登公司及交易所官方质押公告为准）。"
                    else:
                        tdx_res["质押状态说明"] = f"该公司存在股东股权质押明细记录（共 {len(t5_rows)} 条记录，详见 table5 质押明细表）。"
                elif norm_cat == "margin_trading":
                    if "table2" in formatted_tables:
                        for row in formatted_tables["table2"]:
                            for ok, cn in F10_MARGIN_TABLE2_MAP.items():
                                if ok in row:
                                    row[cn] = row.pop(ok)
                    # 修复 P3：两融明细表 table0 为空时主动注入明确无记录说明，杜绝下游只能靠标志位猜疑
                    t0_data = formatted_tables.get("table0", [])
                    if not t0_data:
                        tdx_res["无记录说明"] = f"标的 {symbol} 当前非融资融券标的证券（或暂无公开披露的两融交易明细记录）。"

                # 增强 F10 摘要表：汉化关键财务与估值指标、依据“利润变动方向”精确校准亏损标的同比含义、注入隔日风格标签与破净动态复核指引
                if norm_cat == "summary" or ("table3" in formatted_tables and "table4" in formatted_tables):
                    self._enrich_f10_summary_tables(formatted_tables, tdx_res)

                # 修复 Issue 1: 针对 indicators / financials，挂载宽表字段与专属分类名
                # 针对 indicators 与 financials：赋予明确的参数区分度与专属数据结构 (修复 P2-3)
                if cat_lower == "indicators":
                    tdx_res["category"] = "indicators"
                    tdx_res["category_name"] = "核心财务与经营指标概览 (公司主要财务与估值比率)"
                    formatted_tables["profitability"] = formatted_tables.get("table0", [])
                    formatted_tables["indicators"] = formatted_tables.get("table4", [])
                    formatted_tables["valuation"] = formatted_tables.get("table6", [])
                    formatted_tables["summary"] = formatted_tables.get("table8", [])
                    tdx_res["估值快照清算说明"] = "【估值基准说明】本表收盘后总市值基于当日收盘清算价计算；盘中时段为上一交易日清算口径，盘中精准估值请结合 stock_quotes 实时行情总市值。"
                elif cat_lower == "financials":
                    tdx_res["category"] = "financials"
                    tdx_res["category_name"] = "财务三大报表核心数据精要 (利润表/资产负债表/现金流量表)"
                    try:
                        inc_stmts = self.stock_financials({"symbol": clean_code, "report_type": "income", "limit_periods": 2}).get("statements", [])
                        bal_stmts = self.stock_financials({"symbol": clean_code, "report_type": "balance", "limit_periods": 2}).get("statements", [])
                        cf_stmts = self.stock_financials({"symbol": clean_code, "report_type": "cashflow", "limit_periods": 2}).get("statements", [])
                        formatted_tables = {
                            "income_summary": inc_stmts,
                            "balance_summary": bal_stmts,
                            "cashflow_summary": cf_stmts,
                        }
                    except Exception:
                        formatted_tables["financials_overview"] = formatted_tables.get("table4", [])
                    tdx_res["formatted_tables"] = formatted_tables

                # 防空壳无效标的校验：严格校验所有格式化表中是否存在任何实质有效非空单元格（防止全 None 占位行被误判为有效数据）
                has_any_data = any(
                    any(v is not None and str(v).strip() not in ("", "--", "None", "null") for v in row.values())
                    for rows in formatted_tables.values() for row in rows
                )
                if not has_any_data:
                    entity = self.tdx_client._resolve_exact_security_entity(clean_code)
                    if entity is None:
                        raise ValueError(
                            f"未查询到证券代码 '{symbol}' 的 F10 基本面资料（代码不存在或非有效 A 股/港股标的）。"
                            "建议先调用 stock_lookup 工具核对标准证券名称与代码。"
                        )
                    else:
                        if not tdx_res.get("无记录说明"):
                            tdx_res["无记录说明"] = f"标的 {symbol} 当前分类({category})下暂无公开披露的明细数据。"
                return tdx_res
        except ValueError:
            raise
        except Exception:
            pass

        # 2. 通达信未开放或异常时，自动平滑降级至公开数据中心高可用 F10
        fallback_data = self.fallback_client.get_f10(symbol=symbol, category=norm_cat)
        clean_code = re.sub(r"[^\d]", "", symbol)
        if fallback_data is None or fallback_data == {"status": "暂无数据"}:
            entity = self.tdx_client._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到证券代码 '{symbol}' 的 F10 基本面资料（代码不存在或非有效 A 股/港股标的）。"
                    "建议先调用 stock_lookup 工具核对标准证券名称与代码。"
                )
        category_names = {
            "concepts": "热点题材板块族谱",
            "industry": "热点题材板块族谱",
            "events": "事件驱动催化",
            "dividends": "A股历年分红派息与送转明细",
            "forecast": "机构盈利预测",
            "shareholders": "股东人数与筹码集中度",
            "top_holders": "十大流通股东",
            "margin_trading": "融资融券交易数据",
            "capital_flow": "主力资金流向",
            "limit_up": "涨停分析与异动",
            "summary": "公司概要与最新提示",
            "indicators": "核心财务与经营指标概览 (公司概要与主要指标)",
            "financials": "核心财务与经营指标概览 (公司概要与主要指标)",
            "unlocks": "限售解禁日程表",
            "basic_info": "公司基本工商与发行信息",
            "repurchases": "股份回购明细",
        }
        fallback_res = {
            "symbol": clean_code,
            "category": category,
            "category_name": category_names.get(category, category),
            "source": "public_f10_fallback",
            "data": fallback_data if fallback_data is not None else {"status": "暂无数据"},
        }
        if category == "limit_up":
            fallback_res["无记录说明"] = "该股票在近期统计周期内无涨停或异动记录（白马蓝筹股/低波动标的未触发涨停机制属于常态）。"
        elif category == "forecast":
            fallback_res["无记录说明"] = "该标的近期无卖方机构发布盈利预测报告或未覆盖业绩一致预期数据。"
        elif category == "events":
            fallback_res["无记录说明"] = "该标的近期事件库无记录（近期无重大行业题材或宏观催化事件）。如需查询公司最新日常经营与法定公告，请调用 stock_news 工具。"
        return fallback_res

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

        # 0. 增强 table0 (基础定位与扣非净利润规范汉化，修复 B10 与 P3)
        if "table0" in formatted_tables:
            for row in formatted_tables["table0"]:
                for ok, cn in F10_TABLE0_SUMMARY_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

        # 1. 增强 table4 (关键财务指标映射与亏损同比方向精确判定，修复 B10 与 P3)
        if "table4" in formatted_tables:
            for row in formatted_tables["table4"]:
                for ok, cn in F10_TABLE4_FINANCIAL_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

                g_profit = row.get("归母净利润_元")
                g_tb = row.get("归母净利润同比增减_pct")
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
                k_tb = row.get("扣非净利润同比增减_pct")
                k_profit = row.get("扣非净利润_元")
                if k_profit is None and "table0" in formatted_tables and formatted_tables["table0"]:
                    k_profit = formatted_tables["table0"][0].get("扣非净利润_元")
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

        # 3. 增强 table6 (估值指标映射与盘中重算提示，修复 P1-2 PE_TTM 期次落后自算)
        if "table6" in formatted_tables:
            table4_bgq = ""
            if "table4" in formatted_tables and formatted_tables["table4"]:
                table4_bgq = str(formatted_tables["table4"][0].get("报告期") or formatted_tables["table4"][0].get("bgq") or "")
            for row in formatted_tables["table6"]:
                for ok, cn in F10_TABLE6_VALUATION_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

                # 检验 PE_TTM 是否由于快照批处理入库滞后而落后于最新报告期 (修复 P1-2)
                curr_pe_ttm = row.get("动态市盈率_PE_TTM")
                if curr_pe_ttm is not None and table4_bgq:
                    try:
                        clean_code = tdx_res.get("symbol", "")
                        if clean_code:
                            q_info = self.tdx_client.get_quotes(clean_code)
                            real_ttm = None
                            if isinstance(q_info, dict) and isinstance(q_info.get("估值与基本盘面"), dict):
                                real_ttm = q_info["估值与基本盘面"].get("滚动市盈率_PETTM")
                            if real_ttm is None and isinstance(q_info.get("ProInfo"), dict):
                                real_ttm = q_info["ProInfo"].get("PETTM")
                            if real_ttm is not None and str(real_ttm).strip() != "":
                                real_ttm_val = round(float(real_ttm), 2)
                                if abs(real_ttm_val - float(curr_pe_ttm)) > 1.0:
                                    row["动态市盈率_PE_TTM"] = real_ttm_val
                                    row["PE_TTM校准说明"] = (
                                        f"上游F10估值快照期次入库滞后(原值{curr_pe_ttm})，"
                                        f"已自动自算校准为最新{table4_bgq}期次真实TTM值({real_ttm_val}倍)。"
                                    )
                    except Exception:
                        pass

                row["估值时效提示"] = f"市净率(PB)/市盈率(PE)基于上一交易日清算日({gxrq or '前一交易日'})，盘中请结合 stock_quotes 实时现价动态重算。"

        # 3.5 增强 table8 (基础信息与行业分类汉化映射，修复 P3 杜绝裸英文字段外泄)
        if "table8" in formatted_tables:
            for row in formatted_tables["table8"]:
                for ok, cn in F10_TABLE8_SUMMARY_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

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
        message = (arguments.get("message") or arguments.get("query") or arguments.get("condition") or "").strip()
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
        clean_code = re.sub(r"[^\d]", "", symbol)
        entity_name = None
        if clean_code and len(clean_code) in (5, 6):
            entity = self.tdx_client._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未找到代码为 '{symbol}' 的有效证券标的（代码不存在或非有效 A 股/港股标的），"
                    "已拦截泛化模糊检索以避免返回无关误导资讯。建议先调用 stock_lookup 工具查询标准代码与简称。"
                )
            entity_name = entity.get("entity_name")

        limit = int(arguments.get("limit", 5))
        news = self.fallback_client.get_news(symbol=symbol, limit=limit, entity_name=entity_name)
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
    "description": "查询指定股票的实时五档盘口、买卖挂单、最新现价、涨跌幅、换手率、量比、内外盘强弱、总市值与动态市盈率等高频盘口与专业估值指标。全面支持 A 股（通达信官方专线）与港股（腾讯免费专线回退，如 '00700' 腾讯控股、'09988' 阿里巴巴）。数据全面中文化映射。",
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
    "description": "获取指定股票的多周期历史复权K线走势，并自动计算输出关键技术指标（MA5/MA10/MA20均线、MACD指标DIF/DEA/柱状图、KDJ随机指标、BOLL布林带上中下轨）。全面支持 A 股及港股（腾讯免费专线）。支持日线(day)、周线(week)、月线(month)、60分钟(60m)、15分钟(15m)、5分钟(5m)。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'），港股为5位数字（如'00700'）"},
            "period": {"type": "string", "enum": ["day", "week", "month", "60m", "30m", "15m", "5m", "1m"], "default": "day", "description": "K线周期，默认为 day (日线)"},
            "count": {"type": "integer", "minimum": 1, "maximum": 300, "default": 30, "description": "请求K线根数（1-300），默认30根"},
            "adjust": {"type": "string", "enum": ["qfq", "hfq", "none"], "default": "qfq", "description": "复权方式：qfq=前复权(默认), hfq=后复权, none=不复权"},
            "setcode": {"type": "string", "description": "市场代码：0=深市，1=沪市，2=北交所，31=港交所。默认自动推导"},
            "with_indicators": {"type": "boolean", "default": True, "description": "是否自动附带技术指标计算结果（MA、MACD、KDJ、BOLL），默认为 true"}
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
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'），港股为5位数字（如'00700'）"},
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
    "description": "查询上市公司深度基本面与筹码数据。包括热点题材板块族谱(concepts/industry)、事件驱动催化与分红派息(events/dividends)、机构盈利预测与评级(forecast)、股东人数变化与筹码集中度(shareholders)、十大流通股东(top_holders)、港股公司股份回购明细(repurchases/buyback)、融资融券明细(margin_trading)、主力资金流向(capital_flow)、涨停分析与异动(limit_up/lhb)、限售解禁日程(unlocks)、公司概要与最新提示(summary)、公司基本工商与发行信息(basic_info)。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'），港股为5位数字（如'00700'）"},
            "category": {
                "type": "string",
                "enum": [
                    "concepts", "industry", "events", "dividends", "forecast",
                    "shareholders", "top_holders", "repurchases", "buyback",
                    "margin_trading", "capital_flow", "limit_up", "lhb",
                    "unlocks", "summary", "basic_info", "indicators", "financials"
                ],
                "default": "concepts",
                "description": "基本面分类：concepts/industry=热点题材板块族谱, events/dividends=事件驱动催化与分红派息, forecast=机构盈利预测, shareholders=股东人数与大股东增减持流水, top_holders=十大流通股东(港股为持股5%以上主要股东), repurchases/buyback=股份回购明细(港股), capital_flow=主力资金流向, margin_trading=融资融券, limit_up/lhb=涨停分析与龙虎榜席位, unlocks=限售解禁, summary=公司概要, basic_info=公司基本工商与发行信息, indicators/financials=核心财务与经营指标宽表"
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
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10, "description": "最多返回标的数量（1-100）"},
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
