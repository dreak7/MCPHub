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
        self.assertIn("tavily-search (HTTP 429)", result["warning"])
        self.assertIn("配额耗尽或请求限流", result["warning"])

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
        self.assertIn("tavily-search (HTTP 402: quota exhausted)", result["warning"])
        self.assertIn("配额耗尽或请求限流", result["warning"])
        self.assertIn("或联系MCP提供商", result["warning"])

    def test_anysearch_uses_general_search_and_normalizes_response(self):
        gateway = self.make_gateway()
        provider = {"id": "anysearch", "endpoint": "https://example.test/anysearch"}
        with patch.object(gateway, "_post", return_value={"code": 0, "data": {"results": [{"title": "Result", "url": "https://example.test", "snippet": "Text"}]}}) as post:
            results = gateway._anysearch("https://example.test/anysearch", "", "query", 20, provider, 10)
        self.assertEqual("Result", results[0]["title"])
        self.assertEqual(10, post.call_args.args[2]["max_results"])
        self.assertNotIn("tag", post.call_args.args[2])


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
        self.assertIn("tavily-search (HTTP 429)", result["warning"])
        self.assertIn("配额耗尽或请求限流", result["warning"])
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


if __name__ == "__main__":
    unittest.main()
