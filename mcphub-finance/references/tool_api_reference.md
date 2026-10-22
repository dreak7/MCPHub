# MCPHub 证券与金融专属工具集技术参考手册 (Securities & Finance Tools API Reference)

> **版本**：v1.8.5  
> **受众**：人类软件工程师、接口联调人员、或需要精确核对底层字段/Schema的大模型（按需渐进式调阅）。  
> **定位**：静态技术基准参考库（API Reference & Data Dictionary）。日常 Agent 推理行动请直接遵循 `mcphub-finance/SKILL.md`。

---

## 一、 底层数据通道与多源高可用矩阵

| 市场 / 资产 | 主力通道 (Primary) | 备用通道 (Fallback) | 职责与覆盖能力 |
| :--- | :--- | :--- | :--- |
| **A 股全市场**<br>(沪/深/北/指数/基金) | **通达信官方专线 (TDX)**<br>(TdxShareCW / JNLPSE / PCCW) | 东方财富数据中心 / 公开 REST | Level-1 基础五档买卖盘口、复权 K 线、原生技术指标（MA/MACD/KDJ/BOLL/RSI）、三大报表（工商业 `isjr=0` 与金融企业 `isjr=1` 双准则模板，金融现金流差额自陈，筹资流入小计自动补齐，三大净额镜像挂载）、主力资金流向、F10 深度题材与筹码、自然语言选股。 |
| **港股主板**<br>(5位数字代码标的) | **免 Key 高可用矩阵协同**：<br>1. 腾讯开放专线<br>2. 东方财富数据中心<br>3. 新浪港股专线<br>4. 恒生行业分类智能引擎 | 通达信实体解析 / 新浪港股备用 | - **腾讯开放专线**：实时买卖一档现价、日内成交量额、日内分钟点流原生切片聚合走势 (1m/5m/15m/30m/60m) 与 newfqkline 高可用日线（成交额 100% 真实下发）；<br>- **东方财富数据中心**：法定三张表（元/CNY 统一口径、单季度前驱缓冲区做差推算）、主要财务指标宽表（`indicators` 记账币种动态校准为 CNY）；<br>- **新浪港股专线**：持股 5% 以上大股东最新好仓申报聚合 (`top_holders`，已移除冗余表)、大股东权益申报流水 (`shareholders`，已清洗同日结余相同且涉及股数为 0 的穿透附注行)、股份回购 (`buyback`)、历年分红派息 (`dividends`)、公司工商概况 (`summary`，所属行业 100% 映射恒生 12 大行业)；<br>- **恒生行业分类引擎**：结合官方分类、核心业务词识别与主营描述，100% 覆盖恒生 12 大行业标准分类。 |

---

## 二、 8 大标准证券金融工具技术规格

### 1. `stock_lookup`（标的代码与名称语义智能检索）

- **功能**：输入股票简称、拼音首字母或代码，智能检索出标准 6 位 A 股代码、5 位港股代码及交易所代码。内置纯数字代码三轨正主保全机制与代码精确命中检测。
- **MCP Input Schema**：
  ```json
  {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "实体名称或简称，如'贵州茅台'、'腾讯'、'上证指数'、'00700'"},
      "range": {
        "type": "string",
        "enum": ["AG", "HK-GP", "HK-JJ", "JJ", "MG-GP", "ZS"],
        "default": "AG",
        "description": "市场类型：AG=A股(默认), HK-GP=港股, HK-JJ=港股基金, JJ=基金, MG-GP=美股, ZS=指数"
      }
    },
    "required": ["query"],
    "additionalProperties": false
  }
  ```
- **核心契约字段**：
  - `exact_match` (bool)：目标市场内是否精准命中了该代码或正规全称的上市证券；
  - `matched_code` (str | null)：精准命中的证券代码；
  - `未命中说明` (str)：当 `exact_match == false` 时注入，说明返回列表仅为语义关联候选（非正主），指引核对代码。
- **返回结构示例 (精确命中)**：
  ```json
  {
    "query": "00700",
    "range": "HK-GP",
    "exact_match": true,
    "matched_code": "00700",
    "命中说明": "已精确匹配到目标证券标的 (代码: 00700)",
    "entity_count": 1,
    "entities": [
      {
        "code": "00700",
        "name": "腾讯控股",
        "setcode": "31",
        "market": "HK",
        "entity_code": "00700",
        "entity_name": "腾讯控股",
        "entity_type": "港股代码",
        "entity_setcode": 31,
        "aliases": ["txkg", "Tencent", "腾讯"]
      }
    ]
  }
  ```
- **返回结构示例 (未命中候选防御)**：
  ```json
  {
    "query": "001548",
    "range": "HK-GP",
    "exact_match": false,
    "matched_code": null,
    "未命中说明": "在指定市场范围 (HK-GP) 中未检索到代码为 '001548' 的上市证券标的；下方返回列表为语义模型相关度最近的关联候选（非正主标的），请核对代码后再行查询。",
    "entity_count": 10,
    "entities": [{"code": "00486", "name": "俄铝", ...}]
  }
  ```

---

### 2. `market_context`（国内交易日历与市场时钟感知）

- **功能**：获取东八区北京时间所处的 A 股市场实时交易状态及基准交易日。
- **MCP Input Schema**：
  ```json
  {
    "type": "object",
    "properties": {},
    "additionalProperties": false
  }
  ```
- **返回字段字典**：
  - `current_cst_time` (str)：当前东八区北京时间格式 `YYYY-MM-DD HH:MM:SS`；
  - `market_status` (str)：时段枚举（`call_auction_morning` 盘前集合竞价、`trading_morning` 早盘中、`lunch_break` 午间休市、`trading_afternoon` 午后盘中、`closed_today` 今日已收盘、`weekend_closed` 周末休市）；
  - `market_status_desc` (str)：时段中文说明；
  - `is_trading_time` (bool)：当前时刻交易所是否正处于连续竞价撮合中；
  - `reference_trading_date` (str)：基准交易日。

---

### 3. `stock_quotes`（实时盘口微观高频行情与估值指标）

- **功能**：查询指定股票的五档委托买卖盘口、现价、涨跌幅、换手率、量比、总市值与专业估值指标。
- **MCP Input Schema**：
  ```json
  {
    "type": "object",
    "properties": {
      "symbol": {"type": "string", "description": "证券代码：A股6位数字（如'600519'），港股5位数字（如'00700'）"},
      "setcode": {"type": "string", "description": "市场代码：0=深市, 1=沪市, 2=北交所, 31=港交所。选填"}
    },
    "required": ["symbol"],
    "additionalProperties": false
  }
  ```
- **返回结构核心字段字典**：
  - `盘口核心行情`：
    - `股票名称`, `现价`, `涨跌幅_pct`, `涨跌额_元` (港股为 `涨跌额`), `昨收`, `今开`, `最高`, `最低`；
    - `成交量_手` (港股为 `成交量_股`，整数型), `成交额_元` (港股为 `成交额_港元`)；
    - `换手率_pct` (港股已原生内置推导), `量比`, `更新时间`, `结算货币`；
    - `涨跌停说明` (港股明确标明不设固定涨跌停板，实行市调机制 VCM 冷静期)；
  - `估值与基本盘面`：
    - **A股专线**：`总股本_万股`, `流通股本_万股`, `总市值_元`, `流通市值_元`；
    - **港股双口径市值**：
      - `港股市值_H股流通市值_亿港元`（纯港股在港实际流通规模）；
      - `总市值_全部股本乘H股价_亿港元`（全股本混合估值参考）；
      - `市值口径说明`（详细区分两地上市与单一市场可流通规模）；
    - **估值指标**：
      - `滚动市盈率_PETTM` / `市盈率_TTM`（真 TTM 滚动估值，不受单季淡旺季扭曲）；
      - `静态市盈率_PEStatic` / `静态市盈率_PE_LYR`（上一财年年报静态估值）；
      - `动态市盈率_PE`（现价÷年化EPS）、`市净率_PB`；
      - `估值口径说明`（披露 TTM 与 Static 映射同源性，以及对于人民币编报、港币交易标的底层隐含约 0.868~0.872 CNY/HKD 汇率折算公式）；
      - `52周最高`, `52周最低`, `52周价格区间说明`；
  - `五档深度盘口`：`买一` 至 `买五`、`卖一` 至 `卖五`（价格与委托量；港股公开源提供即时买一/卖一撮合挂单）；
  - `盘口深度说明`：收盘后（≥15:00）自动警示盘后微量残单不代表真实多空力量。

---

### 4. `stock_kline`（多周期历史走势与内置技术指标）

- **功能**：拉取历史复权 K 线，并由纯 Python 原生算法计算输出 MA、MACD、KDJ、BOLL、RSI 等技术指标。
- **MCP Input Schema**：
  ```json
  {
    "type": "object",
    "properties": {
      "symbol": {"type": "string", "description": "证券代码：A股6位（'002594'），港股5位（'00700'）"},
      "period": {
        "type": "string",
        "enum": ["day", "week", "month", "60m", "30m", "15m", "5m", "1m"],
        "default": "day",
        "description": "K线周期，默认为 day"
      },
      "count": {"type": "integer", "minimum": 1, "maximum": 300, "default": 30, "description": "请求K线根数(1-300)"},
      "adjust": {"type": "string", "enum": ["qfq", "hfq", "none"], "default": "qfq", "description": "复权方式：qfq=前复权, hfq=后复权, none=不复权"},
      "setcode": {"type": "string", "description": "市场代码，选填"},
      "with_indicators": {"type": "boolean", "default": true, "description": "是否自动附带服务端原生技术指标计算结果"}
    },
    "required": ["symbol"],
    "additionalProperties": false
  }
  ```
- **技术指标与特色字段**：
  - 均线：`ma5`, `ma10`, `ma20`；
  - MACD：`macd_dif` (快线), `macd_dea` (慢线), `macd_hist` / `macd_bar` (柱状图)；
  - KDJ：`kdj_k`, `kdj_d`, `kdj_j`；
  - BOLL：`boll_mid` (中轨), `boll_upper` / `boll_up` (上轨), `boll_lower` / `boll_dn` (下轨)；
  - RSI：`rsi6`, `rsi12`；
  - **港股分钟线日内切片聚合引擎**：针对 1m/5m/15m/30m/60m，底层调用腾讯实时分钟点流进行日内切片聚合，生成具备标准年月日与时分时间戳（如 `2026-10-22 10:30`）、真实高开低收、成交量、成交额与 MA/MACD 的完整 K 线；
  - **港股 60m 跨午休切分机制**：披露全天 330 分钟按【30+60+60+30】切分为 6 根 bar 机制（10:30, 11:30, 13:30, 14:30, 15:30, 16:00），午休时段标签实时纠偏为 12:00；
  - **服务边界自陈 (`market_notice`)**：明确披露公共数据源不提供跨日历史分钟线，跨日多天技术分析请查日线 (`period='day'`)；
  - **成交额修复保障**：采用高可用 `newfqkline` 接口，`adjust="none"` 不复权模式下日线成交额 100% 真实下发（彻底杜绝恒为 0.0）。

---

### 5. `stock_financials`（结构化财务报表三张表）

- **功能**：查询上市公司资产负债表、利润表、现金流量表。支持报告期累计与单季度做差拆分。
- **MCP Input Schema**：
  ```json
  {
    "type": "object",
    "properties": {
      "symbol": {"type": "string", "description": "证券代码：A股为6位数字，港股为5位数字"},
      "report_type": {
        "type": "string",
        "enum": ["balance", "income", "cashflow"],
        "default": "balance",
        "description": "财报类型：balance=资产负债表, income=利润表, cashflow=现金流量表"
      },
      "period_type": {
        "type": "string",
        "enum": ["report", "quarter"],
        "default": "report",
        "description": "统计口径：report=报告期累计, quarter=单季度做差拆分口径"
      },
      "limit_periods": {"type": "integer", "minimum": 1, "maximum": 10, "default": 4, "description": "返回历史财报期数"}
    },
    "required": ["symbol"],
    "additionalProperties": false
  }
  ```
- **A股准则动态映射字典 (isjr 标志位)**：
  - **金融机构准则 (`isjr=1` 银行/保险/证券)**：
    - 利润表：`利息净收入`, `手续费及佣金净收入`, `已赚保费`, `投资收益`, `公允价值变动收益`, `营业支出`, `信用减值损失`, `业务及管理费`, `利润总额`, `净利润`, `归母净利润`；
    - 资产负债表：`客户存款`, `向中央银行借款`, `同业及其他金融机构存放款项`, `吸收存款及同业存放`, `发放贷款和垫款净额`, `现金及存放中央银行款项`, `存放同业款项`, `金融投资`, `资产总计`, `负债合计`, `所有者权益合计`；
    - 现金流量表：`客户存款和同业存放款项净增加额`, `向中央银行借款净增加额`, `客户贷款及垫款净增加额`, `经营活动现金流量净额`；
    - **金融现金流差额自陈**：`经营活动现金流入不可加总差额_原保险保费及拆入回购等金融科目缺项` 与 `金融现金流编制说明`（针对通达信通用模板缺项自动核算自陈，如中国平安 5,899.44 亿差额逐位精确闭合）；
    - **筹资活动现金流入小计自动补齐**：上游缺列时自动规范填报为 0.0 或由【筹资净额 + 筹资流出小计】恒等式填平，并附带 `筹资流入说明`；
    - **三大活动净额双向镜像挂载**：`经营活动现金流量净额` 与 `经营活动产生的现金流量净额`、`投资活动现金流量净额` 与 `投资活动产生的现金流量净额`、`筹资活动现金流量净额` 与 `筹资活动产生的现金流量净额` 双向镜像互通，免除下游双容错；
  - **一般工商业企业准则 (`isjr=0`)**：
    - 利润表：`营业收入`, `营业成本`, `税金及附加`, `销售费用`, `管理费用`, `研发费用`, `财务费用`, `营业利润`, `利润总额`, `净利润`, `归母净利润`, `扣非净利润`；
    - 轧差与自陈字段：`营业利润不可加总差额_信用减值与资产减值损失及其他`, `流动资产不可加总差额`, `流动负债不可加总差额`, `筹资活动现金流入不可加总差额`；
- **港股专属标准化字段**：
  - `金额单位`：东财主力源统一为【元】；
  - `币种`：东财主力源统一探测校准为【CNY】（中资企业为人民币）；
  - `单季度计算说明`：当 `period_type="quarter"` 时，利用前驱缓冲区推算单季净额（如 2025Q3 营业额由 5518 亿扣减 H1 3615 亿还原为真实单季 1902.76 亿），孤立期标明累计值；
  - `现金桥闭合说明`：期末现金 = 期初现金 + 现金净额 + 汇率及期间变动其他项目；
  - `总负债_测算说明`：底层缺失总负债单项时由【流动负债合计 + 非流动负债合计】恒等式补全。

---

### 6. `stock_f10`（深度基本面、题材催化与筹码追踪）

- **功能**：查询上市公司题材板块、盈利预测、股东流水、股份回购、资金流向等。
- **MCP Input Schema**：
  ```json
  {
    "type": "object",
    "properties": {
      "symbol": {"type": "string", "description": "证券代码：A股6位（如'300750'），港股5位（如'00700'）"},
      "category": {
        "type": "string",
        "enum": [
          "concepts", "industry", "events", "dividends", "forecast",
          "shareholders", "top_holders", "repurchases", "buyback",
          "margin_trading", "capital_flow", "limit_up", "lhb",
          "unlocks", "summary", "basic_info", "indicators", "financials"
        ],
        "default": "concepts",
        "description": "基本面分类枚举"
      }
    },
    "required": ["symbol"],
    "additionalProperties": false
  }
  ```
- **核心分类与跨市场字段规格**：
  - `concepts` / `industry`：
    - A 股：`题材概念名称`, `概念解析与业务关联`, `题材纳入日期`；
    - 港股：`恒生行业分类`（100% 覆盖恒生 12 大行业标准大类，如资讯科技业、工业、必需性消费、电讯业等）、`市场板块`、`互联互通机制`；
  - `top_holders`：
    - A 股：十大流通股东（附带限售股核算指南与 A+H 股本说明）；
    - 港股：持股 5% 以上大股东最新申报聚合截面。**字段为 `申报顺位`（1..10）、`中文股东名称`、`目前结余率(%)`（真实百分比）、`目前类别`（好仓L）、`事件日期`**；已跨期去重，剔除淡仓 (S) 与可借出股份 (P)，**彻底移除冗余的 `major_shareholders` 重复表**；
  - `shareholders`：
    - A 股：股东人数变化与筹码集中度；
    - 港股：大股东增减持历史申报事件流水明细（单一表 `shareholder_events`，**自动清洗港交所 SDI 体系母子穿透申报产生的同日结余相同且涉及股数为 0 的冗余附注记录**）；
  - `repurchases` / `buyback`：港股公司股份回购每日明细（回购股数、最高最低价、耗资额）；
  - `indicators` / `financials`：港股核心财务与经营指标宽表（**记账币种已校准为法定编报币种 CNY，附带市值口径与编报币种说明**）；
  - `summary`：主营业务概要与防幻觉审计层（**严格校验单元格非空非 None 实质数据；港股 `所属行业` 100% 映射恒生 12 大行业分类，彻底消灭空串；对无效代码严禁输出假阳性“无质押风险”**）；
  - `capital_flow`：主力资金流向（超大单/大单/中单/小单净额）；
  - `margin_trading`：融资融券交易（A股融资融券余额与交易明细；港股返回场外孖展机制说明）。

---

### 7. `stock_screener`（国内 A 股自然语言智能选股）

- **功能**：自然语言选股条件筛选。
- **市场范围红线**：**固定覆盖中国 A 股全市场（沪深北）**。
- **港股意图警示机制**：当检索词中包含“港股/香港/HK”关键词时，注入：
  - `market_scope`: `"A股 (中国大陆交易所：沪市/深市/北交所)"`；
  - `is_hk_intent`: `true`；
  - `warning`: `"【市场范围警示】：当前自然语言选股引擎仅覆盖 A 股，暂不支持港股标的自动筛选..."`；
- **MCP Input Schema**：
  ```json
  {
    "type": "object",
    "properties": {
      "message": {"type": "string", "description": "自然语言选股条件，如'主板 绩优 储能'、'低市盈率 连板'"},
      "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10, "description": "返回股票数量上限(1-100)"},
      "page": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1, "description": "分页页码"},
      "filter_st": {"type": "boolean", "default": true, "description": "是否自动过滤 ST/*ST 风险警示股票，默认为 true"}
    },
    "required": ["message"],
    "additionalProperties": false
  }
  ```
- **返回结构核心字段**：
  - `query`, `page`, `limit`, `total_pages`, `matched_count`, `returned_count`, `filtered_st_count`, `market_scope`；
  - `stocks` 数组：`code`/`symbol`, `name`, `market`, `price`, `change_pct`, `is_st`, `indicators`/`matched_indicators`。

---

### 8. `stock_news`（个股最新资讯与重大公告）

- **功能**：获取指定个股最近发布的突发新闻资讯、重大公告、研报与媒体快讯。
- **代码存活性前置拦截**：传入的代码若非有效 A 股或港股上市标的，前置抛错拦截，绝不使用泛化模糊检索返回他股资讯伪装数据。
- **MCP Input Schema**：
  ```json
  {
    "type": "object",
    "properties": {
      "symbol": {"type": "string", "description": "证券代码（如'600519'、'00700'）或公司简称（如'贵州茅台'）"},
      "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5, "description": "返回新闻篇数(1-10)"}
    },
    "required": ["symbol"],
    "additionalProperties": false
  }
  ```
- **返回结构核心字段**：
  - `symbol`, `news_count`；
  - `news` 数组：`title`, `url`, `datetime` (标准 ISO 格式 `YYYY-MM-DD HH:MM:SS`), `time`, `source`, `summary`, `snippet`。
