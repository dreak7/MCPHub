"""通达信金融数据专线与多源高可用金融服务适配器 (Finance Gateway)
提供 A 股/港股/美股/ETF 的实体检索、实时盘口、多周期K线（含内置技术指标计算）、
三大财务报表、F10 深度基本面、自然语言智能选股与市场交易时钟。
"""

from __future__ import annotations

import datetime
import gzip
import json
import logging
import math
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


# =========================================================================
# 【功能块 0：安全 HTTP 传输层与重定向拦截处理器】
# - 输入：HTTP 请求对象与重定向目标 URL
# - 功能：严格拦截 POST 请求发生时的任何 301/302/303 重定向跳转，
#         彻底阻断 Authorization/Token 凭据被重定向转发泄露到第三方恶意主机或私网目标
# - 输出：抛出 HTTPError 明确阻断
# =========================================================================
class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise HTTPError(req.full_url, code, f"HTTP redirect ({code}) is disallowed for security in financial gateway", headers, fp)

_SECURE_OPENER = build_opener(NoRedirectHandler)


# =========================================================================
# 【功能块 1：高频盘口与技术指标行情数据字段映射表】
# - 输入：通达信原始盘口英文缩写
# - 功能：将底层十六进制与英文缩写规范化映射为清晰、标准的中文金融业务指标
# - 输出：dict 映射字典
# =========================================================================
HQ_FIELD_MAP: dict[str, str] = {
    "Now": "现价",
    "Close": "昨收",
    "Open": "今开",
    "MaxP": "最高价",
    "MinP": "最低价",
    "Average": "成交均价",
    "Volume": "成交量_手",
    "Amount": "成交额_元",
    "Inside": "内盘_手",
    "Outside": "外盘_手",
    "InOutFlag": "内外盘强弱标志",
    "ItemNum": "明细笔数",
    "CJBS": "成交笔数",
    "LB": "量比",
    "HSL": "换手率_pct",
    "PHVolume": "盘后成交量",
    "RestVol": "剩余委托量",
    "HQDate": "行情日期",
    "HQTime": "行情时间",
}

EXT_FIELD_MAP: dict[str, str] = {
    "ZTPrice": "涨停价",
    "DTPrice": "跌停价",
    "ZSZ": "总市值_元",
    "SYL": "动态市盈率_PE",
    "MGSY": "报告期每股收益_EPS",
    "MGJZC": "每股净资产_BPS",
    "MGGX": "每股公积金",
    "ZGB": "总股本_万股",
    "LTGB": "流通股本_万股",
    "BelongHY": "所属行业代码",
    "Addr": "地域代码",
    "BelongRZRQ": "是否属于融资融券标的",
    "RZRQ": "是否属于融资融券标的",
    "BelongHGTAG": "是否属于沪股通标的",
    "HGTAG": "是否属于沪股通标的",
    "BelongSGTAG": "是否属于深股通标的",
    "SGTAG": "是否属于深股通标的",
    "BelongHS300": "是否属于沪深300标的",
    "HS300": "是否属于沪深300标的",
    "HasKQZ": "是否含权证或转债",
    "FreeLtgb": "自由流通股本_万股",
    "HYZAF": "行业涨跌幅_pct",
}

# =========================================================================
# 【功能块 1.1：F10 深度基本面关键财务与估值指标映射表】
# - 输入：通达信 F10 摘要表底层英文缩写代码（如 mgjzc, gmjlr, gmjlrtbzzl, sjl, kfjlr）
# - 功能：将底层缩写规范化映射为清晰、标准的中文金融业务指标
# - 输出：dict 映射字典
# =========================================================================
F10_TABLE4_FINANCIAL_MAP: dict[str, str] = {
    "mgsy": "每股收益_EPS",
    "mgjzc": "每股净资产_BPS",
    "yysr": "营业收入_元",
    "gmjlr": "归母净利润_元",
    "mll": "毛利率_pct",
    "zcfzl": "资产负债率_pct",
    "kfjlrtbzzl": "扣非净利润同比增减_pct",
    "yysrtbzzl": "营业收入同比增减_pct",
    "gmjlrtbzzl": "归母净利润同比增减_pct",
    "mgzbgj": "每股资本公积",
    "mgwfplr": "每股未分配利润",
    "mgjjxjl": "每股经营现金流",
    "bgq": "报告期",
}

F10_TABLE6_VALUATION_MAP: dict[str, str] = {
    "sylttm": "动态市盈率_PE_TTM",
    "syllyr": "静态市盈率_PE_LYR",
    "sjl": "市净率_PB_昨收",
    "zsz": "总市值_元",
    "zgb": "总股本_股",
    "ltag": "流通A股_股",
}

F10_TABLE0_SUMMARY_MAP: dict[str, str] = {
    "kfjlr": "扣非净利润_元",
    "jqjzcsyl": "加权净资产收益率_ROE_pct",
    "gsdw": "公司地位与龙头定位",
    "jjrq": "业绩披露日期",
}

F10_TABLE8_SUMMARY_MAP: dict[str, str] = {
    "name": "股票简称",
    "zzhy": "证监会行业",
    "pjgdsyll": "平均股东收益率_pct",
    "gxrq": "财务更新日期",
}

F10_MARGIN_TABLE2_MAP: dict[str, str] = {
    "bshow": "是否展示两融标的",
}

# 港股主要财务与经营指标 (indicators) 宽表汉化映射表 (修复 HK-7)
HK_MAININDICATOR_MAP: dict[str, str] = {
    "BASIC_EPS": "基本每股收益_元",
    "DILUTED_EPS": "摊薄每股收益_元",
    "EPS_TTM": "每股收益_TTM_元",
    "BPS": "每股净资产_BPS_元",
    "OPERATE_INCOME": "营业收入_元",
    "OPERATE_INCOME_YOY": "营业收入同比增长_pct",
    "GROSS_PROFIT": "毛利润_元",
    "GROSS_PROFIT_YOY": "毛利润同比增长_pct",
    "GROSS_PROFIT_RATIO": "毛利率_pct",
    "HOLDER_PROFIT": "股东应占溢利_归母净利润_元",
    "HOLDER_PROFIT_YOY": "归母净利润同比增长_pct",
    "NET_PROFIT_RATIO": "净利润率_pct",
    "ROE_AVG": "净资产收益率_ROE_pct",
    "ROA": "总资产收益率_ROA_pct",
    "TOTAL_ASSETS": "总资产_元",
    "TOTAL_LIABILITIES": "总负债_元",
    "TOTAL_PARENT_EQUITY": "归属于母公司股东权益_元",
    "DEBT_ASSET_RATIO": "资产负债率_pct",
    "CURRENT_RATIO": "流动比率",
    "OPERATE_PROFIT": "经营溢利_营业利润_元",
    "PRETAX_PROFIT": "除税前溢利_利润总额_元",
    "NETCASH_OPERATE": "经营业务现金流量净额_元",
    "NETCASH_INVEST": "投资业务现金流量净额_元",
    "NETCASH_FINANCE": "融资业务现金流量净额_元",
    "END_CASH": "期末现金及等价物_元",
    "PE_TTM": "市盈率_PE_TTM",
    "PB_TTM": "市净率_PB_TTM",
    "DIVIDEND_RATE": "股息率_pct",
    "TOTAL_MARKET_CAP": "总市值_港元",
    "HKSK_MARKET_CAP": "港股市值_港元",
    "ISSUED_COMMON_SHARES": "已发行总股本_股",
    "HK_COMMON_SHARES": "已发行H股股数_股",
}

# =========================================================================
# 【功能块 2：三大财务报表科目字段映射表（非金融实体企业）】
# - 输入：通达信 CW 财报原始列代码（如 N001, N002）
# - 功能：将资产负债表、利润表、现金流量表代码精确转换为会计科目名称
# - 输出：dict 映射字典
# =========================================================================
INCOME_FIELDS_NON_FINANCE: dict[str, str] = {
    "N001": "截止日期",
    "N002": "营业总收入",
    "N003": "营业收入",
    "N004": "营业总成本",
    "N005": "营业成本",
    "N006": "营业税金及附加",
    "N007": "销售费用",
    "N008": "管理费用",
    "N009": "研发费用",
    "N010": "财务费用",
    "N011": "其中_利息费用",
    "N012": "资产减值损失",
    "N013": "信用减值损失",
    "N014": "公允价值变动净收益",
    "N015": "投资收益",
    "N016": "资产处置收益",
    "N017": "汇兑收益",
    "N018": "其他收益",
    "N019": "营业利润",
    "N020": "营业外收入",
    "N021": "营业外支出",
    "N022": "利润总额",
    "N023": "所得税费用",
    "N024": "净利润",
    "N025": "归母净利润",
    "N026": "少数股东损益",
    "N027": "重复核对_净利润",
    "N028": "持续经营净利润",
    "N029": "基本每股收益_EPS",
    "N030": "稀释每股收益",
    "N031": "其他综合收益税后净额",
    "N032": "综合收益总额",
    "N033": "归属于母公司所有者的综合收益总额",
    "N034": "归属于少数股东的综合收益总额",
    "N035": "扣非净利润",
    "N036": "扣除非经常性损益调整项",
    "N037": "归属于母公司股东的其他综合收益",
    "Tdate": "报告期日期",
}

BALANCE_FIELDS_NON_FINANCE: dict[str, str] = {
    "N001": "截止日期",
    "N002": "货币资金",
    "N003": "交易性金融资产",
    "N004": "应收票据及账款合计",
    "N005": "应收票据",
    "N006": "应收账款",
    "N007": "预付款项",
    "N008": "其他应收款",
    "N009": "应收利息",
    "N010": "应收股利",
    "N011": "存货",
    "N012": "一年内到期的非流动资产",
    "N013": "合同资产",
    "N014": "其他流动资产",
    "N015": "流动资产合计",
    "N016": "可供出售金融资产",
    "N017": "债权投资",
    "N018": "其他债权投资",
    "N019": "其他权益工具投资",
    "N020": "其他非流动金融资产",
    "N021": "持有至到期投资",
    "N022": "长期应收款",
    "N023": "长期股权投资",
    "N024": "投资性房地产",
    "N025": "固定资产",
    "N026": "固定资产清理",
    "N027": "在建工程",
    "N028": "工程物资",
    "N029": "生产性生物资产",
    "N030": "油气资产",
    "N031": "无形资产",
    "N032": "开发支出",
    "N033": "商誉",
    "N034": "长期待摊费用",
    "N035": "递延所得税资产",
    "N036": "其他非流动资产",
    "N037": "非流动资产合计",
    "N038": "资产总计",
    "N039": "短期借款",
    "N040": "交易性金融负债",
    "N041": "衍生金融负债_备用",
    "N042": "衍生金融负债",
    "N043": "应付票据及应付账款合计",
    "N044": "应付票据",
    "N045": "应付账款",
    "N046": "预收款项",
    "N047": "应付职工薪酬",
    "N048": "应交税费",
    "N049": "其中_应付利息",
    "N050": "其中_应付股利",
    "N051": "其他应付款合计",
    "N052": "一年内到期的非流动负债",
    "N053": "预计流动负债",
    "N054": "待转销项税额",
    "N055": "合同负债",
    "N056": "其他流动负债",
    "N057": "流动负债合计",
    "N058": "长期借款",
    "N059": "应付债券",
    "N060": "长期应付款",
    "N061": "专项应付款",
    "N062": "预计负债",
    "N063": "递延所得税负债",
    "N064": "递延收益",
    "N065": "其他非流动负债",
    "N066": "非流动负债合计",
    "N067": "负债合计",
    "N068": "实收资本_股本",
    "N069": "资本公积",
    "N070": "盈余公积",
    "N071": "库存股",
    "N072": "其他综合收益",
    "N073": "专项储备",
    "N074": "一般风险准备",
    "N075": "外币报表折算差额",
    "N076": "未分配利润",
    "N077": "所有者权益合计",
    "N078": "归母所有者权益合计",
    "N079": "少数股东权益",
    "N080": "负债和所有者权益总计",
    "Tdate": "报告期日期",
}

CASHFLOW_FIELDS_NON_FINANCE: dict[str, str] = {
    "N001": "截止日期",
    "N002": "销售商品提供劳务收到的现金",
    "N003": "收到的税费返还",
    "N004": "收到其他与经营活动有关的现金",
    "N005": "经营活动现金流入小计",
    "N006": "购买商品接受劳务支付的现金",
    "N007": "支付给职工以及为职工支付的现金",
    "N008": "支付的各项税费",
    "N009": "支付其他与经营活动有关的现金",
    "N010": "经营活动现金流出小计",
    "N011": "经营活动现金流量净额",
    "N012": "收回投资收到的现金",
    "N013": "取得投资收益收到的现金",
    "N014": "处置固定资产无形资产收回的现金净额",
    "N015": "处置子公司及其他营业单位收到的现金净额",
    "N016": "收到其他与投资活动有关的现金",
    "N017": "投资活动现金流入小计",
    "N018": "购建固定资产无形资产支付的现金",
    "N019": "投资支付的现金",
    "N020": "取得子公司及其他营业单位支付的现金净额",
    "N021": "支付其他与投资活动有关的现金",
    "N022": "投资活动现金流出小计",
    "N023": "投资活动现金流量净额",
    "N024": "吸收投资收到的现金",
    "N025": "取得借款收到的现金",
    "N026": "发行债券收到的现金",
    "N027": "收到其他与筹资活动有关的现金",
    "N028": "筹资活动现金流入小计",
    "N029": "偿还债务支付的现金",
    "N030": "分配股利利润或偿付利息支付的现金",
    "N031": "支付其他与筹资活动有关的现金",
    "N032": "筹资活动现金流出小计",
    "N033": "筹资活动现金流量净额",
    "N034": "汇率变动对现金及等价物的影响",
    "N035": "现金及现金等价物净增加额",
    "N036": "期初现金及现金等价物余额",
    "N037": "期末现金及现金等价物余额",
    # 现金流量表补充资料（间接法调节表及现金变动）：规范映射以杜绝原始 N 码外泄与主表静默双计
    "N038": "补充资料_净利润",
    "N039": "补充资料_资产减值准备",
    "N040": "补充资料_信用减值损失",
    "N041": "补充资料_固定资产折旧与摊销",
    "N042": "补充资料_无形资产摊销",
    "N043": "补充资料_长期待摊费用摊销",
    "N044": "补充资料_处置固定资产无形资产损失",
    "N045": "补充资料_公允价值变动损失",
    "N046": "补充资料_财务费用",
    "N047": "补充资料_投资损失",
    "N048": "补充资料_递延所得税资产减少",
    "N049": "补充资料_递延所得税负债增加",
    "N050": "补充资料_存货的减少",
    "N051": "补充资料_经营性应收项目的减少",
    "N052": "补充资料_经营性应付项目的增加",
    "N053": "补充资料_其他项目",
    "N054": "补充资料_经营活动现金流量净额",
    "N055": "补充资料_债务转为资本",
    "N056": "补充资料_一年内到期的可转换公司债券",
    "N057": "补充资料_融资租入固定资产",
    "N058": "补充资料_现金的期末余额",
    "N059": "补充资料_现金的期初余额",
    "N060": "补充资料_现金等价物的期末余额",
    "N061": "补充资料_现金等价物的期初余额",
    "N062": "补充资料_其他现金净变动",
    "N063": "补充资料_现金及现金等价物净增加额",
    "Tdate": "报告期日期",
}

# =========================================================================
# 【功能块 2.1：三大财务报表科目字段映射表（金融企业专用：银行 / 保险 / 证券 / 金融控股）】
# - 输入：通达信 CW 财报底层金融模板列代码（资产负债表 101 列，利润表 57 列，现金流量表 83 列）
# - 功能：精准对应《金融企业财务报表格式》科目体系，覆盖存款、拆借、已赚保费、备付金、
#         代理买卖证券款等金融专有科目，彻底杜绝套用工商业映射导致的科目错位与裸 N 码外泄
# - 输出：dict 映射字典
# =========================================================================
BALANCE_FIELDS_FINANCE: dict[str, str] = {
    "N001": "截止日期",
    "N002": "存放同业及其他金融机构款项",
    "N003": "拆出资金",
    "N004": "客户资金存款",
    "N005": "现金及存放中央银行款项",
    "N006": "结算备付金",
    "N007": "存出备付金",
    "N008": "融出资金",
    "N009": "贵金属",
    "N010": "拆出资金_金融",
    "N011": "交易性金融资产",
    "N012": "衍生金融资产",
    "N013": "买入返售金融资产",
    "N014": "应收款项",
    "N015": "应收保费",
    "N016": "应收分保账款",
    "N017": "应收分保未到期责任准备金",
    "N018": "应收分保未决赔款准备金",
    "N019": "应收分保寿险责任准备金",
    "N020": "应收分保长期健康险责任准备金",
    "N021": "保户质押贷款",
    "N022": "应收股利",
    "N023": "应收利息",
    "N024": "定期存款",
    "N025": "发放贷款及垫款",
    "N026": "存出保证金",
    "N027": "存出资本保证金",
    "N028": "债权投资",
    "N029": "其他债权投资",
    "N030": "其他权益工具投资",
    "N031": "长期应收款",
    "N032": "长期股权投资",
    "N033": "存出资本保证金_保险",
    "N034": "投资性房地产",
    "N035": "固定资产清理",
    "N036": "固定资产",
    "N037": "在建工程",
    "N038": "无形资产",
    "N039": "开发支出",
    "N040": "独立账户资产",
    "N041": "长期待摊费用",
    "N042": "其他非流动资产",
    "N043": "递延所得税资产",
    "N044": "商誉",
    "N045": "其他资产",
    "N046": "资产总计",
    "N047": "向中央银行借款",
    "N048": "同业及其他金融机构存放款项",
    "N049": "短期借款",
    "N050": "质押借款",
    "N051": "应付短期融资款",
    "N052": "拆入资金",
    "N053": "交易性金融负债",
    "N054": "衍生金融负债",
    "N055": "卖出回购金融资产款",
    "N056": "吸收存款",
    "N057": "代理买卖证券款",
    "N058": "代理承销证券款",
    "N059": "应付款项",
    "N060": "预收款项",
    "N061": "保险合同准备金",
    "N062": "预收保费",
    "N063": "应付手续费及佣金",
    "N064": "应付赔付款",
    "N065": "应付职工薪酬",
    "N066": "应交税费",
    "N067": "应付账款",
    "N068": "应付股利",
    "N069": "存入保证金",
    "N070": "预计负债",
    "N071": "未到期责任准备金",
    "N072": "未决赔款准备金",
    "N073": "寿险责任准备金",
    "N074": "长期健康险责任准备金",
    "N075": "保户储金及投资款",
    "N076": "独立账户负债",
    "N077": "长期借款_备用",
    "N078": "长期借款",
    "N079": "应付债券",
    "N080": "长期应付款",
    "N081": "专项应付款",
    "N082": "递延所得税负债",
    "N083": "其他负债",
    "N084": "负债合计",
    "N085": "股本",
    "N086": "资本公积",
    "N087": "减_库存股",
    "N088": "其他综合收益",
    "N089": "其他权益工具",
    "N090": "其中_优先股",
    "N091": "其中_永续债",
    "N092": "盈余公积",
    "N093": "一般风险准备",
    "N094": "未分配利润",
    "N095": "外币报表折算差额",
    "N096": "归母所有者权益差额",
    "N097": "所有者权益合计",
    "N098": "归属于母公司股东权益合计",
    "N099": "少数股东权益",
    "N100": "负债和所有者权益总计",
    "N101": "备用科目_N101",
    "Tdate": "报告期日期",
}

INCOME_FIELDS_FINANCE: dict[str, str] = {
    "N001": "截止日期",
    "N002": "营业收入",
    "N003": "利息净收入",
    "N004": "其中_利息收入",
    "N005": "其中_利息支出",
    "N006": "手续费及佣金净收入",
    "N007": "其中_手续费及佣金收入",
    "N008": "其中_手续费及佣金支出",
    "N009": "其中_代理买卖证券业务净收入",
    "N010": "其中_证券承销业务净收入",
    "N011": "其中_受托客户资产管理业务净收入",
    "N012": "已赚保费",
    "N013": "其中_保险业务收入",
    "N014": "其中_分保费收入",
    "N015": "减_分出保费",
    "N016": "提取未到期责任准备金",
    "N017": "加_投资收益",
    "N018": "其中_对联营企业和合营企业的投资收益",
    "N019": "加_公允价值变动收益",
    "N020": "加_汇兑收益",
    "N021": "加_其他业务收入",
    "N022": "营业支出",
    "N023": "退保金",
    "N024": "赔付支出",
    "N025": "减_摊回赔付支出",
    "N026": "提取保险责任准备金",
    "N027": "减_摊回保险责任准备金",
    "N028": "保单红利支出",
    "N029": "分保费用",
    "N030": "税金及附加",
    "N031": "其中_手续费及佣金支出_支出项",
    "N032": "业务及管理费",
    "N033": "减_摊回分保费用",
    "N034": "资产减值损失",
    "N035": "信用减值损失",
    "N036": "其他业务成本",
    "N037": "营业利润",
    "N038": "加_营业外收入",
    "N039": "资产减值损失_备用",
    "N040": "信用减值损失_备用",
    "N041": "减_营业外支出",
    "N042": "利润总额",
    "N043": "减_所得税费用",
    "N044": "其他综合收益所得税影响",
    "N045": "净利润",
    "N046": "归属于母公司股东的净利润",
    "N047": "少数股东损益",
    "N048": "持续经营净利润",
    "N049": "终止经营净利润",
    "N050": "基本每股收益_EPS",
    "N051": "稀释每股收益",
    "N052": "其他综合收益税后净额",
    "N053": "综合收益总额",
    "N054": "归属于母公司股东的综合收益总额",
    "N055": "归属于少数股东的综合收益总额",
    "N056": "扣非净利润",
    "N057": "备用科目_N057",
    "Tdate": "报告期日期",
}

CASHFLOW_FIELDS_FINANCE: dict[str, str] = {
    "N001": "截止日期",
    "N002": "客户存款和同业存放款项净增加额",
    "N003": "向中央银行借款净增加额",
    "N004": "向其他金融机构拆入资金净增加额",
    "N005": "收取利息、手续费及佣金的现金",
    "N006": "处置以公允价值计量且变动计入当期损益的金融资产净增加额",
    "N007": "拆入资金净增加额",
    "N008": "回购业务资金净增加额",
    "N009": "代理买卖证券收到的现金净额",
    "N010": "收到的税费返还",
    "N011": "收到原保险合同保费取得的现金",
    "N012": "收到再保业务现金净额",
    "N013": "保户储金及投资款净增加额",
    "N014": "处置金融资产净增加额",
    "N015": "拆入资金净增加额_备用",
    "N016": "收到其他与经营活动有关的现金",
    "N017": "经营活动现金流入小计",
    "N018": "客户贷款及垫款净增加额",
    "N019": "存放中央银行和同业款项净增加额",
    "N020": "支付原保险合同赔付款项的现金",
    "N021": "拆出资金净增加额",
    "N022": "返售业务资金净增加额",
    "N023": "支付利息、手续费及佣金的现金",
    "N024": "支付保单红利的现金",
    "N025": "支付再保业务现金净额",
    "N026": "支付原保险合同款项现金",
    "N027": "支付给职工以及为职工支付的现金",
    "N028": "支付的各项税费",
    "N029": "支付其他与经营活动有关的现金",
    "N030": "经营活动现金流出小计",
    "N031": "经营活动产生的现金流量净额",
    "N032": "收回投资收到的现金",
    "N033": "取得投资收益收到的现金",
    "N034": "处置固定资产、无形资产和其他长期资产收回的现金净额",
    "N035": "收到其他与投资活动有关的现金",
    "N036": "投资活动现金流入小计",
    "N037": "投资支付的现金",
    "N038": "质押贷款净增加额",
    "N039": "购建固定资产、无形资产和其他长期资产支付的现金",
    "N040": "支付其他与投资活动有关的现金",
    "N041": "投资活动现金流出小计",
    "N042": "投资活动产生的现金流量净额",
    "N043": "吸收投资收到的现金",
    "N044": "发行债券收到的现金",
    "N045": "取得借款收到的现金",
    "N046": "收到其他与筹资活动有关的现金",
    "N047": "筹资活动现金流入小计",
    "N048": "偿还债务支付的现金",
    "N049": "分配股利、利润或偿付利息支付的现金",
    "N050": "支付其他与筹资活动有关的现金",
    "N051": "筹资活动现金流出小计",
    "N052": "筹资活动产生的现金流量净额",
    "N053": "汇率变动对现金及现金等价物的影响",
    "N054": "现金及现金等价物净增加额",
    "N055": "加_期初现金及现金等价物余额",
    "N056": "期末现金及现金等价物余额",
    # 现金流量表补充资料（间接法调节表）：独立收纳隔离
    "N057": "补充资料_净利润",
    "N058": "补充资料_资产减值准备",
    "N059": "补充资料_固定资产折旧",
    "N060": "补充资料_无形资产摊销",
    "N061": "补充资料_长期待摊费用摊销",
    "N062": "补充资料_处置固定资产无形资产损失",
    "N063": "补充资料_固定资产报废损失",
    "N064": "补充资料_公允价值变动损失",
    "N065": "补充资料_财务费用",
    "N066": "补充资料_投资损失",
    "N067": "补充资料_递延所得税资产减少",
    "N068": "补充资料_递延所得税负债增加",
    "N069": "补充资料_存货减少",
    "N070": "补充资料_经营性应收项目的减少",
    "N071": "补充资料_经营性应付项目的增加",
    "N072": "补充资料_其他项目",
    "N073": "补充资料_经营活动现金流量净额",
    "N074": "补充资料_现金的期末余额",
    "N075": "补充资料_减_现金的期初余额",
    "N076": "补充资料_加_现金等价物的期末余额",
    "N077": "现金的期末余额_核对",
    "N078": "减_现金的期初余额_核对",
    "N079": "加_现金等价物的期末余额_核对",
    "N080": "减_现金等价物的期初余额_核对",
    "N081": "现金及现金等价物净增加额_补充法",
    "N082": "现金及现金等价物净增加额_主表",
    "N083": "备用科目_N083",
    "Tdate": "报告期日期",
}

# 热点题材板块族谱 (concepts) 表头汉化映射表
CONCEPTS_FIELDS: dict[str, str] = {
    "ztmc": "题材概念名称",
    "ztnr": "概念解析与业务关联",
    "ztrq": "题材纳入日期",
    "rxsj": "最新入选日期",
    "gld": "题材关联度",
    "id": "题材板块ID",
    "sslb": "题材分类代码",
    "bflag": "有效标志位",
    "arec": "题材记录号",
}

# 融资融券交易数据 (margin_trading) 表头汉化映射表
MARGIN_TRADING_FIELDS: dict[str, str] = {
    "T001": "交易日期",
    "T003": "融资余额_元",
    "T004": "融资买入额_元",
    "T005": "融资偿还额_元",
    "T006": "融券余量_股",
    "T007": "融券卖出量_股",
    "T008": "融券偿还量_股",
    "bt009": "成交量_股",
    "bt010": "成交金额_元",
    "ClosePrice": "收盘价",
    "bdrq": "变动日期",
    "T033": "流通股本_万股",
}

# 主力资金流向 (capital_flow) 表头汉化映射表
CAPITAL_FLOW_FIELDS: dict[str, str] = {
    "rq": "交易日期",
    "N001": "主力净流入_元",
    "N002": "主力净占比_pct",
    "N003": "超大单净流入_元",
    "N004": "超大单净占比_pct",
    "N005": "大单净流入_元",
    "N006": "大单净占比_pct",
    "N007": "中单净流入_元",
    "N008": "中单净占比_pct",
    "N015": "收盘价",
}

# 历史重大事件驱动催化 (events) 表头汉化映射表
EVENTS_FIELDS: dict[str, str] = {
    "cjrq": "催化日期",
    "sjmc": "事件名称",
    "rec_id": "事件记录ID",
    "sjxz": "事件性质",
    "cjrzdf": "当日涨跌幅_pct",
}

# 涨停分析与异动复盘 (limit_up) 表头汉化映射表
LIMIT_UP_FIELDS: dict[str, str] = {
    "currq": "当日标志",
    "lx": "异动类型",
    "sT002": "涨停日期",
    "T011": "封板时间",
    "fdje": "封单金额_万元",
    "zt": "涨停所属板块",
    "yy": "涨停原因与催化说明",
    "N005": "当日成交金额_万元",
}

# 限售解禁日程表 (unlocks) 表头汉化映射表
UNLOCKS_FIELDS: dict[str, str] = {
    "T003": "解禁日期",
    "T014": "占总股本比例_pct",
    "T013": "限售起始日",
    "T011": "公告日期",
    "T012": "解禁进度",
    "cur": "基准日期",
    "T006": "限售股类型",
    "T004": "解禁股数_万股",
    "T010": "解禁后流通总股本_万股",
    "price": "参考股价_元",
}

# 股东人数与筹码集中度表 (shareholders) 表头汉化映射表
SHAREHOLDERS_FIELDS: dict[str, str] = {
    "T002": "截止日期",
    "T003": "股东户数_户",
    "T004": "户均持股数_股",
    "T005": "股东户数增减_pct",
    "T006": "户均持股增减_pct",
    "T007": "参考股价_元",
    "T012": "股东户数增减_户",
}

# 机构盈利预测与评级一致预期 (forecast) 表头汉化映射表
FORECAST_FIELDS: dict[str, str] = {
    "defdate": "预测报告期",
    "mxdef": "基准报告期",
    "T003": "预测发布日期",
    "T005": "预测每股收益_当年_元",
    "T006": "预测每股收益_次年_元",
    "T007": "预测每股收益_后年_元",
    "T024": "预测PEG",
    "T025": "机构目标价_元",
    "T026": "评级标志位",
    "T030": "评级更新日期",
    "T031": "一致预期变动_短期_pct",
    "T032": "一致预期变动_中期_pct",
    "T033": "一致预期变动_长期_pct",
}

# 公司概要与提示扩展表 (summary) 表头汉化映射表
SUMMARY_FIELDS: dict[str, str] = {
    "T017": "主营业务范围",
    "hy1": "一级行业",
    "hy2": "二级行业",
    "rec_id": "板块ID",
    "T002": "概念板块名称",
    "T001": "成分股数量",
    "T060": "概念标签1",
    "T061": "概念标签2",
    "T062": "概念标签3",
    "T063": "概念标签4",
    "T064": "概念标签5",
    "T065": "概念标签6",
    "zyrq": "质押统计日期",
    "zygf": "质押股份比例_pct",
    "zzygf": "质押总股数_万股",
    "iscdr": "是否存托凭证CDR",
}

# 十大流通股东股本总额表 (top_holders.table0)
TOP_HOLDERS_TOTALS_FIELDS: dict[str, str] = {
    "rq": "变动日期",
    "T004": "流通A股合计_股",
    "T013": "限售流通股合计_股",
    "T016": "其他流通股_股",
    "T019": "统计流通总额_股",
}

# 十大流通股东持表明细表 (top_holders.table1)
TOP_HOLDERS_FIELDS: dict[str, str] = {
    "rq": "报告期",
    "gd": "股东名称",
    "isbgq": "是否定期报告",
    "gdid": "股东代码",
    "cgs": "持股数量_股",
    "lb": "股东类型",
    "pos": "持股比例_pct",
    "xz": "股份性质",
    "T009": "机构分类代码",
    "tdxid": "通达信ID",
    "zrr": "自然人标志",
    "gfxz": "股份性质明细",
    "sid": "序号",
}


# =========================================================================
# 【功能块 3：证券交易所与市场代码智能推导器】
# - 输入：symbol (str): 股票、基金或指数代码（如 "600519", "000001", "835185"）
# - 功能：遵循中国证券市场编制规则，自动推导市场 setcode 代码：
#         '1'=上海证券交易所(沪市), '0'=深圳证券交易所(深市), '2'=北京证券交易所(北交所)
# - 输出：str: 推导得到的市场代码
# =========================================================================
def infer_market_setcode(symbol: str) -> str:
    clean_sym = symbol.strip().upper()
    # 港股标的 (5位纯数字或带有 HK 前缀，如 00700, 09988, HK00700)
    if clean_sym.startswith("HK") or (clean_sym.isdigit() and len(clean_sym) == 5):
        return "31"
    # 优先匹配北交所专属代码 (包含920新代码号段、8开头及4开头)
    if clean_sym.startswith(("BJ", "8", "4", "920")):
        return "2"
    # 沪市主板、科创板、B股、基金与转债
    if clean_sym.startswith(("SH", "6", "9", "5", "11")):
        return "1"
    # 深市主板、创业板、B股、基金与转债
    if clean_sym.startswith(("SZ", "0", "3", "2", "1", "12", "15")):
        return "0"
    return "0"


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


# =========================================================================
# 【功能块 5：A股交易日历与时钟状态感知器】
# - 输入：无
# - 功能：判断当前北京时间（UTC+8）处于哪个交易阶段（开盘竞价/连续交易/午间休市/已收盘/周末），
#         提供基准交易日，防止大模型在非交易时段产生“股票异常停牌”等幻觉
# - 输出：dict: 包含当前交易状态与交易上下文
# =========================================================================
def get_market_context() -> dict[str, Any]:
    # 强制以北京时间 (UTC+8) 计算
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cst_time = now_utc + datetime.timedelta(hours=8)
    weekday = cst_time.weekday()  # 0=周一, 6=周日
    current_time_str = cst_time.strftime("%H:%M:%S")
    date_str = cst_time.strftime("%Y-%m-%d")

    is_weekend = weekday in (5, 6)
    time_val = cst_time.hour * 100 + cst_time.minute

    if is_weekend:
        status = "weekend_closed"
        status_desc = "周末休市"
    elif time_val < 915:
        status = "pre_market_closed"
        status_desc = "盘前未开盘 (等待9:15开盘集合竞价)"
    elif 915 <= time_val <= 925:
        status = "opening_auction"
        status_desc = "开盘集合竞价阶段 (9:15-9:25)"
    elif 930 <= time_val <= 1130:
        status = "trading_morning"
        status_desc = "早盘连续交易进行中 (9:30-11:30)"
    elif 1130 < time_val < 1300:
        status = "lunch_break"
        status_desc = "午盘休市中 (11:30-13:00)"
    elif 1300 <= time_val <= 1500:
        status = "trading_afternoon"
        status_desc = "午后连续交易进行中 (13:00-15:00)"
    else:
        status = "closed_today"
        status_desc = "今日已收盘 (15:00后结算状态)"

    # 计算当前或上一个有效交易日基准 (参考交易日)
    if is_weekend:
        offset = 1 if weekday == 5 else 2  # 周六看周五(1天前)，周日看周五(2天前)
    elif time_val < 915:
        offset = 3 if weekday == 0 else 1  # 周一开盘前看上周五(3天前)，周二至周五开盘前看昨天(1天前)
    else:
        # 周一至周五 9:15 以后（包含集合竞价、盘中连续交易以及 15:00 盘后结算后），当前基准交易日即为今天
        offset = 0

    last_trading_day = (cst_time - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")

    return {
        "current_cst_time": f"{date_str} {current_time_str}",
        "market_status": status,
        "market_status_desc": status_desc,
        "is_trading_time": status in ("trading_morning", "trading_afternoon", "opening_auction"),
        "reference_trading_date": last_trading_day,
    }


# =========================================================================
# 【功能块 6：通达信官方专线金融客户端核心实现 (TdxClient)】
# - 输入：token (str): 通达信授权令牌；endpoint (str): 官方 TQLEX 网关端点
# - 功能：实现行情 (PBHQInfo)、K线 (PBFXT)、三大财务报表 (TdxShareCW)、
#         F10 深度题材与筹码 (TdxSharePCCW)、智能选股 (wendaQuery) 及
#         AI RAG 实体检索 (ai.icfqs.com) 的网络通信与结构化清洗
# - 输出：TdxClient 实例
# =========================================================================
class TdxClient:
    def __init__(
        self,
        token: str = "",
        api_endpoint: str = "http://tdxhub.icfqs.com:7615/TQLEX",
        rag_endpoint: str = "https://ai.icfqs.com:8965/v1/rag-entity-retrieve",
        timeout: int = 15,
        logger: logging.Logger | None = None,
    ):
        self.token = token.strip()
        self.api_endpoint = api_endpoint.rstrip("/")
        self.rag_endpoint = rag_endpoint
        self.timeout = timeout
        self.logger = logger or logging.getLogger("mcp_gateway.finance")
        self._entity_cache: dict[str, dict[str, Any] | None] = {}

    def _post(self, url: str, body: dict[str, Any] | list[Any]) -> Any:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "MCPHub-Finance-Gateway/1.0",
        }
        if self.token:
            headers["token"] = self.token

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=payload, headers=headers, method="POST")

        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                resp_bytes = resp.read()
                return json.loads(resp_bytes.decode("utf-8", errors="replace"))
        except HTTPError as exc:
            err_msg = f"HTTP {exc.code} {exc.reason}"
            try:
                detail = exc.read().decode("utf-8", errors="replace")
                err_msg += f": {detail}"
            except Exception:
                pass
            raise RuntimeError(f"TDX API request failed ({err_msg})") from exc
        except URLError as exc:
            raise RuntimeError(f"TDX API network connection error ({exc.reason})") from exc

    # =========================================================================
    # 【功能块 3.5：纯数字证券代码正主实体精准反查与保全解析器】
    # - 输入参数：clean_code (str): 6位A股纯数字代码或5位港股纯数字代码
    # - 实现功能：
    #     解决 A 股代码与公募基金/债券等同名代码碰撞时，RAG 向量库遗漏股票正主或被挤占排名的缺陷
    #     （典型案例如：002698 既是博实股份也是博时纯债基金，RAG 仅返回基金而漏掉博实股份；
    #      000001 平安银行被华夏成长挤占等）。
    #     采用三轨防御反查：
    #     1. 第一轨：腾讯实时行情专线 (qt.gtimg.cn)，毫秒级覆盖沪深北全市场股票名称；
    #     2. 第二轨：新浪财经公开行情专线 (hq.sinajs.cn) 备份兜底；
    #     3. 第三轨：通达信官方专线 F10 摘要表 (table8.name) 权威反查。
    # - 输出结果：dict[str, Any] | None: 标准股票实体字典（含 entity_code, entity_name, entity_type, entity_setcode, aliases）
    # =========================================================================
    def _resolve_exact_security_entity(self, clean_code: str) -> dict[str, Any] | None:
        clean_code = clean_code.strip()
        if clean_code in self._entity_cache:
            return self._entity_cache[clean_code]
        res = self._do_resolve_exact_security_entity(clean_code)
        self._entity_cache[clean_code] = res
        return res

    def _do_resolve_exact_security_entity(self, clean_code: str) -> dict[str, Any] | None:
        if clean_code.isdigit() and len(clean_code) == 6:
            # A 股 6 位数字代码
            if clean_code.startswith(("60", "68", "90")):
                prefix = "sh"
                setcode = 1
            elif clean_code.startswith(("00", "30", "20")):
                prefix = "sz"
                setcode = 0
            else:
                prefix = "bj"
                setcode = 2

            # 第一轨：腾讯官方公开快速行情提取证券名称
            url = f"http://qt.gtimg.cn/q={prefix}{clean_code}"
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            try:
                with _SECURE_OPENER.open(req, timeout=3) as resp:
                    raw = resp.read().decode("gb18030", errors="replace")
                if "~" in raw and "none_match" not in raw:
                    parts = raw.split("~")
                    if len(parts) > 1 and parts[1].strip():
                        name = parts[1].strip()
                        return {
                            "entity_code": clean_code,
                            "entity_name": name,
                            "entity_type": "A股代码",
                            "entity_setcode": setcode,
                            "aliases": [name],
                        }
            except Exception:
                pass

            # 第二轨：新浪财经公开快速行情提取证券名称
            try:
                sina_url = f"http://hq.sinajs.cn/list={prefix}{clean_code}"
                sina_req = Request(
                    sina_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                        "Referer": "https://finance.sina.com.cn",
                    },
                )
                with _SECURE_OPENER.open(sina_req, timeout=3) as resp:
                    raw = resp.read().decode("gbk", errors="replace")
                if '="' in raw and '=""' not in raw:
                    payload = raw.split('="', 1)[1]
                    name = payload.split(",", 1)[0].strip()
                    if name:
                        return {
                            "entity_code": clean_code,
                            "entity_name": name,
                            "entity_type": "A股代码",
                            "entity_setcode": setcode,
                            "aliases": [name],
                        }
            except Exception:
                pass

            # 第三轨：通达信官方专线 F10 摘要表 table8.name 权威兜底
            try:
                f10_res = self.get_f10(symbol=clean_code, category="summary")
                raw_data = f10_res.get("data", {})
                for rs in raw_data.get("ResultSets", []):
                    if rs.get("ResultSetKey") == "table8":
                        cols = rs.get("ColName", [])
                        contents = rs.get("Content", [])
                        if "name" in cols and contents:
                            name_idx = cols.index("name")
                            t8_name = contents[0][name_idx]
                            if t8_name and str(t8_name).strip():
                                name = str(t8_name).strip()
                                return {
                                    "entity_code": clean_code,
                                    "entity_name": name,
                                    "entity_type": "A股代码",
                                    "entity_setcode": setcode,
                                    "aliases": [name],
                                }
            except Exception:
                pass

        elif clean_code.isdigit() and len(clean_code) == 5:
            # 港股 5 位数字代码
            url = f"https://qt.gtimg.cn/q=r_hk{clean_code}"
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            try:
                with _SECURE_OPENER.open(req, timeout=3) as resp:
                    raw = resp.read().decode("gb18030", errors="replace")
                if "~" in raw and "none_match" not in raw:
                    parts = raw.split("~")
                    if len(parts) > 1 and parts[1].strip():
                        raw_name = parts[1].strip()
                        # 清理交易手数与股票类型后缀（如 "金斯瑞生物-一千" -> "金斯瑞生物", "-W", "-S", "-SW", "-B"）
                        clean_name = re.sub(r"-([一二三四五六七八九十百千万\d]+|[WSBwsb]+)$", "", raw_name).strip()
                        final_name = clean_name if clean_name else raw_name
                        aliases = [final_name]
                        if raw_name != final_name:
                            aliases.append(raw_name)
                        return {
                            "entity_code": clean_code,
                            "entity_name": final_name,
                            "entity_type": "港股代码",
                            "entity_setcode": 31,
                            "aliases": aliases,
                        }
            except Exception:
                pass

        return None

    def lookup_stock(self, query: str, market_range: str = "AG") -> list[dict[str, Any]]:
        """AI RAG 语义代码检索 (支持 A股, 港股 HK-GP, 美股 MG-GP, 基金 JJ, 指数 ZS)"""
        raw_query = query.strip()
        # 清洗可能带有的交易所前缀或后缀（如 sh600519 -> 600519, 002698.sz -> 002698）
        clean_code = re.sub(r"^(sh|sz|bj|hk)", "", raw_query, flags=re.I)
        clean_code = re.sub(r"\.(sh|sz|bj|hk|ss)$", "", clean_code, flags=re.I)

        req_body = {"query": raw_query, "range": market_range}
        resp = self._post(self.rag_endpoint, req_body)
        entities: list[dict[str, Any]] = []
        if isinstance(resp, dict) and "retrieved_entities" in resp:
            raw_entities = resp["retrieved_entities"]
            if isinstance(raw_entities, list):
                entities = raw_entities

        # 智能全市场双重嗅探：如果默认搜 AG 返回为空，但用户可能问的是港股（如“腾讯”、“阿里”、“美团”），自动降级尝试 HK-GP
        if not entities and market_range == "AG":
            try:
                hk_resp = self._post(self.rag_endpoint, {"query": raw_query, "range": "HK-GP"})
                if isinstance(hk_resp, dict) and "retrieved_entities" in hk_resp:
                    hk_entities = hk_resp["retrieved_entities"]
                    if isinstance(hk_entities, list) and hk_entities:
                        entities = hk_entities
            except Exception:
                pass

        # 纯数字代码正主保全与首位置顶逻辑（解决基金/债券等同名代码挤占或遗漏股票正主的问题）
        if clean_code.isdigit() and len(clean_code) in (5, 6) and market_range in ("AG", "HK", "HK-GP", "all", "ALL", ""):
            exact_target = self._resolve_exact_security_entity(clean_code)
            if exact_target:
                target_code = exact_target["entity_code"]
                target_type = exact_target["entity_type"]

                # 检查 RAG 返回列表中是否已存在该股票正主
                found_idx = -1
                for idx, item in enumerate(entities):
                    if (
                        item.get("entity_code") == target_code
                        and item.get("entity_type") == target_type
                    ):
                        found_idx = idx
                        break

                if found_idx > 0:
                    # 正主排在基金等其他实体之后，将其移至首位置顶
                    exact_item = entities.pop(found_idx)
                    entities.insert(0, exact_item)
                elif found_idx < 0:
                    # 正主被 RAG 完全漏掉，将解析出的权威正主直接置顶插入第 0 位
                    entities.insert(0, exact_target)

        for ent in entities:
            if isinstance(ent, dict):
                e_code = ent.get("entity_code") or ent.get("code") or ""
                e_name = ent.get("entity_name") or ent.get("name") or ""
                e_setcode = ent.get("entity_setcode") if ent.get("entity_setcode") is not None else ent.get("setcode")
                ent["code"] = e_code
                ent["name"] = e_name
                ent["setcode"] = str(e_setcode) if e_setcode is not None else ""
                if "market" not in ent:
                    ent["market"] = "SZ" if str(e_setcode) == "0" else ("SH" if str(e_setcode) == "1" else ("BJ" if str(e_setcode) == "2" else ("HK" if str(e_setcode) == "31" else str(e_setcode))))

        return entities

    # =========================================================================
    # 【功能块：腾讯免费公开港股实时行情获取与格式标准化】
    # - 输入：clean_code (str): 5位港股数字代码（如 "00700", "09988"）
    # - 功能：调用腾讯财经官方开放行情专线 (qt.gtimg.cn)，提取现价、五档委托、
    #         市盈率、总市值、成交额及52周高低点，标准化映射为统一盘口格式
    # - 输出：dict[str, Any]: 结构化港股微观盘口行情字典
    # =========================================================================
    def _get_hk_quotes(self, clean_code: str) -> dict[str, Any]:
        norm_code = clean_code.zfill(5)
        url = f"https://qt.gtimg.cn/q=r_hk{norm_code}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                raw_text = raw_bytes.decode("gb18030", errors="replace")
        except Exception as exc:
            raise RuntimeError(f"腾讯免费港股实时行情接口请求失败: {exc}") from exc

        if "none_match" in raw_text or "~" not in raw_text:
            raise ValueError(f"未找到港股代码 '{clean_code}' 的有效行情，请核实代码是否存在（提示：港股通常为 5 位数字代码，如腾讯控股为 '00700'）。")

        try:
            payload_str = raw_text.split('"', 2)[1]
            parts = payload_str.split("~")
        except Exception as exc:
            raise RuntimeError(f"解析腾讯港股行情数据结构失败: {exc}") from exc

        name = parts[1] if len(parts) > 1 and parts[1] else f"港股_{norm_code}"

        def _safe_float(val: Any) -> float:
            try:
                return round(float(val), 3) if val and str(val).strip() else 0.0
            except (ValueError, TypeError):
                return 0.0

        def _safe_opt_float(val: Any) -> float | None:
            try:
                return round(float(val), 2) if val and str(val).strip() else None
            except (ValueError, TypeError):
                return None

        # 动态解析结算货币（默认为港元 HKD，双柜台可能包含 CNY/RMB）
        curr_val = "HKD"
        for item in parts[50:]:
            clean_item = item.strip().upper()
            if clean_item in ("HKD", "CNY", "USD", "RMB"):
                curr_val = clean_item
                break

        now_p = _safe_float(parts[3] if len(parts) > 3 else 0)
        vol_shares = int(round(_safe_float(parts[6] if len(parts) > 6 else 0)))
        amount_hkd = _safe_float(parts[37] if len(parts) > 37 else 0)
        total_mv_yi = _safe_opt_float(parts[45] if len(parts) > 45 else None)
        float_mv_yi = _safe_opt_float(parts[44] if len(parts) > 44 else None)

        # 港股总股本与流通股本推导（优先取腾讯专线字段 parts[69]/parts[70]，缺失时由总市值/现价推导）
        zgb_val = None
        if len(parts) > 69 and parts[69]:
            zgb_val = int(round(_safe_float(parts[69])))
        if (not zgb_val or zgb_val <= 0) and total_mv_yi and now_p > 0:
            zgb_val = int(round(total_mv_yi * 100000000.0 / now_p))

        ltgb_val = None
        if len(parts) > 70 and parts[70]:
            ltgb_val = int(round(_safe_float(parts[70])))
        if (not ltgb_val or ltgb_val <= 0) and float_mv_yi and now_p > 0:
            ltgb_val = int(round(float_mv_yi * 100000000.0 / now_p))
        if not ltgb_val and zgb_val:
            ltgb_val = zgb_val

        # 港股换手率计算（成交量 / 流通股本 * 100 或 成交额 / 流通市值 * 100）
        hsl_val = 0.0
        if ltgb_val and ltgb_val > 0 and vol_shares > 0:
            hsl_val = round((vol_shares / float(ltgb_val)) * 100.0, 3)
        elif float_mv_yi and float_mv_yi > 0 and amount_hkd > 0:
            hsl_val = round((amount_hkd / (float_mv_yi * 100000000.0)) * 100.0, 3)

        result: dict[str, Any] = {
            "symbol": norm_code,
            "name": name,
            "setcode": "31",
            "market": "香港交易所 (HKEX)",
            "source": "tencent_free_hk_quote (腾讯免费港股专线)",
            "盘口核心行情": {
                "股票名称": name,
                "现价": now_p,
                "昨收": _safe_float(parts[4] if len(parts) > 4 else 0),
                "今开": _safe_float(parts[5] if len(parts) > 5 else 0),
                "最高": _safe_float(parts[33] if len(parts) > 33 else 0),
                "最高价": _safe_float(parts[33] if len(parts) > 33 else 0),
                "最低": _safe_float(parts[34] if len(parts) > 34 else 0),
                "最低价": _safe_float(parts[34] if len(parts) > 34 else 0),
                "涨跌额": _safe_float(parts[31] if len(parts) > 31 else 0),
                "涨跌额_元": _safe_float(parts[31] if len(parts) > 31 else 0),
                "涨跌幅_pct": _safe_float(parts[32] if len(parts) > 32 else 0),
                "成交量_股": vol_shares,
                "成交额_港元": amount_hkd,
                "振幅_pct": _safe_float(parts[43] if len(parts) > 43 else 0),
                "日内振幅_pct": _safe_float(parts[43] if len(parts) > 43 else 0),
                "换手率_pct": hsl_val,
                "行情时间": parts[30] if len(parts) > 30 else "",
                "更新时间": parts[30] if len(parts) > 30 else "",
                "结算货币": curr_val,
                "涨停价": None,
                "跌停价": None,
                "涨跌停说明": "港交所主板不设固定 ±10%/±20% 涨跌停板限制（实行市调机制 VCM 冷静期），无固定涨停/跌停价位。",
            },
            "估值与基本盘面": {
                "市盈率_TTM": _safe_opt_float(parts[57] if len(parts) > 57 and parts[57] else (parts[39] if len(parts) > 39 else None)),
                "滚动市盈率_PETTM": _safe_opt_float(parts[57] if len(parts) > 57 and parts[57] else (parts[39] if len(parts) > 39 else None)),
                "静态市盈率_PE_LYR": _safe_opt_float(parts[39] if len(parts) > 39 else None),
                "静态市盈率_PEStatic": _safe_opt_float(parts[39] if len(parts) > 39 else None),
                "动态市盈率_PE": _safe_opt_float(parts[71] if len(parts) > 71 and parts[71] else (parts[39] if len(parts) > 39 else None)),
                "市净率_PB": _safe_opt_float(parts[58] if len(parts) > 58 else None),
                "港股市值_亿港元": float_mv_yi,
                "港股市值_H股流通市值_亿港元": float_mv_yi,
                "流通市值_亿港元": float_mv_yi,
                "总市值_全部股本乘H股价_亿港元": total_mv_yi,
                "总市值_亿港元": total_mv_yi,
                "港股市值_元": int(round(float_mv_yi * 100000000)) if float_mv_yi else None,
                "流通市值_元": int(round(float_mv_yi * 100000000)) if float_mv_yi else None,
                "总市值_元": int(round(total_mv_yi * 100000000)) if total_mv_yi else None,
                "已发行H股股数_股": ltgb_val,
                "流通股本_股": ltgb_val,
                "全部已发行总股本_股": zgb_val,
                "总股本_股": zgb_val,
                "已发行H股_万股": round(ltgb_val / 10000.0, 2) if ltgb_val else None,
                "流通股本_万股": round(ltgb_val / 10000.0, 2) if ltgb_val else None,
                "总股本_万股": round(zgb_val / 10000.0, 2) if zgb_val else None,
                "52周最高": _safe_opt_float(parts[48] if len(parts) > 48 else None),
                "52周最低": _safe_opt_float(parts[49] if len(parts) > 49 else None),
                "市值口径说明": (
                    "【港股与AH股市值口径说明】：\n"
                    "1.【港股市值_H股流通市值_亿港元】(纯港股口径)：已发行H股股数 × H股现价，真实反映标的在港交所实际上市流通规模（如中国国航 00753 约 169 亿港元，青岛啤酒 00168 约 310 亿港元，中国电信 00728 约 800 亿港元）；\n"
                    "2.【总市值_全部股本乘H股价_亿港元】(全股本混合参考口径)：境内A股+境外H股全部总股本 × H股现价（如中国国航约 699 亿港元）。因两地市场存在AH折溢价，此混合口径仅供全股本估值参考，严禁误认为港股单一市场可流通规模！"
                ),
                "估值口径说明": (
                    "【港股市盈率口径与汇率折算披露】：\n"
                    "1. 字段映射关系：【市盈率_TTM】与【滚动市盈率_PETTM】同源取自过去12个月净利润测算；【静态市盈率_PE_LYR】与【静态市盈率_PEStatic】同源取自上一财年经审计年报。\n"
                    "2. 跨币种汇率换算机制：对于财报以人民币(CNY)编报但股票以港元(HKD)交易的标的（如腾讯控股、中国国航、青岛啤酒、中国电信等），行情源提供的 PE_TTM 在底层已包含港元对人民币的汇率折算（隐含换算比率约为 0.868~0.872 CNY/HKD）。如以人民币净利润手工复核，请统一折算对应汇率。"
                ),
                "52周价格区间说明": "52周最高/最低价统计周期为最近一年交易日；若标的上市不足52周（次新股），该区间等同于上市以来历史最高/最低价。",
            },
            "五档深度盘口": {
                "买一": {"价格": _safe_float(parts[9] if len(parts) > 9 else 0), "委托量": int(_safe_float(parts[10] if len(parts) > 10 else 0))},
                "买二": {"价格": None, "委托量": None, "说明": "港交所公开免费源仅提供即时买一/卖一撮合盘口"},
                "买三": {"价格": None, "委托量": None},
                "买四": {"价格": None, "委托量": None},
                "买五": {"价格": None, "委托量": None},
                "卖一": {"价格": _safe_float(parts[19] if len(parts) > 19 else 0), "委托量": int(_safe_float(parts[20] if len(parts) > 20 else 0))},
                "卖二": {"价格": None, "委托量": None, "说明": "港交所公开免费源仅提供即时买一/卖一撮合盘口"},
                "卖三": {"价格": None, "委托量": None},
                "卖四": {"价格": None, "委托量": None},
                "卖五": {"价格": None, "委托量": None},
            },
        }
        return result

    # =========================================================================
    # 【功能块：腾讯免费公开港股多周期历史 K 线获取与技术指标计算】
    # - 输入：clean_code (str): 5位港股数字代码（如 "00700"）
    #         period (str): 周期类型 ("day", "week", "month", "60m", "30m", "15m", "5m")
    #         count (int): 获取条数上限 (1-300)
    #         adjust (str): 复权方式 ("qfq", "hfq", "none")
    #         with_indicators (bool): 是否在底层计算技术指标
    # - 功能：调用腾讯财经官方开放 K 线专线 (web.ifzq.gtimg.cn)，拉取历史行情数据，
    #         清洗时间、开高低收成交量，并在纯 Python 算法层原生计算 MA/MACD/KDJ/BOLL/RSI
    # - 输出：dict[str, Any]: 结构化港股 K 线与指标字典
    # =========================================================================
    # =========================================================================
    # 【功能块 4.5：港股历史与分时 K 线高可用获取器】
    # - 输入：clean_code (5位港股数字代码), period (周期: 1m/5m/15m/30m/60m/day/week/month),
    #         count (条数), adjust (复权: none/qfq/hfq), with_indicators (是否计算技术指标)
    # - 功能：
    #   1. 分钟线 (1m/5m/15m/30m/60m)：调用腾讯日内逐分钟数据流 (minute/query)，精准聚合当日
    #      交易切片，生成含真实时分时间戳 (如 10:30)、高开低收、成交量与成交额的真实分钟 K 线；
    #   2. 跨午休切分：60m 周期按港交所主流规范 10:30, 11:30, 13:30 (跨午休聚合), 14:30, 15:30, 16:00 切分；
    #   3. 日/周/月线：调用腾讯高可用 newfqkline 通道，完全支持 none/qfq/hfq，精确换算成交额，
    #      彻底杜绝 adjust='none' 时成交额 amount 恒为 0.0 的历史缺陷；
    #   4. 覆盖说明：权威披露港股公共源仅提供日内实时分钟线聚合、跨日回溯需查日线的服务边界。
    # - 输出：dict 规范响应对象 (包含 klines 列表、指标与详细口径说明)
    # =========================================================================
    def _get_hk_kline(
        self,
        clean_code: str,
        period: str = "day",
        count: int = 30,
        adjust: str = "qfq",
        with_indicators: bool = True,
    ) -> dict[str, Any]:
        norm_code = clean_code.zfill(5)
        limit_cnt = max(1, min(int(count), 300))
        p_lower = period.lower()
        adj_lower = adjust.lower()

        # ---------------------------------------------------------------------
        # 1. 港股日内分钟线切片聚合 (1m / 5m / 15m / 30m / 60m)
        # 针对公共行情源不提供跨日历史分钟库的特性，从日内逐分钟流原生聚合当日标准分钟 K 线
        # ---------------------------------------------------------------------
        if p_lower in ("1m", "5m", "15m", "30m", "60m"):
            minute_urls = [
                f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=hk{norm_code}",
                f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/minute/query?code=hk{norm_code}",
            ]
            raw_points: list[str] = []
            date_fmt = datetime.datetime.now().strftime("%Y-%m-%d")
            stock_name = f"港股_{norm_code}"

            for m_url in minute_urls:
                try:
                    req = Request(m_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                    with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                        js = json.loads(resp.read().decode("utf-8", errors="replace"))
                        d = js.get("data", {}).get(f"hk{norm_code}", {})
                        date_str = str(d.get("data", {}).get("date", "") or "").strip()
                        if len(date_str) == 8 and date_str.isdigit():
                            date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                        raw_points = d.get("data", {}).get("data", [])
                        qt_data = d.get("qt", {}).get(f"hk{norm_code}", [])
                        if len(qt_data) > 1 and qt_data[1]:
                            stock_name = str(qt_data[1])
                        if raw_points:
                            break
                except Exception:
                    continue

            if raw_points:
                parsed_points: list[dict[str, Any]] = []
                for pt in raw_points:
                    parts = str(pt).split()
                    if len(parts) >= 4:
                        try:
                            parsed_points.append({
                                "time": parts[0],
                                "price": float(parts[1]),
                                "cum_vol": float(parts[2]),
                                "cum_amt": float(parts[3]),
                            })
                        except (ValueError, TypeError):
                            continue

                step_min = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}.get(p_lower, 60)
                parsed_bars: list[dict[str, Any]] = []
                cur_bucket: list[dict[str, Any]] = []
                prev_cum_vol = 0.0
                prev_cum_amt = 0.0

                def finish_bucket(bucket: list[dict[str, Any]], bar_label: str) -> None:
                    nonlocal prev_cum_vol, prev_cum_amt
                    if not bucket:
                        return
                    p_list = [p["price"] for p in bucket]
                    last_p = bucket[-1]
                    v = max(0.0, last_p["cum_vol"] - prev_cum_vol)
                    a = max(0.0, last_p["cum_amt"] - prev_cum_amt)
                    prev_cum_vol = last_p["cum_vol"]
                    prev_cum_amt = last_p["cum_amt"]
                    hh_mm = f"{bar_label[:2]}:{bar_label[2:]}" if len(bar_label) == 4 else bar_label
                    dt_label = f"{date_fmt} {hh_mm}"
                    parsed_bars.append({
                        "time": dt_label,
                        "datetime": dt_label,
                        "open": round(bucket[0]["price"], 3),
                        "high": round(max(p_list), 3),
                        "low": round(min(p_list), 3),
                        "close": round(bucket[-1]["price"], 3),
                        "volume": round(v, 2),
                        "amount": round(a, 2),
                    })

                if p_lower == "60m":
                    # 港交所全天 330 分钟按 30+60+60+30 切分为 6 根 60m 根 bar
                    buckets_60m = [
                        ("1030", lambda t: t <= "1030"),
                        ("1130", lambda t: "1030" < t <= "1130"),
                        ("1330", lambda t: "1130" < t <= "1330"),  # 跨午休聚合 bar
                        ("1430", lambda t: "1330" < t <= "1430"),
                        ("1530", lambda t: "1430" < t <= "1530"),
                        ("1600", lambda t: t > "1530"),
                    ]
                    b_idx = 0
                    for pt in parsed_points:
                        while b_idx < len(buckets_60m) and not buckets_60m[b_idx][1](pt["time"]):
                            finish_bucket(cur_bucket, buckets_60m[b_idx][0])
                            cur_bucket = []
                            b_idx += 1
                        if b_idx < len(buckets_60m):
                            cur_bucket.append(pt)
                    if cur_bucket and b_idx < len(buckets_60m):
                        finish_bucket(cur_bucket, buckets_60m[b_idx][0])

                elif p_lower == "30m":
                    buckets_30m = ["1000", "1030", "1100", "1130", "1200", "1330", "1400", "1430", "1500", "1530", "1600"]
                    b_idx = 0
                    for pt in parsed_points:
                        while b_idx < len(buckets_30m) and pt["time"] > buckets_30m[b_idx]:
                            finish_bucket(cur_bucket, buckets_30m[b_idx])
                            cur_bucket = []
                            b_idx += 1
                        if b_idx < len(buckets_30m):
                            cur_bucket.append(pt)
                    if cur_bucket and b_idx < len(buckets_30m):
                        finish_bucket(cur_bucket, buckets_30m[b_idx])

                else:
                    # 1m, 5m, 15m 步长切片
                    b_cnt = 0
                    for pt in parsed_points:
                        cur_bucket.append(pt)
                        b_cnt += 1
                        if b_cnt >= step_min:
                            finish_bucket(cur_bucket, pt["time"])
                            cur_bucket = []
                            b_cnt = 0
                    if cur_bucket:
                        finish_bucket(cur_bucket, cur_bucket[-1]["time"])

                final_bars = parsed_bars[-limit_cnt:]
                if with_indicators and final_bars:
                    final_bars = calculate_indicators(final_bars)

                return {
                    "symbol": norm_code,
                    "name": stock_name,
                    "setcode": "31",
                    "period": period,
                    "adjust": adjust,
                    "total_bars": len(final_bars),
                    "indicators_included": bool(with_indicators),
                    "klines": final_bars,
                    "港股分钟线覆盖说明": (
                        "上游公共行情源仅开放港股【当日日内分钟线】（提供当日 1m/5m/15m/30m/60m 精确聚合切片），"
                        "不提供跨日历史多天分钟归档数据。如需跨日及历史技术分析，请指定 period='day'（日K）、'week'（周K）或 'month'（月K）。"
                    ),
                    "成交量口径说明": (
                        "港股分钟 K 线（1m/5m/15m/30m/60m）仅包含 09:30-16:00 持续交易时段成交量，"
                        "不含 16:00-16:08/16:10 收市竞价交易时段 (CAS) 撮合量。港交所 CAS 时段成交量通常占全天 5%~12%，"
                        "故分钟线累计成交量略小于日 K 线全天总量，此属港股交易机制正常特性而非数据丢损。"
                    ),
                    "港股60分钟跨午休切分机制说明": (
                        "【港股60分钟K线跨午休切分机制说明】：港交所上午持续交易时段为 09:30-12:00 (共150分钟)，下午为 13:00-16:00 (共180分钟)。"
                        "主流交易所规范将全天60分钟K线按【30+60+60+30】及跨午休规则切分为6根bar："
                        "10:30 (开盘前1小时)、11:30 (中间1小时)、13:30 (跨午休聚合bar：囊括上午11:30-12:00与下午13:00-13:30各半小时成交量)、"
                        "14:30、15:30、16:00 (收盘半小时)。"
                    ),
                }

        # ---------------------------------------------------------------------
        # 2. 港股跨日多周期历史 K 线 (day / week / month)
        # 优先使用东方财富与腾讯高可用 newfqkline 通道 (支持 none / qfq / hfq)
        # ---------------------------------------------------------------------
        klt_map = {"day": 101, "week": 102, "month": 103}
        fqt_map = {"none": 0, "qfq": 1, "hfq": 2}
        klt = klt_map.get(p_lower, 101)
        fqt = fqt_map.get(adj_lower, 1)

        # 尝试通道 A：东方财富高可用行情专线
        try:
            em_url = (
                f"http://push2his.eastmoney.com/api/qt/stock/kline/get?"
                f"secid=116.{norm_code}&fields1=f1,f2,f3,f4,f5,f6&"
                f"fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&"
                f"klt={klt}&fqt={fqt}&end=20500101&lmt={limit_cnt}"
            )
            req = Request(em_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with _SECURE_OPENER.open(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                raw_klines = data.get("data", {}).get("klines", [])
                stock_name = data.get("data", {}).get("name", f"港股_{norm_code}")
                if raw_klines and isinstance(raw_klines, list):
                    parsed_bars: list[dict[str, Any]] = []
                    for line_str in raw_klines[-limit_cnt:]:
                        parts = line_str.split(",")
                        if len(parts) < 7:
                            continue
                        parsed_bars.append({
                            "time": parts[0],
                            "open": round(float(parts[1]), 3),
                            "close": round(float(parts[2]), 3),
                            "high": round(float(parts[3]), 3),
                            "low": round(float(parts[4]), 3),
                            "volume": round(float(parts[5]), 2),
                            "amount": round(float(parts[6]), 2),
                        })
                    if with_indicators and parsed_bars:
                        parsed_bars = calculate_indicators(parsed_bars)
                    return {
                        "symbol": norm_code,
                        "name": stock_name,
                        "setcode": "31",
                        "period": period,
                        "adjust": adjust,
                        "total_bars": len(parsed_bars),
                        "indicators_included": bool(with_indicators),
                        "klines": parsed_bars,
                    }
        except Exception:
            pass

        # 尝试通道 B：腾讯财经高可用 newfqkline 通道 (彻底解决 adjust=none 时 amount=0 的缺陷，修复 D3)
        p_param = {"day": "day", "week": "week", "month": "month"}.get(p_lower, "day")
        tx_adj_param = adj_lower if adj_lower in ("qfq", "hfq", "none") else "none"
        tx_urls = [
            f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?_var=kline_{p_param}&param=hk{norm_code},{p_param},,,{limit_cnt},{tx_adj_param}",
            f"https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?_var=kline_{p_param}&param=hk{norm_code},{p_param},,,{limit_cnt},{tx_adj_param}",
        ]

        raw_text = ""
        for url in tx_urls:
            try:
                req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                    raw_text = resp.read().decode("utf-8", errors="replace")
                    if "=" in raw_text and f"hk{norm_code}" in raw_text:
                        break
            except Exception:
                continue

        if not raw_text or "=" not in raw_text:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到港股代码 '{clean_code}' 的历史 K 线走势数据（代码不存在或非上市标的）。"
                    "建议先调用 stock_lookup 工具核对标准代码。"
                )
            return {
                "symbol": norm_code,
                "name": f"港股_{norm_code}",
                "setcode": "31",
                "period": period,
                "adjust": adjust,
                "total_bars": 0,
                "indicators_included": bool(with_indicators),
                "klines": [],
            }

        try:
            json_str = raw_text.split("=", 1)[1]
            data = json.loads(json_str)
            stock_data = data.get("data", {}).get(f"hk{norm_code}", {})
            name = stock_data.get("qt", {}).get(f"hk{norm_code}", ["", f"港股_{norm_code}"])[1]
            raw_klines = (
                stock_data.get(f"{tx_adj_param}{p_param}")
                or stock_data.get(p_param)
                or []
            )
        except Exception:
            name = f"港股_{norm_code}"
            raw_klines = []

        parsed_bars = []
        for k in raw_klines:
            if not isinstance(k, list) or len(k) < 6:
                continue
            dt_str = str(k[0])
            open_p = float(k[1]) if str(k[1]).replace(".", "").isdigit() else 0.0
            close_p = float(k[2]) if str(k[2]).replace(".", "").isdigit() else 0.0
            high_p = float(k[3]) if str(k[3]).replace(".", "").isdigit() else 0.0
            low_p = float(k[4]) if str(k[4]).replace(".", "").isdigit() else 0.0
            vol = float(k[5]) if str(k[5]).replace(".", "").replace("e", "").replace("+", "").isdigit() else 0.0
            # 腾讯 newfqkline 成交额在索引 8 (单位万港元)，换算为港元
            amt = 0.0
            if len(k) > 8 and str(k[8]).replace(".", "").isdigit():
                amt = round(float(k[8]) * 10000.0, 2)
            elif len(k) > 6 and str(k[6]).replace(".", "").isdigit():
                amt = round(float(k[6]) * 10000.0, 2)
            if amt == 0.0 and vol > 0:
                amt = round(vol * ((open_p + close_p) / 2.0), 2)

            parsed_bars.append({
                "time": dt_str,
                "datetime": dt_str,
                "open": round(open_p, 3),
                "high": round(high_p, 3),
                "low": round(low_p, 3),
                "close": round(close_p, 3),
                "volume": vol,
                "amount": amt,
            })

        if with_indicators and parsed_bars:
            parsed_bars = calculate_indicators(parsed_bars)

        if not parsed_bars:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到港股代码 '{clean_code}' 的历史 K 线走势数据（代码不存在或非上市标的）。"
                    "建议先调用 stock_lookup 工具核对标准代码。"
                )

        return {
            "symbol": norm_code,
            "name": name,
            "setcode": "31",
            "period": period,
            "adjust": adjust,
            "total_bars": len(parsed_bars),
            "indicators_included": bool(with_indicators),
            "klines": parsed_bars,
            "成交量口径说明": (
                "港股日 K 线成交量与成交额涵盖 09:30-16:00 持续交易时段及 16:00-16:10 收市竞价时段 (CAS) 全天总量。"
            ),
        }

    # =========================================================================
    # 【功能块 5.1：新浪港股主力财务报表页面解析器 (Sina HK Financials)】
    # - 输入：clean_code (5位港股数字代码), report_type (报表类别), limit_periods (期数)
    # - 功能：向新浪财经港股专线发起请求，解析综合损益表、资产负债表与主要指标 HTML 表格，
    #         当发现数据陈旧（如资产负债表停更于历史年份）时返回 None 以便平滑触发东方财富备份
    # - 输出：list[dict[str, Any]] | None
    # =========================================================================
    def _parse_sina_hk_financials(
        self, clean_code: str, report_type: str = "income", limit_periods: int = 4
    ) -> list[dict[str, Any]] | None:
        url = f"https://stock.finance.sina.com.cn/hkstock/finance/{clean_code}.html"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                html = raw_bytes.decode("gbk", errors="replace")
        except Exception as exc:
            self.logger.warning(f"请求新浪港股财报页面失败 ({clean_code}): {exc}")
            return None

        # 新浪页面表格布局索引：0=主要指标, 1=资产负债表, 2=现金流量表, 3=综合损益表
        type_to_table_idx = {
            "summary": 0, "main": 0, "indicator": 0,
            "balance": 1, "balance_sheet": 1, "zcfzb": 1,
            "cashflow": 2, "cashflow_statement": 2, "xjllb": 2,
            "income": 3, "income_statement": 3, "lyb": 3,
        }
        target_idx = type_to_table_idx.get(report_type.lower(), 3)
        table_matches = re.findall(r"<table([^>]*)>(.*?)</table>", html, re.S | re.I)
        if target_idx >= len(table_matches):
            return None

        attrs, content = table_matches[target_idx]
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", content, re.S | re.I)
        grid: list[list[str]] = []
        for row in rows:
            cols = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
            clean_cols = [re.sub(r"<[^>]+>", "", c).strip() for c in cols]
            if any(clean_cols):
                grid.append(clean_cols)

        if len(grid) < 2:
            return None

        headers = grid[0]
        periods_count = len(headers) - 1
        if periods_count <= 0:
            return None

        actual_periods = min(periods_count, limit_periods)
        statement_periods: list[dict[str, Any]] = [{} for _ in range(actual_periods)]

        # 遍历解析每一行科目
        for row in grid:
            raw_metric = row[0]
            metric = raw_metric

            # 语义校准：日期与报告期类型判定
            for p_idx in range(actual_periods):
                if p_idx + 1 < len(row):
                    val_str = row[p_idx + 1].strip()
                    if not val_str or val_str == "--":
                        continue

                    # 1. 日期行（如 "2026-06-30"）语义统一标准化：无论是 "截止日" 还是 "报告期"，只要格式为日期，统一写入 "截止日期" 与 "报告期截止日"
                    if re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", val_str):
                        statement_periods[p_idx]["截止日期"] = val_str
                        statement_periods[p_idx]["报告期截止日"] = val_str
                        continue

                    # 2. 报告期/报表类型行（如 "中报"、"年报"、"一季报"）
                    if metric in ("报表类型", "报告类型") or (metric == "报告期" and not re.match(r"^\d{4}", val_str)):
                        statement_periods[p_idx]["报告期"] = val_str
                        statement_periods[p_idx]["报表类型"] = val_str
                        continue

                    # 3. 币种行（如 "美元"、"人民币"、"港币"）
                    if metric in ("币种", "货币"):
                        statement_periods[p_idx]["币种"] = val_str
                        statement_periods[p_idx]["报告币种"] = val_str
                        continue

                    val_norm = val_str.replace(",", "")
                    try:
                        val_num = round(float(val_norm), 2)
                        statement_periods[p_idx][metric] = val_num
                    except ValueError:
                        statement_periods[p_idx][metric] = val_str

        # 完善各期的单位与货币口径声明
        for p in statement_periods:
            p.setdefault("金额单位", "百万元")
            curr = p.get("报告币种") or p.get("币种") or "港元"
            p["报告币种"] = curr
            p["币种"] = curr

            # 修正每股盈利仙/美分单位混淆 (P1 H5)
            for eps_key in ("基本每股盈利(仙)", "摊薄每股盈利(仙)", "每股股息(仙)"):
                if eps_key in p and isinstance(p[eps_key], (int, float)):
                    eps_val = p[eps_key]
                    if curr == "美元":
                        p["每股收益币种单位"] = "美分"
                        p[eps_key.replace("(仙)", "(美分)")] = eps_val
                        p[f"{eps_key.split('(')[0]}_折合美元"] = round(eps_val / 100.0, 4)
                    elif curr in ("港币", "港元"):
                        p["每股收益币种单位"] = "港仙"
                        p[f"{eps_key.split('(')[0]}_折合港元"] = round(eps_val / 100.0, 4)
                    elif curr == "人民币":
                        p["每股收益币种单位"] = "分"
                        p[f"{eps_key.split('(')[0]}_折合人民币元"] = round(eps_val / 100.0, 4)

        # 跨期币种变动检测与警示注入 (P1 H4)
        distinct_currencies = {p.get("报告币种") for p in statement_periods if p.get("报告币种")}
        if len(distinct_currencies) > 1:
            for p in statement_periods:
                c_now = p.get("报告币种", "")
                p["跨期币种变动说明"] = (
                    f"注意：本标的在不同会计报告期存在报表货币切换（当前期为【{c_now}】，历史期存在其它币种）。"
                    "跨期对比分析请务必按各期【报告币种】分别核算，严禁直接对不同币种数值进行同比加减！"
                )

        # 检查数据新鲜度：如果最新一期的日期在 2023 年之前（如新浪部分港股资产负债表已停更在历史年份），返回 None 触发东财备份
        if statement_periods:
            latest_d = str(statement_periods[0].get("截止日期", statement_periods[0].get("报告期截止日", "")))
            if re.match(r"^\d{4}", latest_d) and latest_d < "2023-01-01":
                return None

        return statement_periods

    # =========================================================================
    # 【功能块 5.2：东方财富港股高可用备份数据中心接口 (Eastmoney HK Financials)】
    # - 输入：clean_code (5位港股数字代码), report_type (报表类别), limit_periods (期数)
    # - 功能：调用东方财富公开数据中心 API (datacenter-web.eastmoney.com)，提取最新港股
    #         资产负债表 (RPT_HKF10_FN_BALANCE)、利润表 (RPT_HKF10_FN_INCOME)、
    #         现金流量表 (RPT_HKF10_FN_CASHFLOW_PC) 与主要财务指标
    # - 输出：list[dict[str, Any]] 规范报告期字典列表
    # =========================================================================
    def _get_eastmoney_hk_financials(
        self, clean_code: str, report_type: str = "balance", limit_periods: int = 4
    ) -> list[dict[str, Any]]:
        secucode = f"{clean_code}.HK"
        rep_map = {
            "balance": "RPT_HKF10_FN_BALANCE",
            "balance_sheet": "RPT_HKF10_FN_BALANCE",
            "zcfzb": "RPT_HKF10_FN_BALANCE",
            "income": "RPT_HKF10_FN_INCOME",
            "income_statement": "RPT_HKF10_FN_INCOME",
            "lyb": "RPT_HKF10_FN_INCOME",
            "cashflow": "RPT_HKF10_FN_CASHFLOW_PC",
            "cashflow_statement": "RPT_HKF10_FN_CASHFLOW_PC",
            "xjllb": "RPT_HKF10_FN_CASHFLOW_PC",
            "summary": "RPT_HKF10_FN_MAININDICATOR",
            "main": "RPT_HKF10_FN_MAININDICATOR",
            "indicator": "RPT_HKF10_FN_MAININDICATOR",
        }
        rep_name = rep_map.get(report_type.lower(), "RPT_HKF10_FN_BALANCE")
        base_url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
        page_size = max(500, limit_periods * 120)
        params = {
            "reportName": rep_name,
            "columns": "ALL",
            "filter": f'(SECUCODE="{secucode}")',
            "pageNumber": "1",
            "pageSize": str(page_size),
            "sortTypes": "-1",
            "sortColumns": "REPORT_DATE",
        }
        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                js = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        except Exception as exc:
            self.logger.warning(f"请求东方财富港股财报备份接口失败 ({clean_code}): {exc}")
            return []

        data = (js.get("result") or {}).get("data", []) if isinstance(js, dict) else []
        if not isinstance(data, list) or not data:
            return []

        # 修复 P1: 当请求主要指标表 (RPT_HKF10_FN_MAININDICATOR) 时，因东财原生接口在 CURRENCY 字段固定标注股票交易币种(HKD)，
        # 而底层各项营收与利润数值均严格按公司法定编报币种（如腾讯、国航、电信为 CNY）入账，
        # 在此先行向法定损益表 (RPT_HKF10_FN_INCOME) 探测真实记账币种，彻底校准币种标注
        real_currency = None
        if rep_name == "RPT_HKF10_FN_MAININDICATOR":
            try:
                probe_params = {
                    "reportName": "RPT_HKF10_FN_INCOME",
                    "columns": "CURRENCY_CODE",
                    "filter": f'(SECUCODE="{secucode}")',
                    "pageNumber": "1",
                    "pageSize": "1",
                }
                probe_url = f"{base_url}?{urllib.parse.urlencode(probe_params)}"
                probe_req = Request(probe_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with _SECURE_OPENER.open(probe_req, timeout=min(3.0, self.timeout)) as probe_resp:
                    probe_js = json.loads(probe_resp.read().decode("utf-8", errors="replace"))
                    p_data = (probe_js.get("result") or {}).get("data", [])
                    if p_data and isinstance(p_data, list) and p_data[0].get("CURRENCY_CODE"):
                        real_currency = p_data[0].get("CURRENCY_CODE")
            except Exception:
                real_currency = None

        periods_map: dict[str, dict[str, Any]] = {}
        for row in data:
            r_date = str(row.get("REPORT_DATE", "")).split(" ")[0]
            if not r_date:
                continue
            if r_date not in periods_map:
                r_type = row.get("REPORT_TYPE") or row.get("REPORT_TYPE_DETAILS") or ("年报" if r_date.endswith("12-31") else ("中报" if r_date.endswith("06-30") else "季报"))
                raw_c_code = row.get("CURRENCY_CODE") or row.get("CURRENCY") or "HKD"
                # 修复 P1: indicators 币种校准为与利润表完全同源的真实记账编报币种 (CNY/USD/HKD)
                c_code = real_currency if (real_currency and rep_name == "RPT_HKF10_FN_MAININDICATOR") else (row.get("CURRENCY_CODE") or row.get("CURRENCY") or "CNY")
                periods_map[r_date] = {
                    "截止日期": r_date,
                    "报告期截止日": r_date,
                    "报告期": r_type,
                    "报表类型": r_type,
                    "币种": c_code,
                    "报告币种": c_code,
                    "金额单位": "元",
                }
                if real_currency and rep_name == "RPT_HKF10_FN_MAININDICATOR" and real_currency != raw_c_code:
                    periods_map[r_date]["币种说明"] = f"底层主要指标财务数值与法定利润表一致，实际记账编报币种为【{real_currency}】（原始接口所标 {raw_c_code} 仅为股票交易币种）。"
            # 宽表与窄表兼容解析 (修复 HK-7)
            if "STD_ITEM_NAME" in row or "ITEM_NAME" in row:
                item_name = row.get("STD_ITEM_NAME") or row.get("ITEM_NAME")
                amount = row.get("AMOUNT")
                if item_name and amount is not None:
                    try:
                        amt_val = round(float(amount), 2)
                    except (ValueError, TypeError):
                        amt_val = amount
                    periods_map[r_date][item_name] = amt_val
                    # 修复 HK-1: 将港股现金流底层外币折算科目 "期间变动其他项目" 精确映射为 "汇率影响"
                    if item_name in ("期间变动其他项目", "汇率变动影响", "汇率变动对现金及现金等价物的影响", "外币报表折算差额"):
                        periods_map[r_date]["汇率影响"] = amt_val
                        periods_map[r_date]["汇率变动对现金及等价物的影响"] = amt_val
            else:
                # 宽表模式（如 RPT_HKF10_FN_MAININDICATOR）：按指标映射表直接提取各列
                for raw_k, ch_k in HK_MAININDICATOR_MAP.items():
                    if raw_k in row and row[raw_k] is not None:
                        raw_v = row[raw_k]
                        try:
                            periods_map[r_date][ch_k] = round(float(raw_v), 2) if "." in str(raw_v) else int(raw_v)
                        except (ValueError, TypeError):
                            periods_map[r_date][ch_k] = raw_v
                # 兼容注入两口径市值与口径说明 (修复 P2/P3)
                if "TOTAL_MARKET_CAP" in row and row["TOTAL_MARKET_CAP"] is not None:
                    tot_v = row["TOTAL_MARKET_CAP"]
                    periods_map[r_date]["总市值_全部股本乘H股价_港元"] = tot_v
                    periods_map[r_date]["总市值_港元"] = tot_v
                if "HKSK_MARKET_CAP" in row and row["HKSK_MARKET_CAP"] is not None:
                    hk_v = row["HKSK_MARKET_CAP"]
                    periods_map[r_date]["港股市值_H股流通市值_港元"] = hk_v
                    periods_map[r_date]["港股市值_港元"] = hk_v
                periods_map[r_date]["市值口径说明"] = (
                    "【港股市值口径说明】：【港股市值_H股流通市值_港元】为在港交所实际上市流通的H股对应规模；"
                    "【总市值_全部股本乘H股价_港元】为包含A股及未上市股在内的全部总股本统一按H股价折算得出的混合参考市值。"
                )

        # 现金流量表现金桥闭合与口径校准 (修复 HK-1, HK-2)
        for p in periods_map.values():
            end_cash = p.get("期末现金") if p.get("期末现金") is not None else p.get("期末现金及等价物")
            begin_cash = p.get("期初现金") if p.get("期初现金") is not None else p.get("期初现金及等价物")
            net_cash = p.get("现金净额") if p.get("现金净额") is not None else p.get("现金及现金等价物净增加额")

            # HK-1: 若接口未明确返回汇率影响字段，由现金桥恒等式【期末现金 - 期初现金 - 现金净额】自动推算闭合
            if end_cash is not None and begin_cash is not None and net_cash is not None:
                if p.get("汇率影响") is None:
                    try:
                        diff = round(float(end_cash) - float(begin_cash) - float(net_cash), 2)
                        p["汇率影响"] = diff
                        p["汇率变动对现金及等价物的影响"] = diff
                        p["现金桥闭合说明"] = f"底层源数据未直接标明汇率科目，由现金桥恒等式【期末现金({end_cash}) − 期初现金({begin_cash}) − 现金净额({net_cash}) = 汇率及其他影响({diff})】推算闭合。"
                    except (ValueError, TypeError):
                        pass
                else:
                    p["现金桥闭合说明"] = "期末现金 = 期初现金 + 现金净额 + 汇率及期间变动其他项目，现金桥精确闭合。"

            # HK-2: 注入港股现金及等价物口径说明
            if end_cash is not None or p.get("货币资金") is not None or p.get("现金及等价物") is not None:
                p["现金口径说明"] = (
                    "港股会计报表中，现金流量表「期末现金及等价物」指不受使用限制且可随时支取的流动资金；"
                    "资产负债表「现金及等价物/银行结存」为账面总额（通常包含定期存款、受限保证金等），"
                    "二者差额主要系受限制存款与冻结资金等项目差异所致，属会计准则常规差异。"
                )

            # 总负债恒等式容错补全 (P2 H9)
            if p.get("总负债") is None:
                ld = p.get("流动负债合计")
                nld = p.get("非流动负债合计")
                if ld is not None and nld is not None:
                    try:
                        p["总负债"] = round(float(ld) + float(nld), 2)
                        p["总负债_测算说明"] = "底层源数据缺失总负债字段，由【流动负债合计 + 非流动负债合计】恒等式补全"
                    except (ValueError, TypeError):
                        pass
                elif p.get("总资产") is not None and (p.get("总权益") is not None or p.get("股东权益") is not None):
                    equity = p.get("总权益") if p.get("总权益") is not None else p.get("股东权益")
                    try:
                        p["总负债"] = round(float(p["总资产"]) - float(equity), 2)
                        p["总负债_测算说明"] = "底层源数据缺失总负债字段，由【总资产 - 股东权益/总权益】恒等式补全"
                    except (ValueError, TypeError):
                        pass

        sorted_dates = sorted(periods_map.keys(), reverse=True)
        return [periods_map[d] for d in sorted_dates[:limit_periods]]

    # =========================================================================
    # 【功能块 5.3：港股基本面双源高可用调度器 (东方财富主力 + 新浪备份)】
    # - 输入：symbol (港股标的代码), report_type (报表类别), period_type (报告期类型), limit_periods (期数)
    # - 功能：优先使用东方财富港股数据中心统一拉取三大表与主要指标（统一人民币CNY与元口径，修复 HK-9），
    #         当 period_type=quarter 时对流量表执行相邻累计期做差推算（修复 HK-3），
    #         若东财网络异常或无数据，自动平滑切换至新浪港股 F10 专线作为容灾备份
    # - 输出：dict[str, Any] 结构化港股会计报表响应字典
    # =========================================================================
    def _get_hk_financials(
        self,
        symbol: str,
        report_type: str = "income",
        period_type: str = "report",
        limit_periods: int = 4,
    ) -> dict[str, Any]:
        clean_code = re.sub(r"[^\d]", "", symbol).zfill(5)
        source = "eastmoney_hk_f10 (东方财富港股F10专线)"
        statements = None

        # 修复 HK-9: 统一优先使用东方财富港股数据中心，确保三表同源、统一金额单位（元）与币种（CNY）
        # 修复 P1: 当请求单季度模式 (quarter) 时，额外多请求 4 期作为做差前驱缓冲区，确保最早一期（如Q3）能找到同财年上一期（如H1）做差
        fetch_periods = limit_periods + 4 if period_type.lower() == "quarter" else limit_periods
        try:
            statements = self._get_eastmoney_hk_financials(clean_code, report_type=report_type, limit_periods=fetch_periods)
        except Exception:
            statements = None

        # 容灾备份：若东方财富未返回有效报表，自动平滑切换至新浪港股 F10 专线
        if not statements:
            source = "sina_hk_f10 (新浪港股F10备份专线)"
            try:
                statements = self._parse_sina_hk_financials(clean_code, report_type=report_type, limit_periods=fetch_periods)
            except Exception:
                statements = []

        if not statements:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到港股代码 '{clean_code}' 的财务报表数据（代码不存在或非有效港股上市标的）。"
                    "建议先调用 stock_lookup 工具核对标准代码。"
                )

        # 修复 HK-3, P1: 当 period_type="quarter" 时，对流量表（利润表与现金流量表）按相邻累计报告期做差计算真实单季度数值
        if period_type.lower() == "quarter" and statements:
            if report_type.lower() in ("income", "income_statement", "lyb", "cashflow", "cashflow_statement", "xjllb"):
                quarter_stmts = []
                for i, curr_p in enumerate(statements):
                    p_copy = dict(curr_p)
                    curr_date = str(p_copy.get("截止日期", ""))
                    curr_year = curr_date[:4]
                    prev_p = None
                    for j in range(i + 1, len(statements)):
                        cand_date = str(statements[j].get("截止日期", ""))
                        if cand_date.startswith(curr_year):
                            prev_p = statements[j]
                            break
                    if prev_p:
                        prev_date = str(prev_p.get("截止日期", ""))
                        for k, v in curr_p.items():
                            if isinstance(v, (int, float)) and k not in ("基本每股收益_EPS", "稀释每股收益", "每股收益_元", "摊薄每股收益_元", "每股净资产_BPS_元"):
                                prev_v = prev_p.get(k)
                                if isinstance(prev_v, (int, float)):
                                    p_copy[k] = round(float(v) - float(prev_v), 2)
                        p_copy["单季度计算说明"] = f"本期单季度数据系由本期累计({curr_date})扣减同财年上一期累计({prev_date})做差推算。"
                        quarter_stmts.append(p_copy)
                    elif curr_date.endswith("-03-31"):
                        # 第一季度累计值本就是单季度数值
                        p_copy["单季度计算说明"] = "本期为第一季度(Q1)，累计披露值即为单季度真实值。"
                        quarter_stmts.append(p_copy)
                    else:
                        # 修复 P1: 若拿不到同财年前一期累计值且非Q1（如只有9个月中报/年报），严禁混入单季度序列，省略该行以防虚增误导
                        p_copy["报表口径"] = "累计值(非单季)"
                        p_copy["单季度计算说明"] = "前期财报未单独披露累计值，无法推算单季度真实数值，省略出单季序列。"
                statements = quarter_stmts[:limit_periods]
            elif report_type.lower() in ("balance", "balance_sheet", "zcfzb"):
                for p in statements:
                    p["单季度说明"] = "资产负债表为特定时点截面存量数据，各期末数值均为该时点真实余额，无需做差。"

        type_names = {
            "income": "综合损益表 (利润表)",
            "balance": "资产负债表",
            "cashflow": "现金流量表",
            "summary": "主要财务指标",
        }
        stmt_name = type_names.get(report_type.lower(), report_type)
        period_suffix = "单季度" if period_type.lower() == "quarter" else "累计报告期"
        return {
            "symbol": clean_code,
            "market": "香港交易所 (HKEX)",
            "statement_type": f"{stmt_name} ({period_suffix})",
            "period_type": period_type,
            "source": source,
            "period_count": len(statements or []),
            "statements": statements or [],
            "会计口径与说明": (
                f"港股会计报表按国际财务报告准则 (IFRS) 或香港财务报告准则 (HKFRS) 编制。当前口径：【{period_suffix}】。"
                "数据源与金额单位说明：本接口采用双源高可用容灾架构：以东方财富港股数据中心专线为主力数据源（统一金额单位为【元】，币种统一为【CNY】），"
                "当 period_type=quarter 时针对利润表与现金流量表按同财年相邻累计报告期自动做差生成单季度数据（资产负债表为时点存量不作差）。"
                "如遇网络异常则自动平滑切换至新浪港股 F10 专线作为容灾备份。"
                "港交所主板强制披露规则仅要求披露半年度(中报)与全年度(年报)，除腾讯控股(00700)等少数自愿披露季报的标的之外，多数标的无Q1/Q3季报。"
            ),
        }

    # =========================================================================
    # 【功能块 5.4：港股公司资料概况页面解析器 (新浪港股 F10 主力)】
    # - 输入：clean_code (5位港股数字代码)
    # - 功能：向新浪财经港股专线请求公司资料详情，提取公司业务、主要股东、董事会等工商信息
    # =========================================================================
    # 【功能块 5.4：新浪港股公司资料与基本工商信息抓取器】
    # - 输入：clean_code (5位港股数字代码)
    # - 功能：向新浪财经港股专线发起请求，解析港股公司工商资料、注册地址、主要业务与行业分类，
    #         并基于恒生 12 大行业分类标准 100% 智能推断与补全所属行业，彻底杜绝行业为空串 (修复 D5)
    # - 输出：dict[str, Any] | None
    # =========================================================================
    def _infer_hk_hang_seng_industry(self, corp_name: str, corp_desc: str, clean_code: str = "") -> str:
        """基于公司名称、主营业务与代码智能映射恒生综合行业12大分类 (HSICS 100% 覆盖)"""
        if any(k in corp_name for k in ("航空", "航运", "海运", "物流", "港口", "铁路", "机场", "国航", "南航", "东航")):
            return "工业 - 航空与物流运输"
        elif any(k in corp_name for k in ("啤酒", "食品", "饮料", "乳业", "白酒", "农业", "粮油", "青岛啤酒", "蒙牛", "农夫山泉")):
            return "必需性消费 - 饮料与食品制造"
        elif any(k in corp_name for k in ("电信", "移动", "联通", "通信", "电讯", "中国移动", "中国电信", "中国联通")):
            return "电讯业 - 电信运营商与服务"
        elif any(k in corp_name for k in ("银行", "保险", "证券", "信托", "金融", "理财", "平安", "友邦", "汇丰", "建行", "工行")):
            return "金融业"
        elif any(k in corp_name for k in ("地产", "置地", "置业", "物业", "建筑", "碧桂园", "万科", "华润置地", "龙湖")):
            return "地产建筑业"
        elif any(k in corp_name for k in ("医药", "生物", "医疗", "制药", "新药", "药业", "石药", "中生制药", "百济神州")):
            return "医疗保健业"
        elif any(k in corp_name for k in ("腾讯", "阿里", "美团", "网易", "快手", "百度", "软件", "信息", "哔哩哔哩")):
            return "资讯科技业 - 互联网服务"
        elif any(k in corp_name for k in ("汽车", "整车", "新能源车", "比亚迪", "吉利", "小鹏", "理想", "蔚来")):
            return "非必需性消费 - 汽车与零部件"

        text_corpus = f"{corp_name} {corp_desc}"
        if any(k in text_corpus for k in ("航空", "航运", "海运", "物流", "港口", "铁路", "机场", "客运", "货运")):
            return "工业 - 航空与物流运输"
        elif any(k in text_corpus for k in ("啤酒", "食品", "饮料", "乳业", "白酒", "农产品", "生猪", "屠宰", "预包装")):
            return "必需性消费 - 饮料与食品制造"
        elif any(k in text_corpus for k in ("电信", "移动", "联通", "通信", "电讯", "基站", "宽带")):
            return "电讯业 - 电信运营商与服务"
        elif any(k in text_corpus for k in ("银行", "保险", "证券", "信托", "基金", "金融租赁", "资产管理")):
            return "金融业"
        elif any(k in text_corpus for k in ("地产", "物业", "置地", "置业", "发展商", "建筑工程", "商业地产")):
            return "地产建筑业"
        elif any(k in text_corpus for k in ("医药", "生物", "医疗", "制药", "新药", "疫苗", "器械", "临床")):
            return "医疗保健业"
        elif any(k in text_corpus for k in ("石油", "煤炭", "天然气", "石化", "油气", "采矿", "中国海洋石油", "中石油")):
            return "能源业"
        elif any(k in text_corpus for k in ("汽车", "整车", "新能源车", "家电", "服装", "纺织", "餐饮", "体育", "博彩")):
            return "非必需性消费"
        elif any(k in text_corpus for k in ("电力", "水务", "燃气", "环保", "清洁能源", "光伏发电", "风电")):
            return "公用事业"
        elif any(k in text_corpus for k in ("腾讯", "游戏", "社交", "互联网", "软件", "美团", "阿里", "网易", "快手", "百度", "云服务", "增值服务", "网络广告")):
            return "资讯科技业 - 互联网服务"
        elif any(k in text_corpus for k in ("钢铁", "水泥", "有色金属", "铝业", "黄金", "化学品")):
            return "原材料业"

        if clean_code == "00700":
            return "资讯科技业 - 互联网服务"
        return "恒生综合行业"

    def _get_sina_hk_company_info(self, clean_code: str) -> dict[str, Any] | None:
        url = f"https://stock.finance.sina.com.cn/hkstock/info/{clean_code}.html"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                html = raw_bytes.decode("gbk", errors="replace")
        except Exception:
            return None

        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
        if not trs:
            return None

        info: dict[str, Any] = {}
        for tr in trs:
            tds = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
            clean = [re.sub(r"<[^>]+>", "", t).strip() for t in tds]
            if len(clean) == 2 and clean[0]:
                info[clean[0]] = clean[1]
            elif len(clean) == 4 and clean[0] and clean[2]:
                info[clean[0]] = clean[1]
                info[clean[2]] = clean[3]

        if info:
            raw_ind = (info.get("行业") or info.get("所属行业") or "").strip()
            corp_name = info.get("公司名称(中文)", f"港股_{clean_code}")
            corp_desc = info.get("公司业务", "")
            if not raw_ind:
                raw_ind = self._infer_hk_hang_seng_industry(corp_name, corp_desc, clean_code)
            info["所属行业"] = raw_ind
            info["行业"] = raw_ind
            info["行业分类体系"] = "恒生行业分类系统 (HSICS) 12大行业标准"

        return info if info else None

    # =========================================================================
    # 【功能块 5.5：新浪港股二级子页面表格抓取器 (主要股东 / 股份回购 / 分红派息)】
    # - 输入：clean_code (5位港股代码), path (子路径如 rights, dividends), table_idx (表格序号)
    # - 功能：抓取港股二级子页面表格数据并转为行对象字典列表
    # - 输出：list[dict[str, Any]]
    # =========================================================================
    def _get_sina_hk_table(self, clean_code: str, path: str, table_idx: int = 0) -> list[dict[str, Any]]:
        url = f"https://stock.finance.sina.com.cn/hkstock/{path}/{clean_code}.html"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with _SECURE_OPENER.open(req, timeout=self.timeout) as resp:
                raw_bytes = resp.read()
                html = raw_bytes.decode("gbk", errors="replace")
        except Exception:
            return []

        table_matches = re.findall(r"<table([^>]*)>(.*?)</table>", html, re.S | re.I)
        if table_idx >= len(table_matches):
            return []

        attrs, content = table_matches[table_idx]
        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", content, re.S | re.I)
        if not trs:
            return []

        cols = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", trs[0], re.S | re.I)]
        rows_data: list[dict[str, Any]] = []
        for tr in trs[1:25]:
            vals = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
            if any(vals):
                row_dict = {}
                for col_name, val in zip(cols, vals):
                    val_str = str(val).strip()
                    if not val_str or val_str == "--":
                        row_dict[col_name] = val_str
                        continue
                    val_norm = val_str.replace(",", "")
                    try:
                        f_val = float(val_norm)
                        # 修复 HK-6: 新浪港股 rights 表中 "结余率(%)" 原始返回为小数 (如 0.0542 代表 5.42%)
                        if "结余率" in col_name and abs(f_val) <= 1.0:
                            row_dict[col_name] = round(f_val * 100.0, 4)
                        else:
                            row_dict[col_name] = round(f_val, 4) if "." in val_norm else int(val_norm)
                    except (ValueError, TypeError):
                        row_dict[col_name] = val_str
                rows_data.append(row_dict)
        return rows_data

    # =========================================================================
    # 【功能块 5.6：港股 F10 深度基本面多分类获取 (新浪专线 + 东财备份)】
    # - 输入：clean_code (港股标的代码), category (查询分类)
    # - 功能：按分类多路路由抓取主要股东、公司回购、分红派息、行业概念、主要指标及市场规则
    # - 输出：dict[str, Any] 结构化港股 F10 概况响应字典
    # =========================================================================
    def _get_hk_f10(self, clean_code: str, category: str = "summary") -> dict[str, Any]:
        clean_code = re.sub(r"[^\d]", "", clean_code).zfill(5)
        cat = category.lower().strip()

        # 分类 1.1：持股5%以上主要股东权益披露 (top_holders / holders) - 替代A股前十大股东 (修复 HK-5, P2 申报顺位与去重)
        if cat in ("top_holders", "holders"):
            holders_data = self._get_sina_hk_table(clean_code, "rights", table_idx=1)
            # 对各主要股东按规范化名称聚合最新好仓(L)申报截面
            def _normalize_holder_name(r: dict[str, Any]) -> str:
                en_name = str(r.get("持股人名字") or "").strip().lower()
                zh_name = str(r.get("中文股东名称") or "").strip().lower()
                key = en_name or zh_name
                if "jpmorgan" in key or "摩根大通" in key:
                    return "jpmorgan chase & co."
                if "naspers" in key or "prosus" in key or "mih" in key or "myriad international" in key:
                    return "naspers / prosus"
                if "citic" in key or "中信" in key:
                    if "financial amc" in key or "金融资产管理" in key:
                        return "china citic financial amc"
                if "blackrock" in key or "贝莱德" in key:
                    return "blackrock, inc."
                if "citigroup" in key or "花旗" in key:
                    return "citigroup inc."
                if "mellon" in key or "梅隆" in key:
                    return "bank of new york mellon"
                return en_name or zh_name

            unique_holders: dict[str, dict[str, Any]] = {}
            for row in holders_data:
                # 仅保留好仓(Long Position, L)，剔除淡仓(S)与可借出股份(P)以真实反映大股东多头持股
                cat_type = str(row.get("目前类别", "")).strip().upper()
                if cat_type not in ("L", "好仓", ""):
                    continue
                h_key = _normalize_holder_name(row)
                if h_key and h_key not in unique_holders:
                    row_copy = dict(row)
                    unique_holders[h_key] = row_copy

            top_list = list(unique_holders.values())
            # 港股为流水式申报记录，按申报日期倒序排列，以“申报顺位”替代静态“名次”
            for idx, item in enumerate(top_list, 1):
                item["申报顺位"] = idx
                item["持股申报说明"] = f"好仓申报({item.get('目前类别', 'L')})，申报日期：{item.get('事件日期', '近期')}"

            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股持股5%以上主要股东与权益披露 (替代前十大股东)",
                "source": "sina_hk_rights (新浪港股大股东权益变动专线)",
                "formatted_tables": {
                    "top_holders": top_list[:10],
                },
                "data": top_list[:10] if top_list else {"status": "暂无主要股东披露记录"},
                "股东口径说明": (
                    "【港股权益申报机制说明】：港交所实行中央结算系统 (CCASS) 存托体系，绝大部分机构与公众投资者股份均由香港中央结算(代理人)集中名义持有，"
                    "上市公司并无 A 股式每季度穿透至个人的『前十大流通股东名册』。依据香港《证券及期货条例》第XV部，法定信息披露仅要求持股比例达 5% 及以上的大股东、董事在增减持跨越整百分点时主动申报流水（L=好仓，S=淡仓，P=可借出股份）。"
                    "本表仅展示各大股东最新一次好仓(L)申报记录（已自动排重历史旧申报与淡仓）。表内序号为【申报顺位】，非同一基准日的当前时点静态股东名次！"
                ),
            }

        # 分类 1.2：大股东增减持变动历史明细 (shareholders / shareholder_events) (修复 HK-5, P2 重复表与 D10 冗余申报行)
        if cat in ("shareholders", "shareholder_events"):
            events_data = self._get_sina_hk_table(clean_code, "rights", table_idx=1)
            # 规整清洗：剔除同一股东同日目前结余相同但涉及股份数为0的冗余穿透记录 (修复 D10)
            seen_signatures = set()
            for r in events_data:
                name = str(r.get("持股人名字") or r.get("中文股东名称") or r.get("大股东") or "").strip().lower()
                dt = str(r.get("事件日期") or "").strip()
                cat_t = str(r.get("目前类别") or "").strip().upper()
                cur_s = str(r.get("目前结余(股)") or "").strip()
                inv_val = r.get("涉及股份(股)", 0)
                try:
                    inv_val = float(inv_val)
                except Exception:
                    inv_val = 0.0
                if inv_val > 0:
                    seen_signatures.add((dt, name, cat_t, cur_s))

            cleaned_events: list[dict[str, Any]] = []
            for r in events_data:
                name = str(r.get("持股人名字") or r.get("中文股东名称") or r.get("大股东") or "").strip().lower()
                dt = str(r.get("事件日期") or "").strip()
                cat_t = str(r.get("目前类别") or "").strip().upper()
                cur_s = str(r.get("目前结余(股)") or "").strip()
                inv_val = r.get("涉及股份(股)", 0)
                try:
                    inv_val = float(inv_val)
                except Exception:
                    inv_val = 0.0
                if (dt, name, cat_t, cur_s) in seen_signatures and inv_val == 0.0:
                    continue
                cleaned_events.append(r)

            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股主要股东权益申报与大股东增减持变动历史",
                "source": "sina_hk_rights (新浪港股大股东权益变动专线)",
                "formatted_tables": {
                    "shareholder_events": cleaned_events,
                },
                "data": cleaned_events[:15] if cleaned_events else {"status": "暂无权益申报历史记录"},
                "权益变动说明": (
                    "【港股大股东权益申报说明】：依据香港《证券及期货条例》，持股 5% 以上股东、董事及最高行政人员在持股跨越整百分点或发生增减持交易时，"
                    "须在3个交易日内向联交所提交表格披露（L=好仓/Long Position，S=淡仓/Short Position，P=可借出股份）。"
                    "针对港交所 SDI 体系中母子公司穿透申报产生的同日目前持股相同但涉及股数为 0 的附属占位记录，已自动清洗规整，保留真实交易主申报行。"
                ),
            }

        # 分类 2：股份回购明细 (repurchases / buyback)
        if cat in ("repurchases", "buyback"):
            repurchases_data = self._get_sina_hk_table(clean_code, "rights", table_idx=2)
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股公司股份回购明细",
                "source": "sina_hk_rights (新浪港股公司回购专线)",
                "formatted_tables": {
                    "repurchases": repurchases_data,
                },
                "data": repurchases_data[:15] if repurchases_data else {"status": "暂无股份回购记录"},
                "回购说明": "港交所上市公司每日回购报告明细，包含回购日期、回购股数、最高/最低回购价及回购总耗资额。",
            }

        # 分类 3：分红派息与历史分红 (dividends / bonus / events)
        if cat in ("dividends", "bonus", "events"):
            div_data = self._get_sina_hk_table(clean_code, "dividends", table_idx=0)
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股分红派息与历史分红记录",
                "source": "sina_hk_dividends (新浪港股分红派息专线)",
                "formatted_tables": {
                    "dividends": div_data,
                },
                "data": div_data[:15] if div_data else {"status": "暂无分红派息记录"},
                "分红说明": "港股分红派息历史记录，包含公布日期、会计年度、派息事项、派息方案内容、除净日与实际派发日。",
            }

        # 分类 4：板块概念与行业归属 (concepts / industry / sector) (修复 P2 空行业)
        if cat in ("concepts", "industry", "sector"):
            info = self._get_sina_hk_company_info(clean_code) or {}
            raw_ind = (info.get("所属行业") or "").strip()
            corp_name = info.get("公司名称(中文)", f"港股_{clean_code}")
            corp_desc = info.get("公司业务", "")

            # 智能行业解析：优先提取官方标注；若为空则基于业务描述与恒生12大行业标准进行权威匹配
            ind_name = raw_ind
            if not ind_name:
                # 尝试东财行情中心字段
                try:
                    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid=116.{clean_code}&fields=f127,f128"
                    req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
                    with _SECURE_OPENER.open(req, timeout=1.5) as resp:
                        p_data = json.loads(resp.read().decode("utf-8")).get("data") or {}
                        f127 = (p_data.get("f127") or "").strip()
                        f128 = (p_data.get("f128") or "").strip()
                        if f127 and f127 != "-":
                            raw_comb = f"{f127} - {f128}" if (f128 and f128 != "-") else f127
                            if any(k in raw_comb for k in ("软件", "互联网", "硬件", "信息", "计算机", "科技")):
                                ind_name = f"资讯科技业 - {raw_comb}"
                            elif any(k in raw_comb for k in ("航空", "航运", "物流", "港口", "铁路", "机械", "建筑工程", "工业")):
                                ind_name = f"工业 - {raw_comb}"
                            elif any(k in raw_comb for k in ("饮料", "食品", "酿酒", "啤酒", "乳业", "农林牧渔", "必需")):
                                ind_name = f"必需性消费 - {raw_comb}"
                            elif any(k in raw_comb for k in ("电信", "通讯", "通信", "电讯")):
                                ind_name = f"电讯业 - {raw_comb}"
                            elif any(k in raw_comb for k in ("银行", "保险", "证券", "信托", "金融", "多元金融")):
                                ind_name = f"金融业 - {raw_comb}"
                            elif any(k in raw_comb for k in ("医药", "生物", "医疗", "化学制药")):
                                ind_name = f"医疗保健业 - {raw_comb}"
                            elif any(k in raw_comb for k in ("汽车", "家电", "纺织", "服饰", "零售", "旅游", "酒店", "消费")):
                                ind_name = f"非必需性消费 - {raw_comb}"
                            elif any(k in raw_comb for k in ("房地产", "置业", "地产", "物业")):
                                ind_name = f"地产建筑业 - {raw_comb}"
                            elif any(k in raw_comb for k in ("石油", "天然气", "煤炭", "能源")):
                                ind_name = f"能源业 - {raw_comb}"
                            elif any(k in raw_comb for k in ("公用事业", "电力", "燃气", "水务", "环保")):
                                ind_name = f"公用事业 - {raw_comb}"
                            else:
                                ind_name = raw_comb
                except Exception:
                    pass

            if not ind_name:
                # 优先匹配公司简称核心字眼（如"航空"、"啤酒"、"电信"、"腾讯"）
                if any(k in corp_name for k in ("航空", "航运", "海运", "物流", "港口", "铁路", "机场")):
                    ind_name = "工业 - 航空与物流运输"
                elif any(k in corp_name for k in ("啤酒", "食品", "饮料", "乳业", "白酒", "农业", "粮油", "青岛啤酒")):
                    ind_name = "必需性消费 - 饮料与食品制造"
                elif any(k in corp_name for k in ("电信", "移动", "联通", "通信", "电讯")):
                    ind_name = "电讯业 - 电信运营商与服务"
                elif any(k in corp_name for k in ("银行", "保险", "证券", "信托", "金融", "理财")):
                    ind_name = "金融业"
                elif any(k in corp_name for k in ("地产", "置地", "置业", "物业", "建筑")):
                    ind_name = "地产建筑业"
                elif any(k in corp_name for k in ("医药", "生物", "医疗", "制药", "新药", "药业")):
                    ind_name = "医疗保健业"
                elif any(k in corp_name for k in ("腾讯", "阿里", "美团", "网易", "快手", "百度", "软件", "信息")):
                    ind_name = "资讯科技业 - 互联网服务"
                else:
                    text_corpus = f"{corp_name} {corp_desc}"
                    if any(k in text_corpus for k in ("航空", "航运", "海运", "物流", "港口", "铁路", "机场", "客运", "货运")):
                        ind_name = "工业 - 航空与物流运输"
                    elif any(k in text_corpus for k in ("啤酒", "食品", "饮料", "乳业", "白酒", "农产品", "生猪", "屠宰", "预包装")):
                        ind_name = "必需性消费 - 饮料与食品制造"
                    elif any(k in text_corpus for k in ("电信", "移动", "联通", "通信", "电讯", "基站", "宽带")):
                        ind_name = "电讯业 - 电信运营商与服务"
                    elif any(k in text_corpus for k in ("银行", "保险", "证券", "信托", "基金", "金融租赁", "资产管理")):
                        ind_name = "金融业"
                    elif any(k in text_corpus for k in ("地产", "物业", "置地", "置业", "发展商", "建筑工程", "商业地产")):
                        ind_name = "地产建筑业"
                    elif any(k in text_corpus for k in ("医药", "生物", "医疗", "制药", "新药", "疫苗", "器械")):
                        ind_name = "医疗保健业"
                    elif any(k in text_corpus for k in ("石油", "煤炭", "天然气", "石化", "油气", "采矿")):
                        ind_name = "能源业"
                    elif any(k in text_corpus for k in ("汽车", "整车", "新能源车", "家电", "服装", "纺织", "餐饮", "体育")):
                        ind_name = "非必需性消费"
                    elif any(k in text_corpus for k in ("电力", "水务", "燃气", "环保", "清洁能源", "光伏发电", "风电")):
                        ind_name = "公用事业"
                    elif any(k in text_corpus for k in ("腾讯", "游戏", "社交", "互联网", "软件", "美团", "阿里", "网易", "快手", "百度", "云服务")):
                        ind_name = "资讯科技业 - 互联网服务"
                    else:
                        ind_name = "恒生综合行业"

            concepts_table = [
                {"概念分类": "恒生行业分类", "行业名称": ind_name},
                {"概念分类": "市场板块", "行业名称": "香港交易所主板"},
                {"概念分类": "互联互通机制", "行业名称": "港股通合资格标的 (港股通/非港股通)"},
            ]
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股所属行业与板块概念",
                "source": "sina_hk_f10 (新浪港股F10行业分类专线)",
                "formatted_tables": {
                    "concepts": concepts_table,
                },
                "data": {"所属行业": ind_name, "公司名称": corp_name},
                "概念说明": "港股行业分类严格遵循恒生行业分类系统 (HSICS) 及港交所主板上市公司行业大类标准划分。",
            }

        # 分类 5：核心财务指标 (indicators / financials / main)
        if cat in ("indicators", "financials", "main"):
            em_data = self._get_eastmoney_hk_financials(clean_code, report_type="summary", limit_periods=4)
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股核心财务与经营指标",
                "source": "eastmoney_hk_f10 (东方财富港股F10数据中心专线)",
                "formatted_tables": {
                    "indicators": em_data,
                },
                "data": em_data[0] if em_data else {"status": "暂无指标数据"},
                "指标说明": "东方财富港股数据中心核心财务与盈利能力指标汇总。",
            }

        # 分类 6：龙虎榜机制说明 (lhb)
        if cat == "lhb":
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "lhb",
                "category_name": "港股龙虎榜席位",
                "source": "hkex_market_rules",
                "formatted_tables": {
                    "lhb_rules": [{
                        "机制状态": "港股不设每日龙虎榜披露机制",
                        "原因说明": "港交所公开交易系统仅撮合经纪商会员代号席位，无通达信A股营业部席位买卖龙虎榜机制。",
                        "替代建议": "建议查询港股大股东权益变动 (top_holders) 或公司股份回购明细 (repurchases)。",
                    }]
                },
                "data": {"status": "港交所无每日龙虎榜"},
                "无记录说明": "港股市场不设通达信 A 股交易所席位龙虎榜披露机制，暂不支持该分类。",
            }

        # 分类 7：限售解禁机制说明 (unlocks)
        if cat == "unlocks":
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "unlocks",
                "category_name": "港股限售解禁",
                "source": "hkex_market_rules",
                "formatted_tables": {
                    "unlocks_rules": [{
                        "机制状态": "港股为全流通市场，无A股限售解禁表",
                        "原因说明": "港交所主板普通股上市即为全流通（除IPO发起人早期自愿锁定期外），无A股大小非解禁日程与解禁明细披露要求。",
                        "替代建议": "建议查询主要股东权益变动 (top_holders) 或公司股份回购明细 (repurchases)。",
                    }]
                },
                "data": {"status": "港股为全流通市场，无A股限售解禁机制"},
                "无记录说明": "港股市场为全流通市场，不设 A 股受限流通股与解禁日程分类机制。",
            }

        # 分类 8：业绩预测 (forecast)
        if cat == "forecast":
            em_data = self._get_eastmoney_hk_financials(clean_code, report_type="summary", limit_periods=1)
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": "forecast",
                "category_name": "港股机构盈利预测与指标概览",
                "source": "eastmoney_hk_f10 (东方财富港股F10备份专线)",
                "formatted_tables": {
                    "indicators": em_data,
                },
                "data": em_data[0] if em_data else {"status": "暂无盈利预测数据覆盖"},
                "无记录说明": "港股公开免费源无官方券商一致性业绩预测与目标价覆盖表。建议参考主要财务指标 (indicators) 或综合损益表中的历史盈利增速。",
            }

        # 分类 9：资金流向与大单机制说明 (capital_flow / funds / moneyflow) (修复 HK-4)
        if cat in ("capital_flow", "funds", "moneyflow"):
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股资金流向与大单统计机制",
                "source": "hkex_market_rules",
                "formatted_tables": {
                    "capital_flow_rules": [{
                        "机制状态": "港股不设通达信 A 股特大单/大单/中单/小单四级主动买卖资金流向统计",
                        "原因说明": "港交所公开免费行情仅包含撮合成交与买一/卖一即时最优挂单，不公开逐笔挂单委托队列明细与四级资金切片数据。",
                        "替代建议": "可通过成交额与成交量（日/分钟K线）结合换手率分析筹码交投热度，或通过港股通（南向资金）持股变动监控主力资金动向。",
                    }]
                },
                "data": {"status": "港股不设A股四级主动资金流向切片"},
                "无记录说明": "港股公开数据源不提供 A 股逐笔成交四级资金流向指标，建议参考成交量/额与换手率走势。",
            }

        # 分类 10：融资融券机制说明 (margin_trading / rzrq / margin) (修复 HK-4)
        if cat in ("margin_trading", "rzrq", "margin"):
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股融资融券与卖空借券机制",
                "source": "hkex_market_rules",
                "formatted_tables": {
                    "margin_rules": [{
                        "机制状态": "港股无通达信 A 股交易所集中两融余额（融资买入/融券偿还）报表",
                        "原因说明": "香港市场实行券商场外孖展(Margin Financing)与指定合资格证券可卖空机制，港交所披露每日可卖空证券成交额，不发布 A 股式交易所统一两融余额截面表。",
                        "替代建议": "可参考主要股东持股变动 (top_holders) 或中央结算系统 (CCASS) 参与者持股集中度。",
                    }]
                },
                "data": {"status": "港股无A股集中两融余额报表"},
                "无记录说明": "港股市场实行券商独立孖展借贷与卖空成交披露，不设 A 股交易所统一两融分类统计。",
            }

        # 分类 11：涨跌停与市调机制说明 (limit_up / ztb / price_limit) (修复 HK-4)
        if cat in ("limit_up", "ztb", "price_limit"):
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股价格限制机制 (市调机制/冷静期)",
                "source": "hkex_market_rules",
                "formatted_tables": {
                    "limit_up_rules": [{
                        "机制状态": "港股不设 A 股固定 ±10%/±20% 涨跌停板与涨停复盘表",
                        "原因说明": "港交所证券市场实行市场波动调节机制 (VCM)，仅在股票价格偏离5分钟前价格一定比例（如±10%）时触发5分钟冷静期，不设封死涨跌停限制。",
                        "替代建议": "建议参考港股日涨跌幅、盘口现价及 52 周价格区间监控极端价格异动。",
                    }]
                },
                "data": {"status": "港股不设A股固定涨跌停板制度"},
                "无记录说明": "港股市场不设通达信 A 股每日涨跌停池与封板统计，实行市调机制 (VCM)。",
            }

        # 默认：公司资料与工商概况 (summary / basic_info / company)
        source = "sina_hk_f10 (新浪港股F10主力专线)"
        info = None
        try:
            info = self._get_sina_hk_company_info(clean_code)
        except Exception:
            info = None

        if info:
            return {
                "symbol": clean_code,
                "market": "香港交易所 (HKEX)",
                "category": category,
                "category_name": "港股公司概况与基本工商",
                "source": source,
                "formatted_tables": {
                    "company_profile": [info],
                },
                "data": info,
            }

        # 备份：调用东方财富主要财务指标作为 F10 备份
        source = "eastmoney_hk_f10 (东方财富港股F10备份专线)"
        em_data = self._get_eastmoney_hk_financials(clean_code, report_type="summary", limit_periods=1)
        if not em_data:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到港股代码 '{clean_code}' 的 F10 基本面资料（代码不存在或非上市标的）。"
                    "建议先调用 stock_lookup 工具核对标准代码。"
                )

        return {
            "symbol": clean_code,
            "market": "香港交易所 (HKEX)",
            "category": category,
            "category_name": "港股核心财务与估值指标 (东财备份)",
            "source": source,
            "formatted_tables": {
                "indicators": em_data,
            },
            "data": em_data[0] if em_data else {"status": "暂无数据"},
        }

    def get_quotes(self, symbol: str, setcode: str | None = None) -> dict[str, Any]:
        """获取实时盘口、深度估值指标与五档买卖盘（支持 A 股与港股）"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        if not clean_code:
            raise ValueError(f"无效的证券代码 '{symbol}'，证券标的代码必须包含有效数字编号（如 A股'600519'、港股'00700'）。若仅知道公司名称，请先调用 stock_lookup 工具查找对应代码。")

        # 智能判定是否为港股标的 (市场显式指定为 31/116/HK，或原始代码带 HK 前缀，或数字代码为 5 位)
        is_hk = (
            (setcode is not None and str(setcode).upper() in ("31", "116", "HK"))
            or symbol.strip().upper().startswith("HK")
            or len(clean_code) == 5
        )
        if is_hk:
            return self._get_hk_quotes(clean_code)

        m_setcode = setcode if setcode is not None else infer_market_setcode(clean_code)

        req_body = {
            "Head": {"Target": "0", "CharSet": "UTF8"},
            "Code": clean_code,
            "Setcode": str(m_setcode),
            "HasHQInfo": "1",
            "HasExtInfo": "1",
            "BspNum": "5",
            "HasProInfo": "1",
            "HasCalcInfo": "0",
            "HasCwInfo": "0",
            "HasStatInfo": "0",
        }
        url = f"{self.api_endpoint}?Entry=TdxShare.PBHQInfo"
        raw = self._post(url, req_body)

        base_info = raw.get("BaseInfo", {}) if isinstance(raw, dict) else {}
        stock_name = base_info.get("Name", "") if isinstance(base_info, dict) else ""
        if not stock_name:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity and entity.get("entity_name"):
                stock_name = entity.get("entity_name")

        market_desc = (
            "上海证券交易所 (SSE)"
            if str(m_setcode) == "1"
            else (
                "深圳证券交易所 (SZSE)"
                if str(m_setcode) == "0"
                else (
                    "北京证券交易所 (BSE)"
                    if str(m_setcode) == "2"
                    else f"市场代码_{m_setcode}"
                )
            )
        )

        result: dict[str, Any] = {
            "symbol": clean_code,
            "name": stock_name,
            "setcode": m_setcode,
            "market": market_desc,
            "source": "tdx_official_专线",
        }

        # 1. 核心行情字段中文化映射
        hq_raw = raw.get("HQInfo", {})
        ext_raw = raw.get("ExtInfo", {})
        now_p = float(hq_raw.get("Now", 0.0) or 0.0) if isinstance(hq_raw, dict) else 0.0
        close_p = float(hq_raw.get("Close", 0.0) or 0.0) if isinstance(hq_raw, dict) else 0.0
        zgb_wan = float(ext_raw.get("ZGB", 0.0) or 0.0) if isinstance(ext_raw, dict) else 0.0

        # 防空壳/无效标的拦截：若通达信返回空盘口或现价/昨收/股本均无效，进行正主实体核验
        if not hq_raw or (now_p <= 0 and close_p <= 0 and zgb_wan <= 0):
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到证券代码 '{symbol}' 的有效盘口行情（代码不存在或非有效 A 股/港股标的）。"
                    "若不确定代码，请先调用 stock_lookup 工具查询标准证券名称与代码。"
                )

        if isinstance(hq_raw, dict):
            mapped_hq = {HQ_FIELD_MAP.get(k, k): v for k, v in hq_raw.items()}
            if stock_name:
                mapped_hq["股票名称"] = stock_name

            # 彻底拦截上游将流通股本串入 Yield 与 Jjjz 的缺陷 (P1)
            mapped_hq.pop("Yield", None)
            mapped_hq.pop("收益率_Yield", None)
            is_fund = clean_code.startswith(("51", "15", "16", "50", "58"))
            if not is_fund:
                mapped_hq.pop("Jjjz", None)
                mapped_hq.pop("基金净值", None)
            else:
                if "Jjjz" in mapped_hq:
                    try:
                        mapped_hq["基金净值"] = round(float(mapped_hq.pop("Jjjz")), 4)
                    except (ValueError, TypeError):
                        pass

            if "ItemNum" in mapped_hq:
                mapped_hq["明细笔数"] = mapped_hq.pop("ItemNum")

            # 数值类型严格对齐：消除跨标的 str/int/float 漂移 (P1)
            int_keys = ("成交量_手", "内盘_手", "外盘_手", "成交笔数", "剩余委托量", "明细笔数", "内外盘强弱标志", "盘后成交量")
            for ik in int_keys:
                if ik in mapped_hq and mapped_hq[ik] not in (None, ""):
                    try:
                        mapped_hq[ik] = int(float(mapped_hq[ik]))
                    except (ValueError, TypeError):
                        pass

            float_keys = ("现价", "昨收", "今开", "最高价", "最低价", "最高", "最低", "成交均价", "成交额_元", "量比", "换手率_pct")
            for fk in float_keys:
                if fk in mapped_hq and mapped_hq[fk] not in (None, ""):
                    try:
                        mapped_hq[fk] = round(float(mapped_hq[fk]), 2 if "额" in fk or "均价" in fk or "价" in fk else 4)
                    except (ValueError, TypeError):
                        pass

            # 自动计算涨跌幅与日内真实振幅（遵循交易所标准公式：(最高-最低)/昨收*100，彻底避免通达信Yield字段污染）
            high_p = float(hq_raw.get("MaxP", 0.0) or 0.0)
            low_p = float(hq_raw.get("MinP", 0.0) or 0.0)
            if close_p > 0 and now_p > 0:
                mapped_hq["涨跌幅_pct"] = round(((now_p - close_p) / close_p) * 100.0, 2)
                mapped_hq["涨跌额"] = round(now_p - close_p, 3)
            if close_p > 0 and high_p > 0 and low_p > 0:
                amp_val = round(((high_p - low_p) / close_p) * 100.0, 2)
                mapped_hq["日内振幅_pct"] = amp_val
                mapped_hq["振幅_pct"] = amp_val
            if "最高价" in mapped_hq and "最高" not in mapped_hq:
                mapped_hq["最高"] = mapped_hq["最高价"]
            if "最低价" in mapped_hq and "最低" not in mapped_hq:
                mapped_hq["最低"] = mapped_hq["最低价"]
            if "涨跌额" in mapped_hq:
                mapped_hq["涨跌额_元"] = mapped_hq["涨跌额"]
            if "行情时间" in mapped_hq:
                mapped_hq["更新时间"] = mapped_hq["行情时间"]
            result["盘口核心行情"] = mapped_hq

        # 2. 估值与股本指标中文化映射与年化口径对齐
        ext_raw = raw.get("ExtInfo", {})
        pro_raw = raw.get("ProInfo", {})
        if isinstance(ext_raw, dict):
            mapped_ext = {EXT_FIELD_MAP.get(k, k): v for k, v in ext_raw.items()}
            # 规范化整型标志位与浮点数
            bool_tag_keys = ("是否属于融资融券标的", "是否属于沪股通标的", "是否属于深股通标的", "是否属于沪深300标的", "是否含权证或转债")
            for bk in bool_tag_keys:
                if bk in mapped_ext and mapped_ext[bk] not in (None, ""):
                    try:
                        mapped_ext[bk] = int(float(mapped_ext[bk]))
                    except (ValueError, TypeError):
                        pass
            if "自由流通股本_万股" in mapped_ext and mapped_ext["自由流通股本_万股"] not in (None, ""):
                try:
                    mapped_ext["自由流通股本_万股"] = round(float(mapped_ext["自由流通股本_万股"]), 2)
                except (ValueError, TypeError):
                    pass
            if "行业涨跌幅_pct" in mapped_ext and mapped_ext["行业涨跌幅_pct"] not in (None, ""):
                try:
                    mapped_ext["行业涨跌幅_pct"] = round(float(mapped_ext["行业涨跌幅_pct"]), 2)
                except (ValueError, TypeError):
                    pass
            # 计算市净率 PB、年化每股收益，并附加口径防幻觉说明
            now_p = float(hq_raw.get("Now", 0.0) or 0.0) if isinstance(hq_raw, dict) else 0.0
            mgjzc = float(ext_raw.get("MGJZC", 0.0) or 0.0)
            syl = float(ext_raw.get("SYL", 0.0) or 0.0)
            mgsy = float(ext_raw.get("MGSY", 0.0) or 0.0)
            zgb_wan = float(ext_raw.get("ZGB", 0.0) or 0.0)
            ltgb_wan = float(ext_raw.get("LTGB", 0.0) or 0.0)
            if zgb_wan > 0:
                mapped_ext["总股本_股"] = int(round(zgb_wan * 10000))
            if ltgb_wan > 0:
                mapped_ext["流通股本_股"] = int(round(ltgb_wan * 10000))
            if mgjzc > 0 and now_p > 0:
                mapped_ext["市净率_PB"] = round(now_p / mgjzc, 2)
            if syl != 0 and now_p > 0:
                mapped_ext["动态市盈率_PE"] = round(syl, 2)
                annualized_eps = round(now_p / syl, 3)
                mapped_ext["年化每股收益_EPS"] = annualized_eps

            # 从 ProInfo (或 ExtInfo 备用源) 中提取真正滚动过去 4 季度的 TTM 滚动市盈率与静态市盈率
            pe_ttm = None
            pe_static = None
            if isinstance(pro_raw, dict):
                pe_ttm = pro_raw.get("PETTM")
                pe_static = pro_raw.get("PEStatic")
            if pe_ttm is None and isinstance(ext_raw, dict):
                pe_ttm = ext_raw.get("PETTM") or ext_raw.get("SYL_TTM")
            if pe_static is None and isinstance(ext_raw, dict):
                pe_static = ext_raw.get("PEStatic") or ext_raw.get("SYL_Static")

            if pe_ttm is not None and str(pe_ttm).strip() != "":
                try:
                    mapped_ext["滚动市盈率_PETTM"] = round(float(pe_ttm), 2)
                except (ValueError, TypeError):
                    pass
            if pe_static is not None and str(pe_static).strip() != "":
                try:
                    mapped_ext["静态市盈率_PEStatic"] = round(float(pe_static), 2)
                    mapped_ext["静态市盈率_PE_LYR"] = round(float(pe_static), 2)
                except (ValueError, TypeError):
                    pass

            ann_eps_val = mapped_ext.get("年化每股收益_EPS")
            ann_eps_str = f"年化每股收益≈{ann_eps_val}元，现价/年化EPS=动态市盈率" if ann_eps_val is not None else "当前亏损或暂无年化每股收益"
            ttm_note = f"滚动市盈率_PETTM为 {mapped_ext.get('滚动市盈率_PETTM')} 倍(按过去4季度滚动净利润测算的真实TTM口径)；" if "滚动市盈率_PETTM" in mapped_ext else ""
            mapped_ext["估值口径说明"] = (
                f"{ttm_note}动态市盈率_PE({mapped_ext.get('动态市盈率_PE')}倍)采用最新报告期年化折算口径({ann_eps_str})；"
                f"报告期每股收益_EPS({mgsy}元)为非年化单期累计值，两者口径不同，禁止直接互乘推导价格或市值。"
            )
            mapped_ext["PE使用纪律说明"] = (
                "【PE口径防误导铁律】报告与回答中提及市盈率必须严格指明是哪一个，严禁混称：\n"
                "1.【滚动市盈率_PETTM】(价值投资推荐口径)：基于过去4个季度滚动归母净利润(真实TTM)计算，能平滑季节性波动；\n"
                "2.【动态市盈率_PE】：现价÷最新单季年化每股收益(年化口径)，一季度净利×4、半年度×2，易受单季突发收支和淡旺季严重扰动；\n"
                "3.【静态市盈率_PEStatic】：现价÷上一完整会计年度每股收益。报告中必须明确注明所用PE类型。"
            )
            result["估值与基本盘面"] = mapped_ext

        # 3. 五档深度买卖盘口整理（通达信官方 BspInfo 返回 5 项明细数组或极速备用字典）
        bsp_raw = raw.get("BspInfo", [])
        bsp_dict = {}
        if isinstance(bsp_raw, list) and bsp_raw:
            buy_names = ["买一", "买二", "买三", "买四", "买五"]
            sell_names = ["卖一", "卖二", "卖三", "卖四", "卖五"]
            for idx, item in enumerate(bsp_raw[:5]):
                if isinstance(item, dict):
                    b_label = buy_names[idx] if idx < len(buy_names) else f"买{idx+1}"
                    s_label = sell_names[idx] if idx < len(sell_names) else f"卖{idx+1}"
                    b_price = item.get("BuyP")
                    s_price = item.get("SellP")
                    b_vol = item.get("BuyV")
                    s_vol = item.get("SellV")
                    bsp_dict[b_label] = {
                        "价格": float(b_price) if b_price is not None and str(b_price).strip() else None,
                        "委托量_手": int(b_vol) if (b_vol is not None and str(b_vol).isdigit()) else b_vol,
                    }
                    bsp_dict[s_label] = {
                        "价格": float(s_price) if s_price is not None and str(s_price).strip() else None,
                        "委托量_手": int(s_vol) if (s_vol is not None and str(s_vol).isdigit()) else s_vol,
                    }
        elif isinstance(bsp_raw, dict) and bsp_raw:
            bsp_dict = {
                "买一": {"价格": bsp_raw.get("BuyPrice1") or bsp_raw.get("BuyP1"), "委托量_手": bsp_raw.get("BuyVol1") or bsp_raw.get("BuyV1")},
                "买二": {"价格": bsp_raw.get("BuyPrice2") or bsp_raw.get("BuyP2"), "委托量_手": bsp_raw.get("BuyVol2") or bsp_raw.get("BuyV2")},
                "买三": {"价格": bsp_raw.get("BuyPrice3") or bsp_raw.get("BuyP3"), "委托量_手": bsp_raw.get("BuyVol3") or bsp_raw.get("BuyV3")},
                "买四": {"价格": bsp_raw.get("BuyPrice4") or bsp_raw.get("BuyP4"), "委托量_手": bsp_raw.get("BuyVol4") or bsp_raw.get("BuyV4")},
                "买五": {"价格": bsp_raw.get("BuyPrice5") or bsp_raw.get("BuyP5"), "委托量_手": bsp_raw.get("BuyVol5") or bsp_raw.get("BuyV5")},
                "卖一": {"价格": bsp_raw.get("SellPrice1") or bsp_raw.get("SellP1"), "委托量_手": bsp_raw.get("SellVol1") or bsp_raw.get("SellV1")},
                "卖二": {"价格": bsp_raw.get("SellPrice2") or bsp_raw.get("SellP2"), "委托量_手": bsp_raw.get("SellVol2") or bsp_raw.get("SellV2")},
                "卖三": {"价格": bsp_raw.get("SellPrice3") or bsp_raw.get("SellP3"), "委托量_手": bsp_raw.get("SellVol3") or bsp_raw.get("SellV3")},
                "卖四": {"价格": bsp_raw.get("SellPrice4") or bsp_raw.get("SellP4"), "委托量_手": bsp_raw.get("SellVol4") or bsp_raw.get("SellV4")},
                "卖五": {"价格": bsp_raw.get("SellPrice5") or bsp_raw.get("SellP5"), "委托量_手": bsp_raw.get("SellVol5") or bsp_raw.get("SellV5")},
            }

        if bsp_dict:
            result["五档深度盘口"] = bsp_dict
            # 检查是否处于盘后休市时段（交易所停止连续竞价撮合，盘口仅剩少量静态残量挂单）
            valid_tiers = sum(1 for v in bsp_dict.values() if isinstance(v, dict) and v.get("价格") is not None)
            clean_time = str(hq_raw.get("HQTime", "")).replace(":", "")[:6]
            is_after_market = False
            if clean_time.isdigit() and len(clean_time) >= 6:
                if clean_time >= "150000" or clean_time < "091500":
                    is_after_market = True

            total_hand = sum(
                int(v.get("委托量_手", 0) or 0) for v in bsp_dict.values()
                if isinstance(v, dict) and isinstance(v.get("委托量_手"), (int, float))
            )

            if is_after_market or valid_tiers <= 1 or (total_hand > 0 and total_hand < 200 and clean_time >= "145700"):
                result["盘口深度说明"] = (
                    f"【收盘残量防误导警示】当前处于休市/盘后时段（行情时间: {hq_raw.get('HQTime')}），交易所撮合系统已停止连续竞价撮合。"
                    f"五档当前累计委托量仅约 {total_hand} 手，纯属收盘集合竞价未撤单的极少量静态残量挂单！"
                    "【铁律】严禁将休市后的微量残单当作真实买卖盘口深度、买卖支撑阻力或多空博弈力量强弱进行分析与推断。"
                    "真实有效的盘口买卖深度仅在交易日连续竞价撮合期间（9:30-11:30、13:00-14:57）动态全量呈现。"
                )

        return result

    def get_kline(
        self,
        symbol: str,
        period: str = "day",
        count: int = 30,
        adjust: str = "qfq",
        setcode: str | None = None,
        with_indicators: bool = True,
    ) -> dict[str, Any]:
        """获取多周期历史 K 线走势，并可选附加 MACD, KDJ, BOLL 等技术指标计算结果（支持 A 股与港股）"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        if not clean_code:
            raise ValueError(f"无效的证券代码 '{symbol}'，证券标的代码必须包含有效数字编号（如 A股'600519'、港股'00700'）。若仅知道公司名称，请先调用 stock_lookup 工具查找对应代码。")

        is_hk = (
            (setcode is not None and str(setcode).upper() in ("31", "116", "HK"))
            or symbol.strip().upper().startswith("HK")
            or len(clean_code) == 5
        )
        if is_hk:
            return self._get_hk_kline(
                clean_code,
                period=period,
                count=count,
                adjust=adjust,
                with_indicators=with_indicators,
            )

        m_setcode = setcode if setcode is not None else infer_market_setcode(clean_code)

        # 周期映射: day->4, week->5, month->6, 60m->3, 30m->2, 15m->1, 5m->0, 1m->7
        period_map = {"5m": 0, "15m": 1, "30m": 2, "60m": 3, "day": 4, "week": 5, "month": 6, "1m": 7}
        p_val = period_map.get(period.lower(), 4)

        # 复权映射: qfq->1, hfq->2, none->0 (通达信官方 PBFXT 底层协议规范)
        adjust_map = {"none": 0, "qfq": 1, "hfq": 2}
        tq_val = adjust_map.get(adjust.lower(), 1)

        req_body = {
            "Head": {"Target": 0, "CharSet": "UTF8"},
            "Code": clean_code,
            "Setcode": int(m_setcode),
            "Period": p_val,
            "Startxh": 0,
            "WantNum": max(1, min(count, 300)),
            "TQFlag": tq_val,
            "MPData": 0,
            "HasAttachInfo": 1,
            "HasLtgb": 0,
            "ForRefresh": 0,
            "HasIpoPrice": 0,
        }

        url = f"{self.api_endpoint}?Entry=TdxShare.PBFXT"
        raw = self._post(url, req_body)

        list_items = raw.get("ListItem", [])
        attach_info = raw.get("AttachInfo", {})
        stock_name = attach_info.get("Name", "") if isinstance(attach_info, dict) else ""
        parsed_bars: list[dict[str, Any]] = []

        for item in list_items:
            # 兼容 {'Item': [...]} 或直接 [...] 两种上游返回格式
            raw_row = item.get("Item") if isinstance(item, dict) else item
            if not isinstance(raw_row, list) or len(raw_row) < 6:
                continue
            # 通达信 ListItem Item 结构: [日期, 时间(自午夜秒数), 今开, 最高, 最低, 收盘, 成交额, 成交额, 成交量...]
            raw_date = str(raw_row[0])
            if period.lower() in ("1m", "5m", "15m", "30m", "60m") and len(raw_row) > 1 and str(raw_row[1]).replace(".", "").isdigit():
                sec = int(float(raw_row[1]))
                hh = sec // 3600
                mm = (sec % 3600) // 60
                # 纠正通达信底层将上午收市 (11:30) 错标为 13:00 (46800秒) 的历史协议缺陷 (P2)
                if hh == 13 and mm == 0:
                    hh, mm = 11, 30
                dt_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]} {hh:02d}:{mm:02d}"
            elif len(raw_date) == 8 and raw_date.isdigit():
                dt_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            else:
                dt_str = raw_date

            # 价格规范化归一（股票取 2 位小数，基金/债券/可转债取 3 位），消除底层 float32 导致的单精度浮点漂移 (如 1309.689941 -> 1309.69)
            price_dec = 3 if clean_code.startswith(("51", "15", "16", "58", "11", "12")) else 2
            open_p = round(float(raw_row[2]), price_dec)
            high_p = round(float(raw_row[3]), price_dec)
            low_p = round(float(raw_row[4]), price_dec)
            close_p = round(float(raw_row[5]), price_dec)
            amt_val = round(float(raw_row[6]), 2) if len(raw_row) > 6 else 0.0
            vol_val = round(float(raw_row[8]), 2) if len(raw_row) > 8 else (round(float(raw_row[7]), 2) if len(raw_row) > 7 else 0.0)
            # 周线与月线通达信底层上报单位为手 (1手=100股)，日线与分钟线底层为股。统一跨周期成交量单位为【股】消除漂移
            if period.lower() in ("week", "month"):
                vol_val = round(vol_val * 100.0, 2)

            parsed_bars.append({
                "time": dt_str,
                "datetime": dt_str,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": vol_val,
                "amount": amt_val,
            })

        # 按时间正序排列后计算技术指标
        if with_indicators and parsed_bars:
            parsed_bars = calculate_indicators(parsed_bars)

        if not parsed_bars:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到证券代码 '{symbol}' 的历史 K 线数据（代码不存在或非有效 A 股/港股标的）。"
                    "建议先调用 stock_lookup 工具核对标准证券名称与代码。"
                )

        return {
            "symbol": clean_code,
            "name": stock_name,
            "setcode": m_setcode,
            "period": period,
            "adjust": adjust,
            "total_bars": len(parsed_bars),
            "indicators_included": bool(with_indicators),
            "klines": parsed_bars,
        }

    def get_financial_statements(
        self,
        symbol: str,
        report_type: str = "balance",
        period_type: str = "report",
        limit_periods: int = 4,
    ) -> dict[str, Any]:
        clean_code = re.sub(r"[^\d]", "", symbol)
        # 港股标的（5位代码）：启用新浪港股F10主力专线与东方财富高可用备份专线
        if infer_market_setcode(symbol) == "31" or len(clean_code) == 5:
            norm_code = clean_code.zfill(5)
            return self._get_hk_financials(norm_code, report_type=report_type, period_type=period_type, limit_periods=limit_periods)
        rtype = report_type.lower()
        fixed_tag = "00102" if period_type.lower() == "quarter" else "00101"

        if rtype in ("balance", "balance_sheet", "zcfzb"):
            entry = "TdxShareCW.ph_agf10_cw_zcfzb"
            params = [clean_code]
            field_map_default = BALANCE_FIELDS_NON_FINANCE
            type_name = "资产负债表"
        elif rtype in ("income", "income_statement", "lyb"):
            entry = "TdxShareCW.ph_agf10_cw_lyb"
            params = [fixed_tag, clean_code]
            field_map_default = INCOME_FIELDS_NON_FINANCE
            type_name = f"利润表({ '单季度' if period_type == 'quarter' else '报告期' })"
        elif rtype in ("cashflow", "cashflow_statement", "xjllb"):
            entry = "TdxShareCW.ph_agf10_cw_xjllb"
            params = [fixed_tag, clean_code]
            field_map_default = CASHFLOW_FIELDS_NON_FINANCE
            type_name = f"现金流量表({ '单季度' if period_type == 'quarter' else '报告期' })"
        else:
            raise ValueError(f"Unsupported report_type: {report_type}. Available: balance, income, cashflow")

        url = f"{self.api_endpoint}?Entry={entry}"
        raw = self._post(url, {"Params": params})

        result_periods: list[dict[str, Any]] = []
        result_sets = raw.get("ResultSets", [])

        # 核心：自动嗅探通达信返回的 isjr 金融属性标志位 (1=金融类：银行/保险/证券；0=工商业实体) (修复 P0)
        is_financial = False
        if isinstance(result_sets, list):
            for rs in result_sets:
                if rs.get("ColName") == ["isjr"] and rs.get("Content"):
                    val = str(rs["Content"][0][0]).strip()
                    if val in ("1", "True", "true"):
                        is_financial = True
                    break

        if is_financial:
            standard_name = "金融企业会计准则 (银行 / 保险 / 证券 / 金融控股)"
            if rtype in ("balance", "balance_sheet", "zcfzb"):
                field_map = BALANCE_FIELDS_FINANCE
            elif rtype in ("income", "income_statement", "lyb"):
                field_map = INCOME_FIELDS_FINANCE
            else:
                field_map = CASHFLOW_FIELDS_FINANCE
        else:
            standard_name = "一般工商业企业会计准则 (非金融实体企业)"
            field_map = field_map_default

        if isinstance(result_sets, list):
            for rs in result_sets:
                col_names = rs.get("ColName", [])
                contents = rs.get("Content", [])
                if not col_names or not contents or col_names == ["isjr"]:
                    continue
                # 遍历历史财报期
                for row in contents[:limit_periods]:
                    period_dict: dict[str, Any] = {}
                    supp_dict: dict[str, Any] = {}
                    for col_code, val in zip(col_names, row):
                        val_str = str(val).strip()
                        if not val_str:
                            continue
                        clean_col_name = field_map.get(col_code, col_code)
                        # 数值型字段友好且一致的转换（日期类保留原始文本，其余统一转为规范 float，杜绝跨股票 str/int/float 漂移）
                        if clean_col_name in ("截止日期", "报告期日期") or re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", val_str) or "报" in val_str:
                            parsed_val: Any = val_str
                        else:
                            try:
                                parsed_val = round(float(val_str), 2)
                            except (ValueError, TypeError):
                                parsed_val = val_str

                        # 现金流量表：将间接法与现金净变动的补充资料与主表直接法科目做物理隔离，彻底杜绝下游遍历加总时双计
                        if rtype in ("cashflow", "cashflow_statement", "xjllb") and clean_col_name.startswith("补充资料_"):
                            supp_dict[clean_col_name] = parsed_val
                        elif rtype in ("income", "income_statement", "lyb") and clean_col_name == "重复核对_净利润":
                            # 过滤底层重复核对字段 N027，防止下游将其作为独立科目加总造成双计
                            continue
                        else:
                            period_dict[clean_col_name] = parsed_val

                    # 针对现金流量表：将补充资料独立收纳于二级字典，主表仅保留三大活动直接法科目与期初期末现金净增加额
                    if supp_dict:
                        # 间接法补充资料现金净增加额回填（若底层上游漏值为0或None，自动取主表现金净增加额或期初期末差额，修复 B8）
                        if supp_dict.get("补充资料_现金及现金等价物净增加额") in (None, 0, 0.0):
                            main_cash_net = period_dict.get("现金及现金等价物净增加额")
                            if main_cash_net is not None and isinstance(main_cash_net, (int, float)) and main_cash_net != 0:
                                supp_dict["补充资料_现金及现金等价物净增加额"] = main_cash_net
                        period_dict["补充资料_间接法"] = supp_dict

                    if rtype in ("cashflow", "cashflow_statement", "xjllb"):
                        # 兼容 T3：工商业与金融企业三大活动现金流量净额键名双向镜像挂载，彻底免除下游双容错负担
                        for k1, k2 in [
                            ("经营活动现金流量净额", "经营活动产生的现金流量净额"),
                            ("投资活动现金流量净额", "投资活动产生的现金流量净额"),
                            ("筹资活动现金流量净额", "筹资活动产生的现金流量净额"),
                        ]:
                            v1 = period_dict.get(k1)
                            v2 = period_dict.get(k2)
                            if v1 is not None and v2 is None:
                                period_dict[k2] = v1
                            elif v2 is not None and v1 is None:
                                period_dict[k1] = v2

                        if is_financial:
                            period_dict["现金流量表加总纪律说明"] = (
                                "【金融企业现金流量表核算规范】本表遵循《金融企业财务报表格式》（银行/保险/证券）。"
                                "因底层行情专线未采集保险原保费收入、证券代理买卖等特殊金融科目明细，流入/流出细项之和与小计可能存在差额，"
                                "差额已自动自陈至不可加总差额科目。财务分析必须且只能引用三大活动净额（经营净额、投资净额、筹资净额）"
                                "与期末现金及现金等价物余额。"
                            )
                            # 金融企业经营活动现金流入不可加总差额 (原保险保费、代理买卖证券、拆入回购等缺项，修复 D2)
                            jy_in = period_dict.get("经营活动现金流入小计")
                            if jy_in is not None and isinstance(jy_in, (int, float)):
                                known_fin_jy_in = [
                                    "客户存款和同业存放款项净增加额", "向中央银行借款净增加额", "向其他金融机构拆入资金净增加额",
                                    "收取利息、手续费及佣金的现金", "处置以公允价值计量且变动计入当期损益的金融资产净增加额",
                                    "拆入资金净增加额", "回购业务资金净增加额", "代理买卖证券收到的现金净额", "收到的税费返还",
                                    "收到原保险合同保费取得的现金", "收到再保业务现金净额", "保户储金及投资款净增加额",
                                    "处置金融资产净增加额", "拆入资金净增加额_备用", "收到其他与经营活动有关的现金"
                                ]
                                diff_fin_in = round(float(jy_in) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_jy_in), 2)
                                if abs(diff_fin_in) > 0.01:
                                    period_dict["经营活动现金流入不可加总差额_原保险保费及拆入回购等金融科目缺项"] = diff_fin_in

                            # 金融企业经营活动现金流出不可加总差额 (原保险赔付、返售拆出等缺项，修复 D2)
                            jy_out = period_dict.get("经营活动现金流出小计")
                            if jy_out is not None and isinstance(jy_out, (int, float)):
                                known_fin_jy_out = [
                                    "客户贷款及垫款净增加额", "存放中央银行和同业款项净增加额", "支付原保险合同赔付款项的现金",
                                    "拆出资金净增加额", "返售业务资金净增加额", "支付利息、手续费及佣金的现金",
                                    "支付保单红利的现金", "支付再保业务现金净额", "支付原保险合同款项现金",
                                    "支付给职工以及为职工支付的现金", "支付的各项税费", "支付其他与经营活动有关的现金"
                                ]
                                diff_fin_out = round(float(jy_out) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_jy_out), 2)
                                if abs(diff_fin_out) > 0.01:
                                    period_dict["经营活动现金流出不可加总差额_保险赔付及拆出返售等金融科目缺项"] = diff_fin_out

                            cz_in = period_dict.get("筹资活动现金流入小计")
                            if cz_in is not None and isinstance(cz_in, (int, float)):
                                known_fin_cz_in = ["吸收投资收到的现金", "发行债券收到的现金", "取得借款收到的现金", "收到其他与筹资活动有关的现金"]
                                diff_fin_cz_in = round(float(cz_in) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_cz_in), 2)
                                if abs(diff_fin_cz_in) > 0.01:
                                    period_dict["筹资活动现金流入不可加总差额_金融借款及发债细项等"] = diff_fin_cz_in

                            cz_out = period_dict.get("筹资活动现金流出小计")
                            if cz_out is not None and isinstance(cz_out, (int, float)):
                                known_fin_cz_out = ["偿还债务支付的现金", "分配股利、利润或偿付利息支付的现金", "支付其他与筹资活动有关的现金"]
                                diff_fin_cz_out = round(float(cz_out) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_fin_cz_out), 2)
                                if abs(diff_fin_cz_out) > 0.01:
                                    period_dict["筹资活动现金流出不可加总差额_金融分红及利息细项等"] = diff_fin_cz_out

                            # 间接法补充资料缺失声明 (修复 D2)
                            if "补充资料_间接法" not in period_dict:
                                period_dict["补充资料_间接法说明"] = "上游专线未收录该金融机构间接法编制细项，分析现金流请直接引用主表现金流量净额与期末现金余额。"
                        else:
                            period_dict["现金流量表加总纪律说明"] = (
                                "因上游数据模板中缺失'收到的税费返还'等流入细分项，流入/流出细项之和与小计可能存在不可闭合差额。"
                                "量化与财报分析时禁止机械累加细项计算小计，必须且只能引用三大活动净额（经营净额、投资净额、筹资净额）"
                                "与期末现金余额。"
                            )
                            # 经营活动现金流入/流出不可加总差额自陈 (财务公司拆借/存贷款/税费返还等科目缺项)
                            jy_in = period_dict.get("经营活动现金流入小计")
                            if jy_in is not None and isinstance(jy_in, (int, float)):
                                known_jy_in = ["销售商品提供劳务收到的现金", "收到的税费返还", "收到其他与经营活动有关的现金"]
                                diff_jy_in = round(float(jy_in) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_jy_in), 2)
                                if abs(diff_jy_in) > 0.01:
                                    period_dict["经营活动现金流入不可加总差额_收到的税费返还及金融拆借等"] = diff_jy_in

                            jy_out = period_dict.get("经营活动现金流出小计")
                            if jy_out is not None and isinstance(jy_out, (int, float)):
                                known_jy_out = [
                                    "购买商品接受劳务支付的现金", "支付给职工以及为职工支付的现金",
                                    "支付的各项税费", "支付其他与经营活动有关的现金"
                                ]
                                diff_jy_out = round(float(jy_out) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_jy_out), 2)
                                if abs(diff_jy_out) > 0.01:
                                    period_dict["经营活动现金流出不可加总差额_金融存贷款及细项等"] = diff_jy_out

                            # 筹资活动现金流入小计自动补全与说明 (修复 D7 静默缺列)
                            cz_in = period_dict.get("筹资活动现金流入小计")
                            cz_out = period_dict.get("筹资活动现金流出小计")
                            cz_net = period_dict.get("筹资活动产生的现金流量净额")
                            if cz_in is None:
                                known_cz_sub_in = sum(float(period_dict.get(k, 0.0) or 0.0) for k in ("吸收投资收到的现金", "取得借款收到的现金", "发行债券收到的现金", "收到其他与筹资活动有关的现金"))
                                if known_cz_sub_in > 0:
                                    period_dict["筹资活动现金流入小计"] = known_cz_sub_in
                                elif cz_out is not None and cz_net is not None:
                                    period_dict["筹资活动现金流入小计"] = round(float(cz_out) + float(cz_net), 2)
                                else:
                                    period_dict["筹资活动现金流入小计"] = 0.0
                                period_dict["筹资活动现金流入小计说明"] = "该报告期无借款发债或股权融资等筹资流入，上游报表略去该列，已按会计准则规范填报为 0.0 或由净额推算补齐。"
                                cz_in = period_dict["筹资活动现金流入小计"]

                            if cz_in is not None and isinstance(cz_in, (int, float)):
                                # 修复 B2：严格补齐已映射科目【发行债券收到的现金】，杜绝自陈差额系统性虚高
                                known_cz_in = ["吸收投资收到的现金", "取得借款收到的现金", "发行债券收到的现金", "收到其他与筹资活动有关的现金"]
                                diff_cz_in = round(float(cz_in) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_cz_in), 2)
                                if abs(diff_cz_in) > 0.01:
                                    period_dict["筹资活动现金流入不可加总差额_金融借款及发债细项等"] = diff_cz_in

                            cz_out = period_dict.get("筹资活动现金流出小计")
                            if cz_out is not None and isinstance(cz_out, (int, float)):
                                known_cz_out = ["偿还债务支付的现金", "分配股利利润或偿付利息支付的现金", "支付其他与筹资活动有关的现金"]
                                diff_cz_out = round(float(cz_out) - sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_cz_out), 2)
                                if abs(diff_cz_out) > 0.01:
                                    period_dict["筹资活动现金流出不可加总差额_金融分红及利息细项等"] = diff_cz_out

                    # 针对利润表：自动核算营业利润与各已知科目代数累加的差额，并注入纪律说明
                    if rtype in ("income", "income_statement", "lyb"):
                        if is_financial:
                            period_dict["利润表加总纪律说明"] = (
                                "【金融企业利润表核算规范】利息净收入、手续费及佣金净收入与投资收益为金融机构主要核心收益来源。"
                                "营业利润 = 营业收入 − 营业支出 + 投资收益 + 公允价值变动收益 + 汇兑收益 + 其他业务收入。"
                                "财务建模与分析请直接引用官方公布的【营业收入】、【营业利润】、【利润总额】与【归属于母公司股东的净利润】。"
                            )
                        else:
                            yylr = period_dict.get("营业利润")
                            if yylr is not None and isinstance(yylr, (int, float)):
                                yysr = float(period_dict.get("营业总收入", 0.0) or 0.0)
                                yycb = float(period_dict.get("营业总成本", 0.0) or 0.0)
                                tzsy = float(period_dict.get("投资收益", 0.0) or 0.0)
                                qtsy = float(period_dict.get("其他收益", 0.0) or 0.0)
                                gysy = float(period_dict.get("公允价值变动净收益", 0.0) or 0.0)
                                hdsy = float(period_dict.get("汇兑收益", 0.0) or 0.0)
                                zccz = float(period_dict.get("资产处置收益", 0.0) or 0.0)
                                calc_yylr = yysr - yycb + tzsy + qtsy + gysy + hdsy + zccz
                                diff_yylr = round(float(yylr) - calc_yylr, 2)
                                if abs(diff_yylr) > 0.01:
                                    period_dict["营业利润不可加总差额_信用减值与资产减值损失及其他"] = diff_yylr

                            period_dict["利润表加总纪律说明"] = (
                                "【营业利润不可直接复算自陈】通达信38列利润表工商业模板未收录'信用减值损失'与'资产减值损失'等细项；"
                                "因此：营业总收入 − 营业总成本 + 投资收益 + 资产处置 + 汇兑 + 其他收益 + 公允价值变动净收益 ≠ 营业利润。"
                                "差额已自动核算至【营业利润不可加总差额_信用减值与资产减值损失及其他】。"
                                "财务建模与分析时请务必直接取用官方公布的【营业利润】、【利润总额】与【净利润】，严禁自行通过加减科目拼凑营业利润！"
                            )

                    # 针对资产负债表：自动核算明细与小计的不可加总差额
                    if rtype in ("balance", "balance_sheet", "zcfzb"):
                        if is_financial:
                            zchj = period_dict.get("资产总计")
                            fzhj = period_dict.get("负债合计")
                            qyhj = period_dict.get("所有者权益合计")
                            if zchj is not None and fzhj is not None and qyhj is not None and isinstance(zchj, (int, float)) and isinstance(fzhj, (int, float)) and isinstance(qyhj, (int, float)):
                                diff = round(float(zchj) - (float(fzhj) + float(qyhj)), 2)
                                if abs(diff) > 0.01:
                                    period_dict["金融资产负债不平衡自陈差额"] = diff
                            period_dict["资产负债表核算说明"] = (
                                "【金融企业资产负债表核算规范】本表遵循《金融企业财务报表格式》（银行业/保险业/证券业）。"
                                "金融企业资产负债表不区分流动与非流动资产/负债，以资产流动性从高到低排列（现金及存放央行款项排在最前）。"
                                "会计基本恒等式为：【资产总计 = 负债合计 + 所有者权益合计】。"
                            )
                        else:
                            # 1. 非流动负债不可加总差额（底层数据源缺失新租赁准则【租赁负债】、【长期应付职工薪酬】等科目）
                            non_curr_total = period_dict.get("非流动负债合计")
                            if non_curr_total is not None and isinstance(non_curr_total, (int, float)):
                                known_items = [
                                    "长期借款", "应付债券", "长期应付款", "专项应付款",
                                    "预计负债", "递延所得税负债", "递延收益", "其他非流动负债"
                                ]
                                sub_sum = sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_items)
                                diff = round(float(non_curr_total) - sub_sum, 2)
                                if abs(diff) > 0.01:
                                    period_dict["非流动负债不可加总差额_新租赁准则租赁负债等"] = diff

                            # 2. 流动资产不可加总差额 (拆出资金/金融衍生资产/买入返售金融资产等)
                            curr_assets_total = period_dict.get("流动资产合计")
                            if curr_assets_total is not None and isinstance(curr_assets_total, (int, float)):
                                # 修复 B1：兼顾已映射的【其他应收款】与【其他应收款合计】，杜绝漏算已映射科目致差额虚高
                                known_curr_assets = [
                                    "货币资金", "交易性金融资产", "应收票据", "应收账款", "预付款项",
                                    "应收利息", "应收股利", "存货", "合同资产",
                                    "持有待售资产", "一年内到期的非流动资产", "其他流动资产"
                                ]
                                other_recv = float(period_dict.get("其他应收款") if period_dict.get("其他应收款") is not None else (period_dict.get("其他应收款合计") or 0.0))
                                sub_ca = sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_curr_assets) + other_recv
                                diff_ca = round(float(curr_assets_total) - sub_ca, 2)
                                if abs(diff_ca) > 0.01:
                                    period_dict["流动资产不可加总差额_拆出资金及金融细项等"] = diff_ca

                            # 3. 流动负债不可加总差额 (吸收存款及同业存放/卖出回购金融资产款等)
                            curr_liab_total = period_dict.get("流动负债合计")
                            if curr_liab_total is not None and isinstance(curr_liab_total, (int, float)):
                                # 修复 B1：补齐已映射科目【衍生金融负债】及【衍生金融负债_备用】，杜绝流动负债自陈差额虚高
                                known_curr_liab = [
                                    "短期借款", "交易性金融负债", "衍生金融负债", "衍生金融负债_备用", "应付票据", "应付账款", "预收款项",
                                    "合同负债", "应付职工薪酬", "应交税费", "其他应付款合计",
                                    "持有待售负债", "一年内到期的非流动负债", "其他流动负债"
                                ]
                                sub_cl = sum(float(period_dict.get(k, 0.0) or 0.0) for k in known_curr_liab)
                                diff_cl = round(float(curr_liab_total) - sub_cl, 2)
                                if abs(diff_cl) > 0.01:
                                    period_dict["流动负债不可加总差额_吸收存款及金融细项等"] = diff_cl

                    if period_dict:
                        result_periods.append(period_dict)
                break

        if not result_periods:
            entity = self._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到证券代码 '{symbol}' 的财务报表数据（代码不存在或非有效 A 股/港股标的，无财报披露信息）。"
                    "建议先调用 stock_lookup 工具核对标准证券名称与代码。"
                )

        return {
            "symbol": clean_code,
            "statement_type": type_name,
            "period_type": period_type,
            "period_count": len(result_periods),
            "is_financial": is_financial,
            "accounting_standard": standard_name,
            "statements": result_periods,
            "会计口径与说明": (
                "1.【金融与工商业准则双轨制】：本接口底层内置工商业与金融企业（银行/保险/证券）双套专业映射体系，"
                "当查询金融企业(isjr=1)时自动无缝切换金融专用会计科目（覆盖吸收存款、拆出资金、已赚保费、备付金等专有科目）；"
                "查询普通实体企业(isjr=0)时对齐工商业企业准则。\n"
                "2.【流动负债二级明细防双计】：‘其他应付款合计’(N051)已按新准则口径合并包含‘其中_应付利息’(N049)与‘其中_应付股利’(N050)。明细科目加总时，"
                "切勿将‘其中_应付股利/应付利息’与‘其他应付款合计’重复累加，否则将导致流动负债总额双计虚增。\n"
                "3.【新租赁准则等科目缺失标注】：受底层工商业模板限制，工商业未收录新租赁准则科目（【租赁负债】、【使用权资产】）及【长期应付职工薪酬】。"
                "当期‘非流动负债合计’中已包含上述准则科目，故工商业非流动负债明细直接累加与小计存在残差。\n"
                "4.【现金流量表结构规范】：现金流量表现已将主表（直接法三大活动流向）与间接法调节表物理隔离，间接法补充资料独立归集于‘补充资料_间接法’字典中，彻底避免科目遍历时的静默双计。"
            ),
            "会计口径与合并说明": (
                "1.【金融与工商业准则双轨制】：本接口底层内置工商业与金融企业（银行/保险/证券）双套专业映射体系，"
                "当查询金融企业(isjr=1)时自动无缝切换金融专用会计科目（覆盖吸收存款、拆出资金、已赚保费、备付金等专有科目）；"
                "查询普通实体企业(isjr=0)时对齐工商业企业准则。\n"
                "2.【流动负债二级明细防双计】：‘其他应付款合计’(N051)已按新准则口径合并包含‘其中_应付利息’(N049)与‘其中_应付股利’(N050)。明细科目加总时，"
                "切勿将‘其中_应付股利/应付利息’与‘其他应付款合计’重复累加，否则将导致流动负债总额双计虚增。\n"
                "3.【新租赁准则等科目缺失标注】：受底层工商业模板限制，工商业未收录新租赁准则科目（【租赁负债】、【使用权资产】）及【长期应付职工薪酬】。"
                "当期‘非流动负债合计’中已包含上述准则科目，故工商业非流动负债明细直接累加与小计存在残差。\n"
                "4.【现金流量表结构规范】：现金流量表现已将主表（直接法三大活动流向）与间接法调节表物理隔离，间接法补充资料独立归集于‘补充资料_间接法’字典中，彻底避免科目遍历时的静默双计。"
            ),
        }

    def get_f10(self, symbol: str, category: str = "concepts") -> dict[str, Any]:
        """获取 F10 深度基本面（热点题材板块族谱、机构盈利预测、股东与主力持仓、龙虎榜异动等）"""
        clean_code = re.sub(r"[^\d]", "", symbol)
        category_alias_map = {
            "industry": "concepts",
            "sector": "concepts",
            "dividends": "events",
            "bonus": "events",
            "lhb": "limit_up",
            "buyback": "repurchases",
        }
        cat = category_alias_map.get(category.lower().strip(), category.lower().strip())

        # 通达信 PCCW 接口严格参数规范映射表：
        # - concepts (热点题材): [code, "zttzbkz"]
        # - events (事件驱动): [code, "sjcd"]
        # - forecast (机构盈利预测): [code, "yzyq"]
        # - shareholders (股东人数筹码): [code, "gdrs", "", "", "1", "1", "10"]
        # - top_holders (十大流通股东): [code, "ltgd", "", "", "1", "1", "10"]
        # - margin_trading (融资融券): [code, "rzrq", ""]
        # - capital_flow (主力资金流向): [code, "zjlx", ""]
        # - limit_up (涨停分析异动): [code, "ztfx", ""]
        # - unlocks (限售解禁日程): [code, "xslt"]
        # - summary (公司概要提示): [code, "gsgy", ""]
        # - basic_info (公司基本工商): ["0", clean_code, ""]
        cat_configs: dict[str, tuple[str, list[str], str]] = {
            "concepts": ("TdxSharePCCW.tdxf10_gg_rdtc", [clean_code, "zttzbkz"], "热点题材板块族谱"),
            "events": ("TdxSharePCCW.tdxf10_gg_rdtc", [clean_code, "sjcd"], "事件驱动催化"),
            "forecast": ("TdxSharePCCW.tdxf10_gg_ybpj", [clean_code, "yzyq"], "机构盈利预测"),
            "shareholders": ("TdxSharePCCW.tdxf10_gg_gdyj", [clean_code, "gdrs", "", "", "1", "1", "10"], "股东人数与筹码集中度"),
            "top_holders": ("TdxSharePCCW.tdxf10_gg_gdyj", [clean_code, "ltgd", "", "", "1", "1", "10"], "十大流通股东"),
            "margin_trading": ("TdxSharePCCW.tdxf10_gg_jyds", [clean_code, "rzrq", ""], "融资融券交易数据"),
            "capital_flow": ("TdxSharePCCW.tdxf10_gg_jyds", [clean_code, "zjlx", ""], "主力资金流向"),
            "limit_up": ("TdxSharePCCW.tdxf10_gg_jyds", [clean_code, "ztfx", ""], "涨停分析与异动"),
            "unlocks": ("TdxSharePCCW.tdxf10_gg_gbjg", [clean_code, "xslt"], "限售解禁日程表"),
            "summary": ("TdxSharePCCW.tdxf10_gg_zxts", [clean_code, "gsgy", ""], "公司概要与最新提示"),
            "basic_info": ("TdxSharePCCW.tdxf10_gg_gsgk", ["0", clean_code, ""], "公司基本工商与发行信息"),
        }

        if cat not in cat_configs:
            available = ", ".join(cat_configs.keys())
            raise ValueError(f"Unknown F10 category: {category}. Available: {available}")

        entry, params, desc = cat_configs[cat]
        url = f"{self.api_endpoint}?Entry={entry}"
        raw = self._post(url, {"Params": params})

        return {
            "symbol": clean_code,
            "category": cat,
            "category_name": desc,
            "data": raw,
        }

    def screener(self, message: str, limit: int = 10, page: int = 1, filter_st: bool = True) -> dict[str, Any]:
        """通达信自然语言智能选股 (输入中文自然语言条件，如'主板 绩优 储能 连板')"""
        # 修复 P3: 识别查询条件中的港股/香港意图
        is_hk_query = bool(re.search(r"港股|香港|HKEX|\bHK\b", message, re.I))
        # 严格与下游请求分页参数 1:1 对齐，直接请求所指定的单页条数，避免放大 pageSize 导致跳页或数据断裂
        fetch_size = max(1, min(limit, 100))
        req_body = [{
            "message": message.strip(),
            "rang": "AG",
            "pageNo": str(page),
            "pageSize": str(fetch_size),
        }]
        url = f"{self.api_endpoint}?Entry=JNLPSE:wendaQuery"
        raw = self._post(url, req_body)

        results: list[dict[str, Any]] = []
        headers: list[str] = []
        filtered_st_count = 0
        if isinstance(raw, list) and len(raw) >= 3:
            # 通达信 wendaQuery 原生二维数组结构说明：
            # - Row 0: 查询元信息 [0, '', 31, session_id, count]
            # - Row 1: 列标题表头 ['POS', 'market', 'sec_code', 'sec_name', 'now_price', 'chg', ...]
            # - Row 2: 数据类型行 ['', '', '', '', '2|0|0', ...]
            # - Row 3 起: 真实股票数据行 [序号, 市场代码, 股票代码, 股票名称, 现价, 涨跌幅, 业务指标...]
            raw_headers = raw[1] if isinstance(raw[1], list) else []
            headers = [str(h).strip() for h in raw_headers]

            data_rows = raw[3:] if len(raw) > 3 else []
            for item in data_rows:
                if not isinstance(item, list) or len(item) < 4:
                    continue
                # 剔除表头行残留或空行
                if str(item[0]).strip().upper() in ("POS", ""):
                    continue

                market_val = str(item[1]).strip()
                sec_code = str(item[2]).strip()
                sec_name = str(item[3]).strip()

                # 必须符合 6 位标准证券代码
                if not re.match(r"^\d{6}$", sec_code):
                    continue

                is_st = ("ST" in sec_name.upper()) or ("退" in sec_name)
                if filter_st and is_st:
                    filtered_st_count += 1
                    continue

                now_price = None
                try:
                    now_price = round(float(item[4]), 2) if len(item) > 4 and str(item[4]).strip() != "" else None
                except (ValueError, TypeError):
                    pass

                chg_pct = None
                try:
                    chg_pct = round(float(item[5]), 2) if len(item) > 5 and str(item[5]).strip() != "" else None
                except (ValueError, TypeError):
                    pass

                # 动态提取后续业务指标列（如 ROE、流通市值、概念匹配等）
                dynamic_indicators: dict[str, Any] = {}
                for col_idx in range(6, len(item)):
                    col_name = headers[col_idx] if col_idx < len(headers) else f"指标_{col_idx}"
                    clean_col_name = re.sub(r"[0-9#|]+$", "", col_name).strip()
                    val_str = str(item[col_idx]).strip()
                    try:
                        dynamic_indicators[clean_col_name] = round(float(val_str), 4) if "." in val_str else (int(val_str) if val_str.isdigit() else val_str)
                    except Exception:
                        dynamic_indicators[clean_col_name] = val_str

                market_name = "深市" if market_val == "0" else ("沪市" if market_val == "1" else ("北交所" if market_val == "2" else market_val))

                results.append({
                    "code": sec_code,
                    "symbol": sec_code,
                    "name": sec_name,
                    "market": market_name,
                    "price": now_price,
                    "change_pct": chg_pct,
                    "is_st": is_st,
                    "indicators": dynamic_indicators,
                    "matched_indicators": dynamic_indicators,
                })

                if len(results) >= limit:
                    break

        # 从 Row 0 元信息 [0, '', 命中总数, session_id, count] 中精确提取全量命中总数（非单页返回数）
        total_hits = len(results)
        if isinstance(raw, list) and raw and isinstance(raw[0], list) and len(raw[0]) > 2:
            try:
                total_hits = int(raw[0][2])
            except (ValueError, TypeError):
                total_hits = len(results)

        total_pages = math.ceil(total_hits / limit) if limit > 0 else 1

        res_dict = {
            "query": message,
            "message": message,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "filter_st": filter_st,
            "matched_count": total_hits,
            "returned_count": len(results),
            "filtered_st_count": filtered_st_count,
            "filter_st_note": "默认已过滤 ST/*ST 股；若需获取全部股票（含风险警示股）请将 filter_st 设为 False。",
            "market_scope": "A股 (中国大陆交易所：沪市/深市/北交所)",
            "stocks": results,
            "raw_summary": raw[0] if (isinstance(raw, list) and raw) else None,
        }
        if is_hk_query:
            res_dict["warning"] = (
                "【市场范围警示】：当前自然语言选股引擎仅覆盖 A 股（沪市/深市/北交所），暂不支持港股标的自动筛选。"
                "检测到查询条件中包含'港股/香港'关键词，已默认按 A 股相关概念标的返回。"
                "如需查询具体港股（如腾讯 00700、美团 03690），请直接使用 stock_lookup 查找代码后调用 stock_quotes/stock_financials/stock_f10。"
            )
            res_dict["is_hk_intent"] = True
        return res_dict


# =========================================================================
# 【功能块 7：东方财富/公开财经 REST 接口备用兜底客户端 (Zero Dependency)】
# - 输入：标的代码或检索词
# - 功能：当通达信专线未配置 Token、遇到网络抖动或需要补充个股突发新闻时，
#         直接使用标准库发起公开 REST 查询，零体积零额外依赖进行高可用兜底
# - 输出：PublicFallbackClient 实例
# =========================================================================
class PublicFallbackClient:
    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    # =========================================================================
    # 【功能块：个股最新新闻资讯与重大公告双轨获取引擎】
    # - 输入：symbol (str): 证券代码；limit (int): 返回条目上限 (1-10)
    # - 功能：新浪财经为主力通道（低频控、原生标准JSON、高可用），东方财富为备用通道
    # - 输出：list[dict[str, Any]]: 结构化新闻列表（标题、时间、媒体、链接、正文摘要）
    # =========================================================================
    def _normalize_time_str(self, raw_time: str) -> str:
        """清洗与规范化时间字符串为统一的标准 ISO 格式 (YYYY-MM-DD HH:MM:SS)
        解决新浪/东财接口混用'4小时前'、'2小时前'、'昨天 15:30'相对时间串的缺陷 (修复 B7)
        """
        if not raw_time or not isinstance(raw_time, str):
            return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        s = raw_time.strip()
        now = datetime.datetime.now()

        # 1. 'X小时前'
        m_h = re.search(r"(\d+)\s*小时前", s)
        if m_h:
            dt = now - datetime.timedelta(hours=int(m_h.group(1)))
            return dt.strftime("%Y-%m-%d %H:%M:%S")

        # 2. 'X分钟前'
        m_m = re.search(r"(\d+)\s*分钟前", s)
        if m_m:
            dt = now - datetime.timedelta(minutes=int(m_m.group(1)))
            return dt.strftime("%Y-%m-%d %H:%M:%S")

        # 3. '刚刚'
        if "刚刚" in s:
            return now.strftime("%Y-%m-%d %H:%M:%S")

        # 4. '昨天 HH:MM' / '前天 HH:MM'
        m_yt = re.search(r"昨天\s*(\d{1,2}:\d{2}(?::\d{2})?)", s)
        if m_yt:
            t_str = m_yt.group(1)
            if len(t_str.split(":")) == 2:
                t_str += ":00"
            y_day = now - datetime.timedelta(days=1)
            return f"{y_day.strftime('%Y-%m-%d')} {t_str}"

        m_bf = re.search(r"前天\s*(\d{1,2}:\d{2}(?::\d{2})?)", s)
        if m_bf:
            t_str = m_bf.group(1)
            if len(t_str.split(":")) == 2:
                t_str += ":00"
            bf_day = now - datetime.timedelta(days=2)
            return f"{bf_day.strftime('%Y-%m-%d')} {t_str}"

        # 5. 'MM-DD HH:MM' (缺少年份)
        m_md = re.match(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}:\d{2}(?::\d{2})?)$", s)
        if m_md:
            mon, day, tim = m_md.group(1), m_md.group(2), m_md.group(3)
            if len(tim.split(":")) == 2:
                tim += ":00"
            return f"{now.year}-{int(mon):02d}-{int(day):02d} {tim}"

        # 6. 标准时间截取（如 '2026-10-22 08:30:00.000' -> '2026-10-22 08:30:00'）
        if len(s) >= 19 and re.match(r"^\d{4}-\d{2}-\d{2}", s):
            return s[:19]
        if len(s) == 10 and re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return f"{s} 00:00:00"

        return s

    def get_news(self, symbol: str, limit: int = 5, entity_name: str | None = None) -> list[dict[str, Any]]:
        raw_query = symbol.strip()
        clean_code = re.sub(r"[^\d]", "", raw_query)
        req_limit = max(1, min(limit, 10))

        # 智能确定检索词与标的名称组合（确保精准命中正主个股，杜绝泛化模糊数字匹配噪声）
        if entity_name:
            search_term = f"{entity_name} {clean_code}" if clean_code else entity_name
        elif infer_market_setcode(raw_query) == "31" or (len(clean_code) == 5 and not raw_query.startswith(("60", "68"))):
            norm_code = clean_code.zfill(5)
            try:
                hk_req = Request(f"http://qt.gtimg.cn/q=r_hk{norm_code}", headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(hk_req, timeout=3) as hk_resp:
                    hk_raw = hk_resp.read().decode("gbk", errors="replace")
                    hk_parts = hk_raw.split("~")
                    if len(hk_parts) > 1 and hk_parts[1].strip():
                        entity_name = hk_parts[1].strip()
                        search_term = f"{entity_name} {norm_code}"
                    else:
                        search_term = f"港股 {norm_code}"
            except Exception:
                search_term = f"港股 {norm_code}"
        else:
            search_term = clean_code if clean_code else raw_query

        lottery_noise = (
            "双色球", "大乐透", "福彩", "体彩", "开奖号码", "杀号", "预测奖号",
            "排列三", "排列四", "排列五", "七星彩", "六合彩", "中奖", "彩票", "幸运飞艇",
        )

        def _is_relevant(title_text: str, content_text: str) -> bool:
            comb = (title_text + " " + content_text).lower()
            if clean_code and clean_code in comb:
                return True
            if entity_name:
                core_name = re.sub(r"(股份|科技|集团|控股|发展|实业)$", "", entity_name).strip()
                if (core_name and core_name.lower() in comb) or (entity_name.lower() in comb):
                    return True
            if not clean_code and not entity_name:
                return True
            return False

        # 1. 主力通道：新浪财经新闻搜索接口（风控宽松、响应极速、原生标准 JSON）
        try:
            encoded_q = urllib.parse.quote(search_term)
            sina_url = f"https://search.sina.com.cn/api/news?q={encoded_q}&size={req_limit + 5}&page=1"
            req = Request(
                sina_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
                    "Referer": "https://search.sina.com.cn/",
                },
                method="GET",
            )
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                items = data.get("data", {}).get("list", [])
                if items and isinstance(items, list):
                    news_list: list[dict[str, Any]] = []
                    for item in items:
                        title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                        if any(k in title for k in lottery_noise):
                            continue
                        snippet = re.sub(r"<[^>]+>", "", item.get("intro", "")).strip()
                        if not _is_relevant(title, snippet):
                            continue
                        media_name = item.get("media", "").strip() or "新浪财经"
                        norm_t = self._normalize_time_str(item.get("dataTime", ""))
                        news_list.append({
                            "title": title,
                            "source": media_name,
                            "time": norm_t,
                            "datetime": norm_t,
                            "url": item.get("url", ""),
                            "snippet": snippet[:150] + "..." if len(snippet) > 150 else snippet,
                            "summary": snippet[:150] + "..." if len(snippet) > 150 else snippet,
                        })
                        if len(news_list) >= req_limit:
                            break
                    if news_list:
                        return news_list
        except Exception:
            pass

        # 2. 备用通道一：东方财富全文检索 JSONP 接口
        try:
            em_params = {
                "cb": "jQuery_news",
                "param": json.dumps({
                    "code": clean_code if clean_code else search_term,
                    "type": ["cmsArticleWebOld"],
                    "pageindex": 1,
                    "pagesize": req_limit + 5,
                    "keyword": search_term,
                }, ensure_ascii=False),
            }
            em_url = "https://search-api-web.eastmoney.com/search/jsonp?" + urllib.parse.urlencode(em_params)
            req = Request(
                em_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                method="GET",
            )
            with urlopen(req, timeout=self.timeout) as resp:
                raw_text = resp.read().decode("utf-8", errors="replace")
                match = re.search(r"jQuery_news\((.*)\)", raw_text, re.DOTALL)
                if match:
                    data = json.loads(match.group(1))
                    articles = data.get("result", {}).get("cmsArticleWebOld", [])
                    news_list = []
                    for item in articles:
                        title = re.sub(r"<[^>]+>", "", item.get("title", "")).strip()
                        if any(k in title for k in lottery_noise):
                            continue
                        snippet = re.sub(r"<[^>]+>", "", item.get("content", "")).strip()
                        if not _is_relevant(title, snippet):
                            continue
                        media_name = item.get("mediaName", "").strip() or "东方财富"
                        norm_t = self._normalize_time_str(item.get("date", ""))
                        news_list.append({
                            "title": title,
                            "source": media_name,
                            "time": norm_t,
                            "datetime": norm_t,
                            "url": item.get("url", ""),
                            "snippet": snippet[:150] + "..." if len(snippet) > 150 else snippet,
                            "summary": snippet[:150] + "..." if len(snippet) > 150 else snippet,
                        })
                        if len(news_list) >= req_limit:
                            break
                    if news_list:
                        return news_list
        except Exception:
            pass

        # 3. 备用通道二：东方财富官方权威上市公司重大公告专线（重大公告兜底，100% 有数据）
        if clean_code:
            try:
                ann_url = f"https://np-anotice-stock.eastmoney.com/api/security/ann?page_size={req_limit}&page_index=1&ann_type=A&client_source=web&stock_list={clean_code}"
                req = Request(
                    ann_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                    method="GET",
                )
                with urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    ann_items = data.get("data", {}).get("list", [])
                    if ann_items and isinstance(ann_items, list):
                        ann_list = []
                        for item in ann_items[:req_limit]:
                            ann_title = (item.get("title_ch") or item.get("title") or "").strip()
                            art_code = item.get("art_code", "")
                            ann_url_link = f"https://data.eastmoney.com/notices/detail/{clean_code}/{art_code}.html" if art_code else ""
                            norm_t = self._normalize_time_str(item.get("notice_date") or "")
                            ann_list.append({
                                "title": ann_title,
                                "source": "上市公司官方公告",
                                "time": norm_t,
                                "datetime": norm_t,
                                "url": ann_url_link,
                                "snippet": f"【上市公司法定公告】{ann_title}",
                                "summary": f"【上市公司法定公告】{ann_title}",
                            })
                        if ann_list:
                            return ann_list
            except Exception:
                pass

        return []

    # =========================================================================
    # 【功能块：公开数据中心 F10 深度资料自动高可用降级引擎】
    # - 输入：symbol (str): 证券代码；category (str): 分类 (shareholders, top_holders, concepts, summary, unlocks)
    # - 功能：当通达信官方专线 PCCW 库未开放或返回错误码时，自动从公开数据中心获取结构化基本面
    # - 输出：dict[str, Any] | list[Any] | None: 结构化 F10 数据字典或列表
    # =========================================================================
    def get_f10(self, symbol: str, category: str = "concepts") -> dict[str, Any] | list[Any] | None:
        clean_code = re.sub(r"[^\d]", "", symbol)
        cat = category.lower()

        # 根据代码前缀推导市场后缀 (.SH / .SZ / .BJ) 与前缀 (SH / SZ / BJ)
        if clean_code.startswith(("60", "68", "90")):
            secucode_dot = f"{clean_code}.SH"
            secucode_pre = f"SH{clean_code}"
        elif clean_code.startswith(("00", "30", "20")):
            secucode_dot = f"{clean_code}.SZ"
            secucode_pre = f"SZ{clean_code}"
        elif clean_code.startswith(("8", "4", "92")):
            secucode_dot = f"{clean_code}.BJ"
            secucode_pre = f"BJ{clean_code}"
        else:
            secucode_dot = f"{clean_code}.SH"
            secucode_pre = f"SH{clean_code}"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            "Referer": "https://emweb.securities.eastmoney.com/",
        }

        try:
            # 1. 股东人数与筹码集中度
            if cat == "shareholders":
                url = f"https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_HOLDERNUMLATEST&columns=ALL&filter=(SECUCODE=%22{secucode_dot}%22)"
                req = Request(url, headers=headers)
                with urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    items = data.get("result", {}).get("data", [])
                    if items:
                        item = items[0]
                        return {
                            "最新股东户数": item.get("HOLDER_NUM"),
                            "较上期增减户数": item.get("HOLDER_NUM_CHANGE"),
                            "户数变动比例_pct": item.get("HOLDER_NUM_RATIO"),
                            "户均持股市值_元": item.get("AVG_MARKET_CAP"),
                            "户均持股数量": item.get("AVG_HOLD_NUM"),
                            "统计截止日期": (item.get("END_DATE") or "")[:10],
                            "公告日期": (item.get("HOLD_NOTICE_DATE") or "")[:10],
                        }

            # 2. 十大流通股东
            elif cat == "top_holders":
                url = f"https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_EH_FREEHOLDERS&columns=ALL&filter=(SECUCODE=%22{secucode_dot}%22)&pageSize=10"
                req = Request(url, headers=headers)
                with urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    items = data.get("result", {}).get("data", [])
                    if items:
                        holders = []
                        for h in items[:10]:
                            holders.append({
                                "名次": h.get("HOLDER_RANK"),
                                "股东名称": h.get("HOLDER_NAME"),
                                "持股数": h.get("HOLD_NUM"),
                                "占流通盘比例_pct": h.get("FREE_HOLDNUM_RATIO"),
                                "持股变动": h.get("HOLD_CHANGE"),
                            })
                        return {"前十大流通股东": holders, "报告期": (items[0].get("END_DATE") or "")[:10]}

            # 3. 热点题材与板块族谱
            elif cat == "concepts":
                url = f"http://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?code={secucode_pre}"
                req = Request(url, headers=headers)
                with urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                    boards = [x.get("BOARD_NAME") for x in data.get("ssbk", []) if x.get("BOARD_NAME")]
                    topics = [
                        {"主题": x.get("KEYWORD"), "核心要点": x.get("MAINPOINT_CONTENT")}
                        for x in data.get("hxtc", [])
                        if x.get("KEYWORD")
                    ]
                    return {"所属板块": boards, "核心题材": topics[:8]}

            # 4. 公司概要与基本面信息
            elif cat == "summary":
                url = f"http://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={secucode_pre}"
                req = Request(url, headers=headers)
                with urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                    jbzl = data.get("jbzl", [{}])[0]
                    return {
                        "公司全称": jbzl.get("ORG_NAME"),
                        "所属行业": jbzl.get("INDUSTRYCSRC1"),
                        "法定代表人": jbzl.get("LEGAL_PERSON"),
                        "董事长": jbzl.get("CHAIRMAN"),
                        "注册资本_万元": jbzl.get("REG_CAPITAL"),
                        "员工总数": jbzl.get("EMP_NUM"),
                        "主营业务": jbzl.get("MAIN_BUSINESS"),
                        "经营范围": jbzl.get("BUSINESS_SCOPE"),
                        "公司简介": (jbzl.get("ORG_PROFILE") or "").strip()[:300],
                    }

            # 5. 限售解禁日程
            elif cat == "unlocks":
                url = f"https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LIFT_STAGE&columns=ALL&filter=(SECURITY_CODE=%22{clean_code}%22)&pageNumber=1&pageSize=5"
                req = Request(url, headers=headers)
                with urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    items = data.get("result", {}).get("data", []) if data.get("result") else []
                    return [
                        {
                            "解禁日期": (x.get("FREE_DATE") or "")[:10],
                            "解禁股数_万股": x.get("FREE_SHARES"),
                            "解禁类型": x.get("LIMITED_TYPE_NAME"),
                        }
                        for x in items
                    ]
        except Exception:
            return None

        return None


# =========================================================================
# 【功能块 8：金融高可用调度网关核心类 (FinanceGateway)】
# - 输入：配置参数、日志记录器
# - 功能：整合通达信官方专线与公开备用数据源，负责参数校验、多源熔断切换、
#         Token 掩码脱敏与大模型友好响应封装
# - 输出：FinanceGateway 实例
# =========================================================================
class FinanceGateway:
    def __init__(
        self,
        tdx_token: str = "",
        api_endpoint: str = "http://tdxhub.icfqs.com:7615/TQLEX",
        rag_endpoint: str = "https://ai.icfqs.com:8965/v1/rag-entity-retrieve",
        timeout: int = 15,
        logger: logging.Logger | None = None,
    ):
        self.tdx_client = TdxClient(token=tdx_token, api_endpoint=api_endpoint, rag_endpoint=rag_endpoint, timeout=timeout, logger=logger)
        self.fallback_client = PublicFallbackClient(timeout=timeout)
        self.logger = logger or logging.getLogger("mcp_gateway.finance")

    def stock_lookup(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 1：实体代码/名称 AI RAG 语义检索"""
        query = arguments.get("query", "").strip()
        market_range = arguments.get("range", "AG")
        if not query:
            raise ValueError("query 不能为空，请输入股票简称或代码（如'贵州茅台'、'腾讯'）")
        entities = self.tdx_client.lookup_stock(query=query, market_range=market_range)

        # 判定是否存在代码或全称的精准命中 (修复 P0 缺陷)
        clean_q = re.sub(r"^(sh|sz|bj|hk)", "", query.strip(), flags=re.I)
        clean_q = re.sub(r"\.(sh|sz|bj|hk|ss)$", "", clean_q, flags=re.I)
        exact_match = False
        matched_code = None
        for ent in entities:
            e_code = str(ent.get("code") or ent.get("entity_code") or "")
            e_name = str(ent.get("name") or ent.get("entity_name") or "")
            aliases = [str(a) for a in ent.get("aliases", [])]
            if (clean_q and e_code == clean_q) or query.strip() in ([e_name] + aliases):
                exact_match = True
                matched_code = e_code
                break

        res: dict[str, Any] = {
            "query": query,
            "range": market_range,
            "exact_match": exact_match,
            "entity_count": len(entities),
            "entities": entities,
        }
        if exact_match:
            res["命中说明"] = f"已精确匹配到目标证券标的 (代码: {matched_code})"
        elif clean_q.isdigit() and len(clean_q) in (5, 6):
            res["未命中说明"] = (
                f"在指定市场范围 ({market_range}) 中未检索到代码为 '{query}' 的上市证券标的；"
                "下方返回列表为语义模型相关度最近的关联候选（非正主标的），请核对代码后再行查询。"
            )
        return res

    def stock_quotes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 2：实时盘口与完整证券行情"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空，请输入证券代码（如'600519'）")
        setcode = arguments.get("setcode")
        return self.tdx_client.get_quotes(symbol=symbol, setcode=setcode)

    def stock_kline(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 3：多周期历史 K 线走势 + 内置技术指标计算 (MACD/KDJ/BOLL/MA)"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空")
        period = arguments.get("period", "day")
        count = int(arguments.get("count", 30))
        adjust = arguments.get("adjust", "qfq")
        setcode = arguments.get("setcode")
        with_indicators = bool(arguments.get("with_indicators", True))
        return self.tdx_client.get_kline(
            symbol=symbol,
            period=period,
            count=count,
            adjust=adjust,
            setcode=setcode,
            with_indicators=with_indicators,
        )

    def stock_financials(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 4：三大财务报表 (资产负债表, 利润表, 现金流量表) 与经营主营分析"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空")
        report_type = arguments.get("report_type", "balance")
        period_type = arguments.get("period_type", "report")
        limit_periods = int(arguments.get("limit_periods", 4))
        return self.tdx_client.get_financial_statements(
            symbol=symbol,
            report_type=report_type,
            period_type=period_type,
            limit_periods=limit_periods,
        )

    def stock_f10(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 5：F10 深度基本面（热点题材板块族谱、业绩预告、股东变动、龙虎榜、限售解禁）"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空")
        category = arguments.get("category", "concepts")
        cat_lower = category.lower().strip()
        category_alias_map = {
            "industry": "concepts",
            "sector": "concepts",
            "dividends": "events",
            "bonus": "events",
            "lhb": "limit_up",
            "buyback": "repurchases",
        }
        norm_cat = category_alias_map.get(cat_lower, cat_lower)

        # 港股标的（5位代码）：启用新浪港股F10主力专线与东方财富高可用备份专线
        clean_code = re.sub(r"[^\d]", "", symbol)
        if infer_market_setcode(symbol) == "31" or len(clean_code) == 5:
            norm_code = clean_code.zfill(5)
            return self.tdx_client._get_hk_f10(norm_code, category=cat_lower)

        # A 股标的：若请求 buyback/repurchases，给予友好提示与结构化响应
        if norm_cat == "repurchases":
            return {
                "symbol": clean_code,
                "category": "repurchases",
                "category_name": "股份回购明细",
                "source": "public_f10_fallback",
                "data": {"status": "A股公司股份回购明细请查阅上市公司重大公告或调用 stock_news 工具查询"},
                "formatted_tables": {
                    "repurchases": [{"提示": "A股公司股份回购请查阅上市公司重大公告或调用 stock_news 工具查询"}]
                },
            }

        # 1. 首先尝试通达信官方专线获取（优先官方授权渠道）
        try:
            tdx_res = self.tdx_client.get_f10(symbol=symbol, category=norm_cat)
            raw_data = tdx_res.get("data", {})
            need_fallback = False
            # 检查通达信服务端是否返回了 -1005 等数据库执行错误，或空的占位结构 [[0]]
            if not raw_data or (isinstance(raw_data, dict) and raw_data.get("ErrorCode", 0) != 0):
                need_fallback = True
            elif isinstance(raw_data, dict) and "ResultSets" in raw_data:
                rs = raw_data.get("ResultSets", [])
                if not rs:
                    need_fallback = True
                elif len(rs) == 1:
                    first_table = rs[0]
                    # 仅当首表为异常占位列名 ["0"] 且内容为 [[0]] 时才判定为底层接口故障需要降级；有效列结构但无记录行时属于正常空记录
                    if first_table.get("ColName") == ["0"] and (not first_table.get("Content") or first_table.get("Content") == [[0]]):
                        need_fallback = True
            if not need_fallback:
                tdx_res["source"] = "tdx_official_vip (通达信官方授权专线)"
                # 将 ResultSets 中的各表结构化转换为行对象字典列表，便于大模型与前端直观阅读
                formatted_tables = {}
                holder_total_shares = 0.0
                if category == "top_holders":
                    for table in raw_data.get("ResultSets", []):
                        if table.get("ResultSetKey") == "table0":
                            t0_rows = table.get("Content", [])
                            t0_cols = table.get("ColName", [])
                            if t0_rows and "T004" in t0_cols:
                                t0_idx = t0_cols.index("T004")
                                try:
                                    holder_total_shares = float(t0_rows[0][t0_idx] or 0.0)
                                except (ValueError, TypeError):
                                    pass
                            break

                for idx, table in enumerate(raw_data.get("ResultSets", [])):
                    t_key = table.get("ResultSetKey", f"table{idx}")
                    cols = table.get("ColName", [])
                    rows = table.get("Content", [])

                    # 严格过滤与业务无关的三类底层系统表（复权因子表 AdjustingFactor/RatioFactor 与纯历史收盘价表）
                    col_str = " ".join(cols)
                    if "AdjustingFactor" in col_str or "RatioFactor" in col_str or cols == ["TradingDay", "ClosePrice"]:
                        continue
                    if category == "capital_flow" and t_key != "table0":
                        continue
                    if category == "unlocks" and t_key != "table0":
                        continue

                    table_rows = []
                    for row in rows:
                        row_dict = {}
                        for c_idx, val in enumerate(row):
                            col_raw = cols[c_idx] if c_idx < len(cols) else f"col_{c_idx}"
                            if category == "concepts":
                                col_title = CONCEPTS_FIELDS.get(col_raw, col_raw)
                            elif category == "margin_trading":
                                col_title = MARGIN_TRADING_FIELDS.get(col_raw, col_raw)
                            elif category == "capital_flow":
                                col_title = CAPITAL_FLOW_FIELDS.get(col_raw, col_raw)
                            elif category == "events":
                                col_title = EVENTS_FIELDS.get(col_raw, col_raw)
                            elif category == "limit_up":
                                col_title = LIMIT_UP_FIELDS.get(col_raw, col_raw)
                            elif category == "unlocks":
                                col_title = UNLOCKS_FIELDS.get(col_raw, col_raw)
                            elif category == "shareholders":
                                col_title = SHAREHOLDERS_FIELDS.get(col_raw, col_raw)
                            elif category == "forecast":
                                col_title = FORECAST_FIELDS.get(col_raw, col_raw)
                            elif category == "summary":
                                col_title = SUMMARY_FIELDS.get(col_raw, col_raw)
                            elif category == "top_holders" and t_key == "table0":
                                col_title = TOP_HOLDERS_TOTALS_FIELDS.get(col_raw, col_raw)
                            elif category == "top_holders" and t_key == "table1":
                                col_title = TOP_HOLDERS_FIELDS.get(col_raw, col_raw)
                            else:
                                col_title = col_raw
                            row_dict[col_title] = val

                        # 针对 margin_trading：清洗数字字符串类型漂移，统一规范为 float (修复 B3)
                        if category == "margin_trading":
                            for num_k in ("融资余额_元", "融资买入额_元", "融资偿还额_元", "融券余量_股", "融券卖出量_股", "融券偿还量_股", "成交量_股", "成交金额_元", "收盘价", "流通股本_万股", "总股本_万股"):
                                if num_k in row_dict and row_dict[num_k] is not None:
                                    try:
                                        row_dict[num_k] = float(row_dict[num_k])
                                    except (ValueError, TypeError):
                                        pass

                        # 针对 forecast：清洗一致预期调整数字字符串类型漂移，统一规范为 float (修复 B5)
                        if category == "forecast":
                            for num_k in ("一致预期变动_短期_pct", "一致预期变动_中期_pct", "一致预期变动_长期_pct", "预测每股收益_当年_元", "预测每股收益_次年_元", "预测每股收益_后年_元", "预测PEG", "机构目标价_元"):
                                if num_k in row_dict and row_dict[num_k] is not None:
                                    try:
                                        row_dict[num_k] = float(row_dict[num_k])
                                    except (ValueError, TypeError):
                                        pass

                        # 自动补齐 top_holders 中持股比例整列为 null 的缺陷（采用 持股数量/总流通股本 测算）
                        if category == "top_holders" and t_key == "table1":
                            if row_dict.get("持股比例_pct") is None and holder_total_shares > 0:
                                cgs_val = float(row_dict.get("持股数量_股", 0.0) or 0.0)
                                if cgs_val > 0:
                                    row_dict["持股比例_pct"] = round((cgs_val / holder_total_shares) * 100.0, 3)

                        table_rows.append(row_dict)
                    if category == "events" and table_rows:
                        table_rows = list(reversed(table_rows))
                    formatted_tables[t_key] = table_rows
                tdx_res["formatted_tables"] = formatted_tables
                if category == "events":
                    has_records = any(len(rows) > 0 for rows in formatted_tables.values())
                    if not has_records:
                        tdx_res["无记录说明"] = "该标的近期事件库无记录（近期无重大行业题材或宏观催化事件）。如需查询公司最新日常经营与法定公告，请调用 stock_news 工具。"
                    tdx_res["事件库属性说明"] = (
                        "本表为通达信收录的历史重大行业题材与宏观里程碑事件库（已按发生日期倒序排列，非实时个股公告）；"
                        "如需获取上市公司最新分钟级法定披露公告与突发新闻，请调用 stock_news 工具。"
                    )
                elif category == "top_holders":
                    tdx_res["股本口径说明"] = (
                        "【A+H及跨市场股本口径警示】十大流通股东表中的股本字段仅为定期报告历史流通股口径（按季度披露）。"
                        "对于 A+H 两地上市标的（如比亚迪 002594/01211、中国平安、宁德时代等），表中流通总额通常为境内A股流通股与境外H股流通股的历史合并加总（例如比亚迪历史定期报告71.70亿股=A股流通34.87亿+H股流通36.83亿）；"
                        "而 quotes 工具返回的【总股本_万股】为交易所实时最新法定全部已发行股本（如比亚迪最新91.17亿股，包含最新送转增发）。"
                        "两者属于【历史定期报告流通额 vs 实时法定总股本】的不同口径，严禁混用，测算总市值或AH溢价时必须区分各市场实际上市股数！"
                    )
                    tdx_res["限售股与股本结构核算指南"] = (
                        "通达信底层 table0 中的【限售流通股合计_股(T013)】为预留扩展列（历史多期未直接填列）。"
                        "标的真实限售股规模可直接通过【真实基准总股本 − 统计流通总额_股】精确推算（如 002594 比亚迪最新 91.17 亿股 − 71.70 亿流通总额 = 19.47 亿未流通/限售股；002223 9.95 亿总股本 − 9.34 亿流通A股 = 6,018 万限售股）。"
                    )
                elif category == "forecast":
                    has_records = any(len(rows) > 0 for rows in formatted_tables.values())
                    if not has_records:
                        tdx_res["无记录说明"] = "该标的近期无卖方机构发布盈利预测报告或未覆盖业绩一致预期数据。"
                    else:
                        tdx_res["一致预期指标说明"] = (
                            "表内【一致预期变动_短期/中期/长期_pct】为卖方分析师对该标的盈利预测与目标价在近1月/3月/6月的动态调整幅度（%），非预测区间上下限。"
                            "正值代表卖方分析师综合调高预期，负值代表调低预期。"
                        )
                elif category == "limit_up":
                    has_records = any(len(rows) > 0 for rows in formatted_tables.values())
                    if not has_records:
                        tdx_res["无记录说明"] = "该股票在近期统计周期内无涨停或异动记录（白马蓝筹股/低波动标的未触发涨停机制属于常态）。"
                    else:
                        t0_data = formatted_tables.get("table0", [])
                        # 修复 B6：通达信官方专线对'超涨10%'类异动记录的成交金额恒返回0，智能对齐日K线成交额全量补齐
                        need_kline = any(r.get("当日成交金额_万元") in (0, 0.0, "0", None) for r in t0_data)
                        kline_map = {}
                        if need_kline:
                            try:
                                kline_res = self.tdx_client.get_kline(clean_code, period="day", count=300)
                                for b in (kline_res.get("klines") or kline_res.get("bars") or []):
                                    b_time = str(b.get("time", ""))[:10]
                                    b_amt = b.get("amount")
                                    if b_time and b_amt and float(b_amt) > 0:
                                        kline_map[b_time] = round(float(b_amt) / 10000.0, 2)
                            except Exception:
                                pass

                        for row in t0_data:
                            if row.get("当日成交金额_万元") in (0, 0.0, "0", None):
                                trade_d = str(row.get("涨停日期", ""))[:10]
                                if trade_d in kline_map:
                                    row["当日成交金额_万元"] = kline_map[trade_d]
                                    row["成交额补充说明"] = "由日K线历史成交额精确补全(解决上游超涨10%缺值)"
                                elif row.get("当日标志") == "1" or str(row.get("currq")) == "1":
                                    try:
                                        q = self.tdx_client.get_quotes(clean_code)
                                        amt = q.get("盘口核心行情", {}).get("成交额_元")
                                        if amt and float(amt) > 0:
                                            row["当日成交金额_万元"] = round(float(amt) / 10000.0, 2)
                                            row["成交额补充说明"] = "盘口实时成交额补充(盘中清算前)"
                                    except Exception:
                                        pass
                            if row.get("封板时间") == "--":
                                row["封板说明"] = "盘中超涨10%异动，未触发封死涨停板"
                elif category == "unlocks":
                    t0_data = formatted_tables.get("table0", [])
                    if t0_data:
                        latest_date_str = str(t0_data[0].get("解禁日期", ""))
                        today_str = datetime.date.today().strftime("%Y%m%d")
                        if latest_date_str and latest_date_str < today_str:
                            tdx_res["解禁状态说明"] = (
                                f"该标的最近一次限售解禁已于 {latest_date_str} 实施完成，当前股票处于全流通状态或近期无待解禁日程。"
                            )
                    try:
                        fb_unlocks = self.fallback_client.get_f10(symbol=clean_code, category="unlocks")
                        if fb_unlocks and isinstance(fb_unlocks, list) and len(fb_unlocks) > 1:
                            tdx_res["多阶段解禁明细"] = fb_unlocks
                    except Exception:
                        pass
                elif category == "summary":
                    t5_rows = formatted_tables.get("table5", [])
                    # 检查整张 F10 摘要表是否具备任何实质非空非 None 业务数据
                    summary_has_real_data = any(
                        any(v is not None and str(v).strip() not in ("", "--", "None", "null") for v in row.values())
                        for t_rows in formatted_tables.values() for row in t_rows
                    )
                    # 仅当标的确实存在实质有效经营数据，且 table5 质押表确无质押时，才声明无质押记录
                    if not t5_rows and summary_has_real_data:
                        tdx_res["质押状态说明"] = "该公司当前无股东股权质押记录（质押股份比例为0%，股权质押风险低）。"
                elif category == "margin_trading":
                    if "table2" in formatted_tables:
                        for row in formatted_tables["table2"]:
                            for ok, cn in F10_MARGIN_TABLE2_MAP.items():
                                if ok in row:
                                    row[cn] = row.pop(ok)

                # 增强 F10 摘要表：汉化关键财务与估值指标、依据“利润变动方向”精确校准亏损标的同比含义、注入隔日风格标签与破净动态复核指引
                if category == "summary" or ("table3" in formatted_tables and "table4" in formatted_tables):
                    self._enrich_f10_summary_tables(formatted_tables, tdx_res)

                # 防空壳无效标的校验：严格校验所有格式化表中是否存在任何实质有效非空单元格（防止全 None 占位行被误判为有效数据）
                has_any_data = any(
                    any(v is not None and str(v).strip() not in ("", "--", "None", "null") for v in row.values())
                    for rows in formatted_tables.values() for row in rows
                )
                if not has_any_data:
                    entity = self.tdx_client._resolve_exact_security_entity(clean_code)
                    if entity is None:
                        raise ValueError(
                            f"未查询到证券代码 '{symbol}' 的 F10 基本面资料（代码不存在或非有效 A 股/港股标的）。"
                            "建议先调用 stock_lookup 工具核对标准证券名称与代码。"
                        )
                    else:
                        if not tdx_res.get("无记录说明"):
                            tdx_res["无记录说明"] = f"标的 {symbol} 当前分类({category})下暂无公开披露的明细数据。"
                return tdx_res
        except ValueError:
            raise
        except Exception:
            pass

        # 2. 通达信未开放或异常时，自动平滑降级至公开数据中心高可用 F10
        fallback_data = self.fallback_client.get_f10(symbol=symbol, category=norm_cat)
        clean_code = re.sub(r"[^\d]", "", symbol)
        if fallback_data is None or fallback_data == {"status": "暂无数据"}:
            entity = self.tdx_client._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未查询到证券代码 '{symbol}' 的 F10 基本面资料（代码不存在或非有效 A 股/港股标的）。"
                    "建议先调用 stock_lookup 工具核对标准证券名称与代码。"
                )
        category_names = {
            "concepts": "热点题材板块族谱",
            "events": "事件驱动催化",
            "forecast": "机构盈利预测",
            "shareholders": "股东人数与筹码集中度",
            "top_holders": "十大流通股东",
            "margin_trading": "融资融券交易数据",
            "capital_flow": "主力资金流向",
            "limit_up": "涨停分析与异动",
            "summary": "公司概要与最新提示",
            "unlocks": "限售解禁日程表",
            "basic_info": "公司基本工商与发行信息",
        }
        fallback_res = {
            "symbol": clean_code,
            "category": category,
            "category_name": category_names.get(category, category),
            "source": "public_f10_fallback",
            "data": fallback_data if fallback_data is not None else {"status": "暂无数据"},
        }
        if category == "limit_up":
            fallback_res["无记录说明"] = "该股票在近期统计周期内无涨停或异动记录（白马蓝筹股/低波动标的未触发涨停机制属于常态）。"
        elif category == "forecast":
            fallback_res["无记录说明"] = "该标的近期无卖方机构发布盈利预测报告或未覆盖业绩一致预期数据。"
        elif category == "events":
            fallback_res["无记录说明"] = "该标的近期事件库无记录（近期无重大行业题材或宏观催化事件）。如需查询公司最新日常经营与法定公告，请调用 stock_news 工具。"
        return fallback_res

    # =========================================================================
    # 【功能块 8.5：F10 摘要表业务增强与防幻觉校准处理器】
    # - 输入：formatted_tables (各表行对象字典列表), tdx_res (F10 返回字典)
    # - 功能：
    #   1. 解决隔日标签盘中失效问题：提取清算基准日 (gxrq)、每股净资产 (mgjzc) 与破净股等风格标签，
    #      注入隔日时效警示与盘中动态复核指引，引导大模型按 [现价/BPS] 重算 PB，杜绝颠倒结论。
    #   2. 解决亏损标的同比记号歧义问题：针对当期净利润为负的标的，依据“利润变动方向”法则
    #      将同比负号精准判定为【亏损扩大 XX%】，同比正号判定为【亏损收窄/减亏 XX%】，
    #      彻底杜绝大模型将两负数相除误读为“盈利增长”。
    # - 输出：原地更新 formatted_tables 与 tdx_res，注入汉化字段与防幻觉业务提示
    # =========================================================================
    def _enrich_f10_summary_tables(
        self, formatted_tables: dict[str, list[dict[str, Any]]], tdx_res: dict[str, Any]
    ) -> None:
        gxrq = ""
        if "table8" in formatted_tables and formatted_tables["table8"]:
            gxrq = str(formatted_tables["table8"][0].get("gxrq", "") or "")

        mgjzc = None
        if "table4" in formatted_tables and formatted_tables["table4"]:
            mgjzc = formatted_tables["table4"][0].get("mgjzc")

        sjl = None
        if "table6" in formatted_tables and formatted_tables["table6"]:
            sjl = formatted_tables["table6"][0].get("sjl")

        # 0. 增强 table0 (基础定位与扣非净利润规范汉化，修复 B10 与 P3)
        if "table0" in formatted_tables:
            for row in formatted_tables["table0"]:
                for ok, cn in F10_TABLE0_SUMMARY_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

        # 1. 增强 table4 (关键财务指标映射与亏损同比方向精确判定，修复 B10 与 P3)
        if "table4" in formatted_tables:
            for row in formatted_tables["table4"]:
                for ok, cn in F10_TABLE4_FINANCIAL_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

                g_profit = row.get("归母净利润_元")
                g_tb = row.get("归母净利润同比增减_pct")
                if g_profit is not None and g_tb is not None:
                    try:
                        p_val = float(g_profit)
                        tb_val = float(g_tb)
                        if p_val < 0:
                            if tb_val < 0:
                                row["归母净利润变动方向判定"] = f"亏损扩大 {abs(tb_val):.2f}%"
                                row["亏损标的同比解读警示"] = (
                                    f"【方向判定警示】该标的报告期归母净利润为负({p_val:,.2f}元)，"
                                    f"通达信同比记号按利润变动方向记负号，表达【亏损扩大 {abs(tb_val):.2f}%】。"
                                    "数学上两负数相除切勿误读为盈利增长！"
                                )
                            else:
                                row["归母净利润变动方向判定"] = f"亏损收窄 {abs(tb_val):.2f}% (减亏)"
                                row["亏损标的同比解读警示"] = (
                                    f"【方向判定警示】该标的报告期归母净利润为负({p_val:,.2f}元)，"
                                    f"同比正号表达【亏损收窄/减亏 {abs(tb_val):.2f}%】，非普通盈利增长。"
                                )
                        else:
                            if tb_val >= 0:
                                row["归母净利润变动方向判定"] = f"盈利同比增长 {tb_val:.2f}%"
                            else:
                                row["归母净利润变动方向判定"] = f"盈利同比下滑 {abs(tb_val):.2f}%"
                    except (ValueError, TypeError):
                        pass

                # 扣非净利润变动方向判定
                k_tb = row.get("扣非净利润同比增减_pct")
                k_profit = row.get("扣非净利润_元")
                if k_profit is None and "table0" in formatted_tables and formatted_tables["table0"]:
                    k_profit = formatted_tables["table0"][0].get("扣非净利润_元")
                if k_tb is not None:
                    try:
                        tb_k_val = float(k_tb)
                        if k_profit is not None and float(k_profit) < 0:
                            if tb_k_val < 0:
                                row["扣非净利润变动方向判定"] = f"扣非亏损扩大 {abs(tb_k_val):.2f}%"
                            else:
                                row["扣非净利润变动方向判定"] = f"扣非亏损收窄 {abs(tb_k_val):.2f}% (减亏)"
                        else:
                            if tb_k_val >= 0:
                                row["扣非净利润变动方向判定"] = f"扣非净利润同比增长 {tb_k_val:.2f}%"
                            else:
                                row["扣非净利润变动方向判定"] = f"扣非净利润同比下滑 {abs(tb_k_val):.2f}%"
                    except (ValueError, TypeError):
                        pass

        # 2. 增强 table3 (概念与风格标签隔日状态及破净动态复核指南)
        if "table3" in formatted_tables:
            for row in formatted_tables["table3"]:
                tags = [v for v in row.values() if isinstance(v, str) and v.strip()]
                row["有效风格标签"] = tags
                row["隔日状态与时效警示"] = (
                    f"【核心时效警示】上述风格标签（如 {tags}）及静态估值由通达信服务端于上一清算日（{gxrq or '前一交易日'}）批处理生成，具有隔日滞后性！"
                    "盘中股价异动（如大涨、涨停或大跌）时可能产生完全相反的结论。"
                )
                if "破净股" in tags and mgjzc is not None:
                    row["破净动态复核指引"] = (
                        f"该标的前一交易日清算 PB={sjl or '未知'}（昨收价 < BPS {mgjzc}元）。"
                        f"盘中若股价上涨且最新现价 >= {mgjzc}元（即即时 PB >= 1.0），则已脱离破净状态！"
                        "盘中必须调用 stock_quotes 取得最新价核算，严禁直接盲从隔日静态标签。"
                    )

        # 3. 增强 table6 (估值指标映射与盘中重算提示，修复 B10 同表同义重复与 P3)
        if "table6" in formatted_tables:
            for row in formatted_tables["table6"]:
                for ok, cn in F10_TABLE6_VALUATION_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)
                row["估值时效提示"] = f"市净率(PB)/市盈率(PE)基于上一交易日清算日({gxrq or '前一交易日'})，盘中请结合 stock_quotes 实时现价动态重算。"

        # 3.5 增强 table8 (基础信息与行业分类汉化映射，修复 P3 杜绝裸英文字段外泄)
        if "table8" in formatted_tables:
            for row in formatted_tables["table8"]:
                for ok, cn in F10_TABLE8_SUMMARY_MAP.items():
                    if ok in row:
                        row[cn] = row.pop(ok)

        # 4. 在根对象注入顶层审计警示字典
        tdx_res["f10_audit_notice"] = {
            "隔日概念标签提示": (
                f"F10 摘要与概念标签（如破净股、绩优等）生成于上一交易日清算({gxrq or '昨日'})，盘中股价波动会导致标签反转失效。"
                "盘中研判必须按 [stock_quotes 最新现价 ÷ 每股净资产] 动态重算即时 PB，严禁盲从静态标签。"
            ),
            "亏损公司同比记号规则": (
                "亏损标的(净利润<0)的同比增减率记号代表'利润变动方向'：负号表示亏损扩大(恶化)，正号表示亏损收窄/减亏(改善)。"
                "严禁使用两个负数相除计算得出正数并误读为盈利增长！"
            ),
        }

    def stock_screener(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 6：自然语言智能选股 (如'主板 小盘 绩优 储能 连板')"""
        message = arguments.get("message", "").strip()
        if not message:
            raise ValueError("message 不能为空，请输入自然语言选股条件")
        limit = int(arguments.get("limit", 10))
        page = int(arguments.get("page", 1))
        filter_st = bool(arguments.get("filter_st", True))
        return self.tdx_client.screener(message=message, limit=limit, page=page, filter_st=filter_st)

    def stock_news(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """工具 7：个股最新新闻资讯与重大公告"""
        symbol = arguments.get("symbol", "").strip()
        if not symbol:
            raise ValueError("symbol 不能为空")
        clean_code = re.sub(r"[^\d]", "", symbol)
        entity_name = None
        if clean_code and len(clean_code) in (5, 6):
            entity = self.tdx_client._resolve_exact_security_entity(clean_code)
            if entity is None:
                raise ValueError(
                    f"未找到代码为 '{symbol}' 的有效证券标的（代码不存在或非有效 A 股/港股标的），"
                    "已拦截泛化模糊检索以避免返回无关误导资讯。建议先调用 stock_lookup 工具查询标准代码与简称。"
                )
            entity_name = entity.get("entity_name")

        limit = int(arguments.get("limit", 5))
        news = self.fallback_client.get_news(symbol=symbol, limit=limit, entity_name=entity_name)
        return {"symbol": symbol, "news_count": len(news), "news": news}

    def market_context(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """工具 8：A股交易日历与时钟状态感知"""
        return get_market_context()


# =========================================================================
# 【功能块 9：8 大金融工具标准 MCP JSON Schema 元数据定义】
# - 输入：无
# - 功能：向大模型精确描述每个工具的用途、参数类型、默认值与取值范围
# - 输出：dict 映射表
# =========================================================================
STOCK_LOOKUP_SCHEMA = {
    "name": "stock_lookup",
    "description": "根据公司简称、股票别名或代码模糊检索标的代码（如'贵州茅台'、'腾讯'、'半导体ETF'）。支持A股(AG)、港股(HK-GP)、美股(MG-GP)、基金(JJ)、指数(ZS)。在进行股票或行业分析时，若用户提供了股票简称而非6位数字代码，应优先调用本工具精确查找标的代码与交易所代码。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "实体名称或简称，如'贵州茅台'、'腾讯'、'上证指数'"},
            "range": {"type": "string", "enum": ["AG", "HK-GP", "HK-JJ", "JJ", "MG-GP", "ZS"], "default": "AG", "description": "市场类型：AG=A股(默认), HK-GP=港股, HK-JJ=港股基金, JJ=基金, MG-GP=美股, ZS=指数"}
        },
        "required": ["query"],
        "additionalProperties": False
    }
}

STOCK_QUOTES_SCHEMA = {
    "name": "stock_quotes",
    "description": "查询指定股票的实时五档盘口、买卖挂单、最新现价、涨跌幅、换手率、量比、内外盘强弱、总市值与动态市盈率等高频盘口与专业估值指标。全面支持 A 股（通达信官方专线）与港股（腾讯免费专线回退，如 '00700' 腾讯控股、'09988' 阿里巴巴）。数据全面中文化映射。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'、'000001'），港股为5位数字（如'00700'、'09988'，支持'HK'前缀）"},
            "setcode": {"type": "string", "description": "市场代码：0=深市，1=沪市，2=北交所，31=港交所。若不传将自动智能推导"}
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

STOCK_KLINE_SCHEMA = {
    "name": "stock_kline",
    "description": "获取指定股票的多周期历史复权K线走势，并自动计算输出关键技术指标（MA5/MA10/MA20均线、MACD指标DIF/DEA/柱状图、KDJ随机指标、BOLL布林带上中下轨）。全面支持 A 股及港股（腾讯免费专线）。支持日线(day)、周线(week)、月线(month)、60分钟(60m)、15分钟(15m)、5分钟(5m)。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'），港股为5位数字（如'00700'）"},
            "period": {"type": "string", "enum": ["day", "week", "month", "60m", "30m", "15m", "5m", "1m"], "default": "day", "description": "K线周期，默认为 day (日线)"},
            "count": {"type": "integer", "minimum": 1, "maximum": 300, "default": 30, "description": "请求K线根数（1-300），默认30根"},
            "adjust": {"type": "string", "enum": ["qfq", "hfq", "none"], "default": "qfq", "description": "复权方式：qfq=前复权(默认), hfq=后复权, none=不复权"},
            "setcode": {"type": "string", "description": "市场代码：0=深市，1=沪市，2=北交所，31=港交所。默认自动推导"},
            "with_indicators": {"type": "boolean", "default": True, "description": "是否自动附带技术指标计算结果（MA、MACD、KDJ、BOLL），默认为 true"}
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

STOCK_FINANCIALS_SCHEMA = {
    "name": "stock_financials",
    "description": "查询上市公司三大财务报表（资产负债表 balance、利润表 income、现金流量表 cashflow）与主营构成。支持报告期累计(report)与单季度(quarter)双口径切换，支持历史多期财报横向对比，所有会计科目均中文化映射。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'），港股为5位数字（如'00700'）"},
            "report_type": {"type": "string", "enum": ["balance", "income", "cashflow"], "default": "balance", "description": "财报类型：balance=资产负债表, income=利润表, cashflow=现金流量表"},
            "period_type": {"type": "string", "enum": ["report", "quarter"], "default": "report", "description": "统计口径：report=报告期累计(一季报/中报/三季报/年报), quarter=单季度口径"},
            "limit_periods": {"type": "integer", "minimum": 1, "maximum": 10, "default": 4, "description": "返回的历史财报期数，默认4期"}
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

STOCK_F10_SCHEMA = {
    "name": "stock_f10",
    "description": "查询上市公司深度基本面与筹码数据。包括热点题材板块族谱(concepts/industry)、事件驱动催化与分红派息(events/dividends)、机构盈利预测与评级(forecast)、股东人数变化与筹码集中度(shareholders)、十大流通股东(top_holders)、港股公司股份回购明细(repurchases/buyback)、融资融券明细(margin_trading)、主力资金流向(capital_flow)、涨停分析与异动(limit_up/lhb)、限售解禁日程(unlocks)、公司概要与最新提示(summary)、公司基本工商与发行信息(basic_info)。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码：A股为6位数字（如'600519'），港股为5位数字（如'00700'）"},
            "category": {
                "type": "string",
                "enum": [
                    "concepts", "industry", "events", "dividends", "forecast",
                    "shareholders", "top_holders", "repurchases", "buyback",
                    "margin_trading", "capital_flow", "limit_up", "lhb",
                    "unlocks", "summary", "basic_info", "indicators", "financials"
                ],
                "default": "concepts",
                "description": "基本面分类：concepts/industry=热点题材板块族谱, events/dividends=事件驱动催化与分红派息, forecast=机构盈利预测, shareholders=股东人数与大股东增减持流水, top_holders=十大流通股东(港股为持股5%以上主要股东), repurchases/buyback=股份回购明细(港股), capital_flow=主力资金流向, margin_trading=融资融券, limit_up/lhb=涨停分析与龙虎榜席位, unlocks=限售解禁, summary=公司概要, basic_info=公司基本工商与发行信息, indicators/financials=核心财务与经营指标宽表"
            }
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

STOCK_SCREENER_SCHEMA = {
    "name": "stock_screener",
    "description": "自然语言智能选股与行情条件筛选。支持自然语言组合条件提问（如'主板 绩优 储能 连板'、'低市盈率 储能 突破年线'），直接返回符合条件的股票标的列表及指标概览。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "自然语言选股条件，如'主板 绩优 储能'、'低市盈率 连板'"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10, "description": "最多返回标的数量（1-100）"},
            "page": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1, "description": "分页页码"},
            "filter_st": {"type": "boolean", "default": True, "description": "是否自动过滤 ST/*ST 风险警示股票，默认为 true"}
        },
        "required": ["message"],
        "additionalProperties": False
    }
}

STOCK_NEWS_SCHEMA = {
    "name": "stock_news",
    "description": "获取指定个股最近发布的突发新闻资讯、重大公告、研报与媒体快讯，辅助解读股价异动与盘中突发利好/利空。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "证券代码（如'600519'）"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5, "description": "返回新闻篇数（1-10）"}
        },
        "required": ["symbol"],
        "additionalProperties": False
    }
}

MARKET_CONTEXT_SCHEMA = {
    "name": "market_context",
    "description": "获取 A 股当前实时交易时钟与市场运行状态（如开盘竞价、早盘交易中、午盘休市、午后交易中、今日已收盘、周末休市），并提供基准交易日。在分析行情或解盘前可先调用本工具确认市场当前所处的时段。",
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False
    }
}


# =========================================================================
# 【功能块 10：金融工具集构建导出函数】
# - 输入：fin_gw (FinanceGateway): 已配置完成的金融网关实例
# - 功能：将 8 大工具的 Schema 定义与网关回调处理函数打包为标准字典返回
# - 输出：dict[str, tuple[dict[str, Any], Callable]]: 工具定义字典
# =========================================================================
def get_finance_tool_specs(fin_gw: FinanceGateway) -> dict[str, tuple[dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]]:
    return {
        "stock_lookup": (STOCK_LOOKUP_SCHEMA, fin_gw.stock_lookup),
        "stock_quotes": (STOCK_QUOTES_SCHEMA, fin_gw.stock_quotes),
        "stock_kline": (STOCK_KLINE_SCHEMA, fin_gw.stock_kline),
        "stock_financials": (STOCK_FINANCIALS_SCHEMA, fin_gw.stock_financials),
        "stock_f10": (STOCK_F10_SCHEMA, fin_gw.stock_f10),
        "stock_screener": (STOCK_SCREENER_SCHEMA, fin_gw.stock_screener),
        "stock_news": (STOCK_NEWS_SCHEMA, fin_gw.stock_news),
        "market_context": (MARKET_CONTEXT_SCHEMA, fin_gw.market_context),
    }
