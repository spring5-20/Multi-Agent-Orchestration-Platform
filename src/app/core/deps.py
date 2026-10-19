"""FastAPI 公共依赖：认证。"""

import jwt as pyjwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .security import decode_access_token

_bearer = HTTPBearer(auto_error=False)


def resolve_user_id(token: str | None) -> int | None:
    """把访问令牌解析成用户 id，解析失败返回 None。

    HTTP 依赖与 WebSocket 握手共用这段逻辑：浏览器无法为 WebSocket 设置自定义
    请求头，令牌只能通过查询参数传递，但两者的校验口径必须一致。
    """
    if not token:
        return None
    try:
        return int(decode_access_token(token))
    except (ValueError, pyjwt.PyJWTError):
        return None


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> int:
    """从 Bearer 令牌解析当前登录用户 id（整型）。"""
    if credentials is None:
        raise HTTPException(status_code=401, detail="未提供认证令牌")
    user_id = resolve_user_id(credentials.credentials)
    if user_id is None:
        raise HTTPException(status_code=401, detail="认证令牌无效或已过期")
    return user_id
