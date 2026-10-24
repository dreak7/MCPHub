"""MCPHub Finance 核心市场工具与交易上下文模块。
包含根据证券代码智能推导交易所市场代码 (setcode)，
以及基于北京时间 (UTC+8) 的交易时钟与日历状态感知。
"""

from __future__ import annotations

import datetime
from typing import Any

# =========================================================================
# 【功能块 3：证券交易所与市场代码智能推导器】
# - 输入：symbol (str): 股票、基金或指数代码（如 "600519", "000001", "835185"）
# - 功能：遵循中国证券市场编制规则，自动推导市场 setcode 代码：
#         '1'=上海证券交易所(沪市), '0'=深圳证券交易所(深市), '2'=北京证券交易所(北交所)
# - 输出：str: 推导得到的市场代码
# =========================================================================
def infer_market_setcode(symbol: str) -> str:
    clean_sym = symbol.strip().upper()
    # 港股标的 (5位纯数字或带有 HK 前缀，如 00700, 09988, HK00700)
    if clean_sym.startswith("HK") or (clean_sym.isdigit() and len(clean_sym) == 5):
        return "31"
    # 优先匹配北交所专属代码 (包含920新代码号段、8开头及4开头)
    if clean_sym.startswith(("BJ", "8", "4", "920")):
        return "2"
    # 沪市主板、科创板、B股、基金与转债
    if clean_sym.startswith(("SH", "6", "9", "5", "11")):
        return "1"
    # 深市主板、创业板、B股、基金与转债
    if clean_sym.startswith(("SZ", "0", "3", "2", "1", "12", "15")):
        return "0"
    return "0"


# =========================================================================
# 【功能块 5：A股交易日历与时钟状态感知器】
# - 输入：无
# - 功能：判断当前北京时间（UTC+8）处于哪个交易阶段（开盘竞价/连续交易/午间休市/已收盘/周末），
#         提供基准交易日，防止大模型在非交易时段产生“股票异常停牌”等幻觉
# - 输出：dict: 包含当前交易状态与交易上下文
# =========================================================================
def get_market_context() -> dict[str, Any]:
    # 强制以北京时间 (UTC+8) 计算
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cst_time = now_utc + datetime.timedelta(hours=8)
    weekday = cst_time.weekday()  # 0=周一, 6=周日
    current_time_str = cst_time.strftime("%H:%M:%S")
    date_str = cst_time.strftime("%Y-%m-%d")

    is_weekend = weekday in (5, 6)
    time_val = cst_time.hour * 100 + cst_time.minute

    if is_weekend:
        status = "weekend_closed"
        status_desc = "周末休市"
    elif time_val < 915:
        status = "pre_market_closed"
        status_desc = "盘前未开盘 (等待9:15开盘集合竞价)"
    elif 915 <= time_val <= 925:
        status = "opening_auction"
        status_desc = "开盘集合竞价阶段 (9:15-9:25)"
    elif 930 <= time_val <= 1130:
        status = "trading_morning"
        status_desc = "早盘连续交易进行中 (9:30-11:30)"
    elif 1130 < time_val < 1300:
        status = "lunch_break"
        status_desc = "午盘休市中 (11:30-13:00)"
    elif 1300 <= time_val <= 1500:
        status = "trading_afternoon"
        status_desc = "午后连续交易进行中 (13:00-15:00)"
    else:
        status = "closed_today"
        status_desc = "今日已收盘 (15:00后结算状态)"

    # 计算当前或上一个有效交易日基准 (参考交易日)
    if is_weekend:
        offset = 1 if weekday == 5 else 2  # 周六看周五(1天前)，周日看周五(2天前)
    elif time_val < 915:
        offset = 3 if weekday == 0 else 1  # 周一开盘前看上周五(3天前)，周二至周五开盘前看昨天(1天前)
    else:
        # 周一至周五 9:15 以后（包含集合竞价、盘中连续交易以及 15:00 盘后结算后），当前基准交易日即为今天
        offset = 0

    last_trading_day = (cst_time - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")

    # -------------------------------------------------------------------------
    # 港股交易时钟与日历感知 (港交所持续交易 09:30-12:00, 13:00-16:00, CAS 16:00-16:10)
    # -------------------------------------------------------------------------
    if is_weekend:
        hk_status = "weekend_closed"
        hk_status_desc = "周末休市"
    elif time_val < 900:
        hk_status = "pre_market_closed"
        hk_status_desc = "港股盘前未开盘 (等待9:00开市前时段)"
    elif 900 <= time_val < 920:
        hk_status = "opening_auction"
        hk_status_desc = "港股开市前时段/开盘竞价 (9:00-9:20)"
    elif 920 <= time_val < 930:
        hk_status = "order_matching"
        hk_status_desc = "港股对盘与暂缓时段 (9:20-9:30)"
    elif 930 <= time_val <= 1200:
        hk_status = "trading_morning"
        hk_status_desc = "港股早盘连续交易进行中 (9:30-12:00)"
    elif 1200 < time_val < 1300:
        hk_status = "lunch_break"
        hk_status_desc = "港股午盘休市中 (12:00-13:00)"
    elif 1300 <= time_val < 1600:
        hk_status = "trading_afternoon"
        hk_status_desc = "港股午后连续交易进行中 (13:00-16:00)"
    elif 1600 <= time_val <= 1610:
        hk_status = "closing_auction"
        hk_status_desc = "港股收市竞价时段 (CAS 16:00-16:10)"
    else:
        hk_status = "closed_today"
        hk_status_desc = "港股今日已收盘 (16:10后结算状态)"

    hk_offset = offset
    if is_weekend:
        hk_offset = 1 if weekday == 5 else 2
    elif time_val < 900:
        hk_offset = 3 if weekday == 0 else 1
    else:
        hk_offset = 0
    last_hk_trading_day = (cst_time - datetime.timedelta(days=hk_offset)).strftime("%Y-%m-%d")

    return {
        "current_cst_time": f"{date_str} {current_time_str}",
        # A 股时钟与状态 (完全保持向后兼容)
        "market_status": status,
        "market_status_desc": status_desc,
        "is_trading_time": status in ("trading_morning", "trading_afternoon", "opening_auction"),
        "reference_trading_date": last_trading_day,
        # 港股时钟与状态 (解决 P3-4: 增加港股交易日历感知)
        "hk_market_status": hk_status,
        "hk_market_status_desc": hk_status_desc,
        "hk_is_trading_time": hk_status in ("trading_morning", "trading_afternoon", "opening_auction", "closing_auction"),
        "hk_reference_trading_date": last_hk_trading_day,
        "market_hours_comparison": (
            "【A股与港股交易时间差异说明】：\n"
            "· A股：早盘 09:30-11:30，午盘 13:00-15:00（午休 1.5 小时，15:00 收盘）；\n"
            "· 港股：早盘 09:30-12:00，午盘 13:00-16:00，收市竞价 CAS 16:00-16:10（午休 1 小时，16:10 收市）。"
        ),
    }


