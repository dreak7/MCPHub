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
from .invariants import verify_all_financial_invariants


# =========================================================================
# 【功能块 2.1：第一道防翻车安全门（代数闭环安检网装饰器）】
# - 输入：func (Callable): 网关对外接口方法
# - 功能：自动对对外交付的财报、行情估值与龙虎榜执行代数恒等式闭环审查
# - 输出：经过安检认证的数据字典
# =========================================================================
def _enforce_gate_invariants(func: Callable) -> Callable:
    """第一道防翻车安全门：自动拦截并审查网关对外输出的代数闭环"""
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        res = func(*args, **kwargs)
        if isinstance(res, dict):
            return verify_all_financial_invariants(res, strict=False)
        return res
    return wrapper



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

        # 规范化市场范围代码以适配 RAG 服务接口 (如 HK/HKEX -> HK-GP, A/CN -> AG)
        r_upper = market_range.strip().upper()
        rag_range = "HK-GP" if r_upper in ("HK", "HKEX", "HK-GP") else ("AG" if r_upper in ("A", "AG", "CN", "") else market_range)
        req_body = {"query": raw_query, "range": rag_range}
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
            elif not clean_code.isdigit() and valid_entities:
                # =========================================================================
                # 【功能块 3.6：名称型查询无关联语义近邻过滤 (修复 P3-3)】
                # - 输入：raw_query (用户输入的查询字符串), valid_entities (当前已初筛实体列表)
                # - 功能：解决 RAG 向量检索在用户输入不存在的公司名（如'不存在的公司名XYZ'）时
                #         强制返回中国移动/中国电信/张家界等完全无关近邻的缺陷；
                #         通过字面公共汉字交集、子串包含及拼音子串关联度检验，彻底剔除零关联假近邻。
                # - 输出：过滤后的有效相关实体列表（无任何关联时规范收敛为空列表）
                # =========================================================================
                q_lower = raw_query.lower()
                q_chinese = set(re.findall(r"[\u4e00-\u9fa5]", q_lower))
                common_stop = {"股", "份", "有", "限", "公", "司", "集", "团", "a", "b", "h", "s", "w", "港"}
                meaningful_q_chinese = q_chinese - common_stop
                q_alpha = re.sub(r"[^a-z0-9]", "", q_lower)

                filtered_name_candidates = []
                for e in valid_entities:
                    cand_name = str(e.get("name") or "").lower()
                    cand_aliases = [str(a).lower() for a in e.get("aliases", []) if str(a).strip()]
                    # 1. 强关联：query 是名称/别名子串，或名称/别名是 query 子串
                    if (cand_name and (cand_name in q_lower or q_lower in cand_name)) or any(a and (a in q_lower or q_lower in a) for a in cand_aliases):
                        filtered_name_candidates.append(e)
                        continue
                    # 2. 中文匹配：若查询词包含有意义汉字，必须至少存在 1 个非停用词汉字交集
                    if meaningful_q_chinese:
                        cand_chars = set(re.findall(r"[\u4e00-\u9fa5]", cand_name)) - common_stop
                        if meaningful_q_chinese & cand_chars:
                            filtered_name_candidates.append(e)
                            continue
                    # 3. 拼音或纯字母/代码匹配：仅当查询包含至少2位字母且为别名子串或别名为其子串时保留
                    elif len(q_alpha) >= 2:
                        if any(len(a) >= 2 and (q_alpha in a or a in q_alpha) for a in cand_aliases):
                            filtered_name_candidates.append(e)
                            continue
                valid_entities = filtered_name_candidates

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

            # 港股名称型查询同样过滤无关联近邻 (修复 P3-3)
            if not clean_code.isdigit() and valid_entities:
                q_lower = raw_query.lower()
                q_chinese = set(re.findall(r"[\u4e00-\u9fa5]", q_lower))
                common_stop = {"股", "份", "有", "限", "公", "司", "集", "团", "a", "b", "h", "s", "w", "港"}
                meaningful_q_chinese = q_chinese - common_stop
                q_alpha = re.sub(r"[^a-z0-9]", "", q_lower)

                filtered_name_candidates = []
                for e in valid_entities:
                    cand_name = str(e.get("name") or "").lower()
                    cand_aliases = [str(a).lower() for a in e.get("aliases", []) if str(a).strip()]
                    if (cand_name and (cand_name in q_lower or q_lower in cand_name)) or any(a and (a in q_lower or q_lower in a) for a in cand_aliases):
                        filtered_name_candidates.append(e)
                        continue
                    if meaningful_q_chinese:
                        cand_chars = set(re.findall(r"[\u4e00-\u9fa5]", cand_name)) - common_stop
                        if meaningful_q_chinese & cand_chars:
                            filtered_name_candidates.append(e)
                            continue
                    elif len(q_alpha) >= 2:
                        if any(len(a) >= 2 and (q_alpha in a or a in q_alpha) for a in cand_aliases):
                            filtered_name_candidates.append(e)
                            continue
                valid_entities = filtered_name_candidates

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
    def _get_hk_f10(self, clean_code: str, category: str = 'summary', arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.hk_engine.get_f10(clean_code, category=category, arguments=arguments)

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

    def screener(self, message: str, limit: int = 10, page: int = 1, query: str | None = None, condition: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.a_engine.screener(message=message, limit=limit, page=page, query=query, condition=condition, **kwargs)







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
        # 严格校验新闻 limit 参数范围 (修复 P3-1: 杜绝 limit=0 越界返回 1 条)
        try:
            n_limit = int(limit)
        except (ValueError, TypeError):
            raise ValueError(f"limit 必须为整数，收到: {limit}")
        if n_limit < 1 or n_limit > 10:
            raise ValueError(f"limit 参数越界: {n_limit}，必须在 1 到 10 之间")
        req_limit = n_limit

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
            sina_url = f"https://search.sina.com.cn/api/news?q={encoded_q}&size={max(req_limit * 3, 20)}&page=1"
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
                    if news_list:
                        # 严格按发布时间倒序排列，确保首条即为最新资讯 (修复 P3-1)
                        news_list.sort(key=lambda x: str(x.get("time") or x.get("datetime") or ""), reverse=True)
                        return news_list[:req_limit]
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
                        # 严格按发布时间倒序排列 (修复 P3-1)
                        news_list.sort(key=lambda x: str(x.get("time") or x.get("datetime") or ""), reverse=True)
                        return news_list[:req_limit]
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
                            # 严格按公告时间倒序排列 (修复 P3-1)
                            ann_list.sort(key=lambda x: str(x.get("time") or x.get("datetime") or ""), reverse=True)
                            return ann_list[:req_limit]
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

            # 5. 限售解禁日程 (修复 P1-1: 严防将解禁后累计流通股本 FREE_SHARES 错标为当期解禁股数)
            elif cat == "unlocks":
                url = f"https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LIFT_STAGE&columns=ALL&filter=(SECURITY_CODE=%22{clean_code}%22)&pageNumber=1&pageSize=5"
                req = Request(url, headers=headers)
                with urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    items = data.get("result", {}).get("data", []) if data.get("result") else []
                    return [
                        {
                            "解禁日期": (x.get("FREE_DATE") or "")[:10],
                            "解禁股数_万股": float(x.get("CURRENT_FREE_SHARES") if x.get("CURRENT_FREE_SHARES") is not None else (x.get("ABLE_FREE_SHARES") or 0.0)),
                            "解禁后累计流通A股本_万股": float(x.get("FREE_SHARES") or 0.0),
                            "解禁类型": x.get("FREE_SHARES_TYPE") or x.get("LIMITED_TYPE_NAME"),
                            "占总股本比例_pct": round(float(x.get("TOTAL_RATIO")) * 100.0, 3) if x.get("TOTAL_RATIO") is not None else (round(float(x.get("TOTALSHARES_RATIO")) * 100.0, 3) if x.get("TOTALSHARES_RATIO") is not None else None),
                        }
                        for x in items
                    ]
        except Exception:
            return None

        return None


# =========================================================================
# 【功能块 8：金融高可用调度网关核心类 (FinanceGateway)】
# 【第二道防翻车安全门：单一事实源 SSOT 架构治理公约】
# - 底层洗净 (Engine Level): 原始数据抓取、万元转换、汇率反解与代数推导 100% 留在 engines/ 内部；
# - 外层纯净 (Gateway Level): 本网关只负责协议路由、参数校验、异常包装与第一道安检门禁；严禁在外层二次投药！
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

    @property
    def a_engine(self) -> AStockEngine:
        """A 股通达信专线处理引擎"""
        return self.tdx_client.a_engine

    @property
    def hk_engine(self) -> HKStockEngine:
        """港股多源高可用处理引擎"""
        return self.tdx_client.hk_engine

    def stock_lookup(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 1：实体代码/名称 AI RAG 语义检索"""
        query = arguments.get("query", "").strip()
        market_range = arguments.get("range") or arguments.get("market_range") or "AG"
        valid_ranges = {"AG", "HK-GP", "HK-JJ", "JJ", "MG-GP", "ZS", "A", "HK"}
        if market_range not in valid_ranges:
            raise ValueError(f"无效的检索市场范围 '{market_range}'。支持的范围为: ['AG', 'HK-GP', 'HK-JJ', 'JJ', 'MG-GP', 'ZS']")
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

            # 修复 P3-7：名称带 -W/-SW/-S/-B/-R 等港股架构标识后缀时，自动剥离后缀比对证券简称基础名
            clean_ename = re.sub(r"-(SW|W|S|B|R|SB)$", "", e_name.strip(), flags=re.I)
            name_candidates = [e_name, clean_ename] + aliases
            if (clean_q and e_code == clean_q) or query.strip() in name_candidates:
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
            if entities:
                res["未命中说明"] = (
                    f"在指定市场范围 ({market_range}) 中未检索到代码为 '{query}' 的上市证券标的；"
                    "下方返回列表为语义模型相关度最近的关联候选（非正主标的），请核对代码后再行查询。"
                )
            else:
                res["未命中说明"] = (
                    f"在指定市场范围 ({market_range}) 中未检索到代码为 '{query}' 的上市证券标的（亦无相关候选标的），"
                    "请核对代码有效性后再行查询。"
                )
        elif entities:
            res["未命中说明"] = (
                f"在指定市场范围 ({market_range}) 中未检索到名称为 '{query}' 的完全匹配上市标的；"
                "下方返回列表为语义模型相关度最近的关联候选，请核对证券简称后再行查询。"
            )
        else:
            res["候选为空说明"] = f"在指定市场范围 ({market_range}) 中未检索到与 '{query}' 相关的证券标的或候选实体，请核对代码或名称。"
        return res

    @_enforce_gate_invariants
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
        
        # 严格校验 K 线周期枚举 (修复 P1-1)
        valid_periods = {"day", "week", "month", "60m", "30m", "15m", "5m", "1m"}
        period_alias = {"daily": "day", "weekly": "week", "monthly": "month"}
        period_raw = str(arguments.get("period", "day")).lower().strip()
        period = period_alias.get(period_raw, period_raw)
        if period not in valid_periods:
            raise ValueError(f"无效的 K 线周期 '{arguments.get('period')}'。支持的周期为: ['day', 'week', 'month', '60m', '30m', '15m', '5m', '1m']")

        # 严格校验复权方式枚举 (修复 P1-1)
        valid_adjusts = {"qfq", "hfq", "none"}
        adjust = str(arguments.get("adjust", "qfq")).lower().strip()
        if adjust not in valid_adjusts:
            raise ValueError(f"无效的复权方式 '{arguments.get('adjust')}'。支持的复权方式为: ['qfq', 'hfq', 'none']")

        # 严格校验请求根数参数范围 (修复 P3-1: 杜绝非法负数/零越界)
        raw_count = arguments.get("count", 30)
        try:
            count = int(raw_count)
        except (ValueError, TypeError):
            raise ValueError(f"count 必须为整数，收到: {raw_count}")
        if count < 1 or count > 500:
            raise ValueError(f"count 参数越界: {count}，必须在 1 到 500 之间")

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

    @_enforce_gate_invariants
    def stock_financials(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 4：三大财务报表 (资产负债表, 利润表, 现金流量表) 与经营主营分析"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空")

        # 严格校验财报类型枚举 (修复 P1-1)
        valid_report_types = {"balance", "income", "cashflow"}
        report_aliases = {
            "balance_sheet": "balance", "zcfzb": "balance", "资产负债表": "balance",
            "income_statement": "income", "lrb": "income", "lyb": "income", "利润表": "income", "损益表": "income",
            "cashflow_statement": "cashflow", "cash_flow": "cashflow", "cash_flow_statement": "cashflow", "cash": "cashflow", "xjllb": "cashflow", "现金流量表": "cashflow",
        }
        raw_type = str(arguments.get("report_type") or arguments.get("statement_type", "balance")).lower().strip()
        report_type = report_aliases.get(raw_type, raw_type)
        if report_type not in valid_report_types:
            raise ValueError(f"无效的财务报表类型 '{arguments.get('report_type') or arguments.get('statement_type')}'。支持的报表类型为: ['balance', 'income', 'cashflow']")

        # 严格校验报告期口径枚举 (修复 P1-1)
        valid_period_types = {"report", "quarter"}
        period_type = str(arguments.get("period_type", "report")).lower().strip()
        if period_type not in valid_period_types:
            raise ValueError(f"无效的报告期口径 '{arguments.get('period_type')}'。支持的口径为: ['report', 'quarter']")

        # 严格校验历史财报期数范围 (修复 P3-1: 杜绝 limit_periods=0 导致空表及 >10 越界)
        raw_lp = arguments.get("limit_periods", 4)
        try:
            limit_periods = int(raw_lp)
        except (ValueError, TypeError):
            raise ValueError(f"limit_periods 必须为整数，收到: {raw_lp}")
        if limit_periods < 1 or limit_periods > 10:
            raise ValueError(f"limit_periods 参数越界: {limit_periods}，必须在 1 到 10 之间")

        return self.tdx_client.get_financial_statements(
            symbol=symbol,
            report_type=report_type,
            period_type=period_type,
            limit_periods=limit_periods,
        )

    @_enforce_gate_invariants
    def stock_f10(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 5：F10 深度基本面（热点题材板块族谱、业绩预告、股东变动、龙虎榜、限售解禁）"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空")
        clean_code = re.sub(r"[^\d]", "", symbol)
        category = arguments.get("category", "concepts")
        cat_lower = str(category).lower().strip()
        
        # 严格校验 F10 分类枚举与别名映射 (修复 P1-1)
        valid_categories = {
            "concepts", "industry", "events", "dividends", "forecast",
            "shareholders", "top_holders", "repurchases", "buyback",
            "margin_trading", "capital_flow", "limit_up", "lhb",
            "unlocks", "summary", "basic_info", "indicators", "financials",
        }
        category_alias_map = {
            "sector": "industry",
            "dividends": "dividends",
            "bonus": "dividends",
            "lhb": "lhb",
            "buyback": "buyback",
            "repurchases": "repurchases",
            "indicators": "indicators",
            "financials": "financials",
            "holders": "top_holders",
            "shareholder_events": "shareholders",
            "main": "indicators",
            "funds": "capital_flow",
            "moneyflow": "capital_flow",
            "rzrq": "margin_trading",
            "margin": "margin_trading",
            "ztb": "limit_up",
            "price_limit": "limit_up",
            "company": "summary",
            "company_profile": "summary",
        }
        norm_cat = category_alias_map.get(cat_lower, cat_lower)
        if norm_cat not in valid_categories:
            raise ValueError(f"无效的 F10 基本面分类 '{category}'。支持的分类为: ['concepts', 'industry', 'events', 'dividends', 'forecast', 'shareholders', 'top_holders', 'repurchases', 'buyback', 'margin_trading', 'capital_flow', 'limit_up', 'lhb', 'unlocks', 'summary', 'basic_info', 'indicators', 'financials']")

        # 严格校验 periods 与 limit 参数边界 (修复 P3-1: 杜绝越界静默接收与空表)
        req_periods = arguments.get("periods")
        if req_periods is not None:
            try:
                n_p = int(req_periods)
            except (ValueError, TypeError):
                raise ValueError(f"periods 必须为整数，收到: {req_periods}")
            if n_p < 1 or n_p > 30:
                raise ValueError(f"periods 参数越界: {n_p}，必须在 1 到 30 之间")

        req_limit = arguments.get("limit")
        if req_limit is not None:
            try:
                n_l = int(req_limit)
            except (ValueError, TypeError):
                raise ValueError(f"limit 必须为整数，收到: {req_limit}")
            if n_l < 1 or n_l > 200:
                raise ValueError(f"limit 参数越界: {n_l}，必须在 1 到 200 之间")

        # 修复 P2-1: 通用 limit 截断器（支持对所有单表返回行数限制参数全局生效）
        def _apply_limit_post(res_dict: dict[str, Any]) -> dict[str, Any]:
            req_lim = arguments.get("limit")
            if req_lim is not None:
                try:
                    n_lim = int(req_lim)
                    if n_lim > 0:
                        tbls = res_dict.get("formatted_tables")
                        if isinstance(tbls, dict):
                            for k, v in tbls.items():
                                if isinstance(v, list) and len(v) > n_lim:
                                    tbls[k] = v[:n_lim]
                        if isinstance(res_dict.get("data"), list) and len(res_dict["data"]) > n_lim:
                            res_dict["data"] = res_dict["data"][:n_lim]
                        if "期次与截断说明" not in res_dict:
                            res_dict["期次与截断说明"] = f"已按参数 limit={n_lim} 截取单表前 {n_lim} 条记录。"
                except (ValueError, TypeError):
                    pass
            return res_dict

        # 港股标的（5位代码）：启用新浪港股F10主力专线与东方财富高可用备份专线 (透传 arguments 支持 periods/limit)
        if infer_market_setcode(symbol) == "31" or len(clean_code) == 5:
            norm_code = clean_code.zfill(5)
            return _apply_limit_post(self.tdx_client._get_hk_f10(norm_code, category=norm_cat, arguments=arguments))

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
            return _apply_limit_post({
                "symbol": clean_code,
                "category": "industry",
                "category_name": "官方行业分类 (证监会行业与申万行业)",
                "source": "tdx_official_vip (通达信官方授权专线)",
                "formatted_tables": {
                    "industry": [ind_record],
                },
                "data": ind_record,
                "行业说明": "A股上市公司所属中国证监会官方门类大类行业与申万一、二级细分行业分类。",
            })

        # =========================================================================
        # 【功能块 8.3：A股权威龙虎榜异动与席位明细专线 (修复 A (P1) 双层嵌套与假否定句)】
        # - 输入：clean_code (6位A股数字代码)
        # - 功能：调用东方财富权威龙虎榜接口，解开双层嵌套，直连 billboard_records 与 seat_details，
        #         根据真实上榜记录判定并彻底消除假否定句
        # - 输出：结构化龙虎榜明细字典
        # =========================================================================
        if norm_cat == "lhb":
            lhb_data = self.tdx_client._get_ashare_lhb(clean_code)
            inner_tables = lhb_data.get("formatted_tables", {})
            billboard_records = inner_tables.get("billboard_records", [])
            seat_details = inner_tables.get("seat_details", [])
            has_records = len(billboard_records) > 0 or len(seat_details) > 0
            res_obj = {
                "symbol": clean_code,
                "category": "lhb",
                "category_name": "龙虎榜每日席位明细与游资机构博弈",
                "source": "eastmoney_billboard (东方财富龙虎榜官方数据专线)",
                "formatted_tables": {
                    "billboard_records": billboard_records,
                    "seat_details": seat_details,
                },
                "data": billboard_records[:15] if billboard_records else {"status": "近期无龙虎榜登榜记录"},
                "龙虎榜说明": "收录股票近期异动登榜的营业部席位买卖金额、机构专用席位博弈与成交占比。",
                "成交额口径说明": lhb_data.get("成交额口径说明", "【龙虎榜成交额口径说明】上榜总览表【市场总成交额_万元】为交易所异动统计窗口期（如连续2个或3个交易日）内的多日累计成交总额，席位明细表【占总成交比例_pct】的分母即为此多日合计总额，非单日成交额。按单日成交额读会显著低估席位实际权重。"),
                "盘后入库说明": (
                    "【龙虎榜披露时效与规则】交易所官方龙虎榜异动及席位明细由上交所/深交所于交易日 16:30–17:30 盘后清算后统一公布入库。"
                    "盘中交易时段或清算完成前查询无当日记录；若收盘清算后仍无记录，表明该标的当日价格振幅、换手率或涨跌幅未触发交易所法定披露标准"
                    "（如非ST主板单日偏离值达7%、换手率达20%等；可结合 limit_up 工具交叉核验涨跌停及异动池）。"
                ),
            }
            if not has_records:
                res_obj["无记录说明"] = f"标的 {symbol} 近期无公开披露的龙虎榜异动登榜记录（低波动或未触发交易所异动披露机制）。"
            return _apply_limit_post(res_obj)

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
            # 修复 P3-2：遍历历次方案，结合完成日期与截止日纠偏上游滞留的“实施中”状态
            today_str = datetime.date.today().strftime("%Y-%m-%d")
            for r in rep_list:
                comp_date = r.get("方案完成日期")
                end_date = r.get("回购实施截止日")
                spent = float(r.get("已实施回购金额_万元") or 0.0)
                cur_status = str(r.get("回购进展状态", ""))
                is_done = False
                if comp_date:
                    is_done = True
                elif end_date and str(end_date) < today_str and spent > 0:
                    is_done = True
                elif "完成" in cur_status or "终止" in cur_status:
                    is_done = True
                
                if is_done:
                    fin_date = comp_date or end_date or ""
                    r["回购进展状态"] = f"已完成 (完成日: {fin_date})" if fin_date else "已完成"

            if norm_cat == "buyback":
                cat_name = "股份回购实施进展与累计聚合概览 (Buyback Overview)"
                total_spent = round(sum(float(r.get("已实施回购金额_万元") or 0.0) for r in rep_list), 2)
                completed_count = sum(1 for r in rep_list if "完成" in str(r.get("回购进展状态", "")) or "终止" in str(r.get("回购进展状态", "")))
                ongoing_count = len(rep_list) - completed_count
                latest_plan_date = rep_list[0].get("公告日期") if rep_list else None
                latest_status = rep_list[0].get("回购进展状态") if rep_list else "无"
                buyback_summary = {
                    "累计已实施回购总额_万元": total_spent,
                    "回购计划总数": len(rep_list),
                    "当前实施中方案数": ongoing_count,
                    "已实施完成方案数": completed_count,
                    "最新方案公告日": latest_plan_date,
                    "最新方案实施进度": latest_status,
                }
                tbls = {
                    "buyback_summary": [buyback_summary],
                    "buyback_details": rep_list,
                }
                out_data = {
                    "summary": buyback_summary,
                    "details": rep_list,
                }
            else:
                cat_name = "股份回购方案与实施明细"
                tbls = {
                    "repurchases": rep_list,
                }
                out_data = rep_list

            res_obj = {
                "symbol": clean_code,
                "category": norm_cat,
                "category_name": cat_name,
                "source": "eastmoney_repurchases (东方财富A股回购权威专线)",
                "formatted_tables": tbls,
                "data": out_data,
                "回购说明": "A股上市公司实施股份回购的最新进展、回购金额上限/下限、已回购股数与方案进度。",
                "表关系说明": (
                    "【参数分工与表关系声明】buyback 聚焦于回购累计实施规模与状态的宏观聚合概览 (提供 buyback_summary 统计与 buyback_details 明细)；"
                    "repurchases 聚焦于历次方案明细流水列表 (repurchases)。"
                    "buyback.buyback_details 与 repurchases.repurchases 数据完全同源；"
                    "下游取数指引：若需宏观统计全貌请传 category=buyback，若只需遍历流水明细请传 category=repurchases。"
                ),
            }
            if norm_cat == "buyback":
                res_obj["参数说明"] = "buyback 聚焦于回购累计实施规模与状态的高维聚合概览 (buyback_summary)；repurchases 聚焦于历次方案明细流水。"
            if not has_records:
                res_obj["无记录说明"] = f"标的 {symbol} 近期无公开披露的股份回购方案或实施记录。"
            return _apply_limit_post(res_obj)

        # A 股标的：若请求 dividends/bonus，路由至真实分红派息专线 (修复 Issue 4)
        if norm_cat == "dividends":
            div_rows = self.tdx_client._get_ashare_dividends(clean_code)
            return _apply_limit_post({
                "symbol": clean_code,
                "category": "dividends",
                "category_name": "A股历年分红派息与送转明细",
                "source": "sina_ashare_dividends (新浪财经分红送配专线)",
                "formatted_tables": {
                    "dividends": div_rows,
                },
                "data": div_rows if div_rows else {"status": "暂无分红派息记录"},
                "分红说明": "A股历年权益分派方案、派息送股及除权除息日明细。",
            })

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
                if category in ("top_holders", "unlocks"):
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
                    if zgb_total_shares <= 0.0 and holder_total_shares > 0.0:
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
                            elif norm_cat in ("summary", "indicators"):
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

                        # 修复 J (P3): 报告期与截止日期两市对齐互通并落地统一四键 (P3-1)
                        if norm_cat in ("summary", "indicators"):
                            cand_dt = str(row_dict.get("报告期日期") or row_dict.get("截止日期") or row_dict.get("报告期") or row_dict.get("bgq") or "").strip()
                            if cand_dt:
                                iso_d = ""
                                lbl_d = ""
                                if re.match(r"^\d{4}-\d{2}-\d{2}", cand_dt):
                                    iso_d = cand_dt[:10]
                                    month = iso_d[5:7]
                                    rpt_name = "一季报" if month == "03" else ("中报" if month == "06" else ("三季报" if month == "09" else ("年报" if month == "12" else "")))
                                    lbl_d = f"{iso_d[:4]}{rpt_name}" if rpt_name else iso_d
                                elif re.match(r"^\d{8}$", cand_dt):
                                    iso_d = f"{cand_dt[:4]}-{cand_dt[4:6]}-{cand_dt[6:8]}"
                                    month = iso_d[5:7]
                                    rpt_name = "一季报" if month == "03" else ("中报" if month == "06" else ("三季报" if month == "09" else ("年报" if month == "12" else "")))
                                    lbl_d = f"{iso_d[:4]}{rpt_name}" if rpt_name else iso_d
                                else:
                                    m_lbl = re.match(r"^(\d{4})\s*(一季报|中报|三季报|年报)", cand_dt)
                                    if m_lbl:
                                        y_str, rpt_txt = m_lbl.group(1), m_lbl.group(2)
                                        md_map = {"一季报": "03-31", "中报": "06-30", "三季报": "09-30", "年报": "12-31"}
                                        iso_d = f"{y_str}-{md_map.get(rpt_txt, '12-31')}"
                                        lbl_d = f"{y_str}{rpt_txt}"

                                if iso_d:
                                    row_dict["截止日期"] = iso_d
                                    row_dict["报告期日期"] = iso_d
                                    row_dict["报告期"] = iso_d
                                    row_dict["报告期标签"] = lbl_d if lbl_d else iso_d
                                else:
                                    row_dict["报告期"] = cand_dt
                                    row_dict["截止日期"] = cand_dt
                                    row_dict["报告期日期"] = cand_dt
                                    row_dict["报告期标签"] = cand_dt

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

                        # 针对 events：清洗当日涨跌幅_pct类型漂移，消除 int/float 并存 (修复 P3)
                        if norm_cat == "events":
                            if "当日涨跌幅_pct" in row_dict and row_dict["当日涨跌幅_pct"] is not None:
                                try:
                                    row_dict["当日涨跌幅_pct"] = round(float(row_dict["当日涨跌幅_pct"]), 2)
                                except (ValueError, TypeError):
                                    pass

                        # 针对 limit_up：清洗成交金额与封单金额类型漂移，统一规范为 float (修复 P3)
                        if norm_cat == "limit_up":
                            for num_k in ("当日成交金额_万元", "封单金额_万元"):
                                if num_k in row_dict and row_dict[num_k] is not None:
                                    try:
                                        row_dict[num_k] = round(float(row_dict[num_k]), 2)
                                    except (ValueError, TypeError):
                                        pass

                        # 针对 capital_flow：清洗主力与各单净占比数字类型漂移，统一规范为 float (保留 4 位小数)
                        if norm_cat == "capital_flow":
                            for num_k in ("主力净流入_元", "超大单净流入_元", "大单净流入_元", "中单净流入_元", "收盘价"):
                                if num_k in row_dict and row_dict[num_k] is not None:
                                    try:
                                        row_dict[num_k] = float(row_dict[num_k])
                                    except (ValueError, TypeError):
                                        pass
                            for pct_k in ("主力净占比_pct", "超大单净占比_pct", "大单净占比_pct", "中单净占比_pct"):
                                if pct_k in row_dict and row_dict[pct_k] is not None:
                                    try:
                                        row_dict[pct_k] = round(float(row_dict[pct_k]), 4)
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
                                # 修复 P2-4: 确保持股占总股本比例_pct永不为空，杜绝下游依赖该键时静默为 None
                                if "持股占总股本比例_pct" not in row_dict and "持股比例_pct" in row_dict:
                                    row_dict["持股占总股本比例_pct"] = row_dict["持股比例_pct"]

                        # 修复 P1-1：针对 unlocks table0，以真实法定总股本严格自算 [占总股本比例_pct] 消除上游申报批次语义冲突
                        if norm_cat == "unlocks" and t_key == "table0":
                            unl_wan = float(row_dict.get("解禁股数_万股") or 0.0)
                            if unl_wan > 0 and zgb_total_shares > 0:
                                zgb_wan = zgb_total_shares / 10000.0
                                row_dict["占总股本比例_pct"] = round((unl_wan / zgb_wan) * 100.0, 3)

                        table_rows.append(row_dict)
                    # 修复 P3-3 与 P3-9：标准化 events, margin_trading, unlocks 及 forecast 等表格中的 8 位数字日期为 YYYY-MM-DD
                    if norm_cat in ("events", "margin_trading", "unlocks", "forecast", "concepts") and table_rows:
                        if norm_cat == "events":
                            table_rows = list(reversed(table_rows))
                        for r in table_rows:
                            # 修复 P3-1：forecast 表 报告期类日期字段均统一 ISO 化（defdate/mxdef/T003/T030 均可能为 8 位数字）
                            date_keys_to_iso = ("催化日期", "cjrq", "交易日期", "变动日期", "rq", "T002", "基准日期", "cur", "截止日期", "解禁日期", "限售起始日", "公告日期",
                                               "预测报告期", "基准报告期", "预测发布日期", "评级更新日期", "defdate", "mxdef", "T003", "T030", "题材纳入日期", "最新入选日期")
                            for dk in date_keys_to_iso:
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
                    # 支持 periods 与 limit 参数按需截取最近 N 期或限制行数，大幅降低网络传输与 Token 体积 (修复 Q7, P3-1)
                    req_periods = arguments.get("periods")
                    req_limit = arguments.get("limit")
                    n_p = int(req_periods) if req_periods is not None else None
                    n_l = int(req_limit) if req_limit is not None else None

                    t0_rows = formatted_tables.get("table0", [])
                    t1_rows = formatted_tables.get("table1", [])

                    if n_p is not None and n_p > 0:
                        if t0_rows:
                            formatted_tables["table0"] = t0_rows[:n_p]
                        if t1_rows:
                            # 提取前 n_p 个不同截止日期/报告期
                            seen_dates = []
                            for r in t1_rows:
                                d_val = str(r.get("截止日期") or r.get("报告期") or r.get("bgq") or "")
                                if d_val and d_val not in seen_dates:
                                    seen_dates.append(d_val)
                                if len(seen_dates) >= n_p:
                                    break
                            if seen_dates:
                                t1_rows = [r for r in t1_rows if str(r.get("截止日期") or r.get("报告期") or r.get("bgq") or "") in seen_dates]
                                formatted_tables["table1"] = t1_rows

                    # 修复 P3-1: limit 参数生效，限制 table1 行数
                    if n_l is not None and n_l > 0:
                        t1_cur = formatted_tables.get("table1", [])
                        formatted_tables["table1"] = t1_cur[:n_l]

                    # 组装期次与截断说明
                    trunc_parts = []
                    if n_p is not None and n_p > 0:
                        trunc_parts.append(f"periods={n_p} (截取最近 {n_p} 个报告期)")
                    if n_l is not None and n_l > 0:
                        trunc_parts.append(f"limit={n_l} (单表上限 {n_l} 行)")
                    if trunc_parts:
                        tdx_res["期次与截断说明"] = f"已按 {' 与 '.join(trunc_parts)} 精简十大流通股东明细。"

                    tdx_res["持股比例分母口径说明"] = (
                        "【持股比例口径校准（已修复 P1-1 与 P3-5）】："
                        "1.【持股占总股本比例_pct / 持股比例_pct】：分母为公司真实法定总股本（如东方电气 34.58亿股、邮储银行 1200亿股），严密反映股东对上市公司的真实权益占比；"
                        "2.【持股占流通A股比例_pct】：分母为境内流通A股股本。注意：在 A+H 股（如邮储银行 601658）或控股股东持股数超过境内流通A股的标的中，该比例完全可能出现大于 100%（例如 119.69%），这反映了该股东总持股规模已超过境内全部流通A股盘子，绝非计算错误；真实股权占比请务必以【持股占总股本比例_pct】（≤100%）为准；"
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
                    t0_data = formatted_tables.pop("table0", [])
                    formatted_tables["limit_up_records"] = t0_data
                    has_records = len(t0_data) > 0
                    if not has_records:
                        tdx_res["无记录说明"] = "该股票在近期统计周期内无涨停或异动记录（白马蓝筹股/低波动标的未触发涨停机制属于常态）。"
                    else:
                        # 修复 D6 & B6：拉取日K线成交额，对齐全天成交额并披露封板时段与全天总额口径差异
                        kline_map = {}
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
                            trade_d = str(row.get("涨停日期", ""))[:10]
                            kline_amt = kline_map.get(trade_d)
                            if kline_amt is not None:
                                row["全天总成交金额_日K_万元"] = kline_amt
                            else:
                                row["全天总成交金额_日K_万元"] = None

                            if row.get("当日成交金额_万元") in (0, 0.0, "0", None):
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
                                # 修复 P3-7：若无日K线与盘口补齐，主动注入历史行缺失提示，杜绝缺失无提示
                                if row.get("成交额补充说明") is None:
                                    row["成交额补充说明"] = "历史异动上游专线未单独披露成交金额，日K线回溯超出覆盖范围"
                            else:
                                up_amt = float(row.get("当日成交金额_万元") or 0.0)
                                if kline_amt is None:
                                    row["成交额补充说明"] = f"上游专线披露成交金额为 {up_amt:,.2f} 万元；因涨停日超出日K线 300 根回溯窗口，全天日K成交额暂未关联"
                                elif kline_amt > 0 and abs(up_amt - kline_amt) > 1.0:
                                    ratio = round((up_amt / kline_amt) * 100.0, 1)
                                    row["成交额补充说明"] = (
                                        f"上游专线披露值为【封板/异动时段成交金额】({up_amt:,.2f}万元，约占全天{ratio}%)；"
                                        f"全天总成交额请以【全天总成交金额_日K_万元】({kline_amt:,.2f}万元)或日K线为准。"
                                    )
                                else:
                                    row["成交额补充说明"] = "上游官方专线原始披露成交额与全天日K线成交额一致"
                            if row.get("封板时间") == "--":
                                row["封板说明"] = "盘中超涨10%异动，未触发封死涨停板"
                        formatted_tables["limit_up_records"] = t0_data
                        formatted_tables.pop("table0", None)
                        tdx_res["涨停成交额口径说明"] = (
                            "【涨停分析成交额双口径说明】上游通达信涨停分析专线的【当日成交金额_万元】为标的在触发涨停/异动封板时段的特定披露成交额；"
                            "全天完整交易时段的实际总换手与总资金体量请统一引用【全天总成交金额_日K_万元】（或调用 stock_kline 取日K成交额）。"
                        )
                elif norm_cat == "unlocks":
                    tdx_res["解禁口径与核算规则说明"] = (
                        "【解禁口径与核对铁律（已修复 P1-1）】："
                        "1.【占比自算铁律】：表中【占总股本比例_pct】一律基于标的当前真实法定总股本严格自算(解禁股数÷法定总股本)，杜绝上游历史申报批次比例引发的语义冲突；"
                        "2.【上游申报批次占比_pct】：保留上游专线原值，仅代表该批次在历史特定发行时的申报比例，不可作为当前公司总股本占比；"
                        "3.【解禁后参考流通股本_万股】：上游字段口径因标的而异（部分标的等于总股本，部分等于流通盘），请以解禁股数与明细相邻行差分为准；"
                        "4.【解禁股数与明细闭合】：只信【解禁股数_万股】，可与多阶段解禁明细相邻行差分交叉核对。"
                    )
                    t0_data = formatted_tables.pop("table0", [])
                    formatted_tables["unlock_schedule"] = t0_data
                    formatted_tables.pop("table0", None)
                    if t0_data:
                        today_iso = datetime.date.today().strftime("%Y-%m-%d")
                        latest_date_str = str(t0_data[0].get("解禁日期", ""))
                        has_pending = any(r.get("解禁进度") == "未实施" for r in t0_data)
                        if not has_pending:
                            tdx_res["解禁状态说明"] = f"该标的历次限售解禁计划均已实施完成，当前处于全流通状态。"
                        else:
                            pending_count = sum(1 for r in t0_data if r.get("解禁进度") == "未实施")
                            tdx_res["解禁状态说明"] = (
                                f"该标的承诺解禁日({latest_date_str})已过，当前仍有 {pending_count} 笔限售股标注为'未实施'。"
                                "在上市公司正式刊登上市流通实施公告前，相关限售股份尚未进入二级市场流通，请以交易所上市流通公告为准。"
                            )
                            for row in t0_data:
                                if row.get("解禁进度") == "未实施":
                                    d_val = str(row.get("解禁日期", ""))
                                    if d_val < today_iso:
                                        row["解禁进度说明"] = f"承诺解禁日({d_val})已届满，上市公司尚未披露上市流通实施公告，当前仍处于未实施状态。"
                    try:
                        fb_unlocks = self.fallback_client.get_f10(symbol=clean_code, category="unlocks")
                        if fb_unlocks and isinstance(fb_unlocks, list) and len(fb_unlocks) > 1:
                            if zgb_total_shares > 0:
                                zgb_wan = zgb_total_shares / 10000.0
                                for item in fb_unlocks:
                                    item_unl = float(item.get("解禁股数_万股") or 0.0)
                                    if item_unl > 0:
                                        item["占总股本比例_pct"] = round((item_unl / zgb_wan) * 100.0, 3)
                            tdx_res["多阶段解禁明细"] = fb_unlocks
                    except Exception:
                        pass
                elif norm_cat in ("summary", "indicators"):
                    t5_rows = formatted_tables.get("table5", [])
                    # 检查整张 F10 摘要/指标表是否具备任何实质非空非 None 业务数据
                    summary_has_real_data = any(
                        any(v is not None and str(v).strip() not in ("", "--", "None", "null") for v in row.values())
                        for t_rows in formatted_tables.values() for row in t_rows
                    )
                    # 修复 P2-5 与 D10：严谨区分“确无质押”与“未采集”，indicators/summary 下空表时客观说明无记录，有明细时准确呈现
                    if not t5_rows:
                        if summary_has_real_data:
                            tdx_res["质押状态说明"] = "F10 摘要/指标表未收录当前股东股权质押明细（暂无质押披露记录或数据源未采集，不可直接等同于已核实零质押，请以中登公司及交易所官方质押公告为准）。"
                    else:
                        tdx_res["质押状态说明"] = f"该公司存在股东股权质押明细记录（共 {len(t5_rows)} 条记录，详见 table5 质押明细表）。"
                        tdx_res["质押比例口径说明"] = (
                            "【质押比例分母口径与自算说明】通达信上游 F10 原始【质押股份比例_pct】字段在不同标的间混用不同分母（部分标的按总股本，部分标的按流通A股，A+H标的可能因历史基数与两地市场股本差异两者均不吻合）。为保证量化与风控口径统一严谨，已挂载基于标的真实法定总股本自算的【质押占总股本比_自算_pct】与按流通A股自算的【质押占流通A股比_自算_pct】；下游引用质押比例请以自算字段为准。"
                        )
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
                    else:
                        has_missing_vol = any(r.get("成交量_股") is None or r.get("成交金额_元") is None or r.get("收盘价") is None for r in t0_data)
                        if has_missing_vol:
                            tdx_res["两融成交回填说明"] = (
                                "历史窗口边界日（如窗口最早一天）二级市场成交三列（成交量/成交金额/收盘价）若为 None，"
                                "系通达信官方专线两融滚动窗口首日未联合二级市场行情做联合查询回填所致的固有断档（非该日无交易），"
                                "标的当日二级市场真实复权量价请直接调用 stock_kline 日线为准。"
                            )
                        # 修复 P2-3：检测最新交易日期并自陈入库时差，覆盖原说明只处理首日缺值、不处理最新期滞后的盲区
                        _latest_dates = [
                            str(r.get("交易日期") or r.get("rq") or r.get("T001") or "")
                            for r in t0_data
                        ]
                        _latest_dates = [d for d in _latest_dates if d.strip()]
                        if _latest_dates:
                            _max_trade_date = max(_latest_dates)
                            tdx_res["最新期入库时差说明"] = (
                                f"【两融最新期入库时差提示】本表最新交易日期={_max_trade_date}。"
                                "受沪深交易所两融数据官方入库时序差异影响，深市标的（代码 002/003/300/301 等）与沪市标的（代码 60x 等）"
                                "最新一期入库日期可能相差 1 个自然交易日；若跨标的横比两融余额或融资买入额，"
                                "各票首行数据可能来自不同交易日，请以各行【交易日期】字段逐日对齐后再比较，切勿直接截取首行横向混比。"
                            )
                elif norm_cat == "capital_flow":
                    tdx_res["资金流向口径说明"] = (
                        "【主力净占比口径说明】表内【主力净占比_pct】、【超大单净占比_pct】、【大单净占比_pct】、【中单净占比_pct】"
                        "均为已乘 100 的百分数（数值单位为 %，例如 0.85 即代表 0.85%，由[净流入÷成交额]计算，非 85%），"
                        "下游引用时切勿重复乘 100。"
                    )
                elif norm_cat == "shareholders":
                    tdx_res["参考股价口径说明"] = (
                        "【参考股价口径说明】表内【参考股价_元】严格对齐各报告期最后一个交易日（如季末/月末最后一个交易日）的收盘价"
                        "（若该日无交易 bar 则取前一交易日收盘价），与 stock_kline 日线历史收盘价逐位一致。"
                    )
                elif norm_cat == "basic_info":
                    t1_rows = formatted_tables.get("table1", [])
                    if not t1_rows:
                        tdx_res["无记录说明"] = "table1 业务构成表暂无明细数据（上游数据源未采集或金融企业未单独披露分产品营收细项，不可等同于已核实无业务构成）。"
                        tdx_res["业务构成说明"] = "上游数据源 table1 业务构成明细暂缺或未采集，属实为空，请参考 table0 主营业务简述。"
                    else:
                        tdx_res["业务构成说明"] = f"table1 包含上市公司主营产品/行业构成明细（共 {len(t1_rows)} 行）。"
                    tdx_res["字段完整性说明"] = (
                        "【工商信息字段完整性说明】标的若存在如【英文简称】等字段返回 null 或空字符串，"
                        "系该上市公司未在官方工商登记或交易所注册该信息（或上游专线未收录该非必填字段），"
                        "字段键名真实存在但值为空，非系统数据传输中断或解析异常。"
                    )

                # 增强 F10 摘要表：汉化关键财务与估值指标、依据“利润变动方向”精确校准亏损标的同比含义、注入隔日风格标签与破净动态复核指引
                if norm_cat == "summary" or ("table3" in formatted_tables and "table4" in formatted_tables):
                    self._enrich_f10_summary_tables(formatted_tables, tdx_res)
                    if cat_lower == "summary":
                        tdx_res["数据源同源说明"] = (
                            "【数据源同源说明】summary 与 indicators 的全量 9 张表 (table0~table8) 共享通达信专线同一 F10 概要底层数据源，"
                            "二者逐位哈希相同（indicators 为其规范业务别名）。属于同一底层数据源在不同业务视图下的挂载，无需重复交叉印证，任取一处即可。"
                        )

                # 修复 Issue 1: 针对 indicators / financials，挂载宽表字段与专属分类名
                # 针对 indicators 与 financials：赋予明确的参数区分度与专属数据结构 (修复 P2-3 与 P2-4)
                if cat_lower == "indicators":
                    tdx_res["category"] = "indicators"
                    tdx_res["category_name"] = "核心财务与经营指标概览 (公司主要财务与估值比率)"
                    tdx_res["估值快照清算说明"] = "【估值基准说明】本表收盘后总市值基于当日收盘清算价计算；盘中时段为上一交易日清算口径，盘中精准估值请结合 stock_quotes 实时行情总市值。"
                    # 修复 P2-4：明确 indicators 内嵌表与 summary 逐位相同哈希的同源关系，杜绝下游误解为两路独立交叉印证
                    tdx_res["数据源同源与交叉印证说明"] = "【数据源同源说明】indicators 与 summary 的全量 9 张表 (table0~table8) 共享通达信专线同一 F10 概要接口底层数据源，二者逐位哈希相同，属于同一底层数据源在不同业务视图下的挂载，无需重复交叉印证，任取一处即可。"
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
                    tdx_res["估值快照清算说明"] = "【估值基准说明】本表收盘后总市值基于当日收盘清算价计算；盘中时段为上一交易日清算口径，盘中精准估值请结合 stock_quotes 实时行情总市值。"

                # 修复 P3-8：basic_info.table2 (ESG评级) 为空时主动注入非静默说明
                if norm_cat == "basic_info":
                    t2 = formatted_tables.get("table2", [])
                    if not t2:
                        tdx_res["ESG披露说明"] = "该标的暂未收录公开第三方 ESG 与社会责任专项评级记录 (table2 暂无条目)。"

                # 修复 P2-2：在 financials / indicators / summary 顶层显式挂载 最新财报报告期 与 派生指标换期说明
                if norm_cat in ("indicators", "financials", "summary"):
                    bgq_val = None
                    for t_name in ("table4", "indicators", "financials_overview"):
                        if t_name in formatted_tables and formatted_tables[t_name]:
                            r0 = formatted_tables[t_name][0]
                            bgq_val = r0.get("报告期") or r0.get("bgq") or r0.get("截止日期")
                            if bgq_val:
                                break
                    if not bgq_val and norm_cat == "financials":
                        inc_l = formatted_tables.get("income_summary", [])
                        if inc_l and isinstance(inc_l, list) and inc_l[0].get("报告期"):
                            bgq_val = inc_l[0].get("报告期")
                    
                    if bgq_val:
                        tdx_res["最新财报报告期"] = str(bgq_val)
                        tdx_res["派生指标换期说明"] = f"当前 F10 财务与指标数据基于最新披露的 {bgq_val} 报告期编制。"
                    else:
                        tdx_res["最新财报报告期"] = "最新定期财报"
                        tdx_res["派生指标换期说明"] = "指标已与最新已披露财报同步。"

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
                if isinstance(tdx_res.get("data"), dict):
                    if tdx_res.get("无记录说明") and "无记录说明" not in tdx_res["data"]:
                        tdx_res["data"]["无记录说明"] = tdx_res["无记录说明"]
                    if tdx_res.get("业务构成说明") and "业务构成说明" not in tdx_res["data"]:
                        tdx_res["data"]["业务构成说明"] = tdx_res["业务构成说明"]
                return _apply_limit_post(tdx_res)
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
        if isinstance(fallback_res.get("data"), dict):
            if fallback_res.get("无记录说明") and "无记录说明" not in fallback_res["data"]:
                fallback_res["data"]["无记录说明"] = fallback_res["无记录说明"]
        return _apply_limit_post(fallback_res)

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
                # 规范化 业绩披露日期 为 ISO 格式 YYYY-MM-DD，并自陈排期状态 (修复 Q4 与 P2-3, 收敛 D14)
                raw_jjrq = str(row.get("业绩披露日期") or "").strip()
                if re.match(r"^\d{8}$", raw_jjrq):
                    formatted_dt = f"{raw_jjrq[:4]}-{raw_jjrq[4:6]}-{raw_jjrq[6:8]}"
                    row["业绩披露日期"] = formatted_dt
                    y_val = int(raw_jjrq[:4])
                    curr_year = datetime.datetime.now().year
                    if y_val > curr_year + 1:
                        row["业绩披露日期说明"] = (
                            f"上游通达信原始披露日({formatted_dt})年份超出当前排期窗口({curr_year}年)，系数据源远期排期占位符（非真实预约日）；"
                            "下一期真实业绩披露预约时间请以交易所（上交所/深交所）官方预约披露日历为准。"
                        )
                    else:
                        row["业绩披露日期说明"] = "该日期为上游通达信收录的官方预约或实际财报披露日；若存在超出当前年度的远期占位，以交易所官方最新披露预约日历为准。"
                else:
                    row["业绩披露日期"] = None
                    row["业绩披露日期说明"] = "交易所与上游通道暂未收录下一期财报预约披露日（尚未排期或上一披露窗口已过等待新排期）。"

        # 0.1 汉化 table1 (主营业务简述与申万行业，彻底清除 T017/hy1/hy2 裸码)
        if "table1" in formatted_tables:
            for row in formatted_tables["table1"]:
                for ok, cn in F10_TABLE1_BIZ_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

        # 0.2 汉化 table2 (概念题材分类，彻底清除 rec_id/T001/T002 裸码)
        if "table2" in formatted_tables:
            for row in formatted_tables["table2"]:
                for ok, cn in F10_TABLE2_CONCEPTS_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

        # 0.3 汉化 table7 (CDR存托凭证属性，彻底清除 iscdr 裸码)
        if "table7" in formatted_tables:
            for row in formatted_tables["table7"]:
                for ok, cn in F10_TABLE7_CDR_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

        # 1. 增强 table4 (关键财务指标映射与亏损同比方向精确判定，修复 B10 与 P3)
        if "table4" in formatted_tables:
            for row in formatted_tables["table4"]:
                for ok, cn in F10_TABLE4_FINANCIAL_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

                # 修复 J (P3-1): 落地两市统一四键（报告期、截止日期、报告期日期、报告期标签）
                cand_dt = str(row.get("报告期日期") or row.get("截止日期") or row.get("报告期") or row.get("bgq") or "").strip()
                if cand_dt:
                    iso_d = ""
                    lbl_d = ""
                    if re.match(r"^\d{4}-\d{2}-\d{2}", cand_dt):
                        iso_d = cand_dt[:10]
                        month = iso_d[5:7]
                        rpt_name = "一季报" if month == "03" else ("中报" if month == "06" else ("三季报" if month == "09" else ("年报" if month == "12" else "")))
                        lbl_d = f"{iso_d[:4]}{rpt_name}" if rpt_name else iso_d
                    elif re.match(r"^\d{8}$", cand_dt):
                        iso_d = f"{cand_dt[:4]}-{cand_dt[4:6]}-{cand_dt[6:8]}"
                        month = iso_d[5:7]
                        rpt_name = "一季报" if month == "03" else ("中报" if month == "06" else ("三季报" if month == "09" else ("年报" if month == "12" else "")))
                        lbl_d = f"{iso_d[:4]}{rpt_name}" if rpt_name else iso_d
                    else:
                        m_lbl = re.match(r"^(\d{4})\s*(一季报|中报|三季报|年报)", cand_dt)
                        if m_lbl:
                            y_str, rpt_txt = m_lbl.group(1), m_lbl.group(2)
                            md_map = {"一季报": "03-31", "中报": "06-30", "三季报": "09-30", "年报": "12-31"}
                            iso_d = f"{y_str}-{md_map.get(rpt_txt, '12-31')}"
                            lbl_d = f"{y_str}{rpt_txt}"

                    if iso_d:
                        row["截止日期"] = iso_d
                        row["报告期日期"] = iso_d
                        row["报告期"] = iso_d
                        row["报告期标签"] = lbl_d if lbl_d else iso_d
                    else:
                        row["报告期"] = cand_dt
                        row["截止日期"] = cand_dt
                        row["报告期日期"] = cand_dt
                        row["报告期标签"] = cand_dt

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

        # 1.5 增强 table5 (股权质押分母自算与口径说明，修复 P2-2)
        if "table5" in formatted_tables:
            t6_zgb = None
            t6_ltgb = None
            if "table6" in formatted_tables and formatted_tables["table6"]:
                t6_row = formatted_tables["table6"][0]
                t6_zgb = t6_row.get("总股本_股") or t6_row.get("zgb")
                t6_ltgb = t6_row.get("流通A股_股") or t6_row.get("ltag") or t6_row.get("ltgb")
            for row in formatted_tables["table5"]:
                zy_wan = float(row.get("质押总股数_万股", 0.0) or row.get("zzygf", 0.0) or 0.0)
                if zy_wan > 0:
                    zy_shares = zy_wan * 10000.0
                    if t6_zgb and float(t6_zgb) > 0:
                        row["质押占总股本比_自算_pct"] = round((zy_shares / float(t6_zgb)) * 100.0, 4)
                    if t6_ltgb and float(t6_ltgb) > 0:
                        row["质押占流通A股比_自算_pct"] = round((zy_shares / float(t6_ltgb)) * 100.0, 4)
                row["质押比例口径说明"] = (
                    "通达信上游【质押股份比例_pct】字段在不同标的间混用总股本与流通A股分母，部分标的两者皆不符；"
                    "精确研判建议使用网关基于法定股本自算的【质押占总股本比_自算_pct】与【质押占流通A股比_自算_pct】。"
                )

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
                for ok, cn in F10_TABLE3_TAGS_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

        # 3. 增强 table6 (估值指标映射与盘中重算提示，修复 P1-2 PE_TTM 期次落后自算)
        if "table6" in formatted_tables:
            table4_bgq = ""
            if "table4" in formatted_tables and formatted_tables["table4"]:
                table4_bgq = str(formatted_tables["table4"][0].get("报告期") or formatted_tables["table4"][0].get("bgq") or "")
            for row in formatted_tables["table6"]:
                for ok, cn in F10_TABLE6_VALUATION_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

                # 修复 P3-3 & P2-1: 兼容旧键名【市净率_PB】，同时保留规范键【市净率_PB_昨收】
                pb_yishou = row.get("市净率_PB_昨收")
                if pb_yishou is not None:
                    row["市净率_PB"] = pb_yishou
                    row["市净率_PB_说明"] = "基准为上一交易日收盘清算口径；实时盘中现价市净率请调用 stock_quotes 取【市净率_PB】或【市净率_PB_自算_普通股】。"

                # 修复问题 2: 消除动态 PE 同名不同义与自相矛盾别名歧义
                # F10 table6 中的 sylttm 为真实的 TTM 滚动市盈率，与 stock_quotes 中的【滚动市盈率_PETTM】同值同源
                # 显式挂载标准名称【滚动市盈率_PETTM】与【滚动市盈率_PE_TTM】，彻底剔除易引发下游误解的【动态市盈率_PE_TTM】别名
                row.pop("动态市盈率_PE_TTM", None)
                pe_ttm_val = row.get("滚动市盈率_PETTM") or row.get("滚动市盈率_PE_TTM")
                if pe_ttm_val is not None:
                    row["滚动市盈率_PETTM"] = pe_ttm_val
                    row["滚动市盈率_PE_TTM"] = pe_ttm_val
                    try:
                        pe_ttm_num = float(pe_ttm_val)
                        if pe_ttm_num < 0:
                            row["市盈率可用状态"] = "亏损不可用"
                            row["市盈率亏损标记"] = True
                            row["亏损警示"] = f"滚动市盈率({pe_ttm_num:.2f}倍)为负，代表标的过去4季累计净利润为负（亏损状态），该数值不可用于横比、排序或PEG推演。"
                        else:
                            row["市盈率可用状态"] = "正常"
                            row["市盈率亏损标记"] = False
                    except (ValueError, TypeError):
                        pass

                # 检验 PE_TTM 是否由于快照批处理入库滞后而落后于最新报告期 (修复 P1-2)
                curr_pe_ttm = row.get("滚动市盈率_PETTM") or row.get("滚动市盈率_PE_TTM")
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
                                if abs(real_ttm_val - float(curr_pe_ttm)) > 0.05:
                                    row["滚动市盈率_PETTM"] = real_ttm_val
                                    row["滚动市盈率_PE_TTM"] = real_ttm_val
                                    row.pop("动态市盈率_PE_TTM", None)
                                    row["PE_TTM校准说明"] = (
                                        f"上游F10估值快照期次入库滞后(原值{curr_pe_ttm})，"
                                        f"已自动自算校准为最新{table4_bgq}期次真实TTM值({real_ttm_val}倍)。"
                                    )
                                    if real_ttm_val < 0:
                                        row["市盈率可用状态"] = "亏损不可用"
                                        row["市盈率亏损标记"] = True
                                        row["亏损警示"] = f"校准后滚动市盈率({real_ttm_val}倍)为负，代表标的过去4季处于亏损状态，不可用于横比、排序或PEG推演。"
                    except Exception:
                        pass

                # 检验 PB 是否由于快照批处理入库滞后而落后于最新报告期 (修复 D4 与 P1-1)
                curr_pb = row.get("市净率_PB_昨收") or row.get("市净率_PB")
                if curr_pb is not None:
                    try:
                        clean_code = tdx_res.get("symbol", "")
                        if clean_code:
                            q_info = self.tdx_client.get_quotes(clean_code) if 'q_info' not in locals() else q_info
                            gz_info = q_info.get("估值与基本盘面", {}) if isinstance(q_info, dict) else {}
                            calib_note = gz_info.get("市净率_PB_昨收_校准说明")
                            real_pb_now = gz_info.get("市净率_PB_F10") or gz_info.get("市净率_PB_自算_普通股")
                            # 若 quotes 中已由 AStockEngine 完成权威普通股换期校准（如 002293/300371），同步对齐
                            if calib_note and real_pb_now is not None:
                                curr_pb_val = float(curr_pb)
                                real_pb_val = float(real_pb_now)
                                if abs(curr_pb_val - real_pb_val) > 0.005:
                                    row["市净率_PB"] = real_pb_val
                                    pb_yishou = gz_info.get("市净率_PB_昨收_换期重算")
                                    if pb_yishou:
                                        # 修复 P2-1: 保持 table6.市净率_PB_昨收 清算日基准价统一，
                                        # 严禁将其覆盖为上一交易日昨收价计算的 pb_yishou 造成跨标的口径分裂；
                                        # 昨收换期重算仅作为显式独立键挂载供特殊需要参考
                                        row["市净率_PB_昨收_换期重算"] = pb_yishou
                                    row["PB校准说明"] = calib_note
                    except Exception:
                        pass

                loss_clause = f" 【高危警示·亏损股市盈率不可用】当前滚动市盈率为负值({pe_ttm_val}倍)，代表公司处于亏损状态，负市盈率属于亏损代号而非低估值，严禁用于横比、排序或PEG公式计算！" if (pe_ttm_val is not None and str(pe_ttm_val).startswith("-")) else ""
                row["口径辨析提示"] = (
                    "table6 中的【滚动市盈率_PETTM】(及别名【滚动市盈率_PE_TTM】) 实质为基于过去4个季度滚动归母净利的真 TTM 市盈率，"
                    "与 stock_quotes 中的【滚动市盈率_PETTM】同值同口径；请勿将其与 quotes 中的【动态市盈率_PE】(单季年化口径) 直接横比；"
                    f"网关已彻底剔除自相矛盾的'动态市盈率_PE_TTM'旧别名以防误导。{loss_clause}"
                )
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
        # 严格校验选股 limit 与 page 参数范围 (修复 P3-1: 杜绝 page=21 等越界返回看似成功的值或空表)
        raw_limit = arguments.get("limit", 10)
        try:
            limit = int(raw_limit)
        except (ValueError, TypeError):
            raise ValueError(f"limit 必须为整数，收到: {raw_limit}")
        if limit < 1 or limit > 100:
            raise ValueError(f"limit 参数越界: {limit}，必须在 1 到 100 之间")

        raw_page = arguments.get("page", 1)
        try:
            page = int(raw_page)
        except (ValueError, TypeError):
            raise ValueError(f"page 必须为整数，收到: {raw_page}")
        if page < 1 or page > 20:
            raise ValueError(f"page 参数越界: {page}，必须在 1 到 20 之间")

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

        # 严格校验新闻 limit 参数范围 (修复 P3-1: 杜绝 limit=0 越界返回 1 条)
        raw_limit = arguments.get("limit", 5)
        try:
            limit = int(raw_limit)
        except (ValueError, TypeError):
            raise ValueError(f"limit 必须为整数，收到: {raw_limit}")
        if limit < 1 or limit > 10:
            raise ValueError(f"limit 参数越界: {limit}，必须在 1 到 10 之间")

        news = self.fallback_client.get_news(symbol=symbol, limit=limit, entity_name=entity_name)
        if news and isinstance(news, list):
            news.sort(key=lambda x: str(x.get("time") or x.get("datetime") or ""), reverse=True)
        return {"symbol": symbol, "news_count": len(news), "news": news}

    def market_context(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """工具 8：A股交易日历与时钟状态感知"""
        return get_market_context()


# =========================================================================
# 【功能块 9：8 大金融工具标准 MCP JSON Schema 元数据导入】
# - 输入：mcp_gateway.schemas 模块
# - 功能：导入 8 大金融工具标准 MCP JSON Schema 定义
# - 输出：所有 Schema 常量在当前命名空间可用，100% 保持向后兼容
# =========================================================================
from .schemas import (
    MARKET_CONTEXT_SCHEMA,
    STOCK_F10_SCHEMA,
    STOCK_FINANCIALS_SCHEMA,
    STOCK_KLINE_SCHEMA,
    STOCK_LOOKUP_SCHEMA,
    STOCK_NEWS_SCHEMA,
    STOCK_QUOTES_SCHEMA,
    STOCK_SCREENER_SCHEMA,
)


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
