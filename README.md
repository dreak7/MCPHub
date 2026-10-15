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
后端服务的认证错误、调用参数错误或拒绝服务不会中断递补：网关会记录原因并继续尝试下一家。
只有调用方传入的本地工具参数不合法时才会立即返回错误。

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
- 认证失败或参数不兼容会记录原因并继续递补下一家服务。如果所有服务都因认证失败，请检查 `.env` 中的变量名和 API Key。
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

两个工具都会在成功结果中附带先前被跳过或拒绝的服务及其原因：搜索工具字段为
`provider_failures`，提取工具字段为 `failures`。当检测到后端服务故障时，结果会额外附加
显式的 `warning` 字段，按故障类型分行显示实际出问题的服务名和错误码：

- **认证类**：`"⚠️ 警告：检测到后端服务认证失败 [<服务名> (<错误原因>)]。请检查 config/.env 中的 API Key 配置，或联系MCP提供商。"`
- **配额/限流类**：`"⚠️ 警告：检测到后端服务配额耗尽或请求限流 [<服务名> (<错误原因>)]。请耐心等待配额重置稍后重试或联系MCP提供商。"`
- **所有服务均耗尽时**：`"⚠️  警告：检测到后端服务配额均耗尽，请耐心等待相关配额重置或联系MCP提供商。"`

其中 `<服务名>` 和 `<错误原因>` 均为动态值，反映实际出问题的服务（如 `tavily-search`、`exa` 等）
和具体的 HTTP 状态码或错误描述。工具元数据已指示 Agent 必须在前端回答中置顶向用户警示。

## 更新记录

### v1.2.0 — 后端故障前端告警与修复指引

- 当任意后端服务出现认证失效（HTTP 401/403 或未配置密钥）时，即使后续服务递补成功，
  也会在返回结果中生成格式化的 `warning` 字段，提示用户检查 `config/.env` 或联系 MCP 提供商。
- 当任意后端服务出现配额耗尽（HTTP 402）或请求限流（HTTP 429）时，同样生成独立的
  `warning` 字段，提示用户耐心等待配额重置稍后重试或联系 MCP 提供商。
- 当所有服务配额均耗尽时，直接提示用户耐心等待相关配额重置或联系 MCP 提供商。
- `warning` 中的服务名与错误原因均为动态值，精确反映实际出问题的服务，而非固定文本。
- 在 `fallback_web_search` 与 `fallback_web_extract` 的工具描述（Schema Description）中
  加入强约束指令，要求 Agent 必须将该警告显式展示在回答开头。
- 当所有服务均不可用时，报错文本中亦自动附带检查 `.env` 与联系 MCP 提供商的指引。

### v1.1.1 — 后端拒绝不断链校正

- 修正 README「排查」一节中"认证失败不会递补"的过时描述，使其与实际代码行为一致。
- 补充测试：提取工具后端认证错误不中断递补链并附带失败记录；搜索工具非 retryable
  错误不进入冷却期。
- 对搜索工具与提取工具的全链路"后端拒绝后继续递补"语义进行审计确认，确保所有
  `SearchError`（无论 `retryable`）都只在当前 provider 记录失败、不终止递补。

### v1.1.0 — 搜索工具隔离与网页正文提取

- **破坏性变更**：原 `web_search` 重命名为 `fallback_web_search`，避免与 Hermes 内置
  `web_search` 混淆。请更新任何提示词、工作流或手动工具调用。
- 新增 `fallback_web_extract`：支持最多 5 个公开 URL 的批量正文提取。
- 提取服务按 Tavily → Exa → AnySearch 递补；Bocha Web Search API 仅用于搜索，不参与
  URL 正文提取。
- 搜索和提取拥有独立的失败冷却状态；新增 URL 公网校验以降低 SSRF 风险。
- 后端服务的认证、参数和拒绝错误也会记录后继续递补；仅本地工具输入校验会立即失败。

### 从 v1.0.0 升级

1. 用新版本覆盖项目文件，但保留自己的 `config/.env`。
2. 确认 `config/providers/search.json` 同时包含 `web_search` 与 `web_extract` 两个能力组。
3. 在 Hermes 中断开并重新连接该 MCP 服务，使工具列表刷新。
4. 将旧名称 `web_search` 替换为 `fallback_web_search`；需要读取正文时调用
   `fallback_web_extract`。
