import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_gateway.search import ExtractGateway, SearchError, SearchGateway
from mcp_gateway.server import McpServer


class FakeLogger:
    def warning(self, *args):
        pass

    def exception(self, *args):
        pass

    def info(self, *args):
        pass


class SearchGatewayTest(unittest.TestCase):
    def make_gateway(self):
        return SearchGateway({"cooldown_seconds": 60, "providers": [
            {"id": "tavily-search", "enabled": True, "priority": 10, "api_key_env": "TAVILY_API_KEY", "endpoint": "https://example.test/tavily"},
            {"id": "exa", "enabled": True, "priority": 20, "api_key_env": "EXA_API_KEY", "endpoint": "https://example.test/exa"},
        ]}, Path(tempfile.mkdtemp()) / "state.json", FakeLogger())

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_rate_limited_primary_falls_back_to_exa(self):
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=[SearchError("HTTP 429", retryable=True), [{"title": "ok", "url": "u", "snippet": "s", "published_date": ""}]]) as call:
            result = gateway.search({"query": "test"})
        self.assertEqual("exa", result["provider"])
        self.assertEqual(2, call.call_count)
        self.assertFalse(gateway.cooldowns.is_available("web_search:tavily-search"))
        self.assertIn("warning", result)
        self.assertEqual(
            "⚠️ 警告：检测到后端服务配额耗尽或请求限流 [tavily-search (HTTP 429)]。请耐心等待配额重置稍后重试或联系MCP提供商。",
            result["warning"],
        )

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_auth_error_falls_back_and_is_reported(self):
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=[SearchError("HTTP 401", retryable=False), [{"title": "ok", "url": "u", "snippet": "s", "published_date": ""}]]) as call:
            result = gateway.search({"query": "test"})
        self.assertEqual("exa", result["provider"])
        self.assertEqual("HTTP 401", result["provider_failures"][0]["reason"])
        self.assertIn("warning", result)
        self.assertIn("tavily-search (HTTP 401)", result["warning"])
        self.assertIn("或联系MCP提供商", result["warning"])
        self.assertEqual(2, call.call_count)

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_all_providers_auth_error_includes_warning_advice(self):
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=SearchError("HTTP 401", retryable=False)):
            with self.assertRaises(SearchError) as ctx:
                gateway.search({"query": "test"})
        self.assertIn("或联系MCP提供商", str(ctx.exception))

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_all_providers_quota_exhausted_warning(self):
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=SearchError("HTTP 429", retryable=True)):
            with self.assertRaises(SearchError) as ctx:
                gateway.search({"query": "test"})
        self.assertIn("⚠️  警告：检测到后端服务配额均耗尽，请耐心等待相关配额重置或联系MCP提供商。", str(ctx.exception))

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_custom_auth_message_triggers_warning(self):
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=[SearchError("Unauthorized: invalid token", retryable=False), [{"title": "ok", "url": "u", "snippet": "s", "published_date": ""}]]) as call:
            result = gateway.search({"query": "test"})
        self.assertIn("warning", result)
        self.assertIn("Unauthorized: invalid token", result["warning"])
        self.assertIn("或联系MCP提供商", result["warning"])

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_quota_exhausted_warning(self):
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=[SearchError("HTTP 402: quota exhausted", retryable=True), [{"title": "ok", "url": "u", "snippet": "s", "published_date": ""}]]) as call:
            result = gateway.search({"query": "test"})
        self.assertIn("warning", result)
        self.assertEqual(
            "⚠️ 警告：检测到后端服务配额耗尽或请求限流 [tavily-search (HTTP 402: quota exhausted)]。请耐心等待配额重置稍后重试或联系MCP提供商。",
            result["warning"],
        )

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_http_432_usage_limit_treated_as_quota_warning(self):
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=[
            SearchError("HTTP 432: quota exhausted", retryable=True),
            [{"title": "ok", "url": "u", "snippet": "s", "published_date": ""}],
        ]) as call:
            result = gateway.search({"query": "test"})
        self.assertEqual("exa", result["provider"])
        self.assertIn("warning", result)
        self.assertEqual(
            "⚠️ 警告：检测到后端服务配额耗尽或请求限流 [tavily-search (HTTP 432: quota exhausted)]。请耐心等待配额重置稍后重试或联系MCP提供商。",
            result["warning"],
        )

    def test_anysearch_uses_general_search_and_normalizes_response(self):
        gateway = self.make_gateway()
        provider = {"id": "anysearch", "endpoint": "https://example.test/anysearch"}
        with patch.object(gateway, "_post", return_value={"code": 0, "data": {"results": [{"title": "Result", "url": "https://example.test", "snippet": "Text"}]}}) as post:
            results = gateway._anysearch("https://example.test/anysearch", "", "query", 20, provider, 10)
        self.assertEqual("Result", results[0]["title"])
        self.assertEqual(10, post.call_args.args[2]["max_results"])
        self.assertNotIn("tag", post.call_args.args[2])

    def test_jina_search_normalizes_response(self):
        gateway = self.make_gateway()
        fake_data = {"code": 200, "data": [{"title": "Jina Title", "url": "https://jina.ai", "description": "Snippet text", "publishedTime": "2026-01-01"}]}
        with patch.object(gateway, "_post", return_value=fake_data) as post:
            results = gateway._jina("https://s.jina.ai", "fake-key", "query", 5, {}, 10)
        self.assertEqual(1, len(results))
        self.assertEqual("Jina Title", results[0]["title"])
        self.assertEqual("https://jina.ai", results[0]["url"])
        self.assertEqual("Snippet text", results[0]["snippet"])
        self.assertEqual("Bearer fake-key", post.call_args.args[1]["Authorization"])
        self.assertEqual("application/json", post.call_args.args[1]["Accept"])

    def test_validate_endpoint_blocks_insecure_or_private_urls(self):
        """测试服务端点严格安全合规校验：拦截非HTTPS、非标端口、凭据及私网/内网IP"""
        gateway = self.make_gateway()
        # 1. 拦截非 HTTPS
        with self.assertRaises(SearchError) as ctx:
            gateway._validate_endpoint("tavily", "http://api.tavily.com/search")
        self.assertIn("must be an HTTPS URL", str(ctx.exception))

        # 2. 拦截嵌入凭据
        with self.assertRaises(SearchError) as ctx:
            gateway._validate_endpoint("tavily", "https://user:pass@api.tavily.com/search")
        self.assertIn("not a valid HTTPS URL", str(ctx.exception))

        # 3. 拦截非标端口（如 8080 等非 443 端口）
        with self.assertRaises(SearchError) as ctx:
            gateway._validate_endpoint("tavily", "https://api.tavily.com:8080/search")
        self.assertIn("non-standard port", str(ctx.exception))

        # 4. 拦截私网/回环 IP（防 SSRF 与私网 Key 盗取）
        for bad_host in ("127.0.0.1", "192.168.1.1", "10.0.0.1", "169.254.169.254", "localhost"):
            with self.assertRaises(SearchError) as ctx:
                gateway._validate_endpoint("tavily", f"https://{bad_host}/search")
            self.assertIn("resolves to a non-public address", str(ctx.exception))

    def test_anysearch_business_error_is_sanitized(self):
        """测试 AnySearch 业务错误响应体脱敏：绝不泄露上游内部敏感信息或临时凭证"""
        gateway = self.make_gateway()
        provider = {"id": "anysearch", "endpoint": "https://api.anysearch.com/v1/search"}
        fake_payload = {"code": 1002, "message": "CRITICAL_SECRET_KEY_12345: request rejected"}
        with patch.object(gateway, "_post", return_value=fake_payload):
            with self.assertRaises(SearchError) as ctx:
                gateway._anysearch("https://api.anysearch.com/v1/search", "", "query", 10, provider, 10)
        self.assertEqual("AnySearch business error (code 1002)", str(ctx.exception))
        self.assertNotIn("CRITICAL_SECRET_KEY_12345", str(ctx.exception))

    def test_bocha_business_error_is_sanitized_and_quota_aware(self):
        """测试 Bocha 业务错误响应体脱敏并正确识别配额耗尽标签"""
        gateway = self.make_gateway()
        fake_payload = {"code": 402, "msg": "INTERNAL_TRACE_ID_999: 账户余额不足，额度耗尽"}
        with patch.object(gateway, "_post", return_value=fake_payload):
            with self.assertRaises(SearchError) as ctx:
                gateway._bocha("https://api.bocha.cn/v1/web-search", "key", "query", 10, {}, 10)
        self.assertEqual("Bocha business error (code 402): quota exhausted", str(ctx.exception))
        self.assertNotIn("INTERNAL_TRACE_ID_999", str(ctx.exception))
        self.assertTrue(ctx.exception.retryable)

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_multi_search_success_aggregates_and_deduplicates(self):
        """测试多源并发聚合搜索：多家引擎同时返回结果，智能去重、合并摘要并置顶多源高信度条目"""
        gateway = self.make_gateway()

        def fake_call(provider, query, max_results, arguments, api_key, timeout):
            if provider["id"] == "tavily-search":
                return [
                    {"title": "Shared Short", "url": "https://example.com/shared?utm_source=twitter", "snippet": "Tavily short text", "published_date": ""},
                    {"title": "Tavily Only", "url": "https://example.com/tavily", "snippet": "Tavily unique article", "published_date": "2026-01-01"},
                ]
            if provider["id"] == "exa":
                return [
                    {"title": "Shared Better Title", "url": "https://example.com/shared/", "snippet": "Exa much longer and detailed text", "published_date": "2026-03-01"},
                    {"title": "Exa Only", "url": "https://example.com/exa", "snippet": "Exa unique article", "published_date": "2026-02-01"},
                ]
            return []

        with patch.object(gateway, "_call", side_effect=fake_call):
            result = gateway.multi_search({"query": "test"})

        self.assertEqual("multi_web_search", result["tool"])
        self.assertIn("tavily-search", result["active_providers"])
        self.assertIn("exa", result["active_providers"])
        self.assertEqual(2, result["total_sources_queried"])
        
        # 结果应去重（从 4 条去重为 3 条）
        results = result["results"]
        self.assertEqual(3, len(results))

        # 多源交叉验证的条目应置顶在第一条
        top_item = results[0]
        self.assertIn(top_item["url"], ("https://example.com/shared?utm_source=twitter", "https://example.com/shared/"))
        self.assertIn("tavily-search", top_item["sources"])
        self.assertIn("exa", top_item["sources"])
        self.assertEqual(2, len(top_item["sources"]))
        # 保留了更详尽的标题和摘要以及发布时间
        self.assertEqual("Shared Better Title", top_item["title"])
        self.assertEqual("Exa much longer and detailed text", top_item["snippet"])
        self.assertEqual("2026-03-01", top_item["published_date"])

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_multi_search_one_provider_fails_others_succeed(self):
        """测试多源并发容错：某一家引擎限流或故障，不影响其他引擎成功聚合，并附带故障警告"""
        gateway = self.make_gateway()

        def fake_call(provider, query, max_results, arguments, api_key, timeout):
            if provider["id"] == "tavily-search":
                raise SearchError("HTTP 429", retryable=True)
            if provider["id"] == "exa":
                return [{"title": "Exa Title", "url": "https://example.com/exa", "snippet": "Exa content", "published_date": ""}]
            return []

        with patch.object(gateway, "_call", side_effect=fake_call):
            result = gateway.multi_search({"query": "test"})

        self.assertEqual("multi_web_search", result["tool"])
        self.assertEqual(["exa"], result["active_providers"])
        self.assertEqual(1, len(result["results"]))
        self.assertEqual("https://example.com/exa", result["results"][0]["url"])
        self.assertIn("warning", result)
        self.assertIn("tavily-search (HTTP 429)", result["warning"])
        # Tavily 应进入冷却
        self.assertFalse(gateway.cooldowns.is_available("web_search:tavily-search"))

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_multi_search_all_providers_fail_raises_error(self):
        """测试所有引擎均失败时抛出异常并附带诊断指引"""
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=SearchError("HTTP 401", retryable=False)):
            with self.assertRaises(SearchError) as ctx:
                gateway.multi_search({"query": "test"})
        self.assertIn("No search provider succeeded", str(ctx.exception))
        self.assertIn("或联系MCP提供商", str(ctx.exception))

    def test_url_normalization(self):
        """测试 URL 规范化清洗函数剥离参数和末尾斜杠"""
        self.assertEqual("https://example.com/test", SearchGateway._normalize_url("https://EXAMPLE.com/test/"))
        self.assertEqual("https://example.com/test", SearchGateway._normalize_url("https://example.com/test?utm_source=twitter&utm_medium=social"))
        self.assertEqual("https://example.com/test?q=python", SearchGateway._normalize_url("https://example.com/test/?q=python&ref=123"))


class McpServerTest(unittest.TestCase):
    def test_initialize_and_tools_list(self):
        server = McpServer(None, None, FakeLogger())
        initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual("local-search-fallback-gateway", initialized["result"]["serverInfo"]["name"])
        import mcp_gateway
        self.assertEqual("1.5.0", mcp_gateway.__version__)
        self.assertEqual("1.5.0", initialized["result"]["serverInfo"]["version"])
        self.assertEqual(mcp_gateway.__version__, initialized["result"]["serverInfo"]["version"])
        tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in tools["result"]["tools"]]
        self.assertEqual(["fallback_web_search", "multi_web_search", "fallback_web_extract"], names)

    def test_invalid_tool_arguments_return_tool_error(self):
        server = McpServer(None, None, FakeLogger())
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "fallback_web_search", "arguments": "not-an-object"}})
        self.assertEqual(-32602, response["error"]["code"])

    def test_server_write_handles_non_gbk_unicode(self):
        import io
        fake_stdout = io.StringIO()
        with patch("sys.stdout", fake_stdout):
            McpServer._write({"jsonrpc": "2.0", "id": 1, "result": {"snippet": "Caduceus \u2624 phonetic \u02c8 warning ⚠️"}})
        output = fake_stdout.getvalue()
        self.assertIn("Caduceus \u2624 phonetic \u02c8 warning ⚠️", output)

    def test_server_write_fallbacks_to_ascii_on_encode_error(self):
        written = []
        call_count = 0

        def fake_write(data):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise UnicodeEncodeError("gbk", "\u2624", 0, 1, "illegal multibyte sequence")
            written.append(data)

        with patch("sys.stdout.write", side_effect=fake_write), patch("sys.stdout.flush"):
            McpServer._write({"jsonrpc": "2.0", "id": 1, "result": {"symbol": "\u2624"}})

        self.assertEqual(2, call_count)
        self.assertEqual(1, len(written))
        self.assertIn("\\u2624", written[0])

    def test_server_run_preserves_request_id_on_unhandled_failure(self):
        import io
        fake_stdout = io.StringIO()
        fake_stdin = io.StringIO('{"jsonrpc":"2.0","id":999,"method":"tools/call","params":{"name":"fallback_web_search","arguments":{"query":"test"}}}\n')
        server = McpServer(None, None, FakeLogger())
        with patch.object(server, "handle", side_effect=RuntimeError("simulated crash")), \
             patch("sys.stdin", fake_stdin), \
             patch("sys.stdout", fake_stdout):
            server.run()
        output = fake_stdout.getvalue()
        response = json.loads(output.strip())
        self.assertEqual(999, response.get("id"))
        self.assertEqual(-32603, response["error"]["code"])
        self.assertIn("simulated crash", response["error"]["message"])

    def test_adaptive_tools_registration_and_call(self):
        from mcp_gateway.server import ToolDefinition
        fake_tool_schema = {"name": "custom_tool", "description": "Custom test tool"}
        server = McpServer(None, None, FakeLogger(), tools={
            "custom_tool": ToolDefinition(schema=fake_tool_schema, handler=lambda args: {"echo": args.get("val")})
        })
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(1, len(tools["result"]["tools"]))
        self.assertEqual("custom_tool", tools["result"]["tools"][0]["name"])

        called = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "custom_tool", "arguments": {"val": 42}}})
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(42, called["result"]["structuredContent"]["echo"])


class AdaptiveConfigTest(unittest.TestCase):
    def test_glob_config_loading(self):
        from mcp_gateway.config import load_config
        config = load_config(Path(__file__).resolve().parent.parent / "config" / "gateway.json")
        self.assertIn("web_search", config["capabilities"])
        self.assertIn("web_extract", config["capabilities"])


class ExtractGatewayTest(unittest.TestCase):
    def test_private_url_is_rejected_before_provider_call(self):
        gateway = ExtractGateway({"providers": []}, Path(tempfile.mkdtemp()) / "state.json", FakeLogger())
        with self.assertRaises(SearchError):
            gateway.extract({"urls": ["http://127.0.0.1:8080/admin"]})

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a"}, clear=False)
    def test_extract_falls_back_after_rate_limit(self):
        gateway = ExtractGateway({"cooldown_seconds": 60, "providers": [
            {"id": "tavily-search", "enabled": True, "priority": 10, "api_key_env": "TAVILY_API_KEY", "endpoint": "https://example.test/tavily"},
            {"id": "exa", "enabled": True, "priority": 20, "api_key_env": "EXA_API_KEY", "endpoint": "https://example.test/exa"},
        ]}, Path(tempfile.mkdtemp()) / "state.json", FakeLogger())
        with patch.dict(os.environ, {"EXA_API_KEY": "b"}, clear=False), patch.object(gateway, "_validate_public_url"), patch.object(gateway, "_extract", side_effect=[SearchError("HTTP 429", retryable=True), [{"url": "https://example.com", "title": "ok", "content": "body", "truncated": False}]]) as extract:
            result = gateway.extract({"urls": ["https://example.com"]})
        self.assertEqual("exa", result["results"][0]["provider"])
        self.assertIn("warning", result)
        self.assertEqual(
            "⚠️ 警告：检测到后端服务配额耗尽或请求限流 [tavily-search (HTTP 429)]。请耐心等待配额重置稍后重试或联系MCP提供商。",
            result["warning"],
        )

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_extract_all_providers_quota_exhausted_warning(self):
        gateway = ExtractGateway({"cooldown_seconds": 60, "providers": [
            {"id": "tavily-search", "enabled": True, "priority": 10, "api_key_env": "TAVILY_API_KEY", "endpoint": "https://example.test/tavily"},
            {"id": "exa", "enabled": True, "priority": 20, "api_key_env": "EXA_API_KEY", "endpoint": "https://example.test/exa"},
        ]}, Path(tempfile.mkdtemp()) / "state.json", FakeLogger())
        with patch.object(gateway, "_validate_public_url"), patch.object(gateway, "_extract", side_effect=SearchError("HTTP 429", retryable=True)):
            result = gateway.extract({"urls": ["https://example.com"]})
        self.assertIn("warning", result)
        self.assertEqual("⚠️  警告：检测到后端服务配额均耗尽，请耐心等待相关配额重置或联系MCP提供商。", result["warning"])

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_extract_auth_error_falls_back_and_is_reported(self):
        """Backend auth/param rejection must not terminate the extract fallback chain.

        When the first extract provider returns a non-retryable error (e.g. 401),
        the gateway should record the failure reason and continue to the next
        provider.  The successful result must carry the prior failure details.
        """
        gateway = ExtractGateway({"cooldown_seconds": 60, "providers": [
            {"id": "tavily-search", "enabled": True, "priority": 10, "api_key_env": "TAVILY_API_KEY", "endpoint": "https://example.test/tavily"},
            {"id": "exa", "enabled": True, "priority": 20, "api_key_env": "EXA_API_KEY", "endpoint": "https://example.test/exa"},
        ]}, Path(tempfile.mkdtemp()) / "state.json", FakeLogger())
        with patch.object(gateway, "_validate_public_url"), \
             patch.object(gateway, "_extract", side_effect=[
                 SearchError("HTTP 401", retryable=False),
                 [{"url": "https://example.com", "title": "ok", "content": "body", "truncated": False}],
             ]) as extract:
            result = gateway.extract({"urls": ["https://example.com"]})
        self.assertEqual("exa", result["results"][0]["provider"])
        self.assertEqual(1, len(result["failures"]))
        self.assertEqual("tavily-search", result["failures"][0]["provider"])
        self.assertEqual("HTTP 401", result["failures"][0]["reason"])
        self.assertIn("warning", result)
        self.assertIn("tavily-search (HTTP 401)", result["warning"])
        self.assertIn("或联系MCP提供商", result["warning"])
        self.assertEqual(2, extract.call_count)

    def test_jina_extract_normalizes_response(self):
        gateway = ExtractGateway({"providers": []}, Path(tempfile.mkdtemp()) / "state.json", FakeLogger())
        fake_data = {"code": 200, "data": {"title": "Page Title", "url": "https://example.com", "content": "Clean page markdown"}}
        with patch.object(gateway, "_post", return_value=fake_data) as post:
            results = gateway._jina_extract("https://r.jina.ai", "fake-key", ["https://example.com"], 5000, 10)
        self.assertEqual(1, len(results))
        self.assertEqual("Page Title", results[0]["title"])
        self.assertEqual("Clean page markdown", results[0]["content"])
        self.assertEqual("Bearer fake-key", post.call_args.args[1]["Authorization"])
        self.assertEqual("application/json", post.call_args.args[1]["Accept"])


class SearchCooldownTest(unittest.TestCase):
    """Verify that non-retryable (auth/param) errors do NOT set cooldown."""

    def make_gateway(self):
        return SearchGateway({"cooldown_seconds": 60, "quota_cooldown_seconds": 259200, "providers": [
            {"id": "tavily-search", "enabled": True, "priority": 10, "api_key_env": "TAVILY_API_KEY", "endpoint": "https://example.test/tavily"},
            {"id": "exa", "enabled": True, "priority": 20, "api_key_env": "EXA_API_KEY", "endpoint": "https://example.test/exa"},
        ]}, Path(tempfile.mkdtemp()) / "state.json", FakeLogger())

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_non_retryable_error_does_not_set_cooldown(self):
        """Auth (401) or bad-request (400) errors should not put the provider
        into cooldown — those problems won't self-heal with time, so the next
        request should still attempt the provider."""
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=[
            SearchError("HTTP 401", retryable=False),
            [{"title": "ok", "url": "u", "snippet": "s", "published_date": ""}],
        ]):
            gateway.search({"query": "test"})
        # tavily-search should NOT be in cooldown after a non-retryable error
        self.assertTrue(gateway.cooldowns.is_available("web_search:tavily-search"))

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_retryable_error_sets_cooldown(self):
        """Contrast: a retryable error (429) SHOULD put the provider into cooldown."""
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=[
            SearchError("HTTP 429", retryable=True),
            [{"title": "ok", "url": "u", "snippet": "s", "published_date": ""}],
        ]):
            gateway.search({"query": "test"})
        self.assertFalse(gateway.cooldowns.is_available("web_search:tavily-search"))

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_rate_limit_sets_standard_cooldown(self):
        """测试 429 频率超限：触发常规 short cooldown（例如 60s / 1小时）"""
        import time
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=[
            SearchError("HTTP 429", retryable=True),
            [{"title": "ok", "url": "u", "snippet": "s", "published_date": ""}],
        ]):
            gateway.search({"query": "test"})
        expire_time = gateway.cooldowns.get_until("web_search:tavily-search")
        self.assertLess(expire_time, time.time() + 120)
        self.assertGreater(expire_time, time.time() + 30)

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_quota_exhausted_sets_longer_tiered_cooldown(self):
        """测试 402/432 配额耗尽：触发分级智能长冷却（3天 / 259200秒）"""
        import time
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=[
            SearchError("HTTP 402: quota exhausted", retryable=True, status=402),
            [{"title": "ok", "url": "u", "snippet": "s", "published_date": ""}],
        ]):
            gateway.search({"query": "test"})
        expire_time = gateway.cooldowns.get_until("web_search:tavily-search")
        self.assertGreater(expire_time, time.time() + 200000)

    def test_key_change_auto_resets_cooldown(self):
        """测试自适应特性 2：更换 API Key 时自动解冻复活服务，无需手动删文件"""
        gateway = self.make_gateway()
        # 模拟旧 Key 触发 3 天冷却
        gateway.cooldowns.set("web_search:tavily-search", 259200, reason_type="quota", api_key="old-key-123")
        # 拿着旧 Key 查，仍在冷却中
        self.assertFalse(gateway.cooldowns.is_available("web_search:tavily-search", current_key="old-key-123"))
        # 用户在 .env 中换了新 Key 查，自适应立刻解除冷却！
        self.assertTrue(gateway.cooldowns.is_available("web_search:tavily-search", current_key="new-key-456"))
        # 冷却记录已被自动抹除
        self.assertEqual(0.0, gateway.cooldowns.get_until("web_search:tavily-search"))

    def test_month_rollover_auto_resets_quota_cooldown(self):
        """测试自适应特性 1：跨入新月份时，月初自动复活配额耗尽的服务，无需手动干预"""
        gateway = self.make_gateway()
        # 模拟上月 (2026-09) 记录的配额耗尽冷却
        gateway.cooldowns._state["web_search:tavily-search"] = {
            "until": 9999999999.0,  # 遥远的未来
            "reason_type": "quota",
            "month": "2026-09",
            "key_hash": ""
        }
        # 当前时间（2026-10）检测到月份不一致，自适应立刻解封！
        self.assertTrue(gateway.cooldowns.is_available("web_search:tavily-search"))
        # 冷却记录已被自动清除
        self.assertEqual(0.0, gateway.cooldowns.get_until("web_search:tavily-search"))


class MainEntryPointTest(unittest.TestCase):
    def test_mcp_gateway_directory_execution(self):
        import subprocess
        import sys
        pkg_path = Path(__file__).resolve().parent.parent / "mcp_gateway"
        self.assertTrue(pkg_path.exists(), "mcp_gateway directory must exist")
        proc = subprocess.run([sys.executable, str(pkg_path), "--help"], capture_output=True, text=True)
        self.assertEqual(0, proc.returncode)
        self.assertIn("Local MCP search fallback gateway", proc.stdout)

    def test_mcp_gateway_main_py_execution(self):
        import subprocess
        import sys
        main_path = Path(__file__).resolve().parent.parent / "mcp_gateway" / "__main__.py"
        self.assertTrue(main_path.exists(), "__main__.py must exist")
        proc = subprocess.run([sys.executable, str(main_path), "--help"], capture_output=True, text=True)
        self.assertEqual(0, proc.returncode)
        self.assertIn("Local MCP search fallback gateway", proc.stdout)



# =========================================================================
# 【功能块：自定义能力分类与引擎协议动态路由测试套件】
# - 输入：临时配置文件与伪造测试环境
# - 功能：验证动态加载自定义能力（如 academic_search）、engine_type 协议映射、
#         冷却隔离以及 GUI 辅助工具的核心逻辑
# - 输出：单元测试断言
# =========================================================================
class CustomCapabilityAndGuiTest(unittest.TestCase):
    def test_custom_capability_dynamic_registration_and_dispatch(self):
        """测试扩展能力（例如 academic_search）在 server 中被动态注册并在 MCP tools/list 和 tools/call 中正常暴露"""
        from mcp_gateway.server import ToolDefinition, SEARCH_TOOL_SCHEMA
        from mcp_gateway.search import SearchGateway

        cap_def = {
            "name": "academic_search",
            "description": "Custom academic search capability",
            "providers": [
                {"id": "arxiv", "type": "tavily-search", "enabled": True, "priority": 10, "api_key_env": "ARXIV_KEY", "endpoint": "https://example.test/arxiv"}
            ]
        }
        gw = SearchGateway(cap_def, Path(tempfile.mkdtemp()) / "state.json", FakeLogger())
        custom_schema = {
            "name": "academic_search",
            "description": cap_def["description"],
            "inputSchema": SEARCH_TOOL_SCHEMA["inputSchema"]
        }
        server = McpServer(None, None, FakeLogger(), tools={
            "academic_search": ToolDefinition(schema=custom_schema, handler=gw.search)
        })

        # 1. 验证 tools/list 自适应暴露出 academic_search 工具
        tools_resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools_list = tools_resp["result"]["tools"]
        tool_names = [t["name"] for t in tools_list]
        self.assertIn("academic_search", tool_names)

        # 2. 验证 tools/call 成功路由并派发到 custom gateway
        with patch.dict(os.environ, {"ARXIV_KEY": "dummy_val"}), patch.object(gw, "_call", return_value=[{"title": "Paper", "url": "https://arxiv.org", "snippet": "Research", "published_date": "2026-10"}]):
            call_resp = server.handle({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "academic_search", "arguments": {"query": "quantum computing"}}
            })
            self.assertFalse(call_resp["result"]["isError"])
            results = call_resp["result"]["structuredContent"]["results"]
            self.assertEqual(1, len(results))
            self.assertEqual("Paper", results[0]["title"])

    def test_search_gateway_custom_capability_cooldown_and_engine_type(self):
        """测试自定义能力下的独立冷却隔离及 engine_type 自适应协议映射"""
        cap_def = {
            "name": "code_search",
            "providers": [
                {"id": "github-search", "type": "tavily-search", "enabled": True, "priority": 10, "api_key_env": "GH_KEY", "endpoint": "https://example.test/gh"}
            ]
        }
        gw = SearchGateway(cap_def, Path(tempfile.mkdtemp()) / "state.json", FakeLogger())
        # 冷却键名应带有分类前缀进行独立隔离
        self.assertEqual("code_search:github-search", gw._cooldown_id("github-search"))

        # 验证 type 字段被识别并路由至 _tavily 适配器
        with patch.object(gw, "_validate_endpoint"), patch.object(gw, "_tavily", return_value=[{"title": "Repo", "url": "https://git.test", "snippet": "code", "published_date": ""}]) as mock_tavily:
            res = gw._call(cap_def["providers"][0], "query", 5, {}, "key", 10)
            self.assertEqual(1, len(res))
            self.assertEqual("Repo", res[0]["title"])
            mock_tavily.assert_called_once()

    def test_gui_dotenv_saver(self):
        """测试 GUI 环境变量安全同步更新函数"""
        from mcp_gateway.gui_add_provider import save_api_key_to_dotenv
        import mcp_gateway.gui_add_provider as gui_mod

        tmp_dir = Path(tempfile.mkdtemp())
        tmp_env = tmp_dir / ".env"
        tmp_env.write_text("# Existing comment\nOLD_KEY=old_val\n", encoding="utf-8")

        with patch.object(gui_mod, "ENV_PATH", tmp_env), patch.object(gui_mod, "ENV_EXAMPLE_PATH", tmp_dir / ".env.example"):
            # 1. 更新已有变量
            save_api_key_to_dotenv("OLD_KEY", "new_val")
            # 2. 追加新变量
            save_api_key_to_dotenv("NEW_KEY", "secret_123")

    def test_hermes_helper_snippets(self):
        """测试 Hermes Desktop 配置片段生成器能够正确获取解释器与项目文件绝对路径"""
        from mcp_gateway.hermes_helper import generate_hermes_snippets
        snippets = generate_hermes_snippets()
        self.assertIn("python_exe", snippets)
        self.assertIn("MCPHub:", snippets["yaml"])
        self.assertIn("gateway.json", snippets["yaml"])
        self.assertIn("PYTHONUTF8", snippets["yaml"])
        self.assertIn("MCPHub", snippets["json"])


    def test_custom_anysearch_instance_anonymous_quota(self):
        """测试使用自定义 ID 的 AnySearch 引擎（type='anysearch'）也能享受免 Key 匿名配额"""
        cap_def = {
            "name": "web_search",
            "providers": [
                {"id": "anysearch-custom", "type": "anysearch", "enabled": True, "priority": 10, "api_key_env": "UNSET_KEY", "endpoint": "https://api.anysearch.com/v1/search"}
            ]
        }
        gw = SearchGateway(cap_def, Path(tempfile.mkdtemp()) / "state.json", FakeLogger())
        with patch.dict(os.environ, {}, clear=True), patch.object(gw, "_call", return_value=[{"title": "ok", "url": "u", "snippet": "s", "published_date": ""}]):
            # 不应该抛出 'API key is not configured'
            result = gw.search({"query": "test"})
            self.assertEqual("anysearch-custom", result["provider"])

    def test_logger_sanitization_on_business_errors(self):
        """测试业务错误日志脱敏：确保上游可能包含敏感 Token 或凭证的原始报文不会写入日志"""
        captured_warnings = []
        class RecordingLogger:
            def warning(self, msg, *args):
                captured_warnings.append(msg % args if args else msg)
            def exception(self, *args):
                pass
            def info(self, *args):
                pass

        logger = RecordingLogger()
        gw = SearchGateway({"providers": []}, Path(tempfile.mkdtemp()) / "state.json", logger)
        secret_text = "LEAKED_SECRET_CREDENTIAL_XYZ"
        fake_payload = {"code": 5001, "message": f"Fatal error: {secret_text}"}

        with patch.object(gw, "_post", return_value=fake_payload):
            with self.assertRaises(SearchError):
                gw._anysearch("https://api.anysearch.com/v1/search", "", "query", 5, {"id": "anysearch"}, 10)

        # 验证捕获到的 warning 日志中绝对不包含 secret_text
        self.assertTrue(any("AnySearch business error: code=5001" in log for log in captured_warnings))
        for log in captured_warnings:
            self.assertNotIn(secret_text, log)


if __name__ == "__main__":
    unittest.main()

