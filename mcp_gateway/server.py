"""Minimal JSON-RPC stdio MCP server implementation."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .config import ConfigError, load_config, resolve_project_path
from .search import ExtractGateway, SearchError, SearchGateway

SEARCH_TOOL_SCHEMA = {
    "name": "fallback_web_search",
    "description": "Search the web with automatic provider fallback: Tavily, Exa, AnySearch, then Bocha Web Search API.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            "search_depth": {"type": "string", "enum": ["basic", "advanced"], "default": "basic"},
            "time_range": {"type": "string", "enum": ["day", "week", "month", "year"]},
            "include_domains": {"type": "array", "items": {"type": "string"}},
            "exclude_domains": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["query"],
        "additionalProperties": False
    }
}

EXTRACT_TOOL_SCHEMA = {
    "name": "fallback_web_extract",
    "description": "Extract clean page content with automatic fallback: Tavily, Exa, then AnySearch. Bocha is search-only.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "urls": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "string", "format": "uri"}},
            "extract_depth": {"type": "string", "enum": ["basic", "advanced"], "default": "basic"},
            "max_characters": {"type": "integer", "minimum": 1000, "maximum": 100000, "default": 10000}
        },
        "required": ["urls"],
        "additionalProperties": False
    }
}


def _logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mcp_gateway")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


class McpServer:
    def __init__(self, search_gateway: SearchGateway, extract_gateway: ExtractGateway, logger: logging.Logger):
        self.search_gateway = search_gateway
        self.extract_gateway = extract_gateway
        self.logger = logger

    def run(self) -> None:
        for raw in sys.stdin:
            try:
                message = json.loads(raw)
                if isinstance(message, dict):
                    response = self.handle(message)
                    if response is not None:
                        self._write(response)
            except Exception as exc:  # Keep protocol alive after a malformed request.
                self.logger.exception("Unhandled MCP request failure")
                self._write(self._error(None, -32603, f"Internal server error: {exc}"))

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return self._error(request_id, -32600, "Invalid Request") if "id" in message else None
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return self._result(request_id, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "local-search-fallback-gateway", "version": "1.0.0"}})
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": [SEARCH_TOOL_SCHEMA, EXTRACT_TOOL_SCHEMA]})
        if method == "tools/call":
            return self._tool_call(request_id, message.get("params"))
        return self._error(request_id, -32601, f"Method not found: {method}") if "id" in message else None

    def _tool_call(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict) or params.get("name") not in ("fallback_web_search", "fallback_web_extract"):
            return self._error(request_id, -32602, "Only fallback_web_search and fallback_web_extract are available")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return self._error(request_id, -32602, "arguments must be an object")
        try:
            if params["name"] == "fallback_web_search":
                payload = self.search_gateway.search(arguments)
            else:
                payload = self.extract_gateway.extract(arguments)
            encoded = json.dumps(payload, ensure_ascii=False)
            return self._result(request_id, {"content": [{"type": "text", "text": encoded}], "structuredContent": payload, "isError": False})
        except SearchError as exc:
            self.logger.warning("Gateway request failed: %s", exc)
            return self._result(request_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _write(message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local MCP search fallback gateway")
    parser.add_argument("--config", required=True, help="Path to config/gateway.json")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        logger = _logger(resolve_project_path(config, "log_file", "../logs/gateway.log"))
        state_path = resolve_project_path(config, "state_file", "../state/provider-state.json")
        search_gateway = SearchGateway(config["capabilities"]["web_search"], state_path, logger)
        extract_gateway = ExtractGateway(config["capabilities"]["web_extract"], state_path, logger)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    McpServer(search_gateway, extract_gateway, logger).run()
