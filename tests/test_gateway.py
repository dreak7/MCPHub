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


class McpServerTest(unittest.TestCase):
    def test_initialize_and_tools_list(self):
        server = McpServer(None, None, FakeLogger())
        initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual("local-search-fallback-gateway", initialized["result"]["serverInfo"]["name"])
        self.assertEqual("1.2.0", initialized["result"]["serverInfo"]["version"])
        tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual("fallback_web_search", tools["result"]["tools"][0]["name"])
        self.assertEqual("fallback_web_extract", tools["result"]["tools"][1]["name"])

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
        return SearchGateway({"cooldown_seconds": 60, "providers": [
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


if __name__ == "__main__":
    unittest.main()
