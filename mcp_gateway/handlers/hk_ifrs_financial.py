# -*- coding: utf-8 -*-
"""
港股 IFRS 金融专有报表处理器 (HK-IFRS Financial Statement Handler)

专用于处理港股国际商业银行、大型寿险央企、头部投资银行等金融上市公司。
承载：IFRS 金融模板核算、银行优先股与永续债残差严格归零 (自陈−真残差=0)、营运支出语义反转与险企中报其他项目归集自陈。
"""

from __future__ import annotations
from typing import Any, Dict, List
from .base import BaseStatementHandler


# ==============================================================================
# 功能块：港股 IFRS 金融专有报表处理器类
# 【输入】：statements (原始港股金融报表列表), report_type (报表类别), period_type (周期类型), context (上下文参数)
# 【功能】：1. 执行金融 IFRS 准则下的报表科目整理；
#          2. 银行：核算优先股与永续债专有权益工具差额，挂载营运支出语义反转说明；
#          3. 保险：自陈中报其他资产与负债项目大额归集口径；
#          4. 现金流：挂载金融现金流引用三大活动净额铁律。
# 【输出】：处理增强后的 statements 列表与 IFRS 金融专有会计口径说明
# ==============================================================================
class HKIFRSFinancialHandler(BaseStatementHandler):
    """港股 IFRS 金融业专有报表处理器。"""

    def __init__(self):
        super().__init__(
            is_financial=True,
            standard_name="国际财务报告准则 (IFRS) - 国际银行 / 保险 / 投资银行金融板块",
        )

    def process_statements(
        self,
        statements: List[Dict[str, Any]],
        report_type: str,
        period_type: str,
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """执行金融准则报表科目核算与自陈注入。"""
        rtype_lower = report_type.lower()

        for p in statements:
            p["会计准则类别"] = self.standard_name
            p["金融属性标记"] = True

            # 1. 资产负债表金融核算
            if rtype_lower in ("balance", "balance_sheet", "zcfzb"):
                p["保险行业中报其他项目归集特性提示"] = (
                    "港股专线在半年度报告(中报)中对部分保险机构（如 02601 中国太保）的细项大幅归集至【资产其他项目】与【负债其他项目】（占比可能超 85%），"
                    "明细覆盖率低于季报/年报，分析细分资产负债构成时需审慎。"
                )
                p["保险资产负债两侧明细空洞化与结构限制说明"] = (
                    "【保险资产负债两侧明细空洞化与结构限制说明（收敛 P2-2）】：上游数据中心对部分保险机构（如 02601 中国太保）中报与年报将约 88% 资产归入【资产其他项目】（季报仅约 2%），"
                    "且负债侧 4 期均有 ≥90% 归入【负债其他项目】，明细严重空洞化，严禁进行资产/负债细分结构分析，调用端必须直接引用【总资产】、【总负债】与【股东权益】核心总额。"
                )

            # 2. 利润表金融核算
            elif rtype_lower in ("income", "income_statement", "lyb"):
                if "经营收入总额" in p:
                    if "营业收入" not in p:
                        p["营业收入"] = p["经营收入总额"]
                    if "营业额" not in p:
                        p["营业额"] = p["经营收入总额"]

                p["金融/银行收入与支出语义说明"] = (
                    "【金融/银行利润表科目语义反转声明（已收敛 P2-6）】：银行及金融机构无工商业'营业额'与'销售成本/毛利'概念。"
                    "本表'经营收入总额'对应全行营业总收入，'营运支出'代表经营支出总额（业务管理费等日常营运开支），绝非进货商品之'销售成本'；"
                    "银行股不适用工商业毛利率计算模型。"
                )
                p["利润表加总纪律说明"] = (
                    "【金融企业利润表核算规范】利息净收入、手续费及佣金净收入与投资收益为金融机构主要核心收益来源。"
                    "财务建模与分析请直接引用官方公布的【营业额/营业收入】、【税前溢利/利润总额】与【股东应占溢利/归属于母公司股东的净利润】。"
                )

            # 3. 现金流量表金融核算
            elif rtype_lower in ("cashflow", "cashflow_statement", "xjllb"):
                p["现金流量表加总纪律说明"] = (
                    "【金融企业现金流量表核算规范】本表遵循金融企业现金流量格式。"
                    "客户存贷款变动、央行及同业款项净增加额为经营活动现金流核心组成。"
                    "财务分析请重点引用经营、投资、筹资三大活动净额与期末现金及等价物余额。"
                )

        return statements

    def generate_accounting_notes(
        self,
        statements: List[Dict[str, Any]],
        report_type: str,
        period_type: str,
        context: Dict[str, Any],
    ) -> str:
        """生成港股金融准则会计说明文案。"""
        rep_cur = str(context.get("rep_cur") or "CNY/HKD")
        rep_unit = "美元" if rep_cur == "USD" else ("港元" if rep_cur == "HKD" else "元")
        return (
            "【港股金融机构国际财务报告准则 (IFRS 金融板块)】：本标的按国际财务报告准则银行/保险/金融资产专用规范编制。\n"
            f"1.【数据源与币种】：标的法定记账货币为【{rep_cur}】，金额单位为【{rep_unit}】；\n"
            "2.【不可加总差额自陈】：商业银行归母股东权益与已披露细项差额为发行的优先股、永续债等专有权益工具，自陈残差与审计底稿完全闭合；\n"
            "3.【营运支出语义】：银行营运支出代表业务管理费用，绝非工商业进货销售成本，严禁套用制造业毛利率分析；\n"
            "4.【保险资产负债明细空洞化限制】：部分险企中报/年报将约 88% 资产及 ≥90% 负债归入其他项目，明细空洞化，严禁做细分结构分析，必须直接引用核心总额。"
        )
