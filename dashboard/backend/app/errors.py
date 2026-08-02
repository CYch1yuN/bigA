"""统一错误结构与安全相关异常。

接口统一返回 {ok: false, error: {code, message}}，
绝不泄露堆栈、命令路径或密钥。
"""

from __future__ import annotations

from typing import Any


class DashboardError(Exception):
    """可安全展示给客户端的业务错误。"""

    status_code = 400

    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        if status_code is not None:
            self.status_code = status_code


def error_body(code: str, message: str) -> dict[str, Any]:
    """构造统一错误结构。"""
    return {"ok": False, "error": {"code": code, "message": message}}


def ok_body(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造统一成功结构。"""
    body: dict[str, Any] = {"ok": True}
    if data is not None:
        body.update(data)
    return body
