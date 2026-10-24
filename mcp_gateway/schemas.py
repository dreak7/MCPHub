"""MCPHub Finance 工具标准 JSON Schema 元数据定义。
向大模型 (LLM / Agent) 与客户端精确描述 8 大金融工具的用途、参数类型、默认值与取值范围。
"""

from __future__ import annotations

# =========================================================================
# 【功能块：8 大金融工具标准 MCP JSON Schema 元数据定义】
# - 输入：无
# - 功能：向大模型精确描述每个工具的用途、参数类型、默认值与取值范围
# - 输出：dict 映射表
# =========================================================================

STOCK_LOOKUP_SCHEMA = {
    "name": "stock_lookup",
    "description": "根据公司简称、股票别名或代码模糊检索标的代码（如'贵州茅台'、'腾讯'、'半导体ETF'）。支持A股(AG)、港股(HK-GP)、美股(MG-GP)、基金(JJ)、指数(ZS)。在进行股票或行业分析时，若用户提供了股票简称而非6位数字代码，应优先调用本工具精确查找标的代码与交易所代码。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "实体名称或简称，如'贵州茅台'、'腾讯'、'上证指数'"},
            "range": {"type": "string", "enum": ["AG", "HK-GP", "HK-JJ", "JJ", "MG-GP", "ZS"], "default": "AG", "description": "市场类型：AG=A股(默认), HK-GP=港股, HK-JJ=港股基金, JJ=基金, MG-GP=美股, ZS=指数"}
        },
        "required": ["query"],
        "additionalProperties": False
    }
}

STOCK_QUOTES_SCHEMA = {
    "name": "stock_quotes",
    "description": "查询指定股票的实时五档盘口、买卖挂单、最新现价、涨跌幅、换手率、量比、内外盘强弱、总市值与动态市盈率等高频盘口与专业估值指标。全面支持 A 股（通达信官方专线）与港股（腾讯免费专线回退，如 '00700' 腾讯控股、'09988' 阿里巴巴）。数据全面中文化映射。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'、'000001'），港股为5位数字（如'00700'、'09988'，支持'HK'前缀）"},
            "setcode": {"type": "string", "description": "市场代码：0=深市，1=沪市，2=北交所，31=港交所。若不传将自动智能推导"}
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

STOCK_KLINE_SCHEMA = {
    "name": "stock_kline",
    "description": "获取指定股票的多周期历史复权K线走势，并自动计算输出关键技术指标（MA5/MA10/MA20均线、MACD指标DIF/DEA/柱状图、KDJ随机指标、BOLL布林带上中下轨）。全面支持 A 股及港股（腾讯免费专线）。支持日线(day)、周线(week)、月线(month)、60分钟(60m)、15分钟(15m)、5分钟(5m)。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'），港股为5位数字（如'00700'）"},
            "period": {"type": "string", "enum": ["day", "week", "month", "60m", "30m", "15m", "5m", "1m"], "default": "day", "description": "K线周期，默认为 day (日线)"},
            "count": {"type": "integer", "minimum": 1, "maximum": 500, "default": 30, "description": "请求K线根数（1-500，A股日K常规截断至300，港股日K支持至500；港股日内分钟线仅覆盖单日，全天最大可用根数：1m约332根、5m为66根、15m为21根、30m为11根、60m为6根，超出时交付单日全量并附超限说明），默认30根"},
            "adjust": {"type": "string", "enum": ["qfq", "hfq", "none"], "default": "qfq", "description": "复权方式：qfq=前复权(默认), hfq=后复权, none=不复权"},
            "setcode": {"type": "string", "description": "市场代码：0=深市，1=沪市，2=北交所，31=港交所。默认自动推导"},
            "with_indicators": {"type": "boolean", "default": True, "description": "是否自动附带技术指标计算结果（MA、MACD、KDJ、BOLL），默认为 true"}
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

STOCK_FINANCIALS_SCHEMA = {
    "name": "stock_financials",
    "description": "查询上市公司三大财务报表（资产负债表 balance、利润表 income、现金流量表 cashflow）与主营构成。支持报告期累计(report)与单季度(quarter)双口径切换，支持历史多期财报横向对比，所有会计科目均中文化映射。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'），港股为5位数字（如'00700'）"},
            "report_type": {"type": "string", "enum": ["balance", "income", "cashflow"], "default": "balance", "description": "财报类型：balance=资产负债表, income=利润表, cashflow=现金流量表"},
            "period_type": {"type": "string", "enum": ["report", "quarter"], "default": "report", "description": "统计口径：report=报告期累计(一季报/中报/三季报/年报), quarter=单季度口径"},
            "limit_periods": {"type": "integer", "minimum": 1, "maximum": 10, "default": 4, "description": "返回的历史财报期数，默认4期"}
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

STOCK_F10_SCHEMA = {
    "name": "stock_f10",
    "description": "查询上市公司深度基本面与筹码数据。包括热点题材板块族谱(concepts/industry)、事件驱动催化与分红派息(events/dividends)、机构盈利预测与评级(forecast)、股东人数变化与筹码集中度(shareholders)、十大流通股东(top_holders)、港股公司股份回购明细(repurchases/buyback)、融资融券明细(margin_trading)、主力资金流向(capital_flow)、涨停分析与异动(limit_up/lhb)、限售解禁日程(unlocks)、公司概要与最新提示(summary)、公司基本工商与发行信息(basic_info)。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'），港股为5位数字（如'00700'）"},
            "category": {
                "type": "string",
                "enum": [
                    "concepts", "industry", "events", "dividends", "forecast",
                    "shareholders", "top_holders", "repurchases", "buyback",
                    "margin_trading", "capital_flow", "limit_up", "lhb",
                    "unlocks", "summary", "basic_info", "indicators", "financials"
                ],
                "default": "concepts",
                "description": "基本面分类：concepts/industry=热点题材板块族谱, events/dividends=事件驱动催化与分红派息, forecast=机构盈利预测, shareholders=股东人数与大股东增减持流水, top_holders=十大流通股东(港股为持股5%以上主要股东), repurchases/buyback=股份回购明细(港股), capital_flow=主力资金流向, margin_trading=融资融券, limit_up/lhb=涨停分析与龙虎榜席位, unlocks=限售解禁, summary=公司概要, basic_info=公司基本工商与发行信息, indicators/financials=核心财务与经营指标宽表"
            },
            "periods": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "description": "历史期次数量限制（针对 top_holders 等多期数据，可选；默认返回全部历史期次如 18 期；指定为 1 可仅取最新期，大幅降低传输体积）"
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "单表返回行数限制（可选）"
            }
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

STOCK_SCREENER_SCHEMA = {
    "name": "stock_screener",
    "description": "自然语言智能选股与行情条件筛选。支持自然语言组合条件提问（如'主板 绩优 储能 连板'、'低市盈率 储能 突破年线'），直接返回符合条件的股票标的列表及指标概览。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "自然语言选股条件，如'主板 绩优 储能'、'低市盈率 连板'"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10, "description": "最多返回标的数量（1-100）"},
            "page": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1, "description": "分页页码"},
            "filter_st": {"type": "boolean", "default": True, "description": "是否自动过滤 ST/*ST 风险警示股票，默认为 true"}
        },
        "required": ["message"],
        "additionalProperties": False
    }
}

STOCK_NEWS_SCHEMA = {
    "name": "stock_news",
    "description": "获取指定个股最近发布的突发新闻资讯、重大公告、研报与媒体快讯，辅助解读股价异动与盘中突发利好/利空。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码（如'600519'）"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5, "description": "返回新闻篇数（1-10）"}
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

MARKET_CONTEXT_SCHEMA = {
    "name": "market_context",
    "description": "获取 A 股当前实时交易时钟与市场运行状态（如开盘竞价、早盘交易中、午盘休市、午后交易中、今日已收盘、周末休市），并提供基准交易日。在分析行情或解盘前可先调用本工具确认市场当前所处的时段。",
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False
    }
}
