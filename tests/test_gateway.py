import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_gateway.search import SearchError, SearchGateway
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
        self.assertFalse(gateway.cooldowns.is_available("tavily-search"))

    @patch.dict(os.environ, {"TAVILY_API_KEY": "a", "EXA_API_KEY": "b"}, clear=False)
    def test_auth_error_does_not_fall_back(self):
        gateway = self.make_gateway()
        with patch.object(gateway, "_call", side_effect=SearchError("HTTP 401", retryable=False)) as call:
            with self.assertRaises(SearchError):
                gateway.search({"query": "test"})
        self.assertEqual(1, call.call_count)

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
        server = McpServer(None, FakeLogger())
        initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual("local-search-fallback-gateway", initialized["result"]["serverInfo"]["name"])
        tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual("web_search", tools["result"]["tools"][0]["name"])

    def test_invalid_tool_arguments_return_tool_error(self):
        server = McpServer(None, FakeLogger())
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "web_search", "arguments": "not-an-object"}})
        self.assertEqual(-32602, response["error"]["code"])


if __name__ == "__main__":
    unittest.main()
