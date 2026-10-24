"""金融数据代数恒等闭环断言与安检门禁系统 (Financial Accounting Invariants Verifier)
提供对资产负债表、工商业利润表、龙虎榜交易量纲与估值换期的代数闭环自动化安全审查。
任何数据在返回前或测试套件中，均可通过本门禁验证，防止任何代数不平衡或量纲错标外泄。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("mcp_gateway.invariants")


# =========================================================================
# 【功能块 1：资产负债表代数恒等式门禁】
# - 输入：statement_rows (list[dict[str, Any]]): 资产负债表多期指标记录列表
# - 功能：严格核验 会计恒等式【资产总计 ≡ 负债合计 + 所有者权益合计】
#         若两者差额绝对值 > 0.05，核实是否存在已自陈的不可加总差额字段；
#         若未自陈且不平衡，直接抛出 AssertionError 阻断错误数据外泄。
# - 输出：bool: 验证通过返回 True，发现违规抛出 AssertionError
# =========================================================================
def verify_balance_sheet_invariants(statement_rows: list[dict[str, Any]], strict: bool = True) -> bool:
    """验证资产负债表代数闭环：资产总计 ≡ 负债合计 + 所有者权益合计"""
    if not statement_rows:
        return True

    for row in statement_rows:
        period = row.get("REPORT_DATE") or row.get("报告期") or row.get("date") or "未知期次"
        
        # 提取三大核心要素（支持中英文字段）
        tot_assets = row.get("资产总计") or row.get("TOTAL_ASSETS") or row.get("TOTAL_ASSET")
        tot_liab = row.get("负债合计") or row.get("TOTAL_LIABILITIES") or row.get("TOTAL_LIAB")
        tot_equity = (
            row.get("所有者权益合计")
            or row.get("股东权益合计")
            or row.get("TOTAL_EQUITY")
            or row.get("TOTAL_SHAREHOLDERS_EQUITY")
        )

        if tot_assets is None or tot_liab is None or tot_equity is None:
            continue

        try:
            val_assets = float(tot_assets)
            val_liab = float(tot_liab)
            val_equity = float(tot_equity)
        except (ValueError, TypeError):
            continue

        calc_sum = val_liab + val_equity
        diff = abs(val_assets - calc_sum)

        # 允许 5 分钱以内的微小浮点误差
        if diff > 0.05:
            # 检查是否有显式自陈说明（例如保险公司 CAS 25 缺失保险合同负债）
            has_declaration = any(
                ("不可加总差额" in k or "差额" in k or "说明" in k or "限制" in k)
                for k in row.keys()
            )
            if not has_declaration:
                err_msg = (
                    f"【门禁拦截 - 资产负债表代数未闭合】报告期 {period}: "
                    f"资产总计({val_assets}) ≠ 负债合计({val_liab}) + 所有者权益合计({val_equity}), "
                    f"差额高达 {diff:.4f}，且未检测到任何自陈字段！严禁带病出厂。"
                )
                if strict:
                    raise AssertionError(err_msg)
                else:
                    logger.warning(err_msg)
                    row["【代数闭环门禁自检告警】"] = err_msg

    return True


# =========================================================================
# 【功能块 2：利润表代数恒等式门禁】
# - 输入：statement_rows (list[dict[str, Any]]): 利润表多期指标记录列表
# - 功能：严格核验 工商业利润表恒等式【利润总额 ≡ 营业利润 + 营业外收入 - 营业外支出】
#         若三项均存在，差额必须在 0.05 容差范围内；若缺失则必须挂载推导说明。
# - 输出：bool: 验证通过返回 True，发现违规抛出 AssertionError
# =========================================================================
def verify_income_statement_invariants(statement_rows: list[dict[str, Any]], strict: bool = True) -> bool:
    """验证工商业利润表代数闭环：利润总额 ≡ 营业利润 + 营业外收入 - 营业外支出"""
    if not statement_rows:
        return True

    for row in statement_rows:
        period = row.get("REPORT_DATE") or row.get("报告期") or row.get("date") or "未知期次"

        tot_profit = row.get("利润总额") or row.get("TOTAL_PROFIT")
        op_profit = row.get("营业利润") or row.get("OPERATING_PROFIT")
        non_op_inc = row.get("营业外收入") or row.get("NON_OPERATING_INCOME")
        non_op_exp = row.get("营业外支出") or row.get("NON_OPERATING_EXPENSES")

        if tot_profit is None or op_profit is None:
            continue

        # 若营业外收支有缺失，检查是否已挂载【利润总额核算说明】
        if non_op_inc is None or non_op_exp is None:
            if "利润总额核算说明" not in row and strict:
                err_msg = f"【门禁拦截 - 利润表关键科目缺失且未自陈】报告期 {period}: 营业外收支存在 null 且未标注推导说明！"
                raise AssertionError(err_msg)
            continue

        try:
            val_tot = float(tot_profit)
            val_op = float(op_profit)
            val_inc = float(non_op_inc)
            val_exp = float(non_op_exp)
        except (ValueError, TypeError):
            continue

        expected_tot = val_op + val_inc - val_exp
        diff = abs(val_tot - expected_tot)

        if diff > 0.05:
            err_msg = (
                f"【门禁拦截 - 利润表代数未闭合】报告期 {period}: "
                f"利润总额({val_tot}) ≠ 营业利润({val_op}) + 营业外收入({val_inc}) - 营业外支出({val_exp}), "
                f"差额为 {diff:.4f}！"
            )
            if strict:
                raise AssertionError(err_msg)
            else:
                logger.warning(err_msg)
                row["【代数闭环门禁自检告警】"] = err_msg

    return True


# =========================================================================
# 【功能块 3：龙虎榜金额量纲与代数恒等式门禁】
# - 输入：billboard_records (list[dict[str, Any]]): 龙虎榜明细记录列表
# - 功能：严格核验 龙虎榜恒等式【买入总额 - 卖出总额 ≡ 净买入额】
#         同时核验金额量纲是否为“万元”（买入总额不应超过 1e7 万元，即 1000 亿元天花板，防止 1e4 膨胀）
# - 输出：bool: 验证通过返回 True，发现违规抛出 AssertionError
# =========================================================================
def verify_lhb_invariants(billboard_records: list[dict[str, Any]], strict: bool = True) -> bool:
    """验证龙虎榜金额恒等式与万元量纲：买入总额 - 卖出总额 ≡ 净买入额 且 无量纲膨胀"""
    if not billboard_records:
        return True

    for row in billboard_records:
        t_date = row.get("交易日期") or row.get("TDATE") or "未知日期"
        buy_amt = row.get("龙虎榜买入总额_万元") or row.get("买入总额_万元") or row.get("BILLBOARD_BUY_AMT")
        sell_amt = row.get("龙虎榜卖出总额_万元") or row.get("卖出总额_万元") or row.get("BILLBOARD_SELL_AMT")
        net_amt = row.get("龙虎榜净买入额_万元") or row.get("净买入额_万元") or row.get("BILLBOARD_NET_AMT")

        if buy_amt is None or sell_amt is None or net_amt is None:
            continue

        try:
            val_buy = float(buy_amt)
            val_sell = float(sell_amt)
            val_net = float(net_amt)
        except (ValueError, TypeError):
            continue

        # 1. 恒等式校验
        expected_net = val_buy - val_sell
        diff = abs(val_net - expected_net)
        if diff > 0.1:
            err_msg = (
                f"【门禁拦截 - 龙虎榜代数未闭合】日期 {t_date}: "
                f"买入总额({val_buy}) - 卖出总额({val_sell}) ≠ 净买入额({val_net}), 差额 {diff:.2f}"
            )
            if strict:
                raise AssertionError(err_msg)
            else:
                logger.warning(err_msg)

        # 2. 万元量纲防膨胀校验：单票单日买入总额若超过 500 亿元（5,000,000 万元），极可能是误将元作为万元
        if val_buy > 5000000.0:
            err_msg = (
                f"【门禁拦截 - 龙虎榜金额单位严重错标】日期 {t_date}: "
                f"买入总额高达 {val_buy} 万元(即 {val_buy/10000:.2f} 亿元)，疑似存在 10000 倍膨胀(元误标为万元)！"
            )
            if strict:
                raise AssertionError(err_msg)
            else:
                logger.warning(err_msg)

    return True


# =========================================================================
# 【功能块 4：实时行情与估值指标换期对齐门禁】
# - 输入：quote_dict (dict[str, Any]): 行情数据字典 (含市净率、BPS、现价等)
# - 功能：严格核验 现价/BPS 与市净率_PB 是否自洽，若偏离 >0.5% 必须挂载换期重算说明
# - 输出：bool: 验证通过返回 True，发现违规抛出 AssertionError
# =========================================================================
def verify_valuation_invariants(quote_dict: dict[str, Any], strict: bool = True) -> bool:
    """验证估值指标自洽性：现价/BPS 与 市净率_PB 相对偏离不超过 0.5%，或已明确自陈换期"""
    if not quote_dict:
        return True

    now_price = quote_dict.get("现价") or quote_dict.get("price")
    bps = quote_dict.get("每股净资产") or quote_dict.get("bps") or quote_dict.get("BPS")
    pb = quote_dict.get("市净率") or quote_dict.get("市净率_PB") or quote_dict.get("pb")

    if now_price is None or bps is None or pb is None:
        return True

    try:
        val_price = float(now_price)
        val_bps = float(bps)
        val_pb = float(pb)
    except (ValueError, TypeError):
        return True

    if val_price > 0 and val_bps > 0 and val_pb > 0:
        theoretical_pb = val_price / val_bps
        rel_diff = abs(val_pb - theoretical_pb) / theoretical_pb

        # 偏离超过 0.5%
        if rel_diff > 0.005:
            has_recalc_note = any(
                ("校准说明" in k or "换期说明" in k or "换期重算" in k or "PB校准" in k)
                for k in quote_dict.keys()
            )
            if not has_recalc_note:
                err_msg = (
                    f"【门禁拦截 - 市净率与BPS脱节未自陈】现价={val_price}, BPS={val_bps}, "
                    f"理论PB={theoretical_pb:.4f}, 返回PB={val_pb:.4f}, 相对偏离={rel_diff:.2%}, "
                    f"且未挂载任何校准或换期重算说明！"
                )
                if strict:
                    raise AssertionError(err_msg)
                else:
                    logger.warning(err_msg)

    return True


# =========================================================================
# 【功能块 5：全品类金融数据统一综合安检门禁 (出口安检总机)】
# - 输入：response_data (dict[str, Any]): 任意金融网关对外返回的字典数据
#         strict (bool): 为 True 时违规抛出 AssertionError，为 False 时记录警告并注入说明
# - 功能：自动根据返回字典中的字段与子表类型，智能调度上述各专业门禁进行全面安全扫描
# - 输出：dict[str, Any]: 经过安全扫描与闭环认证的合规数据字典
# =========================================================================
def verify_all_financial_invariants(response_data: dict[str, Any], strict: bool = True) -> dict[str, Any]:
    """统一金融数据安检门：自动识别财报、龙虎榜、行情估值，执行代数闭环与量纲审查"""
    if not isinstance(response_data, dict):
        return response_data

    # 1. 扫描资产负债表与利润表
    fmt_tables = response_data.get("formatted_tables", {})
    if isinstance(fmt_tables, dict):
        # 资产负债表检查
        bs_rows = fmt_tables.get("balance_sheet") or fmt_tables.get("balance") or []
        if isinstance(bs_rows, list):
            verify_balance_sheet_invariants(bs_rows, strict=strict)

        # 利润表检查
        inc_rows = fmt_tables.get("income_statement") or fmt_tables.get("income") or []
        if isinstance(inc_rows, list):
            verify_income_statement_invariants(inc_rows, strict=strict)

        # 龙虎榜检查
        lhb_rows = fmt_tables.get("billboard_records") or []
        if isinstance(lhb_rows, list):
            verify_lhb_invariants(lhb_rows, strict=strict)

    # 2. 扫描顶层财务列表 (例如 stock_financials 返回)
    statements = response_data.get("statements") or response_data.get("data")
    rep_type = response_data.get("report_type")
    if isinstance(statements, list):
        if rep_type == "balance":
            verify_balance_sheet_invariants(statements, strict=strict)
        elif rep_type == "income":
            verify_income_statement_invariants(statements, strict=strict)

    # 3. 扫描估值与盘口
    verify_valuation_invariants(response_data, strict=strict)

    return response_data
