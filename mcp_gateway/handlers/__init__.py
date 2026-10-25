# -*- coding: utf-8 -*-
"""
四象限报表处理器模块包 (Statement Handlers Package)

根据上市地（A 股 vs 港股）与行业准则（一般工商业 vs 金融业）物理隔离为四大专有处理器：
1. ACASIndustrialHandler: A 股 CAS 一般工商业准则
2. ACASFinancialHandler: A 股 CAS 金融准则 (银行 / 证券 / 保险)
3. HKIFRSIndustrialHandler: 港股 IFRS / HKFRS 一般工商业准则
4. HKIFRSFinancialHandler: 港股 IFRS 国际金融准则
"""

from __future__ import annotations

# ==============================================================================
# 功能块：导出四象限报表处理器基类与具象实现
# 【输入】：无
# 【功能】：集中导出四大独立报表处理器与公共抽象基类接口，供 A 股与港股引擎解耦调用
# 【输出】：BaseStatementHandler, ACASIndustrialHandler, ACASFinancialHandler,
#          HKIFRSIndustrialHandler, HKIFRSFinancialHandler
# ==============================================================================
from .base import BaseStatementHandler
from .a_cas_industrial import ACASIndustrialHandler
from .a_cas_financial import ACASFinancialHandler
from .hk_ifrs_industrial import HKIFRSIndustrialHandler
from .hk_ifrs_financial import HKIFRSFinancialHandler

__all__ = [
    "BaseStatementHandler",
    "ACASIndustrialHandler",
    "ACASFinancialHandler",
    "HKIFRSIndustrialHandler",
    "HKIFRSFinancialHandler",
]
