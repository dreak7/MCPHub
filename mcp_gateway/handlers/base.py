# -*- coding: utf-8 -*-
"""
四象限报表处理器基类模块 (Base Statement Handler)

定义 A 股与港股在工商业与金融业不同会计准则下的四象限处理器统一抽象接口。
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, List


# ==============================================================================
# 功能块：四象限报表处理器抽象基类
# 【输入】：标准参数（报表列表 statements、报表类型 report_type、周期类型 period_type、上下文元数据 context）
# 【功能】：规范工商业与金融业专有处理器的入参与出参契约，实现会计准则多态处理
# 【输出】：dict[str, Any] 处理并增强后的标准化报表结果字典
# ==============================================================================
class BaseStatementHandler(ABC):
    """四象限报表处理器抽象基类。"""

    def __init__(self, is_financial: bool, standard_name: str):
        self.is_financial = is_financial
        self.standard_name = standard_name

    @abstractmethod
    def process_statements(
        self,
        statements: List[Dict[str, Any]],
        report_type: str,
        period_type: str,
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """对各报告期财务报表列表执行准则特定的科目映射、别名平滑与代数自陈。"""
        pass

    @abstractmethod
    def generate_accounting_notes(
        self,
        statements: List[Dict[str, Any]],
        report_type: str,
        period_type: str,
        context: Dict[str, Any],
    ) -> str:
        """生成与本准则和板块严格匹配的会计口径与避坑指南说明文案。"""
        pass
