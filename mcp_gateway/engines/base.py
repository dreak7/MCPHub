"""MCPHub Finance 市场引擎抽象基类规范。
定义所有细分市场（A股、港股及未来扩展市场）独立处理引擎的统一抽象契约。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


# =========================================================================
# 【功能块：市场引擎统一契约抽象基类 (BaseMarketEngine)】
# - 输入：标的代码及各业务参数
# - 功能：规范所有市场独立引擎必须实现的行情、K线、财报与F10四大核心接口，
#         确保路由层与门面层能够进行多态统一调度
# - 输出：各方法返回规范化的数据结构字典
# =========================================================================
class BaseMarketEngine(ABC):
    """金融市场处理引擎抽象基类"""

    @abstractmethod
    def get_quotes(self, symbol: str, setcode: str | None = None) -> dict[str, Any]:
        """获取实时盘口与估值数据"""
        raise NotImplementedError

    @abstractmethod
    def get_kline(
        self,
        symbol: str,
        period: str = "day",
        count: int = 30,
        adjust: str = "qfq",
        setcode: str | None = None,
        with_indicators: bool = True,
    ) -> dict[str, Any]:
        """获取历史多周期 K 线及技术指标"""
        raise NotImplementedError

    @abstractmethod
    def get_financials(
        self,
        symbol: str,
        report_type: str = "balance",
        period_type: str = "report",
        limit_periods: int = 4,
    ) -> dict[str, Any]:
        """获取三大财务报表与核心财务科目"""
        raise NotImplementedError

    @abstractmethod
    def get_f10(self, symbol: str, category: str = "summary") -> dict[str, Any]:
        """获取 F10 深度基本面资料"""
        raise NotImplementedError
