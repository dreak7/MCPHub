# -*- coding: utf-8 -*-
"""
核心基础设施：数据标准化清洗器模块 (Data Normalizer)

本模块为 Phase 1 静态清洗层收拢的核心实现，提供纯函数式的数据清洗与科目对齐服务：
1. 物理清洗内部未映射的裸码 (DATE_TYPE_CODE, rec_id 等)；
2. 规范化 ISO 日期解析转换；
3. 会计跨期科目双向别名平滑互通；
4. 归母股东权益与优先股/永续债残差代数核算。
"""

from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Set, Tuple

# 内部计算用裸码与临时字段黑名单集合（一律在交付前物理弹出清洗）
# 依据 D5 与 P2-2 契约，内部裸码 DATE_TYPE_CODE 在交付层物理清洗，对外统一使用标准中文字段【报告期标签】/【单期标签】
INTERNAL_CODE_BLACKLIST: Set[str] = {
    "DATE_TYPE_CODE",
    "_DATE_TYPE_CODE",
    "rec_id",
    "_原始N036",
    "_原始N037",
}


# ==============================================================================
# 功能块：物理清洗单行字典或列表中的内部裸码与临时计算字段
# 【输入】：data (待清洗的报表行字典或列表对象)
# 【功能】：递归遍历字典或列表，就地 (In-Place) 弹出黑名单内的内部代码（如 DATE_TYPE_CODE）
# 【输出】：原对象 data（已完成就地物理清洗）
# ==============================================================================
def strip_internal_codes(data: Any) -> Any:
    """递归物理清洗字典或列表内部未映射的上游裸码。"""
    if isinstance(data, dict):
        for k in list(data.keys()):
            if k in INTERNAL_CODE_BLACKLIST:
                data.pop(k, None)
            else:
                strip_internal_codes(data[k])
    elif isinstance(data, list):
        for item in data:
            strip_internal_codes(item)
    return data


# ==============================================================================
# 功能块：标准化纯数字日期字符串为标准 ISO 格式
# 【输入】：val (上游原始日期对象，可能为 8位数字字符串如 '20280328'、标准日期串或 None)
# 【功能】：清洗转换为标准 ISO 格式 'YYYY-MM-DD'；若无法解析或为空则返回 None
# 【输出】：Optional[str] 标准 ISO 格式日期字符串或 None
# ==============================================================================
def normalize_iso_date(val: Any) -> Optional[str]:
    """将日期标量转换为标准 ISO 格式 (YYYY-MM-DD)。"""
    if val is None:
        return None
    val_str = str(val).strip()
    if not val_str or val_str.lower() in ("none", "null", ""):
        return None

    # 处理 8 位纯数字日期 (如 20280328)
    if re.match(r"^\d{8}$", val_str):
        return f"{val_str[:4]}-{val_str[4:6]}-{val_str[6:]}"

    # 处理标准 10 位日期 (如 2028-03-28)
    if re.match(r"^\d{4}-\d{2}-\d{2}", val_str):
        return val_str[:10]

    return val_str


# ==============================================================================
# 功能块：对齐常见异名与双向镜像会计科目别名
# 【输入】：row (单期财务报表数据字典)
# 【功能】：1. 建立保留溢利 ↔ 保留溢利(累计亏损) 双向别名；
#          2. 建立其他综合收益 ↔ 其他综合性收益 双向别名；
#          3. 建立储备 ↔ 其他储备 双向别名；
#          4. 建立证券投资 ↔ 其他证券投资 双向别名；
#          5. 建立销售成本 ↔ 营运支出 双向别名；
#          6. 建立归母净利润 ↔ 归属于母公司股东的净利润 双向别名。
# 【输出】：就地完成双向别名补全的 row 字典
# ==============================================================================
def harmonize_accounting_aliases(row: Dict[str, Any]) -> Dict[str, Any]:
    """对齐常见异名与双向镜像会计科目别名，确保跨财期与跨准则调用平滑互通。"""
    if not isinstance(row, dict):
        return row

    # 1. 保留溢利
    if "保留溢利" in row and "保留溢利(累计亏损)" not in row:
        row["保留溢利(累计亏损)"] = row["保留溢利"]
    elif "保留溢利(累计亏损)" in row and "保留溢利" not in row:
        row["保留溢利"] = row["保留溢利(累计亏损)"]

    # 2. 其他综合收益
    if "其他综合收益" in row and "其他综合性收益" not in row:
        row["其他综合性收益"] = row["其他综合收益"]
    elif "其他综合性收益" in row and "其他综合收益" not in row:
        row["其他综合收益"] = row["其他综合性收益"]

    # 3. 储备
    if "储备" in row and "其他储备" not in row:
        row["其他储备"] = row["储备"]
    elif "其他储备" in row and "储备" not in row:
        row["储备"] = row["其他储备"]

    # 4. 证券投资
    if "证券投资" in row and "其他证券投资" not in row:
        row["其他证券投资"] = row["证券投资"]
    elif "其他证券投资" in row and "证券投资" not in row:
        row["证券投资"] = row["其他证券投资"]

    # 5. 销售成本与营运支出
    if "销售成本" in row and "营运支出" not in row:
        row["营运支出"] = row["销售成本"]
    elif "营运支出" in row and "销售成本" not in row:
        row["销售成本"] = row["营运支出"]

    # 6. 归母净利润
    if "归属于母公司股东的净利润" in row and "归母净利润" not in row:
        row["归母净利润"] = row["归属于母公司股东的净利润"]
    elif "归母净利润" in row and "归属于母公司股东的净利润" not in row:
        row["归属于母公司股东的净利润"] = row["归母净利润"]

    return row


# ==============================================================================
# 功能块：核算港股资产负债表归母股东权益与明细加总残差
# 【输入】：row (单期资产负债表字典)
# 【功能】：1. 提取股本、公积金、保留溢利、其他综合收益等已列报权益明细并加总；
#          2. 扣除借方抵减项【库存股】；
#          3. 测算归属于母公司股东权益与明细和的代数差额，判定是否需自陈优先股/永续债。
# 【输出】：Tuple[Optional[float], Optional[str]]：
#          - 元素 0: 代数差额浮点数 (若无残差或差额小于 1.0 则为 None)
#          - 元素 1: 标准自陈说明文案 (若无残差则为 None)
# ==============================================================================
def calculate_hk_parent_equity_residual(
    row: Dict[str, Any]
) -> Tuple[Optional[float], Optional[str]]:
    """核算港股归母股东权益与明细加总残差（对应优先股、永续债等专有权益工具）。"""
    if not isinstance(row, dict):
        return None, None

    eq_parent = (
        row.get("归属于母公司股东权益")
        if row.get("归属于母公司股东权益") is not None
        else (
            row.get("归属于母公司所有者权益")
            if row.get("归属于母公司所有者权益") is not None
            else row.get("股东权益")
        )
    )
    if eq_parent is None:
        return None, None

    try:
        detail_items: List[Tuple[str, float]] = []

        # 1. 股本 / 股本溢价
        sc_val = row.get("股本") if row.get("股本") is not None else row.get("实收资本(或股本)")
        if sc_val is not None:
            detail_items.append(("股本", float(sc_val)))
        prem_val = row.get("股本溢价")
        if prem_val is not None:
            detail_items.append(("股本溢价", float(prem_val)))

        # 2. 其他储备 / 资本公积
        res_val = row.get("其他储备") if row.get("其他储备") is not None else row.get("资本公积")
        if res_val is not None:
            detail_items.append(("其他储备", float(res_val)))

        # 3. 保留溢利(累计亏损)
        re_val = (
            row.get("保留溢利(累计亏损)")
            if row.get("保留溢利(累计亏损)") is not None
            else (row.get("保留溢利") if row.get("保留溢利") is not None else row.get("未分配利润"))
        )
        if re_val is not None:
            detail_items.append(("保留溢利(累计亏损)", float(re_val)))

        # 4. 公积金
        gjj_val = row.get("公积金") if row.get("公积金") is not None else row.get("法定公积金")
        if gjj_val is not None:
            detail_items.append(("公积金", float(gjj_val)))

        # 5. 其他综合收益
        oci_val = row.get("其他综合收益") if row.get("其他综合收益") is not None else row.get("其他综合性收益")
        if oci_val is not None:
            detail_items.append(("其他综合收益", float(oci_val)))

        # 6. 股东权益其他项目
        oth_val = row.get("股东权益其他项目")
        if oth_val is not None:
            detail_items.append(("股东权益其他项目", float(oth_val)))

        # 7. 外币报表折算差额 (独立储备科目，漏加会把差额误归因为优先股/永续债)
        wb_val = row.get("外币报表折算差额")
        if wb_val is not None:
            detail_items.append(("外币报表折算差额", float(wb_val)))

        # 8. 库存股 (已含负号直接累加，或若为正值在下文借方抵减)
        kcg_val = row.get("库存股")
        if kcg_val is not None:
            # 港股财报若库存股为正数，则在权益明细求和中扣除借方抵减
            detail_items.append(("库存股", -abs(float(kcg_val))))

        if not detail_items:
            return None, None

        sum_detail = round(sum(v for _, v in detail_items), 2)
        diff_eq = round(float(eq_parent) - sum_detail, 2)

        if abs(diff_eq) > 1.0:
            details_formula = " + ".join([f"{name} {val}" for name, val in detail_items])

            # 差额若与同表某个已下发科目逐分相等，点名该科目，不再一律扣「优先股/永续债」帽子
            matched_item = None
            for item_name, item_val in row.items():
                if not isinstance(item_val, (int, float)):
                    continue
                if item_name in ("归属于母公司股东权益", "归属于母公司所有者权益", "股东权益"):
                    continue
                if "差额" in item_name or "说明" in item_name:
                    continue
                if abs(float(item_val) - diff_eq) <= 0.01:
                    matched_item = item_name
                    break

            if matched_item:
                explanation = (
                    f"【港股权益科目可拆解性自陈】：已披露明细（{details_formula} = {sum_detail}）与归属于母公司股东权益({eq_parent})"
                    f"存在差额 {diff_eq}，该差额逐分等于同表已下发的【{matched_item}】科目；"
                    "该科目未纳入明细加总（避免与储备/其他综合收益重复计），并非优先股或永续债，"
                    "建模取数请以归属于母公司股东权益总额为准。"
                )
            else:
                explanation = (
                    f"【港股权益科目可拆解性自陈（已收敛 P1-1）】：上游港股数据源未单独拆分优先股、永续债等其他权益工具明细科目；"
                    f"已披露明细（{details_formula} = {sum_detail}）与归属于母公司股东权益({eq_parent})存在差额 {diff_eq}，"
                    "已自陈至【归母所有者权益不可加总差额_优先股永续债及其他权益工具等】。总权益恒等式完全闭合，建模取数请以归属于母公司股东权益总额为准。"
                )
            return diff_eq, explanation

        return None, None

    except (ValueError, TypeError):
        return None, None
