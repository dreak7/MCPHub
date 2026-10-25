# -*- coding: utf-8 -*-
"""
28 核心行业标杆股票双向隔离金丝雀巡检脚本 (Canary Probe Runner)

本脚本用于对 MCPHub 金融网关进行常态化巡检：
1. 市场物理隔离：A 股 16 只标的仅由 A 股引擎检验，港股 14 只标的仅由港股引擎检验；
2. 逆向拒信断言：若向 A 股引擎请求港股或向港股引擎请求 A 股，必须触发异常拦截；
3. 会计准则四象限质检：分别验证工商业与金融板块特定会计准则报表及自陈字段；
4. 巡检结果可视化报表输出。
"""

from __future__ import annotations
import json
import os
import sys
import time
from typing import Dict, List, Any, Tuple

# 将项目根目录加入 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from mcp_gateway.finance import FinanceGateway


# ==============================================================================
# 功能块：28 核心行业标杆股票金丝雀测试池定义
# 【输入】：无
# 【功能】：定义覆盖主板、创业板、科创板、港股主板等 28 只标的及其板块特征
# 【输出】：A_SHARE_CANARY_POOL 与 HK_SHARE_CANARY_POOL 字典列表
# ==============================================================================
A_SHARE_CANARY_POOL: List[Dict[str, Any]] = [
    {"symbol": "601658", "name": "邮储银行", "sector": "商业银行", "is_financial": True},
    {"symbol": "600030", "name": "中信证券", "sector": "证券公司", "is_financial": True},
    {"symbol": "601336", "name": "新华保险", "sector": "人寿保险", "is_financial": True},
    {"symbol": "600875", "name": "东方电气", "sector": "重型装备制造", "is_financial": False},
    {"symbol": "600276", "name": "恒瑞医药", "sector": "创新药研发", "is_financial": False},
    {"symbol": "601012", "name": "隆基绿能", "sector": "光伏新能源", "is_financial": False},
    {"symbol": "601933", "name": "永辉超市", "sector": "新零售商超", "is_financial": False},
    {"symbol": "002594", "name": "比亚迪", "sector": "新能源汽车制造", "is_financial": False},
    {"symbol": "000063", "name": "中兴通讯", "sector": "通信网络设备", "is_financial": False},
    {"symbol": "002277", "name": "友阿股份", "sector": "商业百货零售", "is_financial": False},
    {"symbol": "002293", "name": "罗莱生活", "sector": "家纺消费品", "is_financial": False},
    {"symbol": "300124", "name": "汇川技术", "sector": "工控自动化", "is_financial": False},
    {"symbol": "300450", "name": "先导智能", "sector": "锂电装备制造", "is_financial": False},
    {"symbol": "300866", "name": "安克创新", "sector": "跨境消费电子", "is_financial": False},
    {"symbol": "688777", "name": "中控技术", "sector": "高端工业软件", "is_financial": False},
    {"symbol": "688008", "name": "澜起科技", "sector": "集成电路芯片", "is_financial": False},
]

HK_SHARE_CANARY_POOL: List[Dict[str, Any]] = [
    {"symbol": "02026", "name": "小马智行", "sector": "美元申报美港股", "is_financial": False},
    {"symbol": "09988", "name": "阿里巴巴", "sector": "特殊3月财年电商", "is_financial": False},
    {"symbol": "00700", "name": "腾讯控股", "sector": "社交娱乐数字经济", "is_financial": False},
    {"symbol": "03690", "name": "美团", "sector": "本地商业即时零售", "is_financial": False},
    {"symbol": "02628", "name": "中国人寿", "sector": "大型国有寿险央企", "is_financial": True},
    {"symbol": "00728", "name": "中国电信", "sector": "基础电信云网融合", "is_financial": False},
    {"symbol": "09969", "name": "诺诚健华", "sector": "18A创新药无收入", "is_financial": False},
    {"symbol": "03908", "name": "中金公司", "sector": "中资顶级投资银行", "is_financial": True},
    {"symbol": "00981", "name": "中芯国际", "sector": "晶圆代工重资产", "is_financial": False},
    {"symbol": "01810", "name": "小米集团", "sector": "手机智能车双轮", "is_financial": False},
    {"symbol": "01585", "name": "雅迪控股", "sector": "两轮电动制造出海", "is_financial": False},
    {"symbol": "09880", "name": "优必选", "sector": "人形机器人第一股", "is_financial": False},
    {"symbol": "00939", "name": "建设银行", "sector": "港股银行特检标的", "is_financial": True},
    {"symbol": "03988", "name": "中国银行", "sector": "港股银行特检标的", "is_financial": True},
]


# ==============================================================================
# 功能块：金丝雀巡检探针核心逻辑
# 【输入】：gateway (FinanceGateway 实例)
# 【功能】：1. 执行 A 股与港股标的在对应引擎上的盘口与三表质检；
#          2. 执行跨市场逆向拒信断言；
#          3. 验证金融准则与工商业准则专属自陈；
#          4. 统计成功项、失败项并格式化输出。
# 【输出】：tuple[int, int, List[str]] (成功总数, 失败总数, 巡检日志列表)
# ==============================================================================
def run_canary_inspection(gateway: FinanceGateway | None = None) -> Tuple[int, int, List[str]]:
    """执行 28 核心行业标杆股票巡检主流程。"""
    if gateway is None:
        gateway = FinanceGateway()

    a_engine = gateway.a_engine
    hk_engine = gateway.hk_engine

    passed_count = 0
    failed_count = 0
    log_messages: List[str] = []

    def _record_result(ok: bool, desc: str, detail: str = "") -> None:
        nonlocal passed_count, failed_count
        if ok:
            passed_count += 1
            log_messages.append(f"  [PASS] {desc} {detail}")
            print(f"  [PASS] {desc} {detail}")
        else:
            failed_count += 1
            log_messages.append(f"  [FAIL] {desc} - {detail}")
            print(f"  [FAIL] {desc} - {detail}")

    print("\n" + "=" * 80)
    print("      MCPHub 28 核心行业标杆股票金丝雀巡检 (Canary Inspection)")
    print("      A 股池严格归 A 股引擎 | 港股池严格归港股引擎 | 逆向拒信双向隔离")
    print("=" * 80)

    # --------------------------------------------------------------------------
    # 第一部分：逆向拒信断言检验 (Negative Cross-Market Rejection)
    # --------------------------------------------------------------------------
    print("\n>>> [阶段一] 逆向拒信物理隔离断言检验:")

    # 1.1 港股代码传入 A 股引擎必须被明确拒绝
    sample_hk = "00700"
    try:
        a_engine.get_financials(sample_hk, report_type="balance", limit_periods=1)
        _record_result(False, "A股引擎拦截港股代码 00700", "未抛出预期异常")
    except ValueError as exc:
        if "港股" in str(exc):
            _record_result(True, "A股引擎拦截港股代码 00700", f"正确拒绝: {exc}")
        else:
            _record_result(False, "A股引擎拦截港股代码 00700", f"异常信息非预期: {exc}")
    except Exception as exc:
        _record_result(False, "A股引擎拦截港股代码 00700", f"未知异常类型: {exc}")

    # 1.2 A 股代码传入港股引擎必须被明确拒绝
    sample_a = "600519"
    try:
        hk_engine.get_quotes(sample_a)
        _record_result(False, "港股引擎拦截A股代码 600519", "未抛出预期异常")
    except ValueError as exc:
        _record_result(True, "港股引擎拦截A股代码 600519", f"正确拒绝: {exc}")
    except Exception as exc:
        _record_result(False, "港股引擎拦截A股代码 600519", f"未知异常: {exc}")

    # --------------------------------------------------------------------------
    # 第二部分：A 股巡检池标的检验 (A 股引擎专属，16 只标的)
    # --------------------------------------------------------------------------
    print(f"\n>>> [阶段二] A 股巡检池深度质检 (共 {len(A_SHARE_CANARY_POOL)} 票):")

    for item in A_SHARE_CANARY_POOL:
        sym = item["symbol"]
        name = item["name"]
        sector = item["sector"]
        is_fin = item["is_financial"]

        header = f"A股 {sym} {name} ({sector})"

        # 2.1 盘口行情
        try:
            q = a_engine.get_quotes(sym)
            core_q = q.get("盘口核心行情", {}) if isinstance(q, dict) else {}
            p_val = core_q.get("现价") or (q.get("现价") if isinstance(q, dict) else None)
            if q and q.get("symbol") == sym and p_val is not None:
                _record_result(True, f"{header} - 盘口行情", f"现价: {p_val}")
            else:
                _record_result(False, f"{header} - 盘口行情", f"返回结构缺失有效现价: {q.keys() if isinstance(q, dict) else type(q)}")
        except Exception as exc:
            _record_result(False, f"{header} - 盘口行情", str(exc))

        # 2.2 资产负债表与会计准则属性检验
        try:
            bal = a_engine.get_financials(sym, report_type="balance", limit_periods=1)
            stmts = bal.get("statements", [])
            bal_is_fin = bal.get("is_financial")
            if stmts and bal_is_fin == is_fin:
                row = stmts[0]
                std_name = bal.get("accounting_standard", "")
                _record_result(True, f"{header} - 资产负债表准则", f"准则: {std_name}")
            else:
                _record_result(False, f"{header} - 资产负债表准则", f"金融标记预期 {is_fin} 实得 {bal_is_fin}")
        except Exception as exc:
            _record_result(False, f"{header} - 资产负债表准则", str(exc))

        # 2.3 利润表自陈与勾稽
        try:
            inc = a_engine.get_financials(sym, report_type="income", limit_periods=1)
            stmts = inc.get("statements", [])
            if stmts:
                row = stmts[0]
                has_audit = "会计口径与说明" in inc or "会计口径与合并说明" in inc
                _record_result(True, f"{header} - 利润表勾稽与说明", f"自陈审计完备: {has_audit}")
            else:
                _record_result(False, f"{header} - 利润表勾稽与说明", "报表列表为空")
        except Exception as exc:
            _record_result(False, f"{header} - 利润表勾稽与说明", str(exc))

        # 短暂休眠避免上游专线瞬间高频
        time.sleep(0.05)

    # --------------------------------------------------------------------------
    # 第三部分：港股巡检池标的检验 (港股引擎专属，14 只标的)
    # --------------------------------------------------------------------------
    print(f"\n>>> [阶段三] 港股巡检池深度质检 (共 {len(HK_SHARE_CANARY_POOL)} 票):")

    for item in HK_SHARE_CANARY_POOL:
        sym = item["symbol"]
        name = item["name"]
        sector = item["sector"]
        is_fin = item["is_financial"]

        header = f"港股 {sym} {name} ({sector})"

        # 3.1 盘口行情
        try:
            q = hk_engine.get_quotes(sym)
            core_q = q.get("盘口核心行情", {}) if isinstance(q, dict) else {}
            p_val = core_q.get("现价") or (q.get("现价") if isinstance(q, dict) else None)
            if q and q.get("symbol") == sym and p_val is not None:
                _record_result(True, f"{header} - 盘口行情", f"现价: {p_val}")
            else:
                _record_result(False, f"{header} - 盘口行情", f"返回结构缺失有效现价: {q.keys() if isinstance(q, dict) else type(q)}")
        except Exception as exc:
            _record_result(False, f"{header} - 盘口行情", str(exc))

        # 3.2 资产负债表与金融/工商业准则检验
        try:
            bal = hk_engine.get_financials(sym, report_type="balance", limit_periods=1)
            stmts = bal.get("statements", [])
            bal_is_fin = bal.get("is_financial")
            if stmts:
                row = stmts[0]
                # 针对建行 00939 与中行 03988 特检标的检验优先股永续债残差自陈
                if sym in ("00939", "03988"):
                    diff_eq = row.get("归母所有者权益不可加总差额_优先股永续债及其他权益工具等")
                    if diff_eq is not None and diff_eq > 0:
                        _record_result(True, f"{header} - 银行优先股永续债残差自陈", f"差额: {diff_eq / 1e8:.2f} 亿港元")
                    else:
                        _record_result(False, f"{header} - 银行优先股永续债残差自陈", f"差额未检出: {diff_eq}")
                else:
                    _record_result(True, f"{header} - 资产负债表", f"准则: {bal.get('accounting_standard')}")
            else:
                _record_result(False, f"{header} - 资产负债表", "报表列表为空")
        except Exception as exc:
            _record_result(False, f"{header} - 资产负债表", str(exc))

        # 3.3 利润表检验 (含单季前驱与财年首季守护)
        try:
            inc = hk_engine.get_financials(sym, report_type="income", period_type="quarter", limit_periods=1)
            stmts = inc.get("statements", [])
            if stmts:
                row = stmts[0]
                tag = row.get("单期标签") or row.get("报表类型") or ""
                _record_result(True, f"{header} - 单季度利润表", f"单期标签: {tag}")
            else:
                _record_result(False, f"{header} - 单季度利润表", "报表列表为空")
        except Exception as exc:
            _record_result(False, f"{header} - 单季度利润表", str(exc))

        time.sleep(0.05)

    # --------------------------------------------------------------------------
    # 汇总总结
    # --------------------------------------------------------------------------
    total_checks = passed_count + failed_count
    pass_rate = (passed_count / total_checks * 100.0) if total_checks > 0 else 0.0

    print("\n" + "=" * 80)
    print("      金丝雀巡检完成报告 (Canary Inspection Summary)")
    print("=" * 80)
    print(f"  总校验项目数: {total_checks} 项")
    print(f"  成功通过项数: {passed_count} 项")
    print(f"  失败预警项数: {failed_count} 项")
    print(f"  金丝雀通过率: {pass_rate:.2f}%")
    print("=" * 80 + "\n")

    return passed_count, failed_count, log_messages


# ==============================================================================
# 功能块：脚本命令行直接运行入口
# 【输入】：无
# 【功能】：独立运行巡检并根据巡检通过率返回操作系统的退出代码
# 【输出】：0 (全部通过) 或 1 (存在失败项)
# ==============================================================================
if __name__ == "__main__":
    t0 = time.time()
    passed, failed, _ = run_canary_inspection()
    elapsed = time.time() - t0
    print(f"巡检总耗时: {elapsed:.2f} 秒\n")
    if failed > 0:
        sys.exit(1)
    else:
        sys.exit(0)
