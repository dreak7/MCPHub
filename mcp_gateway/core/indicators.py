"""MCPHub Finance 核心指标纯算库 (Pure Python Indicators Engine)。
提供完全零依赖 (Zero-Dependency) 的金融量化技术指标计算，
杜绝大模型在浮点指标计算过程中的数值幻觉。
"""

from __future__ import annotations

import math
from typing import Any

# =========================================================================
# 【功能块 4：纯 Python 原生轻量技术指标计算引擎 (Zero-Dependency)】
# - 输入：kline_list (list[dict]): 包含 open, close, high, low, volume 的 K 线列表
# - 功能：为历史 K 线滚动计算常用趋势与摆动指标：
#         1. 移动平均线: MA5, MA10, MA20
#         2. 异同移动平均线: MACD (DIF, DEA, MACD柱状图)
#         3. 随机指标: KDJ (9, 3, 3)
#         4. 布林线: BOLL (20日中轨、上轨、下轨)
#         5. 相对强弱指标: RSI (6, 12)
# - 输出：list[dict]: 附加了技术指标计算字段的 K 线字典数组，杜绝大模型计算幻觉
# =========================================================================
def calculate_indicators(kline_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not kline_list:
        return []

    enriched: list[dict[str, Any]] = [dict(item) for item in kline_list]
    closes = [float(k.get("close", 0.0)) for k in enriched]
    highs = [float(k.get("high", 0.0)) for k in enriched]
    lows = [float(k.get("low", 0.0)) for k in enriched]
    total_len = len(closes)

    # 1. 简单移动平均线 (MA5, MA10, MA20)
    for i in range(total_len):
        enriched[i]["ma5"] = round(sum(closes[max(0, i - 4):i + 1]) / (i - max(0, i - 4) + 1), 2) if i >= 4 else None
        enriched[i]["ma10"] = round(sum(closes[max(0, i - 9):i + 1]) / (i - max(0, i - 9) + 1), 2) if i >= 9 else None
        enriched[i]["ma20"] = round(sum(closes[max(0, i - 19):i + 1]) / (i - max(0, i - 19) + 1), 2) if i >= 19 else None

    # 2. 布林带 (BOLL, 20周期, 2倍标准差)
    for i in range(total_len):
        if i >= 19:
            window = closes[i - 19:i + 1]
            mean_val = sum(window) / 20.0
            variance = sum((x - mean_val) ** 2 for x in window) / 20.0
            std_dev = math.sqrt(variance)
            enriched[i]["boll_mid"] = round(mean_val, 2)
            enriched[i]["boll_up"] = round(mean_val + 2 * std_dev, 2)
            enriched[i]["boll_dn"] = round(mean_val - 2 * std_dev, 2)
            enriched[i]["boll_upper"] = enriched[i]["boll_up"]
            enriched[i]["boll_lower"] = enriched[i]["boll_dn"]
        else:
            enriched[i]["boll_mid"] = None
            enriched[i]["boll_up"] = None
            enriched[i]["boll_dn"] = None
            enriched[i]["boll_upper"] = None
            enriched[i]["boll_lower"] = None

    # 3. MACD 指标 (12, 26, 9)
    ema12, ema26, dea = 0.0, 0.0, 0.0
    for i, c in enumerate(closes):
        if i == 0:
            ema12 = c
            ema26 = c
            dif = 0.0
            dea = 0.0
        else:
            ema12 = ema12 * (11 / 13) + c * (2 / 13)
            ema26 = ema26 * (25 / 27) + c * (2 / 27)
            dif = ema12 - ema26
            dea = dea * (8 / 10) + dif * (2 / 10)
        macd_bar = 2 * (dif - dea)
        enriched[i]["macd_dif"] = round(dif, 3)
        enriched[i]["macd_dea"] = round(dea, 3)
        enriched[i]["macd_bar"] = round(macd_bar, 3)
        enriched[i]["macd_hist"] = round(macd_bar, 3)

    # 4. KDJ 指标 (9, 3, 3)
    k_val, d_val = 50.0, 50.0
    for i in range(total_len):
        start_idx = max(0, i - 8)
        c_low = min(lows[start_idx:i + 1])
        c_high = max(highs[start_idx:i + 1])
        rsv = 50.0
        if c_high > c_low:
            rsv = ((closes[i] - c_low) / (c_high - c_low)) * 100.0
        k_val = (2.0 / 3.0) * k_val + (1.0 / 3.0) * rsv
        d_val = (2.0 / 3.0) * d_val + (1.0 / 3.0) * k_val
        j_val = 3.0 * k_val - 2.0 * d_val
        enriched[i]["kdj_k"] = round(k_val, 2)
        enriched[i]["kdj_d"] = round(d_val, 2)
        enriched[i]["kdj_j"] = round(j_val, 2)

    # 5. RSI 相对强弱指标 (6周期 与 12周期)
    for period, name in ((6, "rsi6"), (12, "rsi12")):
        for i in range(total_len):
            if i >= period:
                gains, losses = 0.0, 0.0
                for j in range(i - period + 1, i + 1):
                    diff = closes[j] - closes[j - 1]
                    if diff > 0:
                        gains += diff
                    else:
                        losses -= diff
                if losses == 0.0:
                    enriched[i][name] = 100.0
                else:
                    rs = gains / losses
                    enriched[i][name] = round(100.0 - (100.0 / (1.0 + rs)), 2)
            else:
                enriched[i][name] = None

    return enriched

