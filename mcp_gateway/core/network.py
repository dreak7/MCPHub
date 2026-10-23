"""MCPHub Finance 核心安全网络传输与连接层。
提供拦截 301/302 重定向的安全 Opener，杜绝 Authorization/Token 凭据泄露，
并提供标准 HTTP 网络通信基础设施。
"""

from __future__ import annotations

import logging
import socket
import time
import urllib.parse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


# =========================================================================
# 【功能块：安全 HTTP 传输层与重定向拦截处理器】
# - 输入：HTTP 请求对象与重定向目标 URL
# - 功能：严格拦截 POST 请求发生时的任何 301/302/303 重定向跳转，
#         彻底阻断 Authorization/Token 凭据被重定向转发泄露到第三方恶意主机或私网目标
# - 输出：抛出 HTTPError 明确阻断
# =========================================================================
class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise HTTPError(req.full_url, code, f"HTTP redirect ({code}) is disallowed for security in financial gateway", headers, fp)


_SECURE_OPENER = build_opener(NoRedirectHandler)
