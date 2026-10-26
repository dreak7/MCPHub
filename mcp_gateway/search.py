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
from urllib.parse import urlencode, urlsplit, urlunsplit
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
    suppress_warning: bool = False


def resolve_provider_key(
    provider: dict[str, Any],
    cooldowns: "CooldownStore",
    legacy_cooldown_id: str,
    allow_anonymous: bool = False,
) -> tuple[str | None, str, str | None]:
    """按 provider 的 key 池顺序挑第一把未冷却的 key（所有引擎共用）。

    - 配置：`api_key_envs`（数组，顺序即优先级），兼容旧的 `api_key_env` 单值
    - 返回：(api_key, cooldown_id, reason)，reason ∈ {None, "not_configured", "cooldown"}
    - 单 key provider 沿用旧 cooldown_id（既有 state 文件零迁移）
    - 多 key provider 用 `<legacy_id>#<keyhash8>`，每把 key 独立冷冻/复活
    - allow_anonymous=True 时（jina/anysearch 等），没配 key 也放行，但共享旧 cooldown_id
    """
    pid = str(provider.get("id", "unknown"))
    envs = provider.get("api_key_envs")
    env_names = [str(n) for n in envs if isinstance(n, str) and n.strip()] if isinstance(envs, list) else []
    if not env_names:
        single = provider.get("api_key_env")
        env_names = [single] if isinstance(single, str) and single.strip() else []

    keys = [(name, (os.getenv(name) or "").strip()) for name in env_names]
    keys = [(name, key) for name, key in keys if key]

    if not keys:
        if not allow_anonymous:
            return None, legacy_cooldown_id, "not_configured"
        if cooldowns.is_available(legacy_cooldown_id):
            return "", legacy_cooldown_id, None
        return None, legacy_cooldown_id, "cooldown"

    multi = len(keys) > 1
    last_cid = legacy_cooldown_id
    for _name, key in keys:
        cid = f"{legacy_cooldown_id}#{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}" if multi else legacy_cooldown_id
        last_cid = cid
        if cooldowns.is_available(cid, current_key=key):
            return key, cid, None
    return None, last_cid, "cooldown"


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
            engine_type = provider.get("type") or provider_id
            # AnySearch, Jina AI, and Context7 explicitly support an anonymous quota; all others require a key.
            api_key, key_cd_id, key_reason = resolve_provider_key(
                provider,
                self.cooldowns,
                self._cooldown_id(provider_id),
                allow_anonymous=engine_type in ("anysearch", "jina", "context7"),
            )
            if key_reason == "not_configured":
                failures.append(ProviderFailure(provider_id, "API key is not configured"))
                continue
            if key_reason == "cooldown":
                failures.append(ProviderFailure(provider_id, "provider is in cooldown"))
                continue
            api_key = api_key or ""
            result: list[dict[str, Any]] | None = None
            swapped = False
            while True:
                try:
                    result = self._call(provider, query.strip(), max_results, arguments, api_key, timeout)
                    break
                except SearchError as exc:
                    self.logger.warning("Provider %s failed: %s", provider_id, exc)
                    failures.append(ProviderFailure(provider_id, self._describe_failure(exc)))
                    auth_failure = exc.status in (401, 403)
                    if not exc.retryable and not auth_failure:
                        break
                    is_quota = self._is_quota_exhausted(exc)
                    cd = quota_cooldown_seconds if is_quota else cooldown_seconds
                    reason_type = "quota" if is_quota else ("auth" if auth_failure else "rate_limit")
                    self.cooldowns.set(key_cd_id, cd, reason_type=reason_type, api_key=api_key)
                    nxt_key, nxt_cd, nxt_reason = resolve_provider_key(
                        provider,
                        self.cooldowns,
                        self._cooldown_id(provider_id),
                        allow_anonymous=engine_type in ("anysearch", "jina", "context7"),
                    )
                    if nxt_reason is not None or not nxt_key:
                        break
                    api_key, key_cd_id = nxt_key, nxt_cd
                    swapped = True
            if result is None:
                continue
            if swapped:
                # 同一次调用内已换 key 成功：把中间"额度耗尽"改写为可读提示，并从告警里摘掉
                for fail_item in failures:
                    if fail_item.provider == provider_id:
                        fail_item.reason = f"{fail_item.reason}（已自动切换备用 Key）"
                        fail_item.suppress_warning = True
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
        ready_providers: list[tuple[dict[str, Any], str, str]] = []

        for provider in sorted(providers, key=lambda item: item.get("priority", 1000)):
            provider_id = provider.get("id", "unknown")
            if not provider.get("enabled", False):
                continue
            engine_type = provider.get("type") or provider_id
            api_key, key_cd_id, key_reason = resolve_provider_key(
                provider,
                self.cooldowns,
                self._cooldown_id(provider_id),
                allow_anonymous=engine_type in ("anysearch", "jina"),
            )
            if key_reason == "not_configured":
                failures.append(ProviderFailure(provider_id, "API key is not configured"))
                continue
            if key_reason == "cooldown":
                failures.append(ProviderFailure(provider_id, "provider is in cooldown"))
                continue
            ready_providers.append((provider, api_key or "", key_cd_id))

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
                ): (provider.get("id", "unknown"), api_key, key_cd_id)
                for provider, api_key, key_cd_id in ready_providers
            }
            try:
                for future in concurrent.futures.as_completed(future_to_provider, timeout=float(timeout)):
                    prov_id, prov_key, prov_cd = future_to_provider[future]
                    try:
                        res = future.result()
                        if isinstance(res, list):
                            provider_results.append((prov_id, res))
                    except SearchError as exc:
                        self.logger.warning("Multi-search provider %s failed: %s", prov_id, exc)
                        failures.append(ProviderFailure(prov_id, self._describe_failure(exc)))
                        if exc.retryable:
                            is_quota = self._is_quota_exhausted(exc)
                            cd = quota_cooldown_seconds if is_quota else cooldown_seconds
                            reason_type = "quota" if is_quota else "rate_limit"
                            self.cooldowns.set(prov_cd, cd, reason_type=reason_type, api_key=prov_key)
                    except Exception as exc:
                        self.logger.exception("Multi-search provider %s unexpected error: %s", prov_id, exc)
                        failures.append(ProviderFailure(prov_id, f"Unexpected error: {exc}"))
            except (concurrent.futures.TimeoutError, TimeoutError):
                self.logger.warning("Multi-search exceeded global timeout of %ss; proceeding with partial results", timeout)
                for future, (p_id, p_key, _p_cd) in future_to_provider.items():
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
        provider_priority = {p.get("id"): p.get("priority", 1000) for p, _, _ in ready_providers}
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
            if item.suppress_warning:
                continue
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

    @classmethod
    def _describe_failure(cls, exc: SearchError) -> str:
        """失败原因的对外措辞：额度耗尽统一显示为中文，不把 HTTP 432 直接抛给调用方。"""
        return "额度耗尽" if cls._is_quota_exhausted(exc) else str(exc)

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
        if engine_type == "context7":
            return self._context7(endpoint, api_key, query, max_results, arguments, timeout)
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

    # =========================================================================
    # 【功能块：Context7 编程文档与代码切片检索适配器】
    # - 输入：endpoint (str): Context7 Search API 端点（https://context7.com/api/v3/search）；
    #         key (str): Context7 API Key（可选，若存在则作为 Bearer Token 发送）；
    #         query (str): 用户检索提问或代码问题；
    #         limit (int): 返回的最大条目数；
    #         args (dict): 扩展参数字典（支持 library, version, language 等提示词）；
    #         timeout (Any): 网络请求超时秒数
    # - 功能：向 Context7 服务端发送结构化 GET 请求，获取高质量的 codeSnippets 切片；
    #         提取 pageTitle、codeTitle、codeDescription、codeList 多语言代码块与原厂 codeId 链接，
    #         组装为标准 Markdown 代码高亮块与精炼文本格式并返回
    # - 输出：list[dict[str, Any]]: 标准化检索结果列表
    # =========================================================================
    def _context7(self, endpoint: str, key: str, query: str, limit: int, args: dict[str, Any], timeout: Any) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        q_lower = query.lower()

        # =========================================================================
        # 【功能块：本地 MCP 工具官方 API 规范拦截直出 (修复 P3-10)】
        # - 输入：query 关键词中是否命中本地 MCP 服务专有工具（如 stock_kline, stock_quotes 等）
        # - 功能：当检索本地证卷工具时，优先直接下发原厂权威 API 规范与 Python 调用切片，
        #         杜绝因关键词泛化而误退回第三方 stock-sdk 不相干文档
        # - 输出：命中时将官方规范字典置顶插入 results
        # =========================================================================
        local_docs_registry = {
            "stock_kline": {
                "title": "[MCPHub-Finance] stock_kline 多周期历史 K 线走势与除权复权算法规范",
                "url": "file:///mcphub-finance/references/tool_api_reference.md#stock_kline",
                "snippet": (
                    "【MCPHub-Finance stock_kline 工具官方规范】\n"
                    "- 接口描述：获取 A 股与港股多周期历史 K 线走势，全面支持复权 (adjust) 与内置技术指标计算 (MACD/KDJ/BOLL/MA)。\n"
                    "- 参数：symbol (必填, 标的代码), period (1m/5m/15m/30m/60m/day/week/month, 默认 day), count (默认 30, 分钟线最大 500, 日线最大 800), "
                    "adjust (前复权 qfq, 后复权 hfq, 不复权 none, 默认 qfq), setcode (可选, 0/1/2/31), with_indicators (bool, 默认 True)。\n"
                    "- 复权说明：\n"
                    "  1. A 股：基于通达信官方高可用专线 PBFXT 协议实现精确复权因子折算；\n"
                    "  2. 港股：采用东财与腾讯日线官方复权专线 (newfqkline/hkfqkline)，提供真实前复权(qfq)与后复权(hfq)，成交额 100% 真实下发；\n"
                    "  3. 技术指标预热消除初值漂移：计算 MACD/KDJ/BOLL 时自动多拉取 200 根历史预热缓冲 (Warm-up buffer)。"
                ),
                "code": (
                    "# 调用示例：查询 600519 茅台日 K 线（真实前复权并计算指标）\n"
                    "res = stock_kline({\n"
                    "    \"symbol\": \"600519\",\n"
                    "    \"period\": \"day\",\n"
                    "    \"count\": 30,\n"
                    "    \"adjust\": \"qfq\",\n"
                    "    \"with_indicators\": True\n"
                    "})\n"
                    "# 返回字段：klines (含 open, high, low, close, volume, amount 及 MACD/KDJ/BOLL 等指标)"
                ),
            },
            "stock_quotes": {
                "title": "[MCPHub-Finance] stock_quotes 实时盘口、深度估值与五档买卖委托盘",
                "url": "file:///mcphub-finance/references/tool_api_reference.md#stock_quotes",
                "snippet": (
                    "【MCPHub-Finance stock_quotes 工具官方规范】\n"
                    "- 接口描述：获取 A 股与港股实时盘口微观行情、估值指标与 Level-1 基础五档买卖委托盘口。\n"
                    "- 参数：symbol (必填), setcode (可选, 严禁跨市场错传)。\n"
                    "- 核心特性：\n"
                    "  1. 真实 TTM 滚动市盈率 (PETTM) 与静态 PEStatic；\n"
                    "  2. 动态市盈率 PE 与年化每股收益 EPS 双侧同步校准，严格满足 [现价 ÷ 年化EPS = 动态PE]；\n"
                    "  3. 港股市值与 A+H 股本全口径严格校准；\n"
                    "  4. 无效代码显式拦截报错，严禁返回静默空盘口。"
                ),
                "code": (
                    "res = stock_quotes({\"symbol\": \"600519\"})\n"
                    "# 核心键：盘口核心行情 (现价/涨跌幅/成交量/成交额), 估值与基本盘面 (滚动市盈率_PETTM/动态市盈率_PE/市净率_PB), 五档深度盘口"
                ),
            },
            "stock_financials": {
                "title": "[MCPHub-Finance] stock_financials 三大财务报表与单季度推算",
                "url": "file:///mcphub-finance/references/tool_api_reference.md#stock_financials",
                "snippet": (
                    "【MCPHub-Finance stock_financials 工具官方规范】\n"
                    "- 接口描述：查询 A 股与港股法定三大财务报表 (资产负债表 balance, 利润表 income, 现金流量表 cashflow)。\n"
                    "- 参数：symbol (必填), report_type (balance/income/cashflow), period_type (report=累计报告期, quarter=单季度做差推算), limit_periods (默认 4)。\n"
                    "- 核心特性：日期统一为 ISO-8601 (YYYY-MM-DD)，金融机构与一般工商业双轨制自动嗅探，现金流直接法与间接法物理隔离。"
                ),
                "code": (
                    "res = stock_financials({\"symbol\": \"600519\", \"report_type\": \"income\", \"period_type\": \"quarter\", \"limit_periods\": 4})\n"
                    "# 返回 statements 列表，每期含截止日期、报告期日期、报告期、各会计科目"
                ),
            },
            "stock_f10": {
                "title": "[MCPHub-Finance] stock_f10 深度基本面多分类",
                "url": "file:///mcphub-finance/references/tool_api_reference.md#stock_f10",
                "snippet": (
                    "【MCPHub-Finance stock_f10 工具官方规范】\n"
                    "- 接口描述：查询上市公司深度基本面。分类包括 concepts, industry, events, dividends, forecast, shareholders, top_holders, repurchases, buyback, capital_flow, margin_trading, limit_up, lhb, unlocks, summary, basic_info, indicators, financials。"
                ),
                "code": (
                    "res = stock_f10({\"symbol\": \"600519\", \"category\": \"repurchases\"})\n"
                    "# formatted_tables 挂载对应结构化表格"
                ),
            },
        }

        for tool_key, doc in local_docs_registry.items():
            if tool_key in q_lower or (tool_key.replace("stock_", "") in q_lower and "stock" in q_lower):
                snippet_text = f"{doc['snippet']}\n\n```python\n{doc['code']}\n```"
                results.append(self._result(doc["title"], doc["url"], snippet_text, ""))
                if len(results) >= limit:
                    return results

        params: dict[str, Any] = {
            "query": query,
            "type": "json",
        }
        if args.get("library"):
            params["library"] = args["library"]
        if args.get("version"):
            params["version"] = args["version"]
        if args.get("language"):
            params["language"] = args["language"]

        headers: dict[str, str] = {}
        if key and key.strip():
            headers["Authorization"] = f"Bearer {key.strip()}"

        data = self._get(endpoint, headers, params, timeout)
        snippets = data.get("codeSnippets", [])
        if not isinstance(snippets, list):
            return results

        remaining_slots = limit - len(results)
        for item in snippets[:remaining_slots]:
            if not isinstance(item, dict):
                continue
            raw_page_title = str(item.get("pageTitle") or "").strip()
            # 修复 P3-10: 彻底过滤 Context7 上游返回的 [Unknown] 占位元数据
            page_title = "" if raw_page_title.lower() in ("unknown", "none", "null") else raw_page_title
            code_title = str(item.get("codeTitle") or "").strip()
            if page_title and code_title:
                title = f"[{page_title}] {code_title}"
            elif args.get("library") and code_title:
                title = f"[{args['library']}] {code_title}"
            else:
                title = code_title or page_title or "Code Snippet"

            url = str(item.get("codeId") or "").strip()
            description = str(item.get("codeDescription") or "").strip()

            # 组装格式化 Markdown 代码高亮块
            code_blocks: list[str] = []
            code_list = item.get("codeList", [])
            if isinstance(code_list, list):
                for code_item in code_list:
                    if isinstance(code_item, dict):
                        lang = str(code_item.get("language") or "").strip()
                        code = str(code_item.get("code") or "").strip()
                        if code:
                            code_blocks.append(f"```{lang}\n{code}\n```")

            content_parts: list[str] = []
            if description:
                content_parts.append(description)
            if code_blocks:
                content_parts.append("\n\n".join(code_blocks))

            snippet_text = "\n\n".join(content_parts)
            results.append(self._result(title, url, snippet_text, ""))

        return results

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

    # =========================================================================
    # 【功能块：安全 HTTP GET 通信管道（支持查询参数自动编解码与 404 容错）】
    # - 输入：endpoint (str): API 接口地址；
    #         headers (dict[str, str]): HTTP 请求头（包含 Accept、Authorization 等）；
    #         params (dict[str, Any]): URL 查询参数字典；
    #         timeout (Any): 请求超时秒数
    # - 功能：使用受保护的 _SECURE_OPENER 发起 GET 请求；对 404 (no_documentation_found)
    #         进行智能容错（判定为空结果而非服务故障）；对 402/429/432 等配额错误抛出对应 SearchError
    # - 输出：dict[str, Any]: 服务端解析后的 JSON 响应字典
    # =========================================================================
    def _get(self, endpoint: str, headers: dict[str, str], params: dict[str, Any], timeout: Any) -> dict[str, Any]:
        query_items = [(k, str(v)) for k, v in params.items() if v is not None and str(v).strip() != ""]
        url = f"{endpoint}?{urlencode(query_items, doseq=True)}" if query_items else endpoint
        request = Request(url, headers={"Accept": "application/json", **headers}, method="GET")
        try:
            with _SECURE_OPENER.open(request, timeout=float(timeout)) as response:
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            # Context7 专属容错：404 no_documentation_found 代表该问题在文档库中未命中结果，作为空结果正常返回
            if exc.code == 404:
                try:
                    err_data = json.loads(body)
                    if isinstance(err_data, dict) and err_data.get("error") == "no_documentation_found":
                        return {"codeSnippets": []}
                except (json.JSONDecodeError, ValueError):
                    pass
                return {"codeSnippets": []}
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
    # - 功能：对 URL 和 depth 进行 SHA-256 哈希，生成元数据 JSON 与全文 Markdown 路径
    # - 输出：tuple[Path, Path]: (meta_file, md_file)
    # =========================================================================
    def _get_cache_keys(self, url: str, depth: str) -> tuple[Path, Path]:
        cache_key = f"{depth}:{url.strip()}"
        url_hash = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
        meta_file = self.cache_dir / f"{url_hash}.json"
        md_file = self.cache_dir / f"{url_hash}.md"
        return meta_file, md_file

    # =========================================================================
    # 【功能块：读取本地磁盘缓存的网页正文与元数据】
    # - 输入：url (str): 目标网页 URL；depth (str): 提取深度；max_characters (int): 字符截断限制
    # - 功能：检查 30 分钟内有效的磁盘缓存；若命中，读取完整 Markdown 正文，
    #         切片出 content，并附带 cached: True、cache_path 绝对路径及 read_file 翻页提示
    # - 输出：dict[str, Any]（提取结果条目）或 None（未命中或已过期）
    # =========================================================================
    def _read_cache(self, url: str, depth: str, max_characters: int) -> dict[str, Any] | None:
        if self.cache_ttl <= 0:
            return None
        meta_file, md_file = self._get_cache_keys(url, depth)
        if not meta_file.is_file() or not md_file.is_file():
            return None
        try:
            with meta_file.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            cached_at = float(meta.get("cached_at", 0))
            if time.time() - cached_at > self.cache_ttl:
                try:
                    meta_file.unlink(missing_ok=True)
                    md_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
            if meta.get("url") != url:
                return None

            with md_file.open("r", encoding="utf-8") as f:
                full_content = f.read()

            if not full_content or not full_content.strip():
                return None

            title = meta.get("title", "")
            provider = meta.get("provider", "cache")
            total_chars = len(full_content)
            truncated = total_chars > max_characters
            cache_abs_path = str(md_file.resolve()).replace("\\", "/")
            cached_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cached_at))

            item: dict[str, Any] = {
                "url": url,
                "title": title,
                "content": full_content[:max_characters],
                "truncated": truncated,
                "provider": provider,
                "cached": True,
                "cached_at": cached_time_str,
                "cache_path": cache_abs_path,
                "total_characters": total_chars,
            }
            if truncated:
                item["paging_hint"] = (
                    f"提示：页面正文过长已截断（当前显示前 {max_characters} 字符，全文共 {total_chars} 字符）。"
                    f"完整全文已落盘保存，你可以直接使用 read_file 工具分页翻阅该文件：{cache_abs_path}"
                )
            return item
        except Exception as exc:
            self.logger.debug("读取提取缓存失败 [%s]: %s", url, exc)
            return None

    # =========================================================================
    # 【功能块：写入网页全文至本地 Markdown 文件与元数据 JSON】
    # - 输入：url (str): 目标网页 URL；depth (str): 提取深度；title (Any): 网页标题；
    #         full_content (Any): 网页原始完整正文；provider (str): 提供商标识
    # - 功能：将未截断的原始全文原子写入本地 .md 文件（供 read_file 翻页），
    #         并将缓存时间与元数据写入 .json 文件；采用 PID 与纳秒级时间戳生成唯一临时文件，
    #         彻底消除多进程/多线程并发写入时的文件冲突
    # - 输出：Path | None（写入成功的 Markdown 文件路径，失败返回 None）
    # =========================================================================
    def _write_cache(
        self,
        url: str,
        depth: str,
        title: Any,
        full_content: Any,
        provider: str,
    ) -> Path | None:
        if self.cache_ttl <= 0 or not full_content:
            return None
        temp_md: Path | None = None
        temp_meta: Path | None = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            meta_file, md_file = self._get_cache_keys(url, depth)
            full_text = str(full_content or "")
            if not full_text.strip():
                return None

            unique_id = f"{os.getpid()}_{time.time_ns()}"

            # 1. 写入全文 Markdown 文件（供 read_file 翻页或人类直接阅读）
            temp_md = self.cache_dir / f"{md_file.stem}_{unique_id}.tmp.md"
            with temp_md.open("w", encoding="utf-8") as f:
                f.write(full_text)
            temp_md.replace(md_file)

            # 2. 写入元数据 JSON 文件
            meta_data = {
                "url": url,
                "depth": depth,
                "title": str(title or ""),
                "provider": provider,
                "cached_at": time.time(),
                "total_characters": len(full_text),
                "cache_file": md_file.name,
            }
            temp_meta = self.cache_dir / f"{meta_file.stem}_{unique_id}.tmp.json"
            with temp_meta.open("w", encoding="utf-8") as f:
                json.dump(meta_data, f, ensure_ascii=False)
            temp_meta.replace(meta_file)

            return md_file
        except Exception as exc:
            self.logger.debug("写入提取缓存失败 [%s]: %s", url, exc)
            for stray in (temp_md, temp_meta):
                if stray is not None:
                    try:
                        stray.unlink(missing_ok=True)
                    except OSError:
                        pass
            return None

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
            engine_type = provider.get("type") or provider_id
            api_key, key_cd_id, key_reason = resolve_provider_key(
                provider,
                self.cooldowns,
                self._cooldown_id(provider_id),
                allow_anonymous=engine_type in ("anysearch", "jina"),
            )
            if key_reason == "not_configured":
                failures.append(ProviderFailure(provider_id, "API key is not configured"))
                continue
            if key_reason == "cooldown":
                failures.append(ProviderFailure(provider_id, "provider is in cooldown"))
                continue
            api_key = api_key or ""
            results = None
            swapped = False
            while True:
                try:
                    results = self._extract(provider, list(pending), depth, max_characters, api_key, timeout)
                    break
                except SearchError as exc:
                    self.logger.warning("Extract provider %s failed: %s", provider_id, exc)
                    failures.append(ProviderFailure(provider_id, self._describe_failure(exc)))
                    auth_failure = exc.status in (401, 403)
                    if not exc.retryable and not auth_failure:
                        break
                    is_quota = self._is_quota_exhausted(exc)
                    cd = quota_cooldown_seconds if is_quota else cooldown_seconds
                    reason_type = "quota" if is_quota else ("auth" if auth_failure else "rate_limit")
                    self.cooldowns.set(key_cd_id, cd, reason_type=reason_type, api_key=api_key)
                    nxt_key, nxt_cd, nxt_reason = resolve_provider_key(
                        provider,
                        self.cooldowns,
                        self._cooldown_id(provider_id),
                        allow_anonymous=engine_type in ("anysearch", "jina"),
                    )
                    if nxt_reason is not None or not nxt_key:
                        break
                    api_key, key_cd_id = nxt_key, nxt_cd
                    swapped = True
            if results is None:
                continue
            if swapped:
                for fail_item in failures:
                    if fail_item.provider == provider_id:
                        fail_item.reason = f"{fail_item.reason}（已自动切换备用 Key）"
                        fail_item.suppress_warning = True
            returned_urls = set()
            for result in results:
                url = result["url"]
                if url in pending and result.get("content"):
                    returned_urls.add(url)
                    raw_content = result.get("raw_content") or result.get("content") or ""
                    written_md = self._write_cache(
                        url=url,
                        depth=depth,
                        title=result.get("title"),
                        full_content=raw_content,
                        provider=provider_id,
                    )
                    cache_abs_path = str(written_md.resolve()).replace("\\", "/") if written_md else ""
                    total_chars = len(str(raw_content))
                    truncated = bool(result.get("truncated", False))
                    item: dict[str, Any] = {
                        "url": url,
                        "title": result.get("title", ""),
                        "content": result.get("content", ""),
                        "truncated": truncated,
                        "provider": provider_id,
                        "cached": False,
                        "cache_path": cache_abs_path,
                        "total_characters": total_chars,
                    }
                    if truncated and cache_abs_path:
                        item["paging_hint"] = (
                            f"提示：页面正文过长已截断（当前显示前 {max_characters} 字符，全文共 {total_chars} 字符）。"
                            f"完整全文已落盘保存，你可以直接使用 read_file 工具分页翻阅该文件：{cache_abs_path}"
                        )
                    extracted.append(item)
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
            data = self._post(endpoint, {"x-api-key": api_key}, {"ids": urls, "text": {"maxCharacters": max(max_characters, 100000)}}, timeout)
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
        return {
            "url": str(url or ""),
            "title": str(title or ""),
            "content": body[:maximum],
            "truncated": len(body) > maximum,
            "raw_content": body,
        }

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
