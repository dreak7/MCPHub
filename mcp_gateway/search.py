"""Search API clients, response normalization, and fallback policy."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import concurrent.futures
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


class SearchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class ProviderFailure:
    provider: str
    reason: str


# =========================================================================
# 【功能块：安全 HTTP 传输层与重定向拦截处理器】
# - 输入：HTTP 请求对象与重定向目标 URL
# - 功能：严格拦截 POST 请求发生时的任何 301/302/303 重定向跳转，
#         彻底阻断 Authorization 凭证被重定向转发泄露到第三方恶意主机或私网目标
# - 输出：抛出 HTTPError 明确阻断
# =========================================================================
class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise HTTPError(req.full_url, code, f"HTTP redirect ({code}) is disallowed for security", headers, fp)

_SECURE_OPENER = build_opener(NoRedirectHandler)


# =========================================================================
# 【功能块：持久化冷却状态管理器（支持跨月重置自适应与 Key 变更解冻自适应）】
# - 输入：path (Path): 冷却状态持久化 JSON 文件路径
# - 功能：1. 记录并持久化各提供商的冷却到期时间戳、进入冷却时的月份（YYYY-MM）与 API Key 哈希；
#         2. 跨月自动解冻（月初自适应）：检测到跨入新自然月时，自动将上月因配额耗尽沉睡的服务解冻复活；
#         3. 密钥变更自动解冻（Key自适应）：检测到用户在 .env 中更换了 API Key 时，秒级清除冷却复活服务；
#         4. 100% 向后兼容：兼容旧版本仅存 float 时间戳的历史数据格式。
# - 输出：CooldownStore 实例
# =========================================================================
class CooldownStore:
    def __init__(self, path: Path):
        self.path = path
        self._state: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                return {}
            loaded: dict[str, dict[str, Any]] = {}
            for key, val in parsed.items():
                if isinstance(val, (int, float)):
                    loaded[str(key)] = {
                        "until": float(val),
                        "reason_type": "rate_limit",
                        "month": "",
                        "key_hash": ""
                    }
                elif isinstance(val, dict):
                    loaded[str(key)] = {
                        "until": float(val.get("until", 0)),
                        "reason_type": str(val.get("reason_type", "rate_limit")),
                        "month": str(val.get("month", "")),
                        "key_hash": str(val.get("key_hash", ""))
                    }
            return loaded
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            return {}

    def is_available(self, provider_id: str, current_key: str = "") -> bool:
        record = self._state.get(provider_id)
        if not record:
            return True

        until = record.get("until", 0)
        # 1. 自然时间到期：自适应恢复
        if until <= time.time():
            return True

        # 2. 密钥自适应：检测到用户在配置中更换了 API Key，历史冷却失效
        if current_key and record.get("key_hash"):
            current_hash = hashlib.sha256(current_key.encode("utf-8")).hexdigest()[:16]
            if current_hash != record["key_hash"]:
                self.clear(provider_id)
                return True

        # 3. 月初自适应：跨入新的自然月，自动复活配额耗尽的服务（月度免费额度已刷新）
        if record.get("reason_type") == "quota" and record.get("month"):
            current_month = time.strftime("%Y-%m", time.localtime())
            if current_month != record["month"]:
                self.clear(provider_id)
                return True

        return False

    def get_until(self, provider_id: str) -> float:
        record = self._state.get(provider_id)
        if isinstance(record, dict):
            return float(record.get("until", 0))
        if isinstance(record, (int, float)):
            return float(record)
        return 0.0

    def set(
        self,
        provider_id: str,
        seconds: int | float,
        reason_type: str = "rate_limit",
        api_key: str = ""
    ) -> None:
        key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16] if api_key else ""
        current_month = time.strftime("%Y-%m", time.localtime())
        self._state[provider_id] = {
            "until": time.time() + seconds,
            "reason_type": reason_type,
            "month": current_month,
            "key_hash": key_hash
        }
        self._save()

    def clear(self, provider_id: str) -> None:
        self._state.pop(provider_id, None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            disk_state = self._load()
            disk_state.pop(provider_id, None)
            self._state = disk_state
            unique_tmp = self.path.parent / f"{self.path.stem}_{os.getpid()}_{time.time_ns()}.tmp"
            unique_tmp.write_text(json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8")
            unique_tmp.replace(self.path)
        except OSError:
            pass

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            disk_state = self._load()
            merged = {**disk_state, **self._state}
            self._state = merged
            unique_tmp = self.path.parent / f"{self.path.stem}_{os.getpid()}_{time.time_ns()}.tmp"
            unique_tmp.write_text(json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8")
            unique_tmp.replace(self.path)
        except OSError:
            pass


class SearchGateway:
    def __init__(
        self,
        definition: dict[str, Any],
        state_path: Path,
        logger: logging.Logger,
        cooldown_store: CooldownStore | None = None
    ):
        self.definition = definition
        self.logger = logger
        self.cooldowns = cooldown_store if cooldown_store is not None else CooldownStore(state_path)

    def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise SearchError("query must be a non-empty string", retryable=False)
        max_results = arguments.get("max_results", 10)
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 20:
            raise SearchError("max_results must be an integer between 1 and 20", retryable=False)

        providers = self.definition.get("providers", [])
        if not isinstance(providers, list):
            raise SearchError("Invalid providers configuration", retryable=False)
        enabled_count = sum(1 for p in providers if isinstance(p, dict) and p.get("enabled", False))
        cooldown_seconds = self.definition.get("cooldown_seconds", 3600)
        quota_cooldown_seconds = self.definition.get("quota_cooldown_seconds", 259200)
        timeout = self.definition.get("timeout_seconds", 25)
        failures: list[ProviderFailure] = []
        for provider in sorted(providers, key=lambda item: item.get("priority", 1000)):
            provider_id = provider.get("id", "unknown")
            if not provider.get("enabled", False):
                continue
            key_name = provider.get("api_key_env")
            api_key = os.getenv(key_name, "") if isinstance(key_name, str) else ""
            engine_type = provider.get("type") or provider_id
            # AnySearch and Jina AI explicitly support an anonymous quota; all others require a key.
            if not api_key and engine_type not in ("anysearch", "jina"):
                failures.append(ProviderFailure(provider_id, "API key is not configured"))
                continue
            if not self.cooldowns.is_available(self._cooldown_id(provider_id), current_key=api_key):
                failures.append(ProviderFailure(provider_id, "provider is in cooldown"))
                continue
            try:
                result = self._call(provider, query.strip(), max_results, arguments, api_key, timeout)
                response: dict[str, Any] = {
                    "provider": provider_id,
                    "query": query.strip(),
                    "results": result,
                    "provider_failures": [{"provider": item.provider, "reason": item.reason} for item in failures],
                }
                warning = self._format_warning(failures, all_exhausted=False)
                if warning:
                    response["warning"] = warning
                return response
            except SearchError as exc:
                self.logger.warning("Provider %s failed: %s", provider_id, exc)
                failures.append(ProviderFailure(provider_id, str(exc)))
                if exc.retryable:
                    is_quota = self._is_quota_exhausted(exc)
                    cd = quota_cooldown_seconds if is_quota else cooldown_seconds
                    reason_type = "quota" if is_quota else "rate_limit"
                    self.cooldowns.set(self._cooldown_id(provider_id), cd, reason_type=reason_type, api_key=api_key)

        detail = "; ".join(f"{item.provider}: {item.reason}" for item in failures) or "No provider enabled"
        all_quota = (len(failures) >= enabled_count and enabled_count > 0 and self._is_all_quota(failures))
        auth_warning = self._format_warning(failures, all_exhausted=all_quota)
        extra = f" {auth_warning}" if auth_warning else ""
        raise SearchError(f"No search provider was available ({detail}).{extra}", retryable=False)

    # =========================================================================
    # 【功能块：多源并发聚合网页搜索（Multi-Source Concurrent Search）】
    # - 输入：arguments (dict): 包含以下检索参数：
    #         - query (str, 必填): 检索关键词
    #         - max_results (int, 可选, 默认10, 范围1-20): 最终聚合返回的最大条目数
    #         - search_depth (str, 可选): basic 或 advanced
    #         - time_range (str, 可选): 时间跨度 (day, week, month, year)
    #         - include_domains (list[str], 可选): 包含的域名白名单
    #         - exclude_domains (list[str], 可选): 排除的域名黑名单
    # - 功能：并发（ThreadPoolExecutor）同时调用所有已启用且处于就绪状态的搜索引擎
    #         （Exa, AnySearch, Tavily, Jina, Bocha 等）；各引擎独立容错，部分失败不影响
    #         全局；对搜集到的多路结果进行 URL 规范化清洗、智能去重、多源印证合并（打标
    #         sources 列表并优先置顶多源认可的权威网页），最后截取 top N 结果返回。
    # - 输出：dict 结构化结果：
    #         - tool: "multi_web_search"
    #         - query: 原始检索词
    #         - active_providers: 成功贡献搜索结果的引擎列表
    #         - total_sources_queried: 实际发起并发请求的引擎总数
    #         - results: 融合去重后的标准化搜索条目列表（每条含 sources 来源）
    #         - provider_failures: 故障引擎及失败原因清单
    #         - warning: 如有引擎配额或认证异常，提供醒目的前端告警提示
    # =========================================================================
    def multi_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise SearchError("query must be a non-empty string", retryable=False)
        max_results = arguments.get("max_results", 10)
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 20:
            raise SearchError("max_results must be an integer between 1 and 20", retryable=False)

        providers = self.definition.get("providers", [])
        if not isinstance(providers, list):
            raise SearchError("Invalid providers configuration", retryable=False)
        enabled_count = sum(1 for p in providers if isinstance(p, dict) and p.get("enabled", False))
        cooldown_seconds = self.definition.get("cooldown_seconds", 3600)
        quota_cooldown_seconds = self.definition.get("quota_cooldown_seconds", 259200)
        timeout = self.definition.get("timeout_seconds", 25)

        failures: list[ProviderFailure] = []
        ready_providers: list[tuple[dict[str, Any], str]] = []

        for provider in sorted(providers, key=lambda item: item.get("priority", 1000)):
            provider_id = provider.get("id", "unknown")
            if not provider.get("enabled", False):
                continue
            key_name = provider.get("api_key_env")
            api_key = os.getenv(key_name, "") if isinstance(key_name, str) else ""
            engine_type = provider.get("type") or provider_id
            if not api_key and engine_type not in ("anysearch", "jina"):
                failures.append(ProviderFailure(provider_id, "API key is not configured"))
                continue
            if not self.cooldowns.is_available(self._cooldown_id(provider_id), current_key=api_key):
                failures.append(ProviderFailure(provider_id, "provider is in cooldown"))
                continue
            ready_providers.append((provider, api_key))

        if not ready_providers:
            detail = "; ".join(f"{item.provider}: {item.reason}" for item in failures) or "No provider enabled"
            all_quota = (len(failures) >= enabled_count and enabled_count > 0 and self._is_all_quota(failures))
            auth_warning = self._format_warning(failures, all_exhausted=all_quota)
            extra = f" {auth_warning}" if auth_warning else ""
            raise SearchError(f"No search provider was available ({detail}).{extra}", retryable=False)

        provider_results: list[tuple[str, list[dict[str, Any]]]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(ready_providers)) as executor:
            future_to_provider = {
                executor.submit(
                    self._call,
                    provider,
                    query.strip(),
                    max_results,
                    arguments,
                    api_key,
                    timeout
                ): (provider.get("id", "unknown"), api_key)
                for provider, api_key in ready_providers
            }
            try:
                for future in concurrent.futures.as_completed(future_to_provider, timeout=float(timeout)):
                    prov_id, prov_key = future_to_provider[future]
                    try:
                        res = future.result()
                        if isinstance(res, list):
                            provider_results.append((prov_id, res))
                    except SearchError as exc:
                        self.logger.warning("Multi-search provider %s failed: %s", prov_id, exc)
                        failures.append(ProviderFailure(prov_id, str(exc)))
                        if exc.retryable:
                            is_quota = self._is_quota_exhausted(exc)
                            cd = quota_cooldown_seconds if is_quota else cooldown_seconds
                            reason_type = "quota" if is_quota else "rate_limit"
                            self.cooldowns.set(self._cooldown_id(prov_id), cd, reason_type=reason_type, api_key=prov_key)
                    except Exception as exc:
                        self.logger.exception("Multi-search provider %s unexpected error: %s", prov_id, exc)
                        failures.append(ProviderFailure(prov_id, f"Unexpected error: {exc}"))
            except (concurrent.futures.TimeoutError, TimeoutError):
                self.logger.warning("Multi-search exceeded global timeout of %ss; proceeding with partial results", timeout)
                for future, (p_id, p_key) in future_to_provider.items():
                    if not future.done():
                        future.cancel()
                        failures.append(ProviderFailure(p_id, f"Global timeout of {timeout}s exceeded"))

        if not provider_results:
            detail = "; ".join(f"{item.provider}: {item.reason}" for item in failures) or "All providers failed"
            all_quota = (len(failures) >= enabled_count and enabled_count > 0 and self._is_all_quota(failures))
            auth_warning = self._format_warning(failures, all_exhausted=all_quota)
            extra = f" {auth_warning}" if auth_warning else ""
            raise SearchError(f"No search provider succeeded ({detail}).{extra}", retryable=False)

        # 按引擎优先级对并发搜集到的结果集进行稳定排序（保持 Exa -> AnySearch -> Tavily -> Jina -> Bocha 优先级顺序）
        provider_priority = {p.get("id"): p.get("priority", 1000) for p, _ in ready_providers}
        provider_results.sort(key=lambda item: provider_priority.get(item[0], 1000))

        contributed_providers = [p_id for p_id, items in provider_results if items]
        fused_results = self._fuse_and_deduplicate(provider_results, max_results)

        response: dict[str, Any] = {
            "tool": "multi_web_search",
            "query": query.strip(),
            "active_providers": contributed_providers,
            "total_sources_queried": len(ready_providers),
            "results": fused_results,
            "provider_failures": [{"provider": item.provider, "reason": item.reason} for item in failures],
        }
        all_quota = (
            len(failures) >= enabled_count
            and enabled_count > 0
            and self._is_all_quota(failures)
        )
        warning = self._format_warning(failures, all_exhausted=all_quota)
        if warning:
            response["warning"] = warning
        return response

    # =========================================================================
    # 【功能块：多源结果智能去重与多维融合排序】
    # - 输入：provider_results (list[tuple[str, list[dict[str, Any]]]]):
    #         各引擎原始返回结果列表（格式为 (provider_id, [item, ...])）；
    #         max_results (int): 最终保留的最大结果条数
    # - 功能：1. 对不同引擎返回的 URL 进行归一化清洗（去除追踪参数与末尾斜杠）；
    #         2. 归并相同 URL，保留更长更详实的摘要与有效标题，并在 sources 字段中记录所有推荐该网页的引擎；
    #         3. 强化多源置信度：被多家引擎共同推荐的高信度结果优先排在最前列；
    #         4. 截取前 max_results 条输出。
    # - 输出：list[dict[str, Any]]: 融合去重后的优质搜索结果列表
    # =========================================================================
    @classmethod
    def _fuse_and_deduplicate(
        cls,
        provider_results: list[tuple[str, list[dict[str, Any]]]],
        max_results: int
    ) -> list[dict[str, Any]]:
        merged_by_url: dict[str, dict[str, Any]] = {}
        url_order: list[str] = []

        for provider_id, items in provider_results:
            for item in items:
                raw_url = item.get("url", "")
                if not raw_url:
                    continue
                norm_url = cls._normalize_url(raw_url)
                title = str(item.get("title") or "").strip()
                snippet = str(item.get("snippet") or "").strip()
                published = str(item.get("published_date") or "").strip()

                if norm_url not in merged_by_url:
                    merged_by_url[norm_url] = {
                        "title": title,
                        "url": raw_url,
                        "snippet": snippet,
                        "published_date": published,
                        "sources": [provider_id],
                    }
                    url_order.append(norm_url)
                else:
                    existing = merged_by_url[norm_url]
                    if provider_id not in existing["sources"]:
                        existing["sources"].append(provider_id)
                    if len(title) > len(existing["title"]):
                        existing["title"] = title
                    if len(snippet) > len(existing["snippet"]):
                        existing["snippet"] = snippet
                    if not existing["published_date"] and published:
                        existing["published_date"] = published

        fused = [merged_by_url[u] for u in url_order]
        fused.sort(key=lambda x: len(x["sources"]), reverse=True)
        return fused[:max_results]

    # =========================================================================
    # 【功能块：URL 规范化清洗（过滤跟踪参数与冗余格式）】
    # - 输入：url (str): 待清洗的原始网页链接
    # - 功能：将协议与域名转为小写，去除路径末尾多余斜杠，剥离 utm_*, ref, spm 等常见跟踪参数，
    #         剔除 URL 片段锚点（#hash），确保同一网页在不同搜索引擎的链接能精准匹配去重
    # - 输出：str: 规范化后的纯净 URL 字符串
    # =========================================================================
    @staticmethod
    def _normalize_url(url: str) -> str:
        try:
            parts = urlsplit(url.strip())
            scheme = parts.scheme.lower()
            netloc = parts.netloc.lower()
            path = parts.path
            if path.endswith("/") and len(path) > 1:
                path = path[:-1]
            query = parts.query
            if query:
                ignored_keys = {
                    "fbclid", "gclid", "spm", "yclid", "_hsenc", "_hsmi",
                    "mc_cid", "mc_eid", "igshid", "ref", "source", "feature"
                }
                ignored_prefixes = ("utm_", "ref_")
                filtered_pairs = []
                for pair in query.split("&"):
                    if not pair:
                        continue
                    key = pair.split("=")[0].lower()
                    if key in ignored_keys or any(key.startswith(p) for p in ignored_prefixes):
                        continue
                    filtered_pairs.append(pair)
                query = "&".join(filtered_pairs)
            return urlunsplit((scheme, netloc, path, query, ""))
        except Exception:
            return url.strip().rstrip("/")


    # =========================================================================
    # 【功能块：格式化后端服务故障前端警告（认证 + 配额/限流）】
    # - 输入：failures（列表，包含本次调用过程中出现故障的 ProviderFailure 对象）
    #         all_exhausted（布尔值，是否所有可用后端服务的配额均已耗尽，默认为 False）
    # - 功能：将故障分为两大类别进行识别：
    #         1) 认证类错误（401未授权、403拒绝、未配置API Key、Token无效等）
    #         2) 配额/限流类错误（402额度耗尽、429请求过多、含"quota/额度"关键词等）
    #         当所有服务配额均耗尽时，输出全局耗尽提示；
    #         当部分服务受限或处于递补中时，动态插入实际出问题的服务名与错误信息。
    # - 输出：若存在上述任何错误则返回格式化后的多行警告文本（字符串）；否则返回 None。
    # =========================================================================
    @classmethod
    def _format_warning(cls, failures: list[ProviderFailure], all_exhausted: bool = False) -> str | None:
        auth_tokens = ("401", "api key is not configured", "unauthorized", "invalid token", "invalid api key", "未授权", "认证失败")
        quota_tokens = ("402", "429", "432", "quota", "rate limit", "usage limit", "exceed", "too many", "额度", "余额不足", "频率", "cooldown")
        auth_issues: list[str] = []
        quota_issues: list[str] = []
        for item in failures:
            r = item.reason.lower()
            if any(token in r for token in auth_tokens) or ("403" in r and not any(qt in r for qt in quota_tokens)):
                auth_issues.append(f"{item.provider} ({item.reason})")
            elif any(token in r for token in quota_tokens):
                quota_issues.append(f"{item.provider} ({item.reason})")
        lines: list[str] = []
        if auth_issues:
            joined = "、".join(auth_issues)
            lines.append(f"⚠️ 警告：检测到后端服务认证失败 [{joined}]。请检查 config/.env 中的 API Key 配置，或联系MCP提供商。")
        if quota_issues:
            if all_exhausted:
                lines.append("⚠️  警告：检测到后端服务配额均耗尽，请耐心等待相关配额重置或联系MCP提供商。")
            else:
                joined = "、".join(quota_issues)
                lines.append(f"⚠️ 警告：检测到后端服务配额耗尽或请求限流 [{joined}]。请耐心等待配额重置稍后重试或联系MCP提供商。")
        return "\n".join(lines) if lines else None

    # =========================================================================
    # 【功能块：检查是否所有故障记录均属于配额/限流类错误】
    # - 输入：failures（列表，包含 ProviderFailure 对象）
    # - 功能：遍历所有故障项，校验是否全部为配额超限、频率限制或冷却状态，且无认证错误。
    # - 输出：全部属于配额/限流故障时返回 True，否则返回 False。
    # =========================================================================
    @classmethod
    def _is_all_quota(cls, failures: list[ProviderFailure]) -> bool:
        if not failures:
            return False
        auth_tokens = ("401", "api key is not configured", "unauthorized", "invalid token", "invalid api key", "未授权", "认证失败")
        quota_tokens = ("402", "429", "432", "quota", "rate limit", "usage limit", "exceed", "too many", "额度", "余额不足", "频率", "cooldown")
        for item in failures:
            r = item.reason.lower()
            if any(token in r for token in auth_tokens) or ("403" in r and not any(qt in r for qt in quota_tokens)):
                return False
            if not any(token in r for token in quota_tokens):
                return False
        return True

    def _cooldown_id(self, provider_id: str) -> str:
        cap_name = self.definition.get("name") or self.definition.get("capability_name") or "web_search"
        return f"{cap_name}:{provider_id}"

    # =========================================================================
    # 【功能块：判断异常是否属于上游配额/资产类耗尽】
    # - 输入：exc (SearchError): 发生的异常对象
    # - 功能：判断异常状态码与错误信息是否为真正的配额/余额耗尽（排除普通的 429 短时频率限流）
    # - 输出：bool: 是配额耗尽返回 True，否则返回 False
    # =========================================================================
    @classmethod
    def _is_quota_exhausted(cls, exc: SearchError) -> bool:
        if exc.status in (402, 432):
            return True
        lowered = str(exc).lower()
        quota_tokens = (
            "402", "432", "quota", "usage limit", "insufficient balance",
            "not enough money", "insufficient credit", "out of credit",
            "exceeds api key usage limit", "quota exceeded", "quota exhausted",
            "额度", "余额不足", "用量超限"
        )
        if any(token in lowered for token in quota_tokens) and not ("429" in lowered and "quota" not in lowered and "额度" not in lowered):
            return True
        return False

    # =========================================================================
    # 【功能块：计算故障提供商的分级冷却时长】
    # - 输入：exc (SearchError): 发生的检索/提取异常对象；
    #         default_cooldown (int | float): 常规限流/短时故障冷却时间（秒，默认 3600，即 1 小时）；
    #         quota_cooldown (int | float): 资产/配额耗尽冷却时间（秒，默认 259200，即 3 天）
    # - 功能：精准识别异常是否属于配额耗尽（HTTP 402/432、或包含配额/余额不足/用量超限等关键字）：
    #         若是配额耗尽，返回长达 3 天的 quota_cooldown（避免月度配额耗尽后频繁重试与弹窗骚扰）；
    #         若为普通短时限流（429）或网络波动，返回常规 default_cooldown（1 小时）
    # - 输出：int | float: 应当设置的冷却秒数
    # =========================================================================
    @classmethod
    def _resolve_cooldown_seconds(
        cls,
        exc: SearchError,
        default_cooldown: int | float,
        quota_cooldown: int | float
    ) -> int | float:
        return quota_cooldown if cls._is_quota_exhausted(exc) else default_cooldown

    # =========================================================================
    # 【功能块：服务提供商通信端点（Endpoint）安全合规校验】
    # - 输入：provider_id (str): 提供商标识；endpoint (str): 待校验的接口 URL
    # - 功能：1. 强制协议必须为安全加密的 HTTPS（阻断任何明文 HTTP 请求以防凭据被截获）；
    #         2. 校验 URL 结构合法，禁止携带 username/password 等嵌入式凭证；
    #         3. 严格限制仅允许标准端口（443 或省略端口），拦截针对非标端口与局域网内网服务的探测；
    #         4. 拦截私网/回环地址（如 127.0.0.1、192.168.*.*、10.*.*.*、localhost 等），
    #            以及经 DNS 解析到非全球公网 IP 的目标，阻断配置被篡改后的 API Key 盗取与 SSRF 风险。
    # - 输出：无（若校验失败抛出不可重试的 SearchError）
    # =========================================================================
    @classmethod
    def _validate_endpoint(cls, provider_id: str, endpoint: Any) -> None:
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise SearchError(f"{provider_id}: endpoint must be an HTTPS URL", retryable=False)
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise SearchError(f"{provider_id}: endpoint is not a valid HTTPS URL", retryable=False)
        if parsed.port not in (None, 443):
            raise SearchError(f"{provider_id}: endpoint uses a non-standard port: {parsed.port}", retryable=False)
        hostname = parsed.hostname.lower()
        if hostname in ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"):
            raise SearchError(f"{provider_id}: endpoint resolves to a non-public address: {hostname}", retryable=False)
        try:
            ip = ipaddress.ip_address(hostname)
            if not ip.is_global:
                raise SearchError(f"{provider_id}: endpoint resolves to a non-public address: {hostname}", retryable=False)
            return
        except ValueError:
            pass
        try:
            addresses = {record[4][0] for record in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise SearchError(f"{provider_id}: endpoint hostname cannot be resolved: {hostname}", retryable=False) from exc
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise SearchError(f"{provider_id}: endpoint resolves to a non-public address: {hostname}", retryable=False)

    def _call(self, provider: dict[str, Any], query: str, max_results: int, arguments: dict[str, Any], api_key: str, timeout: Any) -> list[dict[str, Any]]:
        provider_id = provider["id"]
        endpoint = provider.get("endpoint")
        self._validate_endpoint(provider_id, endpoint)
        engine_type = provider.get("type") or provider_id
        if engine_type in ("tavily-search", "tavily"):
            return self._tavily(endpoint, api_key, query, max_results, arguments, timeout)
        if engine_type == "exa":
            return self._exa(endpoint, api_key, query, max_results, arguments, timeout)
        if engine_type == "jina":
            return self._jina(endpoint, api_key, query, max_results, arguments, timeout)
        if engine_type == "anysearch":
            return self._anysearch(endpoint, api_key, query, max_results, provider, timeout)
        if engine_type in ("bocha-search", "bocha"):
            return self._bocha(endpoint, api_key, query, max_results, arguments, timeout)
        raise SearchError(f"Unsupported provider type or id: {engine_type} (id: {provider_id})", retryable=False)

    # =========================================================================
    # 【功能块：Jina AI 网页搜索适配器】
    # - 输入：endpoint (str): Jina 搜索接口地址（例如 https://s.jina.ai）
    #         key (str): Jina API Key（用于 Bearer Token 鉴权）
    #         query (str): 用户检索关键词
    #         limit (int): 返回的最大结果条数
    #         args (dict): 扩展参数字典
    #         timeout (float | int): 网络超时时间（秒）
    # - 功能：向 Jina 搜索服务发送结构化 HTTP 请求，接收并解析其返回的 JSON 列表，
    #         标准化输出为网关统一格式（title, url, snippet, published_date）
    # - 输出：list[dict[str, Any]]: 标准化搜索结果列表
    # =========================================================================
    def _jina(self, endpoint: str, key: str, query: str, limit: int, args: dict[str, Any], timeout: Any) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"q": query}
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = self._post(endpoint, headers, payload, timeout)
        items = data.get("data", [])
        if isinstance(items, dict):
            items = [items]
        elif not isinstance(items, list):
            items = []
        results: list[dict[str, Any]] = []
        for item in items[:limit]:
            if isinstance(item, dict):
                title = item.get("title") or ""
                url = item.get("url") or ""
                snippet = item.get("description") or item.get("content") or ""
                published = item.get("publishedTime") or item.get("date") or ""
                results.append(self._result(title, url, snippet, published))
        return results

    def _tavily(self, endpoint: str, key: str, query: str, limit: int, args: dict[str, Any], timeout: Any) -> list[dict[str, Any]]:
        payload = {"query": query, "max_results": limit, "search_depth": args.get("search_depth", "basic"), "include_answer": False}
        self._add_filters(payload, args)
        data = self._post(endpoint, {"Authorization": f"Bearer {key}"}, payload, timeout)
        return [self._result(item.get("title"), item.get("url"), item.get("content"), item.get("published_date")) for item in data.get("results", [])]

    def _exa(self, endpoint: str, key: str, query: str, limit: int, args: dict[str, Any], timeout: Any) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"query": query, "numResults": limit, "contents": {"highlights": {"highlightsPerUrl": 1, "numSentences": 2}}}
        if args.get("include_domains"):
            payload["includeDomains"] = args["include_domains"]
        if args.get("exclude_domains"):
            payload["excludeDomains"] = args["exclude_domains"]
        data = self._post(endpoint, {"x-api-key": key}, payload, timeout)
        return [self._result(item.get("title"), item.get("url"), self._text(item.get("highlights")) or item.get("text") or item.get("summary"), item.get("publishedDate")) for item in data.get("results", [])]

    def _anysearch(self, endpoint: str, key: str, query: str, limit: int, provider: dict[str, Any], timeout: Any) -> list[dict[str, Any]]:
        # The AnySearch general endpoint infers the best source when tag is absent.
        # Its documented maximum is 10 even though the unified tool allows 20.
        payload: dict[str, Any] = {"query": query, "max_results": min(limit, 10)}
        if provider.get("tag"):
            payload["tag"] = provider["tag"]
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        data = self._post(endpoint, headers, payload, timeout)
        if isinstance(data.get("code"), int) and data["code"] != 0:
            code = data["code"]
            raw_msg = str(data.get("message") or "")
            self.logger.warning("AnySearch business error: code=%s", code)
            is_quota = self._is_quota_message(raw_msg)
            msg = f"AnySearch business error (code {code}): quota exhausted" if is_quota else f"AnySearch business error (code {code})"
            raise SearchError(msg, retryable=is_quota)
        nested = data.get("data")
        items = data.get("results") or (nested.get("results") if isinstance(nested, dict) else nested) or []
        if not isinstance(items, list):
            raise SearchError("AnySearch returned an unrecognized response", retryable=True)
        return [self._result(item.get("title") or item.get("name"), item.get("url") or item.get("link"), item.get("snippet") or item.get("content") or item.get("description"), item.get("published_date") or item.get("publishedAt")) for item in items if isinstance(item, dict)]

    def _bocha(self, endpoint: str, key: str, query: str, limit: int, args: dict[str, Any], timeout: Any) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"query": query, "count": limit, "summary": True, "freshness": self._bocha_freshness(args.get("time_range"))}
        if args.get("include_domains"):
            payload["include"] = "|".join(args["include_domains"])
        if args.get("exclude_domains"):
            payload["exclude"] = "|".join(args["exclude_domains"])
        data = self._post(endpoint, {"Authorization": f"Bearer {key}"}, payload, timeout)
        if data.get("code") not in (None, 200):
            code = data.get("code", "unknown")
            raw_msg = str(data.get("msg") or data.get("message") or "")
            self.logger.warning("Bocha business error: code=%s", code)
            is_quota = self._is_quota_message(raw_msg)
            msg = f"Bocha business error (code {code}): quota exhausted" if is_quota else f"Bocha business error (code {code})"
            raise SearchError(msg, retryable=is_quota)
        items = data.get("data", {}).get("webPages", {}).get("value", [])
        return [self._result(item.get("name"), item.get("url"), item.get("summary") or item.get("snippet"), item.get("datePublished")) for item in items]

    @staticmethod
    def _add_filters(payload: dict[str, Any], args: dict[str, Any]) -> None:
        for key in ("time_range", "include_domains", "exclude_domains"):
            if args.get(key):
                payload[key] = args[key]

    @staticmethod
    def _bocha_freshness(value: Any) -> str:
        return {"day": "oneDay", "week": "oneWeek", "month": "oneMonth", "year": "oneYear"}.get(value, "noLimit")

    @staticmethod
    def _result(title: Any, url: Any, snippet: Any, published: Any) -> dict[str, Any]:
        return {"title": str(title or ""), "url": str(url or ""), "snippet": SearchGateway._text(snippet), "published_date": str(published or "")}

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, list):
            return "\n".join(str(item) for item in value)
        return str(value or "")

    def _post(self, endpoint: str, headers: dict[str, str], payload: dict[str, Any], timeout: Any) -> dict[str, Any]:
        request = Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", **headers}, method="POST")
        try:
            with _SECURE_OPENER.open(request, timeout=float(timeout)) as response:
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            # AnySearch may return newly generated credentials in a 402 body. Never
            # put provider response bodies into logs or MCP output.
            retryable = exc.code in (402, 408, 429, 432) or 500 <= exc.code <= 599 or ((exc.code in (403, 432)) and self._is_quota_message(body)) or self._is_quota_message(body)
            message = f"HTTP {exc.code}: quota exhausted" if (exc.code in (402, 432) or (exc.code == 403 and retryable) or self._is_quota_message(body)) else f"HTTP {exc.code}"
            raise SearchError(message, retryable=retryable, status=exc.code) from exc
        except (URLError, socket.timeout, TimeoutError) as exc:
            raise SearchError(f"Network error: {exc}", retryable=True) from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SearchError("Provider returned invalid JSON", retryable=True) from exc
        if not isinstance(data, dict):
            raise SearchError("Provider returned an invalid response", retryable=True)
        return data

    @staticmethod
    def _is_quota_message(message: str) -> bool:
        lowered = message.lower()
        return any(token in lowered for token in ("quota", "rate limit", "usage limit", "exceed", "too many", "not enough money", "insufficient balance", "额度", "余额不足", "频率"))


class ExtractGateway(SearchGateway):
    """Quota-aware web page extraction with public-URL validation and local disk caching."""

    # =========================================================================
    # 【功能块：初始化网页提取网关与本地磁盘缓存】
    # - 输入：definition (dict[str, Any]): web_extract 能力配置字典；
    #         state_path (Path): 冷却状态持久化路径；
    #         logger (logging.Logger): 日志记录器；
    #         cooldown_store (CooldownStore | None): 共享的冷却状态存储；
    #         cache_dir (Path | None): 本地正文缓存目录（默认 state_path 同级下的 extract_cache）；
    #         cache_ttl_seconds (int): 缓存有效期（默认 1800 秒，即 30 分钟）
    # - 功能：初始化网关基础设施并配置本地磁盘文件缓存的存储路径与有效期
    # - 输出：ExtractGateway 实例
    # =========================================================================
    def __init__(
        self,
        definition: dict[str, Any],
        state_path: Path,
        logger: logging.Logger,
        cooldown_store: CooldownStore | None = None,
        cache_dir: Path | None = None,
        cache_ttl_seconds: int = 1800,
    ):
        super().__init__(definition, state_path, logger, cooldown_store)
        cfg_cache_dir = definition.get("cache_dir")
        if cache_dir is not None:
            self.cache_dir = Path(cache_dir)
        elif cfg_cache_dir:
            self.cache_dir = Path(cfg_cache_dir)
        else:
            self.cache_dir = state_path.parent / "extract_cache"
        self.cache_ttl = int(definition.get("cache_ttl_seconds", cache_ttl_seconds))

    # =========================================================================
    # 【功能块：计算目标 URL 与深度的磁盘缓存路径】
    # - 输入：url (str): 目标网页 URL；depth (str): 提取深度（basic 或 advanced）
    # - 功能：对 URL 和 depth 进行 SHA-256 哈希，生成合法安全的文件名
    # - 输出：Path 实例（指向该 URL 对应的本地缓存 .json 文件）
    # =========================================================================
    def _get_cache_path(self, url: str, depth: str) -> Path:
        cache_key = f"{depth}:{url.strip()}"
        url_hash = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{url_hash}.json"

    # =========================================================================
    # 【功能块：读取本地磁盘缓存的网页正文】
    # - 输入：url (str): 目标网页 URL；depth (str): 提取深度；max_characters (int): 字符截断限制
    # - 功能：检查是否存在 30 分钟以内的有效本地缓存，若有效且未过期则格式化返回
    # - 输出：dict[str, Any]（提取结果对象）或 None（未命中或已过期）
    # =========================================================================
    def _read_cache(self, url: str, depth: str, max_characters: int) -> dict[str, Any] | None:
        if self.cache_ttl <= 0:
            return None
        cache_file = self._get_cache_path(url, depth)
        if not cache_file.is_file():
            return None
        try:
            with cache_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            cached_at = float(data.get("cached_at", 0))
            if time.time() - cached_at > self.cache_ttl:
                try:
                    cache_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
            if data.get("url") != url:
                return None
            title = data.get("title", "")
            content = data.get("content", "")
            was_truncated = bool(data.get("truncated", False))
            if was_truncated and max_characters > len(content):
                return None
            provider = data.get("provider", "cache")
            result = self._extract_result(url, title, content, max_characters)
            result["provider"] = provider
            return result
        except Exception as exc:
            self.logger.debug("读取提取缓存失败 [%s]: %s", url, exc)
            return None

    # =========================================================================
    # 【功能块：写入网页提取结果至本地磁盘缓存】
    # - 输入：url (str): 目标网页 URL；depth (str): 提取深度；title (Any): 网页标题；
    #         content (Any): 网页清洗正文；provider (str): 提供商标识；truncated (bool): 是否被截断
    # - 功能：将成功的提取结果原子写入本地磁盘 JSON 文件，并记录时间戳
    # - 输出：无（异常安全，写入失败仅打日志不阻断业务）
    # =========================================================================
    def _write_cache(
        self,
        url: str,
        depth: str,
        title: Any,
        content: Any,
        provider: str,
        truncated: bool = False,
    ) -> None:
        if self.cache_ttl <= 0 or not content:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = self._get_cache_path(url, depth)
            data = {
                "url": url,
                "depth": depth,
                "title": str(title or ""),
                "content": str(content or ""),
                "provider": provider,
                "truncated": truncated,
                "cached_at": time.time(),
            }
            temp_file = cache_file.with_suffix(".tmp")
            with temp_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            temp_file.replace(cache_file)
        except Exception as exc:
            self.logger.debug("写入提取缓存失败 [%s]: %s", url, exc)

    def extract(self, arguments: dict[str, Any]) -> dict[str, Any]:
        urls = arguments.get("urls")
        if not isinstance(urls, list) or not 1 <= len(urls) <= 5 or not all(isinstance(url, str) for url in urls):
            raise SearchError("urls must contain between 1 and 5 URL strings", retryable=False)
        unique_urls = list(dict.fromkeys(url.strip() for url in urls))
        for url in unique_urls:
            self._validate_public_url(url)
        max_characters = arguments.get("max_characters", 10000)
        if isinstance(max_characters, bool) or not isinstance(max_characters, int) or not 1000 <= max_characters <= 100000:
            raise SearchError("max_characters must be an integer between 1000 and 100000", retryable=False)
        depth = arguments.get("extract_depth", "basic")
        if depth not in ("basic", "advanced"):
            raise SearchError("extract_depth must be basic or advanced", retryable=False)

        providers = self.definition.get("providers", [])
        if not isinstance(providers, list):
            raise SearchError("Invalid extract providers configuration", retryable=False)
        enabled_count = sum(1 for p in providers if isinstance(p, dict) and p.get("enabled", False))
        pending = set(unique_urls)
        extracted: list[dict[str, Any]] = []
        failures: list[ProviderFailure] = []

        # 检查本地磁盘缓存（若在30分钟有效期内命中，直接复用，避免消耗远端 API 额度）
        for url in list(pending):
            cached = self._read_cache(url, depth, max_characters)
            if cached is not None:
                extracted.append(cached)
                pending.remove(url)

        cooldown_seconds = self.definition.get("cooldown_seconds", 3600)
        quota_cooldown_seconds = self.definition.get("quota_cooldown_seconds", 259200)
        timeout = self.definition.get("timeout_seconds", 30)
        for provider in sorted(providers, key=lambda item: item.get("priority", 1000)):
            if not pending:
                break
            provider_id = provider.get("id", "unknown")
            if not provider.get("enabled", False):
                continue
            key_name = provider.get("api_key_env")
            api_key = os.getenv(key_name, "") if isinstance(key_name, str) else ""
            engine_type = provider.get("type") or provider_id
            if not api_key and engine_type not in ("anysearch", "jina"):
                failures.append(ProviderFailure(provider_id, "API key is not configured"))
                continue
            if not self.cooldowns.is_available(self._cooldown_id(provider_id), current_key=api_key):
                failures.append(ProviderFailure(provider_id, "provider is in cooldown"))
                continue
            try:
                results = self._extract(provider, list(pending), depth, max_characters, api_key, timeout)
            except SearchError as exc:
                self.logger.warning("Extract provider %s failed: %s", provider_id, exc)
                failures.append(ProviderFailure(provider_id, str(exc)))
                if exc.retryable:
                    is_quota = self._is_quota_exhausted(exc)
                    cd = quota_cooldown_seconds if is_quota else cooldown_seconds
                    reason_type = "quota" if is_quota else "rate_limit"
                    self.cooldowns.set(self._cooldown_id(provider_id), cd, reason_type=reason_type, api_key=api_key)
                # Authentication, provider-side validation, and URL parsing failures
                # are provider-specific: preserve the reason and try the next service.
                continue
            returned_urls = set()
            for result in results:
                url = result["url"]
                if url in pending and result["content"]:
                    returned_urls.add(url)
                    extracted.append({**result, "provider": provider_id})
                    self._write_cache(
                        url=url,
                        depth=depth,
                        title=result.get("title"),
                        content=result.get("content"),
                        provider=provider_id,
                        truncated=bool(result.get("truncated", False)),
                    )
            pending -= returned_urls

        ordered = sorted(extracted, key=lambda item: unique_urls.index(item["url"]))
        response: dict[str, Any] = {
            "results": ordered,
            "unresolved_urls": [url for url in unique_urls if url in pending],
            "failures": [{"provider": item.provider, "reason": item.reason} for item in failures],
        }
        all_quota = (
            not extracted
            and len(failures) >= enabled_count
            and enabled_count > 0
            and self._is_all_quota(failures)
        )
        warning = self._format_warning(failures, all_exhausted=all_quota)
        if warning:
            response["warning"] = warning
        return response

    def _cooldown_id(self, provider_id: str) -> str:
        return f"web_extract:{provider_id}"

    def _extract(self, provider: dict[str, Any], urls: list[str], depth: str, max_characters: int, api_key: str, timeout: Any) -> list[dict[str, Any]]:
        provider_id = provider["id"]
        endpoint = provider.get("endpoint")
        self._validate_endpoint(provider_id, endpoint)
        engine_type = provider.get("type") or provider_id
        if engine_type in ("tavily-search", "tavily"):
            data = self._post(endpoint, {"Authorization": f"Bearer {api_key}"}, {"urls": urls, "extract_depth": depth, "format": "markdown"}, timeout)
            items = data.get("results", [])
            return [self._extract_result(item.get("url"), item.get("title"), item.get("raw_content") or item.get("content"), max_characters) for item in items if isinstance(item, dict)]
        if engine_type == "exa":
            data = self._post(endpoint, {"x-api-key": api_key}, {"ids": urls, "text": {"maxCharacters": max_characters}}, timeout)
            items = data.get("results", [])
            return [self._extract_result(item.get("url"), item.get("title"), item.get("text") or item.get("content"), max_characters) for item in items if isinstance(item, dict)]
        if engine_type == "jina":
            return self._jina_extract(endpoint, api_key, urls, max_characters, timeout)
        if engine_type == "anysearch":
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            results: list[dict[str, Any]] = []
            for url in urls:
                try:
                    data = self._post(endpoint, headers, {"url": url}, timeout)
                except SearchError as exc:
                    if exc.status == 422:
                        continue
                    raise
                if isinstance(data.get("code"), int) and data["code"] != 0:
                    code = data["code"]
                    self.logger.warning("AnySearch extract business error: code=%s", code)
                    continue
                item = data.get("data", data)
                if isinstance(item, dict):
                    results.append(self._extract_result(item.get("url", url), item.get("title"), item.get("content"), max_characters))
            return results
        raise SearchError(f"Unsupported extract provider type or id: {engine_type} (id: {provider_id})", retryable=False)

    # =========================================================================
    # 【功能块：Jina AI Reader 网页正文提取适配器】
    # - 输入：endpoint (str): Jina Reader 接口地址（例如 https://r.jina.ai）
    #         key (str): Jina API Key（用于 Bearer Token 鉴权）
    #         urls (list[str]): 待提取的公开网页 URL 列表
    #         max_characters (int): 单页面最大字符提取限制
    #         timeout (float | int): 网络超时时间（秒）
    # - 功能：逐个向 Jina Reader 发送 HTTP POST 请求，提取目标网页清洗后的 Markdown 正文，
    #         截断超长字符并封装为统一的 extract 结果结构
    # - 输出：list[dict[str, Any]]: 格式化后的网页正文提取条目列表
    # =========================================================================
    def _jina_extract(self, endpoint: str, key: str, urls: list[str], max_characters: int, timeout: Any) -> list[dict[str, Any]]:
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        results: list[dict[str, Any]] = []
        for url in urls:
            try:
                data = self._post(endpoint, headers, {"url": url}, timeout)
            except SearchError as exc:
                if exc.status == 422:
                    continue
                raise
            item = data.get("data", data)
            if isinstance(item, dict):
                title = item.get("title") or ""
                content = item.get("content") or ""
                results.append(self._extract_result(item.get("url", url), title, content, max_characters))
        return results

    @staticmethod
    def _extract_result(url: Any, title: Any, content: Any, maximum: int) -> dict[str, Any]:
        body = str(content or "")
        return {"url": str(url or ""), "title": str(title or ""), "content": body[:maximum], "truncated": len(body) > maximum}

    @staticmethod
    def _validate_public_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise SearchError(f"URL is not a permitted public HTTP(S) URL: {url}", retryable=False)
        if parsed.port not in (None, 80, 443):
            raise SearchError(f"URL uses a non-standard port: {url}", retryable=False)
        try:
            addresses = {record[4][0] for record in socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise SearchError(f"URL hostname cannot be resolved: {parsed.hostname}", retryable=False) from exc
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise SearchError(f"URL hostname resolves to a non-public address: {parsed.hostname}", retryable=False)
