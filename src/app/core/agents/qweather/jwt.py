"""和风天气（QWeather）JWT 生成工具。

和风天气 API 采用 Ed25519 签名的 JWT 作为认证凭证：
- 请求头 ``alg`` 固定为 ``EdDSA``，``kid`` 为凭据 ID；
- 载荷包含 ``iss``（开发者 ID）、``sub``（项目 ID）、``iat`` 与 ``exp``。

私钥等敏感信息统一从 ``.env`` 配置读取。
"""

import time

import jwt

from ...config import settings


def _normalize_private_key(private_key: str) -> str:
    """把 ``.env`` 中常见的字面量 ``\\n`` 转义还原为真实换行，便于粘贴 PEM 私钥。"""

    if "\\n" in private_key:
        return private_key.replace("\\n", "\n")
    return private_key


def generate_qweather_jwt(
    *,
    subject: str | None = None,
    key_id: str | None = None,
    private_key: str | None = None,
    issuer: str | None = None,
    algorithm: str | None = None,
    ttl_seconds: int | None = None,
    leeway_seconds: int | None = None,
) -> str:
    """生成并返回一个用于和风天气 API 认证的 JWT 字符串。

    未显式传入的参数会回退到 ``Settings`` 中的对应配置；关键配置缺失时抛出
    ``RuntimeError``，避免生成缺少必要声明的令牌。

    Args:
        subject: ``sub`` 声明的项目 ID，默认取 ``settings.QWEATHER_JWT_SUBJECT``。
        key_id: JWT 头部的 ``kid``（凭证 ID），默认取 ``settings.QWEATHER_JWT_KEY_ID``。
        private_key: PEM 格式的 Ed25519 私钥，默认取 ``settings.QWEATHER_JWT_PRIVATE_KEY``。
        issuer: ``iss`` 声明的开发者 ID，默认取 ``settings.QWEATHER_JWT_ISSUER``。
        algorithm: 签名算法，默认取 ``settings.QWEATHER_JWT_ALGORITHM``。
        ttl_seconds: 令牌有效期（秒），默认取 ``settings.QWEATHER_JWT_TTL_SECONDS``。
        leeway_seconds: ``iat`` 相对当前时间向前偏移的秒数，默认取
            ``settings.QWEATHER_JWT_LEEWAY_SECONDS``。

    Returns:
        编码后的 JWT 字符串。

    Raises:
        RuntimeError: 当任一必要配置（私钥、``kid``、``iss``、``sub``）缺失时。
    """
    resolved_subject = subject if subject is not None else settings.QWEATHER_JWT_SUBJECT
    resolved_key_id = key_id if key_id is not None else settings.QWEATHER_JWT_KEY_ID
    resolved_private_key = private_key if private_key is not None else settings.QWEATHER_JWT_PRIVATE_KEY
    resolved_issuer = issuer if issuer is not None else settings.QWEATHER_JWT_ISSUER
    resolved_algorithm = algorithm if algorithm is not None else settings.QWEATHER_JWT_ALGORITHM
    resolved_ttl = ttl_seconds if ttl_seconds is not None else settings.QWEATHER_JWT_TTL_SECONDS
    resolved_leeway = leeway_seconds if leeway_seconds is not None else settings.QWEATHER_JWT_LEEWAY_SECONDS

    missing = [
        name
        for name, value in (
            ("QWEATHER_JWT_PRIVATE_KEY", resolved_private_key),
            ("QWEATHER_JWT_KEY_ID", resolved_key_id),
            ("QWEATHER_JWT_ISSUER", resolved_issuer),
            ("QWEATHER_JWT_SUBJECT", resolved_subject),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"和风天气 JWT 签发配置不完整，请检查 {', '.join(missing)}")

    # 上面的校验已保证私钥非空，这里显式收窄类型，便于静态类型检查
    assert resolved_private_key is not None

    now = int(time.time())
    iat = now - resolved_leeway
    payload: dict[str, object] = {
        "iss": resolved_issuer,
        "sub": resolved_subject,
        "iat": iat,
        "exp": iat + resolved_ttl,
    }
    headers = {"kid": resolved_key_id}

    return jwt.encode(
        payload,
        _normalize_private_key(resolved_private_key),
        algorithm=resolved_algorithm,
        headers=headers,
    )


if __name__ == "__main__":
    print(generate_qweather_jwt())
