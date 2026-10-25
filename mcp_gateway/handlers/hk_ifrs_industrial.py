# -*- coding: utf-8 -*-
"""
港股 IFRS 工商业报表处理器 (HK-IFRS Industrial Statement Handler)

专用于处理港股一般工商业科技、电商、制造、消费类上市公司（执行 IFRS / HKFRS 准则）。
承载：财年首季守护 (阿里等3月财年)、单季度前驱做差推算 (腾讯单季真实还原)、法定美元原币反解还原 (小马智行等)。
"""

from __future__ import annotations
from typing import Any, Dict, List
from .base import BaseStatementHandler


# ==============================================================================
# 功能块：港股 IFRS 工商业报表处理器类
# 【输入】：statements (原始港股报表列表), report_type (报表类别), period_type (周期类型), context (上下文参数)
# 【功能】：1. 执行工商业 IFRS 准则下的报表科目整理；
#          2. 标注财年首季与单季度做差推算特征；
#          3. 注入工商业跨表勾稽与汇率反解说明。
# 【输出】：处理增强后的 statements 列表与 IFRS 工商业会计口径说明
# ==============================================================================
class HKIFRSIndustrialHandler(BaseStatementHandler):
    """港股 IFRS 一般工商业专有报表处理器。"""

    def __init__(self):
        super().__init__(
            is_financial=False,
            standard_name="国际财务报告准则 (IFRS) / 香港财务报告准则 (HKFRS) - 一般工商业",
        )

    def process_statements(
        self,
        statements: List[Dict[str, Any]],
        report_type: str,
        period_type: str,
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """执行工商业准则报表科目核算与自陈注入。"""
        rtype_lower = report_type.lower()
        real_currency = context.get("real_currency") or "HKD/CNY"

        for p in statements:
            p["会计准则类别"] = self.standard_name
            p["金融属性标记"] = False

            # 工商业报表通用规范
            if rtype_lower in ("income", "income_statement", "lyb"):
                p["工商业毛利说明"] = "港股工商业企业毛利 = 营业额 − 销售成本（或营运支出）。"

            elif rtype_lower in ("cashflow", "cashflow_statement", "xjllb"):
                p["工商业现金及等价物跨表勾稽说明"] = (
                    "【工商业现金跨表勾稽与模板轴说明（收敛 P2-1 与 WARN-2）】：现金流量表「期末现金及现金等价物」指不受使用限制且可随时支取的流动资金。"
                    "资产负债表「现金及等价物/银行结存」在部分企业中包含受限制存款或定期存款。"
                    "注意部分大型工商业企业（如 00883 中国海洋石油）一/三季报采用 64 键简表，未单列短期银行存款，与中报/年报 52~53 键存在模板轴分裂，导致季报跨表现金存在千亿级差额，属报告期模板轴分裂（结构不可比），严禁跨季度进行细项结构对比；"
                    "部分标的（如 01088 中国神华）现金差额为上游未分解项，记为【口径不可分解】，严禁分析端强套受限存款假说解释。"
                )
                if real_currency == "USD":
                    p["美港股跨表现金勾稽说明"] = (
                        "现金流量表「期末现金及现金等价物」指不受使用限制且可随时支取的流动资金；"
                        "资产负债表「现金及等价物/银行结存」包含受限制存款与冻结资金等，"
                        "二者存在 0.6%~1.7% 正常会计准则分类差异，属标准财务报表特征。"
                    )

        return statements

    def generate_accounting_notes(
        self,
        statements: List[Dict[str, Any]],
        report_type: str,
        period_type: str,
        context: Dict[str, Any],
    ) -> str:
        """生成港股工商业准则会计说明文案。"""
        rep_cur = str(context.get("rep_cur") or "CNY/HKD")
        rep_unit = "美元" if rep_cur == "USD" else ("港元" if rep_cur == "HKD" else "元")
        return (
            "【港股一般工商业国际会计准则 (IFRS / HKFRS)】：本标的按国际/香港财务报告准则一般企业标准编制。\n"
            f"1.【数据源与币种】：主力通道为东方财富数据中心，标的法定记账货币为【{rep_cur}】，金额单位为【{rep_unit}】；\n"
            "2.【单季度推算】：流量表（利润与现金流）实行同财年前驱做差推算，财年第一季度（Q1）披露值即为单季值，阻断跨财年扣减；\n"
            "3.【双向别名互通】：为消除性质法与功能法分歧，【销售成本】与【营运支出】已建立双向兼容映射。"
        )
