"""认证安全工具：密码哈希与 JWT 签发/解析。"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt

from .config import settings

_PBKDF2_ITERATIONS = 600_000
_HASH_ALGORITHM = "sha256"


def hash_password(password: str) -> str:
    """用 PBKDF2 对密码做单向哈希，返回可存储的格式字符串。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(_HASH_ALGORITHM, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    """校验明文密码与已存哈希是否匹配。"""
    try:
        _, iterations, salt_hex, digest_hex = hashed.split("$")
        salt = bytes.fromhex(salt_hex)
        digest = hashlib.pbkdf2_hmac(_HASH_ALGORITHM, password.encode("utf-8"), salt, int(iterations))
        return secrets.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def create_access_token(subject: str) -> str:
    """为指定主体（用户 uuid）签发访问令牌。"""
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> str:
    """解析访问令牌，返回主体（用户 uuid）。"""
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    return str(payload["sub"])
