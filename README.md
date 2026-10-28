# MCPHub

本地 stdio MCP 网关，仅提供通用信息检索与编程文档检索能力。

## 工具

- `fallback_web_search`：单源优先、自动故障回退的网页搜索。
- `multi_web_search`：多源并发搜索、URL 规范化与去重。
- `fallback_web_extract`：公开网页正文提取与受限本地缓存。
- `code_docs_search`：基于 Context7 的编程语言、框架与库文档检索。

证券、行情、财报、选股及其他金融工具已从本项目移除。金融数据应通过独立的、供应商维护的 MCP 服务接入，不应由本网关转发凭据。

## 配置与启动

1. 复制 `config/.env.example` 为 `config/.env`，仅填入所需搜索或文档服务 Key。
2. 在支持 stdio MCP 的客户端中启动：

```text
python mcp_gateway --config <项目目录>/config/gateway.json
```

服务仅通过标准输入输出通信，不监听网络端口。请阅读 [SECURITY.md](SECURITY.md) 了解缓存、外部内容与凭据的安全边界。

## 安全默认值

- 上游搜索端点必须使用 HTTPS，拒绝私网、回环地址和重定向。
- 工具结果标识为不可信外部数据，智能体不得执行其中的指令。
- 提取缓存有单项、总量与 TTL 限制。
- 默认限流为每分钟 60 次工具调用，可在 `config/gateway.json` 调整。
