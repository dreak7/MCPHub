# Local MCP Search Fallback Gateway

一个给 Hermes Desktop 使用的本地 MCP 搜索聚合器。它只向 Hermes 暴露一个
`fallback_web_search` 与 `fallback_web_extract` 两个工具，以避免和 Hermes 内置
`web_search` / `web_extract` 混淆。

搜索工具按以下固定优先级尝试在线搜索提供商：

1. Tavily Search
2. Exa
3. AnySearch
4. Bocha Web Search API

当服务返回额度耗尽、429、超时、连接错误或暂时性服务器错误时，网关会自动尝试下一项。
认证错误和调用参数错误会直接返回，避免错误配置被悄悄掩盖。

正文提取工具使用 Tavily → Exa → AnySearch。Bocha 的 Web Search API 仅返回搜索结果
和摘要，因此不会用于任意 URL 正文提取。

## 目录与配置

`config/gateway.json` 是入口配置，搜索服务清单在
`config/providers/search.json`。API 密钥在 `config/.env`，密钥文件不应提交或分享。

每个 provider 的 `priority` 决定递补顺序；`enabled: false` 可临时停用服务。某服务因可递补错误失败时，网关会在 `cooldown_seconds` 内跳过它。冷却状态保存在 `state/provider-state.json`，重启 Hermes 后也会保留。

## Windows 安装

1. 在目标电脑安装 **Python 3.11+**，安装器中勾选 **Add Python to PATH**。
2. 将整个项目目录复制到稳定位置，例如 `C:\MCP\mcp-search-fallback-gateway`。
3. 在该目录打开 PowerShell，验证：

   ```powershell
   python --version
   ```

4. 创建密钥文件：

   ```powershell
   Copy-Item config\.env.example config\.env
   notepad config\.env
   ```

   仅填写你已开通的密钥；未填写密钥的服务会被跳过。AnySearch 可以按其匿名额度运行，因此其密钥可留空。

5. 编辑 `config\providers\search.json`，按需要启用服务或调整优先级。
6. 在 Hermes Desktop 的 MCP 设置中添加本地命令服务：

   - Command：`C:\Users\<你的用户名>\AppData\Local\Programs\Python\Python311\python.exe`
     （请改为 `where python` 输出的实际路径）
   - Arguments：`-m mcp_gateway --config C:\MCP\mcp-search-fallback-gateway\config\gateway.json`
   - Working directory：`C:\MCP\mcp-search-fallback-gateway`

   如果 Hermes 没有单独的工作目录字段，可将完整项目路径放在命令参数中的 `--config` 后；程序会据此定位 `.env`、状态和日志。

7. 重新连接 Hermes 中的该 MCP。工具列表应显示 `fallback_web_search` 与
   `fallback_web_extract`。

聚合器与 Hermes 只在本机经标准输入输出通信，不监听网络端口；但 Tavily、Exa、AnySearch 与 Bocha 本身是在线 API，因此目标电脑必须能访问互联网。

## 排查

- MCP 协议数据只写到标准输出；所有运行日志在 `logs/gateway.log`，可安全查看故障原因。
- 认证失败不会递补。检查 `.env` 中的变量名和 API Key。
- 额度/频率耗尽会进入冷却。可等待 `cooldown_seconds` 到期，或在 Hermes 停止后删除 `state/provider-state.json` 以立即恢复尝试。
- 在终端直接运行程序时它会等待 MCP JSON-RPC 输入，这是正常行为；应由 Hermes 启动它。

## 暴露的工具

`fallback_web_search` 接受：

- `query`（必填）
- `max_results`（1–20，默认 5）
- `search_depth`（`basic` / `advanced`，默认 `basic`）
- `time_range`（`day` / `week` / `month` / `year`，可选）
- `include_domains`、`exclude_domains`（字符串数组，可选）

返回 JSON 文本，包含本次成功的 `provider`、原始 `query` 和统一的 `results`（`title`、`url`、`snippet`、`published_date`）。

AnySearch 的上游 API 单次最多返回 10 条；当统一工具请求超过 10 条时，该提供商会自动截为 10 条，其他提供商仍可返回请求的数量。

`fallback_web_extract` 接受：

- `urls`（必填，1–5 个公开 HTTP/HTTPS URL）；
- `extract_depth`（`basic` / `advanced`，默认 `basic`）；
- `max_characters`（每页 1,000–100,000 字符，默认 10,000）。

它返回每个页面的 `url`、`title`、清洗后的 `content`、`truncated` 和实际处理它的
`provider`。不可提取的 URL 会列在 `unresolved_urls`，不影响同一批其他 URL 的结果。
为避免 SSRF，网关拒绝 localhost、私有/保留地址、含用户信息的 URL 与非 80/443 端口。
