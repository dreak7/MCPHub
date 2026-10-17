---
name: mcphub-probe
description: "Use to probe or diagnose the MCPHub search gateway health. MCPHub 探活与取证。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [windows]
metadata:
  hermes:
    tags: [Search, MCP, MCPHub, probe, diagnostics, forensics, timing]
    related_skills: [search-channel-routing]
---

# MCPHub 探针与取证

通道层诊断专用：**探活、失效判定、耗时取证**。应用/操作规定（四段路由、调用方式、换道动作）见 `search-channel-routing` —— 本 skill 只回答"通道现在好不好、刚才那次走了哪个后端"。

## When to Use
- 每个检索任务**首轮**先探活（防静默失效）
- 检索结果可疑 / 来源集中 / 多次重试才拿到 / 用户追问"这条数据从哪来、花了多久"
- 需要还原某次调用实际走了哪个后端

## 探活规程（每个检索任务首轮必做）
1. 跑共享探活脚本（**唯一脚本**，各 profile 通用；脚本路径可从 Hermes 的 `config.yaml` 中 `MCPHub` 节点的网关路径解析其同级的 `scripts/`，或直接使用实际安装路径）：
   ```bash
   bash "<MCPHub安装目录>/scripts/probe_mcphub.sh" <profile>    # default 可省略
   ```
   判据：只看「**当日最后一条 MCPHub 启动记录之后**」有无路径报错；历史累计失败只计 WARN，不参与判定。
2. 发一次 **no-op 检索**验活：`fallback_web_search(query="<当日本地无关短词>", max_results=1)` —— 返回体须含非空 `provider`。
3. 任一失败 → 显式告警「主通道不可用，已回落内置 web_search」并在交付物留痕（含回落原因）。

## 失效判据（诊断口径）
- 返回体出现 `rescued_from` / `backend_error`（内置链），或单源 `provider` ≠ 首选 `exa` / 并发 `active_providers` 不含 `exa`（MCPHub 链）→ **该次未走主后端**。
- **`provider_failures` 非空 ≠ 通道失效**：链式 fallback 会继续试，只要最终 `provider` 有值即成功；**只有多 provider 全失败、返回体无 `provider` 才算链路失效**。
- 单任务「未走主后端」比例 **≥ 50%** → 判定通道失效（换道动作见 `search-channel-routing`，本 skill 只负责判）。
- Tavily 配额耗尽（HTTP 432）→ 3 天冷却（`quota_cooldown_seconds: 259200`）属**预期内降级，不是故障**，不要因此回退内置。

## 通道静默死亡的四种形态
四种都**不产生面向用户的报错** —— 只能靠探针与取证发现：
1. **降级不报错**：主后端全失败，兜底每次返回看似可用结果，痕迹只在 `rescued_from` / `backend_error`。
2. **来源质量滑坡**：单源兜底端上来自动聚合站、多年前旧稿；换 MCPHub 的实际收益是多源并发 + 去重。
3. **进程用旧路径**：配置改了、守护进程仍按旧路径启动，每次启动即退出，工具在 schema 缓存里"看着还在"、实际调不通。
4. **索引失真**：记忆里还记着"哪几个 MCP 在跑"，实际那几个已 `enabled:false`。

## 取证（还原"哪次调用走了哪个后端、耗时与体积"）
见 `references/channel-forensics.md`：调用链看 `state.db`（`messages` 表；参数在 `assistant` 的 `tool_calls`、后端标签在 tool 返回体），耗时/体积看 `agent.log`（`tool completed` 行 + `API call` 行）。

## 边界
只诊断**通道层**，不改任何检索配置；不下"哪条链更好"的结论（要下结论先做受控 A/B，见 `search-channel-routing`）。
