"""Minimal JSON-RPC stdio MCP server implementation."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import ConfigError, load_config, resolve_project_path
from .search import ExtractGateway, SearchError, SearchGateway

SEARCH_TOOL_SCHEMA = {
    "name": "fallback_web_search",
    "description": (
        "Search the web with automatic provider fallback: Tavily, Exa, Jina, AnySearch, then Bocha Web Search API. "
        "Best for fast, economic, single-source queries with automatic fallback on failure. "
        "IMPORTANT: If the returned result contains a 'warning' field, you MUST display that warning prominently to the user at the top of your answer."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            "search_depth": {"type": "string", "enum": ["basic", "advanced"], "default": "basic"},
            "time_range": {"type": "string", "enum": ["day", "week", "month", "year"]},
            "include_domains": {"type": "array", "items": {"type": "string"}},
            "exclude_domains": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["query"],
        "additionalProperties": False
    }
}

MULTI_SEARCH_TOOL_SCHEMA = {
    "name": "multi_web_search",
    "description": (
        "Concurrently search the web across all configured providers (Tavily, Exa, Jina, Bocha, AnySearch) "
        "and synthesize deduplicated results. Best for in-depth research, comprehensive reviews, and multi-source verification. "
        "IMPORTANT: If the returned result contains a 'warning' field, you MUST display that warning prominently to the user at the top of your answer."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
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
    "description": (
        "Extract clean page content with automatic fallback: Tavily, Exa, Jina, then AnySearch. Bocha is search-only. "
        "IMPORTANT: If the returned result contains a 'warning' field, you MUST display that warning prominently to the user at the top of your answer."
    ),
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


# =========================================================================
# 【功能块：MCP 工具定义实体数据类】
# - 输入：schema (dict): 工具元数据描述（包含名称、描述与参数模型）；
#         handler (Callable | None): 工具执行回调函数（接收参数字典并返回结构化结果）
# - 功能：封装工具的对外接口契约与实际执行逻辑，实现工具定义的动态插拔与解耦
# - 输出：ToolDefinition 实例
# =========================================================================
@dataclass
class ToolDefinition:
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None


# =========================================================================
# 【功能块：配置并初始化文件日志记录器】
# - 输入：path (Path): 日志文件的完整路径
# - 功能：创建日志目录并初始化以 UTF-8 编码写入的全局日志记录器
# - 输出：logging.Logger 实例
# =========================================================================
def _logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mcp_gateway")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


# =========================================================================
# 【功能块：配置标准输入输出流编码】
# - 输入：sys.stdin, sys.stdout, sys.stderr
# - 功能：将标准输入、标准输出、标准错误流重新配置为 UTF-8 编码并替换非法字节，
#         避免在 Windows 简体中文系统（默认 GBK 编码）下输出 Unicode 字符
#         （如医学符号 ☤、音标 ˈ、Emoji 等）时触发 UnicodeEncodeError 导致程序崩溃或超时。
# - 输出：无（原地重配标准流）
# =========================================================================
def _configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


# =========================================================================
# 【功能块：MCP 服务端核心类（支持自适应多工具注册与派发）】
# - 输入：search_gateway, extract_gateway, logger 实例，及可选的 tools 工具注册字典
# - 功能：遵循 MCP JSON-RPC 协议规范，通过 stdio 监听并动态响应客户端指令；
#         支持动态工具清单自适应上报（tools/list）与动态工具路由派发（tools/call）
# - 输出：McpServer 实例
# =========================================================================
class McpServer:
    def __init__(
        self,
        search_gateway: SearchGateway | None = None,
        extract_gateway: ExtractGateway | None = None,
        logger: logging.Logger | None = None,
        tools: dict[str, ToolDefinition] | None = None,
    ):
        self.search_gateway = search_gateway
        self.extract_gateway = extract_gateway
        self.logger = logger or logging.getLogger("mcp_gateway")
        
        if tools is not None:
            self.tools: dict[str, ToolDefinition] = dict(tools)
        else:
            self.tools: dict[str, ToolDefinition] = {}
            # 保持与旧版直接传入 search_gateway / extract_gateway 调用方式的完全向后兼容
            if SEARCH_TOOL_SCHEMA:
                search_handler = (lambda args: self.search_gateway.search(args)) if self.search_gateway else None
                self.tools["fallback_web_search"] = ToolDefinition(schema=SEARCH_TOOL_SCHEMA, handler=search_handler)
                multi_search_handler = (lambda args: self.search_gateway.multi_search(args)) if self.search_gateway else None
                self.tools["multi_web_search"] = ToolDefinition(schema=MULTI_SEARCH_TOOL_SCHEMA, handler=multi_search_handler)
            if EXTRACT_TOOL_SCHEMA:
                extract_handler = (lambda args: self.extract_gateway.extract(args)) if self.extract_gateway else None
                self.tools["fallback_web_extract"] = ToolDefinition(schema=EXTRACT_TOOL_SCHEMA, handler=extract_handler)

    def run(self) -> None:
        _configure_stdio()
        for raw in sys.stdin:
            message = None
            try:
                message = json.loads(raw)
                if isinstance(message, dict):
                    response = self.handle(message)
                    if response is not None:
                        self._write(response)
            except Exception as exc:  # Keep protocol alive after a malformed request.
                self.logger.exception("Unhandled MCP request failure")
                request_id = message.get("id") if isinstance(message, dict) else None
                self._write(self._error(request_id, -32603, f"Internal server error: {exc}"))

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return self._error(request_id, -32600, "Invalid Request") if "id" in message else None
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return self._result(request_id, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "local-search-fallback-gateway", "version": "1.5.0"}})
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            # 自适应返回当前所有已注册工具的 Schema 描述清单
            return self._result(request_id, {"tools": [tool.schema for tool in self.tools.values()]})
        if method == "tools/call":
            return self._tool_call(request_id, message.get("params"))
        return self._error(request_id, -32601, f"Method not found: {method}") if "id" in message else None

    def _tool_call(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "params must be an object")
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or tool_name not in self.tools:
            available = ", ".join(self.tools.keys())
            return self._error(request_id, -32602, f"Unknown tool: {tool_name}. Available: {available}")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return self._error(request_id, -32602, "arguments must be an object")

        tool = self.tools[tool_name]
        if tool.handler is None:
            return self._error(request_id, -32602, f"Tool handler for {tool_name} is not initialized")

        try:
            payload = tool.handler(arguments)
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
        if hasattr(sys.stdout, "reconfigure") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        try:
            sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except UnicodeEncodeError:
            sys.stdout.write(json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n")
            sys.stdout.flush()


# =========================================================================
# 【功能块：服务主函数启动入口（支持自适应能力按需挂载）】
# - 输入：命令行参数（--config 配置文件路径）
# - 功能：初始化标准流 UTF-8 编码，加载配置文件与状态持久化模块，按需动态挂载
#         已配置的各项能力工具，启动 MCP JSON-RPC 监听服务
# - 输出：无（持续运行 stdio 通信服务直到退出）
# =========================================================================
def main() -> None:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Local MCP search fallback gateway")
    parser.add_argument("--config", required=True, help="Path to config/gateway.json")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        logger = _logger(resolve_project_path(config, "log_file", "../logs/gateway.log"))
        state_path = resolve_project_path(config, "state_file", "../state/provider-state.json")

        capabilities = config.get("capabilities", {})
        tools: dict[str, ToolDefinition] = {}

        # 动态自适应挂载能力：如果配置了搜索，则装载常规单源搜索与多源并发聚合搜索工具
        search_gw = None
        if "web_search" in capabilities:
            web_search_def = capabilities["web_search"]
            if isinstance(web_search_def, dict):
                web_search_def.setdefault("name", "web_search")
            search_gw = SearchGateway(web_search_def, state_path, logger)
            tools["fallback_web_search"] = ToolDefinition(schema=SEARCH_TOOL_SCHEMA, handler=search_gw.search)
            tools["multi_web_search"] = ToolDefinition(schema=MULTI_SEARCH_TOOL_SCHEMA, handler=search_gw.multi_search)

        # 动态自适应挂载能力：如果配置了正文提取，则装载提取工具
        extract_gw = None
        if "web_extract" in capabilities:
            web_extract_def = capabilities["web_extract"]
            if isinstance(web_extract_def, dict):
                web_extract_def.setdefault("name", "web_extract")
            extract_gw = ExtractGateway(web_extract_def, state_path, logger)
            tools["fallback_web_extract"] = ToolDefinition(schema=EXTRACT_TOOL_SCHEMA, handler=extract_gw.extract)

        # 动态自适应挂载扩展能力：遍历所有自定义能力分类（例如 academic_search、code_search 等）
        for cap_name, cap_def in capabilities.items():
            if cap_name in ("web_search", "web_extract"):
                continue
            if isinstance(cap_def, dict) and "providers" in cap_def:
                cap_def.setdefault("name", cap_name)
                custom_gw = SearchGateway(cap_def, state_path, logger)
                is_multi = cap_def.get("strategy") == "multi"
                default_desc = (
                    f"Multi-source concurrent search using custom {cap_name} capability."
                    if is_multi
                    else f"Search using custom {cap_name} capability."
                )
                tool_desc = cap_def.get("description") or default_desc
                default_schema = MULTI_SEARCH_TOOL_SCHEMA["inputSchema"] if is_multi else SEARCH_TOOL_SCHEMA["inputSchema"]
                custom_schema = {
                    "name": cap_name,
                    "description": tool_desc,
                    "inputSchema": cap_def.get("inputSchema", default_schema),
                }
                handler = custom_gw.multi_search if is_multi else custom_gw.search
                tools[cap_name] = ToolDefinition(schema=custom_schema, handler=handler)

        if not tools:
            raise ConfigError("No runnable tools could be initialized from configured capabilities")

    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    McpServer(search_gw, extract_gw, logger, tools=tools).run()
