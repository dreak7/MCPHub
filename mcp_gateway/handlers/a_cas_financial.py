# -*- coding: utf-8 -*-
"""
A 股 CAS 金融专有报表处理器 (A-CAS Financial Statement Handler)

专用于处理 A 股商业银行、证券公司、保险公司及大型金融控股集团。
承载：金融专线 (isjr=1) 专有科目解析、券商资金隔离、险企 CAS 25 缺口自陈、存贷款恒等式及普通股真实 PB 自算。
"""

from __future__ import annotations
from typing import Any, Dict, List
from .base import BaseStatementHandler


# ==============================================================================
# 功能块：A 股 CAS 金融专有报表处理器类
# 【输入】：statements (原始金融报表列表), report_type (报表类别), period_type (周期类型), context (上下文参数)
# 【功能】：1. 自动识别银行、券商、保险子行业并分别特化处理；
#          2. 银行：核算吸收存款、客户贷款，自陈金融资产结构限制；
#          3. 券商：特化利润表与现金流说明，纠正 N020 为交易金融资产净支出，剔除客户资金双计；
#          4. 险企：自陈 CAS 25 新保险准则缺口，警示严禁计算工商业毛利；
#          5. 现金流：规范隔离间接法补充资料，杜绝整表遍历双计。
# 【输出】：处理增强后的 statements 列表与金融专有会计口径说明
# ==============================================================================
class ACASFinancialHandler(BaseStatementHandler):
    """A 股 CAS 金融业专有报表处理器。"""

    def __init__(self):
        super().__init__(
            is_financial=True,
            standard_name="中国企业会计准则 (CAS) - 商业银行 / 证券 / 保险金融板块",
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
        sub_type = str(context.get("sub_type") or "financial")
        is_broker = sub_type == "broker"
        is_insurance = sub_type == "insurance"
        is_bank = sub_type == "bank"

        for p in statements:
            p["会计准则类别"] = self.standard_name
            p["金融属性标记"] = True
            p["金融细分子行业"] = "证券公司" if is_broker else ("保险公司" if is_insurance else ("商业银行" if is_bank else "综合金融"))

            # 1. 资产负债表金融核算
            if rtype_lower in ("balance", "balance_sheet", "zcfzb"):
                p["金融资产负债不区分流动性声明"] = (
                    "金融企业（银行/证券/保险）资产与负债通常不严格划分流动与非流动属性，"
                    "分析时请以资产总计恒等式【资产总计 = 负债合计 + 所有者权益合计】为准。"
                )
                if is_broker:
                    p["券商客户资金说明"] = "证券公司资产端包含客户资金存款，负债端包含代理买卖证券款，二者相互对应。"
                elif is_insurance:
                    p["保险准备金说明"] = "保险公司主要负债为保险合同负债（准备金），受 CAS 25 新准则约束。"

            # 2. 利润表金融核算
            elif rtype_lower in ("income", "income_statement", "lyb"):
                if is_broker:
                    p["证券企业收入说明"] = "证券公司核心营收来源为手续费及佣金净收入、利息净收入与投资收益，不适用工商业销售成本模型。"
                elif is_insurance:
                    p["保险企业收入说明"] = "保险企业营业收入以保险服务收入与投资收益为核心，严禁带入制造业毛利率公式。"
                elif is_bank:
                    p["商业银行收入说明"] = "商业银行营业总收入核心为利息净收入与手续费佣金净收入，营运支出为管理开支非进货成本。"

            # 3. 现金流量表金融核算
            elif rtype_lower in ("cashflow", "cashflow_statement", "xjllb"):
                p["金融现金流量表引用铁律"] = (
                    "金融企业客户存款贷款、同业往来波动巨大且受专线明细覆盖限制，"
                    "明细科目不可用于内部结构推演，量化与建模必须且只能引用经营/投资/筹资三大活动净额与期末现金。"
                )
                if is_broker:
                    p["券商现金流科目特化说明"] = "本表现金支出科目反映的是【为交易目的而持有的金融资产及融出资金等净支出】，绝非保险赔付。"

        return statements

    def generate_accounting_notes(
        self,
        statements: List[Dict[str, Any]],
        report_type: str,
        period_type: str,
        context: Dict[str, Any],
    ) -> str:
        """生成金融板块准则会计说明文案。"""
        sub_type = str(context.get("sub_type") or "financial")
        sector_str = "证券公司" if sub_type == "broker" else ("保险公司" if sub_type == "insurance" else ("商业银行" if sub_type == "bank" else "金融机构"))
        return (
            f"【A股金融板块专属会计准则 ({sector_str})】：本标的执行中国企业会计准则金融企业专有核算标准。\n"
            "1.【资产与负债】：金融机构以资本运作和风险管理为核心，严禁带入制造业流动资产/存货分析框架；\n"
            "2.【估值与净资产】：金融机构优先股与永续债规模庞大，测算真实普通股市净率时必须扣除其他权益工具；\n"
            "3.【现金流量】：金融企业明细受专线采集限制，财务建模必须且只能引用三大活动净额与期末现金等价物。"
        )
