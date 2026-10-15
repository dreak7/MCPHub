"""Search API clients, response normalization, and fallback policy."""

from __future__ import annotations

import json
import ipaddress
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class SearchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class ProviderFailure:
    provider: str
    reason: str


class CooldownStore:
    def __init__(self, path: Path):
        self.path = path
        self._state: dict[str, float] = self._load()

    def _load(self) -> dict[str, float]:
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(key): float(value) for key, value in parsed.items()}
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            return {}

    def is_available(self, provider_id: str) -> bool:
        return self._state.get(provider_id, 0) <= time.time()

    def set(self, provider_id: str, seconds: int | float) -> None:
        self._state[provider_id] = time.time() + seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        temporary.replace(self.path)


class SearchGateway:
    def __init__(self, definition: dict[str, Any], state_path: Path, logger: logging.Logger):
        self.definition = definition
        self.logger = logger
        self.cooldowns = CooldownStore(state_path)

    def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise SearchError("query must be a non-empty string", retryable=False)
        max_results = arguments.get("max_results", 5)
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 20:
            raise SearchError("max_results must be an integer between 1 and 20", retryable=False)

        providers = self.definition.get("providers", [])
        if not isinstance(providers, list):
            raise SearchError("Invalid providers configuration", retryable=False)
        cooldown_seconds = self.definition.get("cooldown_seconds", 3600)
        timeout = self.definition.get("timeout_seconds", 25)
        failures: list[ProviderFailure] = []
        for provider in sorted(providers, key=lambda item: item.get("priority", 1000)):
            provider_id = provider.get("id", "unknown")
            if not provider.get("enabled", False):
                continue
            key_name = provider.get("api_key_env")
            api_key = os.getenv(key_name, "") if isinstance(key_name, str) else ""
            # AnySearch explicitly supports an anonymous quota; all others require a key.
            if not api_key and provider_id != "anysearch":
                failures.append(ProviderFailure(provider_id, "API key is not configured"))
                continue
            if not self.cooldowns.is_available(self._cooldown_id(provider_id)):
                failures.append(ProviderFailure(provider_id, "provider is in cooldown"))
                continue
            try:
                result = self._call(provider, query.strip(), max_results, arguments, api_key, timeout)
                return {"provider": provider_id, "query": query.strip(), "results": result}
            except SearchError as exc:
                self.logger.warning("Provider %s failed: %s", provider_id, exc)
                failures.append(ProviderFailure(provider_id, str(exc)))
                if not exc.retryable:
                    raise
                self.cooldowns.set(self._cooldown_id(provider_id), cooldown_seconds)

        detail = "; ".join(f"{item.provider}: {item.reason}" for item in failures) or "No provider enabled"
        raise SearchError(f"No search provider was available ({detail})", retryable=False)

    def _cooldown_id(self, provider_id: str) -> str:
        return f"web_search:{provider_id}"

    def _call(self, provider: dict[str, Any], query: str, max_results: int, arguments: dict[str, Any], api_key: str, timeout: Any) -> list[dict[str, Any]]:
        provider_id = provider["id"]
        endpoint = provider.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise SearchError(f"{provider_id}: endpoint must be an HTTPS URL", retryable=False)
        if provider_id == "tavily-search":
            return self._tavily(endpoint, api_key, query, max_results, arguments, timeout)
        if provider_id == "exa":
            return self._exa(endpoint, api_key, query, max_results, arguments, timeout)
        if provider_id == "anysearch":
            return self._anysearch(endpoint, api_key, query, max_results, provider, timeout)
        if provider_id == "bocha-search":
            return self._bocha(endpoint, api_key, query, max_results, arguments, timeout)
        raise SearchError(f"Unsupported provider id: {provider_id}", retryable=False)

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
            raise SearchError(str(data.get("message") or "AnySearch request failed"), retryable=False)
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
            message = str(data.get("msg") or data.get("message") or "Bocha request failed")
            raise SearchError(message, retryable=self._is_quota_message(message))
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
            with urlopen(request, timeout=float(timeout)) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            # AnySearch may return newly generated credentials in a 402 body. Never
            # put provider response bodies into logs or MCP output.
            retryable = exc.code in (402, 408, 429) or 500 <= exc.code <= 599 or (exc.code == 403 and self._is_quota_message(body))
            message = "HTTP 402: quota exhausted" if exc.code == 402 else f"HTTP {exc.code}"
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
        return any(token in lowered for token in ("quota", "rate limit", "too many", "not enough money", "insufficient balance", "额度", "余额不足", "频率"))


class ExtractGateway(SearchGateway):
    """Quota-aware web page extraction with public-URL validation."""

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
        pending = set(unique_urls)
        extracted: list[dict[str, Any]] = []
        failures: list[ProviderFailure] = []
        cooldown_seconds = self.definition.get("cooldown_seconds", 3600)
        timeout = self.definition.get("timeout_seconds", 30)
        for provider in sorted(providers, key=lambda item: item.get("priority", 1000)):
            if not pending:
                break
            provider_id = provider.get("id", "unknown")
            if not provider.get("enabled", False):
                continue
            key_name = provider.get("api_key_env")
            api_key = os.getenv(key_name, "") if isinstance(key_name, str) else ""
            if not api_key and provider_id != "anysearch":
                failures.append(ProviderFailure(provider_id, "API key is not configured"))
                continue
            if not self.cooldowns.is_available(self._cooldown_id(provider_id)):
                failures.append(ProviderFailure(provider_id, "provider is in cooldown"))
                continue
            try:
                results = self._extract(provider, list(pending), depth, max_characters, api_key, timeout)
            except SearchError as exc:
                self.logger.warning("Extract provider %s failed: %s", provider_id, exc)
                failures.append(ProviderFailure(provider_id, str(exc)))
                if exc.retryable:
                    self.cooldowns.set(self._cooldown_id(provider_id), cooldown_seconds)
                    continue
                if exc.status == 422:
                    # A provider cannot parse a page; another provider may succeed.
                    continue
                raise
            returned_urls = set()
            for result in results:
                url = result["url"]
                if url in pending and result["content"]:
                    returned_urls.add(url)
                    extracted.append({**result, "provider": provider_id})
            pending -= returned_urls

        ordered = sorted(extracted, key=lambda item: unique_urls.index(item["url"]))
        return {
            "results": ordered,
            "unresolved_urls": [url for url in unique_urls if url in pending],
            "failures": [{"provider": item.provider, "reason": item.reason} for item in failures],
        }

    def _cooldown_id(self, provider_id: str) -> str:
        return f"web_extract:{provider_id}"

    def _extract(self, provider: dict[str, Any], urls: list[str], depth: str, max_characters: int, api_key: str, timeout: Any) -> list[dict[str, Any]]:
        provider_id = provider["id"]
        endpoint = provider.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise SearchError(f"{provider_id}: endpoint must be an HTTPS URL", retryable=False)
        if provider_id == "tavily-search":
            data = self._post(endpoint, {"Authorization": f"Bearer {api_key}"}, {"urls": urls, "extract_depth": depth, "format": "markdown"}, timeout)
            items = data.get("results", [])
            return [self._extract_result(item.get("url"), item.get("title"), item.get("raw_content") or item.get("content"), max_characters) for item in items if isinstance(item, dict)]
        if provider_id == "exa":
            data = self._post(endpoint, {"x-api-key": api_key}, {"ids": urls, "text": {"maxCharacters": max_characters}}, timeout)
            items = data.get("results", [])
            return [self._extract_result(item.get("url"), item.get("title"), item.get("text") or item.get("content"), max_characters) for item in items if isinstance(item, dict)]
        if provider_id == "anysearch":
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
                    continue
                item = data.get("data", data)
                if isinstance(item, dict):
                    results.append(self._extract_result(item.get("url", url), item.get("title"), item.get("content"), max_characters))
            return results
        raise SearchError(f"Unsupported extract provider id: {provider_id}", retryable=False)

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
