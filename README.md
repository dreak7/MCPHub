# Local MCP Search, Coding & Finance Fallback Gateway (v1.8.1)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-green.svg)](https://www.python.org/)
[![Tests: 77 Passed](https://img.shields.io/badge/Tests-77%20Passed-success.svg)](tests/)
[![Protocol: Model Context Protocol](https://img.shields.io/badge/MCP-Standard-purple.svg)](https://modelcontextprotocol.io/)

> **🙏 致谢与数据源声明 (Special Thanks & Acknowledgments)**  
> 本项目的证券金融专用工具集底层数据接口由开源项目 [TDX-finance-mcp-plugin-v3](https://github.com/adambbhe/TDX-finance-mcp-plugin-v3) 提供支持，特此致谢！

**MCPHub** 是适配 Hermes agent 的自定义 MCP 聚合服务脚本。原生提供 **12 大生产级标准 MCP 工具**，无缝覆盖全网高可用搜索、官方编程文档切片以及 A 股/港股双轨专业证券金融分析能力。

---

## 🌟 三大核心业务能力组

```text
MCPHub 统一服务网关 (12 大标准 MCP 工具)
├── 🌐 全网搜索与正文提取层 (多源高可用与自动回退)
│   ├── fallback_web_search    单源经济型故障回退搜索 (Exa → AnySearch → Tavily → Jina → Bocha)
│   ├── multi_web_search       多源全开并发聚合与多源置信度加权搜索
│   └── fallback_web_extract   长文正文智能提取 (30分钟落盘缓存 + read_file 联动翻页)
├── 💻 开发者编程文档切片层 (官方权威 API 索引)
│   └── code_docs_search       基于 Context7 驱动的编程语言与开源框架文档代码切片检索
└── 📈 专业证券与金融服务层 (A股+港股双轨智能路由，由 TDX-finance-mcp-plugin-v3 提供支持)
    ├── stock_lookup           股票名称/拼音代码语义智能检索 (AI RAG 驱动)
    ├── market_context         A股与港股交易日历感知与市场交易时钟判定
    ├── stock_quotes           实时行情与 Level-1 基础五档买卖委托盘口
    ├── stock_kline            多周期复权K线 + 服务端纯 Python 原生防幻觉指标 (MA/MACD/KDJ/BOLL)
    ├── stock_financials       国内上市公司财务三张表 (80列会计科目标准对齐，无漂移)
    ├── stock_f10              深度题材概念谱系、主力资金博弈与股东筹码 (11类官方专线数据)
    ├── stock_screener         JNLPSE 自然语言智能选股 (清洗通讯表头杂质，默认过滤 ST/*ST)
    └── stock_news             上市公司个股资讯与重大法定披露公告 (三轨高可用防御引擎)
```

---

## 🛠️ 12 大标准工具速查表

| 工具名称 | 分类能力 | 核心入参 | 典型功能与输出特色 |
| :--- | :--- | :--- | :--- |
| **`fallback_web_search`** | 全网搜索 | `query`, `max_results` | 按 Exa $\to$ AnySearch $\to$ Tavily $\to$ Jina $\to$ Bocha 顺序单源回退，最省配额 |
| **`multi_web_search`** | 全网搜索 | `query`, `max_results` | 多源并发检索，URL 规范化清洗，多源共同印证条目置顶加权 |
| **`fallback_web_extract`** | 正文提取 | `urls`, `max_characters` | Markdown 纯净提取，30 分钟落盘缓存，超长截断自动注入 `paging_hint` |
| **`code_docs_search`** | 编程文档 | `query`, `limit` | Context7 官方原厂驱动，检索编程库 API 文档与精选代码切片，零废弃 Token |
| **`stock_lookup`** | 证券金融 | `query`, `range` | 输入自然语言公司名/拼音/纯代码反查，**内置纯代码三轨正主保全层（如 002698 自动置顶博实股份，杜绝基金挤占）** |
| **`market_context`** | 证券金融 | `{}` (缺省) | 毫秒级识别当前是否处于盘前集合竞价、早盘/午后盘中、已收盘或节假日 |
| **`stock_quotes`** | 证券金融 | `symbol`, `setcode` | 现价、涨跌幅、日内真实振幅、**滚动市盈率 PETTM (真实TTM口径)**、动态市盈率 PE、静态市盈率、市净率 PB、**港股市值精准对齐** 及 **Level-1 基础五档买卖盘口深度** (含盘后状态自愈提示) |
| **`stock_kline`** | 证券金融 | `symbol`, `period`, `count` | 多周期行情，**服务端纯 Python 原生计算 MA/MACD/KDJ/BOLL**，杜绝大模型心算幻觉；**港股全面支持前复权 (qfq) 与不复权 (none) 实际走势** |
| **`stock_financials`** | 证券金融 | `symbol`, `report_type` | 资产负债表、利润表、现金流量表，**80 列会计科目彻底校准（负债明细段 N042衍生金融负债/N055合同负债/N056其他流动负债精准对齐，附带集团金融合并残差说明；港股显式提示不支持，杜绝静默空返回）** |
| **`stock_f10`** | 证券金融 | `symbol`, `category` | 支持题材概念、主力资金流向 (全汉化主力/超大单/大单)、两融余额 (全汉化融资融券)、股东户数、解禁、事件驱动 (历史里程碑倒序) 等 11 类深度数据（港股显式提示不支持，杜绝静默空返回） |
| **`stock_screener`** | 证券金融 | `message`, `filter_st` | 中文自然语言条件选股，**自动清洗杂质表头并默认剔除 ST/\*ST 风险股** |
| **`stock_news`** | 证券金融 | `symbol`, `limit` | 新浪财经主力 + 东财公告备份三轨引擎，支持代码与中文简称 URL 编码检索；**港股内置自动反查证券简称与彩票预测号码噪声过滤** |

---

## 🚀 极速上手 (Quick Start)

### 1. 运行管理控制台
在 Windows 系统根目录下，直接双击运行 **`MCPHub服务管理面板.bat`**（或在任意终端执行 `python -m mcp_gateway.console`）。

控制台提供全生命周期自动化运维菜单：
- **`[1] 一键生成并查看 Hermes Desktop 挂载配置`**：自动识别本机实际 Python 绝对路径与配置文件路径，输出零歧义配置；
- **`[2] 启动可视化引擎与能力配置向导 (GUI)`**：原生 Windows 窗口，免手动编辑 JSON 即可添加引擎或自定义能力分类；
- **`[3] 快捷配置/编辑 API 密钥 (.env)`**：快速配置并管理搜索及通达信专线密钥；
- **`[4] 运行服务健康检查与全部测试套件`**：一键运行全量 77 项自动化单元测试。

### 2. 配置 Hermes Desktop
直接打开 Hermes 的用户配置文件 `config.yaml`（或在设置界面中以 stdio 模式添加）：

```yaml
mcp_servers:
  MCPHub:
    command: C:\Users\<你的用户名>\AppData\Local\Programs\Python\Python313\python.exe
    args:
      - C:\MCP\MCPHub\mcp_gateway
      - --config
      - C:\MCP\MCPHub\config\gateway.json
    env:
      PYTHONUTF8: "1"
    enabled: true
```

### 3. 验证连通性
在终端执行 Hermes 专属连通性测试命令：
```powershell
hermes mcp test MCPHub
```
看到 `✓ Connected` 与 `✓ Tools discovered: 12` 即表示 12 大工具已全部就绪！

---

## 🏛️ 核心架构与工程优势

1. **A 股专线与港股免费通道双轨智能路由（Dual-Market Routing）**：
   - 6 位数字代码标的自动直连**通达信官方专线**，提供 Level-1 基础五档买卖盘与权威财务报表；
   - 5 位数字代码（如 `00700` 腾讯控股、`09988` 阿里巴巴）自动路由至**腾讯免费行情接口**，免配置港股 Key 即享极速行情。
2. **防计算幻觉技术指标引擎（Anti-Hallucination Technical Indicators）**：
   - 在服务端纯 Python 进程内完成 MA、MACD、KDJ、BOLL 浮点数学计算，大模型专注趋势与逻辑解读，彻底告别“AI 心算金叉死叉”导致的数值幻觉。
3. **分级智能冷却与自适应解冻（Auto-Healing Cooldown）**：
   - 区分短时限流（429 冷却 1 小时）与额度耗尽（402/432 冷却 3 天）；
   - 支持**自然月月初跨月自动解冻**与 **`.env` 更换 Key 实时热解冻**，完全无需人工干预。
4. **安全加固与边界防护（Security Hardening）**：
   - 严格的 SSRF 防御（阻断 localhost、私有 IP、非标准端口与内嵌凭证）；
   - 禁止重定向跳转（`NoRedirectHandler`），杜绝 301/302 跨域窃取 Authorization 敏感 Token；
   - 零重量级外部依赖（无 pandas、无 numpy），微秒级极速冷启动。

---

## 🧪 自动化测试验证

全套项目经过严苛的工业级测试，在项目根目录运行：
```powershell
python -m unittest discover tests
```
执行结果：
```text
Ran 77 tests in 1.059s
OK
```
测试全量覆盖：搜索多源融合降级、Context7 适配、通达信行情/K线/F10协议解析、资产负债表 80 列对齐（负债明细段精准治理）、港股市值标签修正、港股不复权走势、港股财报显式拦截、港股新闻彩票噪声过滤、选股杂质过滤、腾讯港股分流、SSRF 安全防御等 77 个关键场景。

---

## 📖 深度文档与规范指引

- **证券金融调用规程 (Skill)**：详见 [mcphub-finance/SKILL.md](mcphub-finance/SKILL.md)，包含面向大模型与智能体的严格数据源分级、标的代码预解析、选股防 ST 过滤与标准证券分析 SOP 实战工作流。

---

## 📄 开源许可证 (License)

本项目遵循 [MIT License](LICENSE) 开源许可证。
