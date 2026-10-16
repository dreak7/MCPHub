# Local MCP Search Fallback Gateway (v1.4.1)

一个给 Hermes Desktop 使用的本地 MCP 搜索聚合器。它向 Hermes 暴露
`fallback_web_search`（单源经济型回退搜索）、`multi_web_search`（多源并发聚合融合搜索）
与 `fallback_web_extract`（正文提取）三个工具，以避免和 Hermes 内置
`web_search` / `web_extract` 混淆。

搜索工具按以下固定优先级尝试在线搜索提供商：

1. Tavily Search
2. Exa
3. Jina AI Search
4. AnySearch
5. Bocha Web Search API

当服务返回额度耗尽、429、超时、连接错误或暂时性服务器错误时，网关会自动尝试下一项。
后端服务的认证错误、调用参数错误或拒绝服务不会中断递补：网关会记录原因并继续尝试下一家。
只有调用方传入的本地工具参数不合法时才会立即返回错误。

正文提取工具使用 Tavily → Exa → Jina AI Reader → AnySearch。Bocha 的 Web Search API 仅返回搜索结果
和摘要，因此不会用于任意 URL 正文提取。

## 目录与配置

`config\gateway.json` 是入口配置，支持通配符（如 `providers\*.json`）自适应发现所有配置。搜索服务清单在
`config\providers\search.json`。API 密钥在 `config\.env`，密钥文件不应提交或分享。

每个 provider 的 `priority` 决定递补顺序；`enabled: false` 可临时停用服务。系统采用具有自我感知能力的**分级智能冷却与双重自适应解冻机制**：
- **短时频率限流（429 / 网络抖动）**：冷却 `cooldown_seconds`（默认 1 小时 / 3600 秒），到期自动恢复；
- **月度配额耗尽（402 / 432 / 余额不足）**：冷却 `quota_cooldown_seconds`（默认 3 天 / 259200 秒），杜绝频繁试探骚扰；
- **月初跨月自适应复活**：自动识别日历跨月，新自然月到来时自动解冻上月因配额耗尽沉睡的服务（月度免费额度已重置），无需任何人工干预；
- **密钥变更自适应解冻**：在 `config\.env` 中为某个服务更换了新 API Key 后，网关自动识别并秒级清除历史冷却复活服务，无需手动删除任何状态文件。
冷却状态持久化保存在 `state\provider-state.json`，重启 Hermes 后也会完好保留。

## Windows 安装与配置

1. 在目标电脑安装 **Python 3.11+**（推荐 3.11 或 3.13），安装器中务必勾选 **Add Python to PATH**。
2. 将整个项目目录复制到稳定位置，例如 `C:\MCP\MCPHub`。
3. 在该目录打开 PowerShell，验证 Python 环境并获取真实路径：

   ```powershell
   python --version
   where.exe python
   ```

4. 创建并编辑密钥文件（提供两种 Windows 操作方式，任选其一即可）：

   **方式一：Windows 命令行（PowerShell 与 CMD 均原生通用）**：
   在当前项目目录终端中执行复制并调用系统记事本打开：
   ```cmd
   copy config\.env.example config\.env
   notepad config\.env
   ```

   **方式二：文件资源管理器（鼠标可视化操作，最符合 Windows 习惯）**：
   - 打开 `C:\MCP\MCPHub\config` 文件夹；
   - 鼠标右键复制 `.env.example` 文件并在当前文件夹粘贴；
   - 将粘贴出来的文件重命名为 `.env`；
     > ⚠️ **Windows 避坑提示**：请务必确保文件名没有多出 `.txt` 后缀（即避免变成 `.env.txt`）。若系统默认隐藏了后缀名，建议在文件资源管理器顶部点击「查看」→ 勾选「文件扩展名」，确保文件名纯粹为 `.env`。
   - 鼠标右键点击 `.env` 文件，选择「打开方式」→「记事本」进行编辑。

   **填写说明**：
   在记事本中填写你已开通服务的 API Key，填完按快捷键 `Ctrl + S` 保存后关闭记事本即可。各 Key 直接填写密钥值即可，无需引号包裹（例如 `TAVILY_API_KEY=tvly-xxxx`；程序内部已做单双引号自动剥离容错）。未填写的服务会在运行时安全跳过。

5. 编辑 `config\providers\search.json`，按需要启用服务或调整优先级。
6. 在 Hermes 中添加本地 MCP 服务（强烈推荐直接使用 **方式 A** 编辑 `config.yaml`，最不易出错）：

   **方式 A：直接写入 Hermes 配置文件（config.yaml，官方原生、零歧义，最推荐）**：
   直接打开 Hermes 的用户配置文件 `config.yaml`，在 `mcp_servers`（或相应节点）下填入以下标准配置：
   ```yaml
   MCPHub:
     command: C:\Users\<你的用户名>\AppData\Local\Programs\Python\Python313\python.exe  # 必须指向 python.exe，不能只填目录！
     args:
       - C:\MCP\MCPHub\mcp_gateway
       - --config
       - C:\MCP\MCPHub\config\gateway.json
     env:
       PYTHONUTF8: "1"  # 可选但推荐：显式通知 Python 启用 UTF-8（网关代码内部也已做自动 UTF-8 重配与 ASCII 兜底）
     enabled: true  # 必须为 true，否则服务默认 disabled
   ```
   *(注：args 必须是逐项拆分的数组。亦支持标准包调用方式：将前两项写作 `- -m` 与 `- mcp_gateway` 并配置 `cwd`)*

   **方式 B：Hermes 桌面端表单逐项添加（注意：坚决不要把整行命令塞进单一一栏！）**：
   - **Type**：选择 `stdio`
   - **Command**：`C:\Users\<你的用户名>\AppData\Local\Programs\Python\Python313\python.exe`
     （⚠️ 注意：必须填写完整的 `python.exe` 可执行文件绝对路径，不能只写到 `...\Python313` 文件夹，否则会报 `[WinError 5] 拒绝访问`）
   - **Arguments（逐项添加 3 个独立参数，切勿合在一行）**：
     - 参数项 1：`C:\MCP\MCPHub\mcp_gateway`
     - 参数项 2：`--config`
     - 参数项 3：`C:\MCP\MCPHub\config\gateway.json`
     > ⚠️ **避坑警示**：Hermes 启动进程时不经过 Windows Shell 拆词，它的 `args` 是一个严格的参数数组。如果把上面 3 项合在一行塞进同一个输入框，Python 会将整行字符串当作单一参数，从而抛出 `ModuleNotFoundError`。

   **方式 C：标准 JSON 配置片段**：
   ```json
   {
     "command": "C:\\Users\\<你的用户名>\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
     "args": [
       "C:\\MCP\\MCPHub\\mcp_gateway",
       "--config",
       "C:\\MCP\\MCPHub\\config\\gateway.json"
     ],
     "env": {
       "PYTHONUTF8": "1"
     },
     "enabled": true
   }
   ```

7. 连接与验证（关键步骤）：
   - 在终端执行 Hermes 专属连接测试命令：
     ```powershell
     hermes mcp test MCPHub
     ```
     若配置成功，将输出：
     ```text
     Transport: stdio → ...\Python313\python.exe
     ✓ Connected
     ✓ Tools discovered: 3
         fallback_web_search   单源经济型回退搜索
         multi_web_search      多源并发聚合融合搜索
         fallback_web_extract  自动回退的网页正文提取
     ```
   - 执行 `hermes mcp list` 确认状态为 `✓ enabled`。

聚合器与 Hermes 只在本机经标准输入输出通信，不监听网络端口；但 Tavily、Exa、Jina AI、AnySearch 与 Bocha 本身是在线 API，因此目标电脑必须能访问互联网。

## 排查

- MCP 协议数据只写到标准输出；所有运行日志在 `logs\gateway.log`，可安全查看故障原因。
- 认证失败或参数不兼容会记录原因并继续递补下一家服务。如果所有服务都因认证失败，请检查 `config\.env` 中的变量名和 API Key。
- **冷却状态与自适应恢复**：额度/频率耗尽会进入分级智能冷却。系统原生支持**跨月自动解冻**与**换 Key 自动解冻**，完全无需人工维护；如需即时手动重置，可在 Hermes 停止后删除 `state\provider-state.json`。
- 在终端直接运行程序时它会等待 MCP JSON-RPC 输入，这是正常行为；应由 Hermes 启动它。
- **服务已启用但在当前聊天窗口搜不到新工具？**：Hermes 桌面端在会话建立时会冻结工具目录快照。修改配置或中途重新启用 MCP 服务后，旧会话渲染端可能无法即时刷新。只需在 Hermes 中点击 **`+`（新建会话 / New Chat）**，或者**彻底重启一次 Hermes Desktop 客户端**，新会话即可直接挂载最新工具。

## 暴露的工具

### 1. `fallback_web_search`（单源高可用回退搜索，推荐日常使用）

接受参数：
- `query`（必填）：检索关键词
- `max_results`（1–20，默认 10）：最大返回条数
- `search_depth`（`basic` / `advanced`，默认 `basic`）
- `time_range`（`day` / `week` / `month` / `year`，可选）
- `include_domains`、`exclude_domains`（字符串数组，可选）

工作机制：按优先级从高到低依次尝试（Tavily → Exa → Jina AI → AnySearch → Bocha）。只要当前第一顺位成功即立刻返回，仅在故障时向后递补。最省 API 配额，耗时短，适合高频、常规问题。

### 2. `multi_web_search`（多源并发聚合搜索，推荐深度研究与多方印证）

接受参数与 `fallback_web_search` 完全一致。

工作机制：
- **多路并发全开**：使用 Python 内置 `concurrent.futures.ThreadPoolExecutor`，同时唤醒所有已配置 API Key 且处于就绪状态的引擎并发发起检索；
- **智能容错隔离**：各引擎独立执行，即使其中某一家发生 429 限流或 401 认证异常，系统会自动记录警告并放入冷却，其余引擎依然顺利聚合并呈现；
- **URL 规范化与跟踪参数清洗**：自动剥离 `utm_*`、`ref`、`spm` 等追踪冗余后缀，对域名小写与结尾斜杠统一归一化；
- **多源置信度置顶（Hybrid Fusion）**：如果同一网页被多家搜索引擎独立召回，系统自动合并更丰富的摘要与标题，并为该条目打上 `sources: ["tavily-search", "exa"]` 多源认证标签，优先置顶展示；
- 返回结构包含：`tool: "multi_web_search"`、`active_providers`（实际返回结果的引擎列表）、`total_sources_queried`（发起请求的总数）、`results`（融合去重后的结果清单）以及故障诊断与警告信息。

### 3. `fallback_web_extract`（网页正文智能提取）

接受参数：
- `urls`（必填，1–5 个公开 HTTP/HTTPS URL）；
- `extract_depth`（`basic` / `advanced`，默认 `basic`）；
- `max_characters`（每页 1,000–100,000 字符，默认 10,000）。

它返回每个页面的 `url`、`title`、清洗后的 `content`、`truncated` 和实际处理它的 `provider`。不可提取的 URL 会列在 `unresolved_urls`，不影响同一批其他 URL 的结果。
为避免 SSRF，网关拒绝 localhost、私有/保留地址、含用户信息的 URL 与非 80/443 端口。

---

所有工具都会在结果中透明展示上游故障：单源搜索字段为 `provider_failures`，并发搜索字段为 `provider_failures`，提取工具字段为 `failures`。当检测到后端服务故障时，结果会额外附加显式的 `warning` 字段，按故障类型分行显示实际出问题的服务名和错误码：

- **认证类**：`"⚠️ 警告：检测到后端服务认证失败 [<服务名> (<错误原因>)]。请检查 config/.env 中的 API Key 配置，或联系MCP提供商。"`
- **配额/限流类**：`"⚠️ 警告：检测到后端服务配额耗尽或请求限流 [<服务名> (<错误原因>)]。请耐心等待配额重置稍后重试或联系MCP提供商。"`
- **所有服务均耗尽时**：`"⚠️  警告：检测到后端服务配额均耗尽，请耐心等待相关配额重置或联系MCP提供商。"`

其中 `<服务名>` 和 `<错误原因>` 均为动态值，反映实际出问题的服务（如 `tavily-search`、`exa` 等）
和具体的 HTTP 状态码或错误描述。工具元数据已指示 Agent 必须在前端回答中置顶向用户警示。

## 更新记录

### v1.4.1 — 服务端点安全合规校验、业务错误响应体脱敏与 MIT 开源许可证

- **服务端点深度安全加固（SSRF 防御与 API Key 外发阻断）**：
  - 新增 `_validate_endpoint` 强制校验，严格限制协议必须为 HTTPS、端口仅允许标准端口 443（或缺省）、禁止包含内嵌凭据；
  - 强制对服务端点进行全球公网 IP 校验与 DNS 逆向排查，拦截针对私网/内网保留网段（`127.0.0.1`、`192.168.*.*`、`10.*.*.*`、`169.254.169.254` 等）与 `localhost` 的探测，彻底杜绝配置篡改后的局域网穿透与 API Key 盗取风险。
- **业务错误响应体彻底脱敏（防范上游敏感凭据与调试信息外泄）**：
  - 对齐 `_post` 核心安全约定，针对 AnySearch 与 Bocha 的业务错误分支（HTTP 200，但内部业务码非 0），严格阻断上游原始报错文本直通前端与模型；
  - 统一将业务错误归一化为脱敏标签（如 `AnySearch business error (code {code})`），原始可能包含敏感凭据、请求回显或调试信息的 Body 仅落入本地受控日志。
- **正式发布 MIT 开源许可证**：
  - 在根目录添加标准 `LICENSE` 授权文件，并在文档尾部建立「许可证」专章索引。
- **测试套件扩增至 36 项**：
  - 新增服务端点私网阻断、业务响应体脱敏等专属单元测试，全套 36 项测试 100% 通过。

### v1.4.0 — 多源并发聚合搜索（multi_web_search）与分级智能冷却（Tiered Cooldown）

- **新增多源并发检索工具（`multi_web_search`）**：
  - 与日常经济型的 `fallback_web_search` 并行运作，向大模型同时提供“单源高可用快搜”与“全网多源深度研报搜索”双重能力。
  - 基于 Python 原生标准库 `concurrent.futures.ThreadPoolExecutor`，一键并发唤醒全部已配置 API Key 且就绪的引擎（Tavily, Exa, Jina, Bocha, AnySearch 等），保持极速毫秒级并行响应。
- **智能 URL 归一化与跟踪参数清洗**：
  - 原生内置 URL 规范化清洗，精准剥离 `utm_*`、`ref`、`spm` 等冗余追踪参数并标准化路径斜杠与大小写，确保跨引擎的相同网页精准识别与聚合。
- **多源推荐加权置顶（Hybrid Fusion）**：
  - 自动合并相同 URL 的多源摘要与标题，为条目动态注入 `sources: [...]` 来源证明标签，并将受到多家引擎共同青睐的高信度条目优先置顶呈现给大模型。
- **全链路独立容错隔离**：
  - 某家引擎发生 429 限流或 401 认证异常时独立熔断并生成显式 warning，绝不影响其他引擎并发结果正常合并与返回。
- **分级智能冷却机制（Tiered Cooldown）**：
  - 精准区分“短时频率限流（429）”与“月度配额耗尽（402/432/余额不足）”。
  - 频率限流维持 1 小时冷却，月度配额耗尽自动触发 **3 天（72 小时 / 259,200 秒）** 深层冷却，杜绝配额用光后的频繁重复试探与告警骚扰；支持在配置中个性化自定义。
- **双重自适应解冻机制（Auto-Healing）**：
  - **月初跨月自适应**：日历跨入新自然月时，系统自动识别并清零上月因配额耗尽而沉睡的服务，无需人工介入；
  - **密钥变更自适应**：在 `config/.env` 中修改或更换某服务的 API Key 后，网关即时感知并自动秒级解冻复活该服务。
- **强化安全防护与响应体脱敏**：
  - 新增 `_validate_endpoint` 校验通信端点，严格限制标准 HTTPS 端口并阻断私网/内网 IP（防范 SSRF 与配置篡改导致的 Key 泄露）；
  - 对齐脱敏安全约定，AnySearch 与 Bocha 业务错误分支仅透传结构化状态标签，绝不把上游原始错误响应体暴露给前端或模型。
- **测试套件扩增至 36 项**：
  - 新增端点安全校验、业务错误响应脱敏、多源聚合去重、高信度加权置顶、单源容错隔离、分级冷却、跨月解冻与换 Key 解冻测试，100% 通过。

### v1.3.0 — 插件化自适应架构与 Jina AI 搜索/提取集成

- **插件化配置与动态能力注册（解除硬编码限制）**：
  - `config/gateway.json` 支持通过通配符（如 `"providers/*.json"`）自动扫描发现提供商配置，无需每次手动注册。
  - 解除了此前必须同时具备 `web_search` 和 `web_extract` 的硬编码校验，系统支持动态能力插拔与自适应加载。
  - 服务端引入 `ToolDefinition` 动态工具注册表机制，`tools/list` 与 `tools/call` 全量动态自适应派发，同时完美向后兼容。
- **集成 Jina AI（搜索 + Reader 正文提取）**：
  - 新增 Jina AI 作为搜索梯队与正文提取梯队的通用回退提供商（优先级设为 25，位于 Exa 与 AnySearch 之间）。
  - 支持 `s.jina.ai` 搜索接口与 `r.jina.ai` 极简正文提取接口，自动规范化输出统一 Schema。
  - 支持在 `config/.env` 中通过 `JINA_API_KEY` 进行 Bearer Token 鉴权，享受完整的失败冷却、429/432 限流与配额告警机制。
- **默认检索条数扩增至 10 条**：
  - 将 `fallback_web_search` 的默认返回数量从 5 条提升至 10 条，为大模型提供更充实的上下文与多源交叉校验能力，且与 AnySearch 单次上限完全匹配。
- **测试套件扩充**：测试用例扩展至 25 项，全量覆盖通配符配置发现、动态工具注册派发、Jina Search 结果规范化以及 Jina Reader 正文提取。

### v1.2.0 — 后端故障前端告警、配额状态分流与环境自适应启动

- **原生模块自适应启动升级（无需额外启动脚本）**：
  - 在原生 `mcp_gateway/__main__.py` 中内置路径自适应注入机制与绝对/相对导入双模兼容。
  - 既完全兼容传统的 `python -m mcp_gateway` 启动，又支持直接指定目录 `python path/to/mcp_gateway` 或脚本 `python path/to/mcp_gateway/__main__.py` 启动。
  - 彻底免除外部工作目录或 `PYTHONPATH` 环境变量依赖，完美解决 Hermes Desktop 等 MCP 客户端跨目录调用时可能发生的 `ModuleNotFoundError` 问题。
- **后端故障精准分流与格式化告警**：
  - **认证类异常（401/403/未配置 Key）**：即使后续服务递补成功，亦在返回结果中附加显式 `warning`，提示检查 `config/.env` 或联系 MCP 提供商。
  - **部分服务配额/限流（402/429/432/额度耗尽/用量超限）**：动态捕获具体受限的服务名与错误原因，提示用户耐心等待配额重置稍后重试或联系 MCP 提供商。精准覆盖了 Tavily 等服务的 HTTP 432（`exceeds API key usage limit` 用量超限）状态码，避免告警盲区。
  - **全部服务均耗尽**：当所有可用后端服务的配额均耗尽时，直接向前端输出全局告警 `⚠️  警告：检测到后端服务配额均耗尽，请耐心等待相关配额重置或联系MCP提供商。`
- **动态错误上下文**：`warning` 中的服务名与错误原因均为动态捕获值，精准反映实际发生故障的上游服务，杜绝固定模板混淆。
- **前端强提示约束**：在工具描述（Schema Description）中加入强约束指令，要求 Agent 必须将该告警显式展示在回答开头，保障用户第一时间感知。
- **Windows UTF-8 编码鲁棒性与防超时保障（彻底杜绝 UnicodeEncodeError）**：
  - 启动时自动将 `sys.stdin`、`sys.stdout`、`sys.stderr` 强制重配为 UTF-8（`errors="replace"`），彻底免疫 Windows 默认 GBK（cp936）在输出医学符号（如 ☤）、特殊音标（如 ˈ）或 Emoji 字符时触发的 `UnicodeEncodeError` 导致服务静默超时。
  - 在底层 `_write` 通信层内置双重兜底防护：即使底层流无法承载特殊字符，自动回退转义为标准 ASCII（`\uXXXX`）输出，确保 JSON-RPC 消息永不中断。
  - 全局未捕获异常处理关联原始 `request_id`，杜绝未配对 ID 导致的客户端 300 秒死等超时。
- **测试用例扩充与全链路验证**：测试套件扩充至 21 项，全面覆盖部分限流递补、HTTP 432 用量超限告警、全部耗尽阻断、认证异常捕获、非 GBK Unicode 字符输出、编码异常降级兜底、目录直启与 `__main__.py` 独立入口调用。

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

## 许可证 (License)

本项目遵循 [MIT License](LICENSE) 开源许可证。

