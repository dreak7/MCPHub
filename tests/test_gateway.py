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

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_auth_error_falls_back_and_is_reported(self):
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=[SearchError("HTTP 401", retryable=False), [{"title": "ok", "url": "u", "snippet": "s", "published_date": ""}]]) as call:
            result = gateway.search({"query": "test"})
        self.assertEqual("exa", result["provider"])
        self.assertEqual("HTTP 401", result["provider_failures"][0]["reason"])
        self.assertEqual(2, call.call_count)

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
        self.assertEqual(2, extract.call_count)


if __name__ == "__main__":
    unittest.main()
