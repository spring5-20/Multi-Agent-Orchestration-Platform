"""和风天气（QWeather）API 客户端。

使用新版 API（``/weather/v1/...``），通过 API Host 域名调用，请求头携带 JWT 认证令牌。
"""

from typing import Any

import httpx

from ...config import settings
from .jwt import generate_qweather_jwt


def _parse_location(location: str) -> tuple[float, float]:
    """解析「纬度,经度」格式的坐标字符串。"""
    parts = [part.strip() for part in location.split(",")]
    if len(parts) != 2:
        raise ValueError("location 格式应为「纬度,经度」，例如 39.92,116.41")
    try:
        latitude, longitude = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ValueError("location 中的经纬度必须是数字，例如 39.92,116.41") from exc
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("纬度范围 [-90, 90]，经度范围 [-180, 180]")
    return latitude, longitude


async def _request(api_path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """向和风天气 API Host 发起 GET 请求并返回解析后的 JSON。

    Args:
        api_path: 接口路径，如 ``/weather/v1/current/39.92/116.41``。
        params: 查询参数。

    Raises:
        RuntimeError: 配置缺失、HTTP 失败或和风天气返回错误码时。
    """
    host = settings.QWEATHER_API_HOST
    if not host:
        raise RuntimeError("和风天气配置不完整，请检查 QWEATHER_API_HOST")

    token = generate_qweather_jwt()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://{host.rstrip('/')}{api_path}"

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        response = await client.get(url, headers=headers, params=params)

    if response.status_code != httpx.codes.OK:
        raise RuntimeError(f"和风天气请求失败: HTTP {response.status_code} - {response.text}")

    data: dict[str, Any] = response.json()
    code = data.get("code")
    if code is not None and str(code) != "200":
        raise RuntimeError(f"和风天气返回错误: {code} - {data.get('msg')}")
    return data


async def get_current_weather(location: str) -> dict[str, Any]:
    """查询指定经纬度位置的实时天气。

    Args:
        location: ``纬度,经度`` 格式，如 ``39.92,116.41``。
    """
    latitude, longitude = _parse_location(location)
    return await _request(f"/weather/v1/current/{latitude:.2f}/{longitude:.2f}")


def _format_value(value: Any, unit: str | None = None) -> str:
    """把数值与单位拼成字符串，缺失时返回「未知」。"""
    if value is None:
        return "未知"
    return f"{value}{unit or ''}"


def _format_humidity(value: Any) -> str:
    """新版接口湿度为 [0, 1] 的小数，转成百分比展示。"""
    if value is None:
        return "未知"
    if isinstance(value, (int, float)) and 0 <= value <= 1:
        return f"{value * 100:.0f}%"
    return f"{value}"


def format_current_weather(data: dict[str, Any], location: str) -> str:
    """把新版实时天气 JSON 转成适合大模型阅读的简洁文本。"""
    condition = data.get("condition") or {}
    temperature = data.get("temperature") or {}
    feels_like = data.get("feelsLike") or {}
    wind = data.get("wind") or {}
    wind_direction = wind.get("direction") or {}
    wind_speed = wind.get("speed") or {}
    wind_gust = data.get("windGust") or {}

    lines = [
        f"地点：{location}",
        f"天气：{condition.get('text') or '未知'}",
        f"温度：{_format_value(temperature.get('value'), temperature.get('unit'))}",
        f"体感温度：{_format_value(feels_like.get('value'), feels_like.get('unit'))}",
        f"相对湿度：{_format_humidity(data.get('humidity'))}",
        f"风向：{wind_direction.get('compass') or wind_direction.get('degree') or '未知'}",
        f"风力等级：{wind.get('scale') or '未知'}",
        f"风速：{_format_value(wind_speed.get('value'), wind_speed.get('unit'))}",
        f"阵风风速：{_format_value(wind_gust.get('value'), wind_gust.get('unit'))}",
    ]
    return "\n".join(lines)


async def lookup_city(city: str) -> dict[str, Any]:
    """通过 GeoAPI 城市搜索，返回第一个匹配城市的信息。

    Args:
        city: 城市名或 LocationID。

    Returns:
        城市信息字典，至少包含 ``name``、``id``、``lat``、``lon``、``adm1`` 等字段。

    Raises:
        RuntimeError: 未找到匹配城市时。
    """
    data = await _request("/geo/v2/city/lookup", {"location": city})
    locations = data.get("location") or []
    if not locations:
        raise RuntimeError(f"未找到城市「{city}」")
    location = locations[0]
    if not isinstance(location, dict):
        raise RuntimeError(f"和风天气返回的城市数据格式异常：{location!r}")
    return location


async def get_daily_forecast(latitude: float, longitude: float, days: int = 10) -> dict[str, Any]:
    """查询指定经纬度位置未来 ``days`` 天的每日天气预报。

    Args:
        latitude: 纬度。
        longitude: 经度。
        days: 预报天数，取值范围 1-10，默认 10。

    Raises:
        ValueError: ``days`` 超出 1-10 范围时。
    """
    if not 1 <= days <= 10:
        raise ValueError("days 取值范围为 1-10")
    return await _request(
        f"/weather/v1/daily/{latitude:.2f}/{longitude:.2f}",
        {"days": days},
    )


def format_daily_forecast(data: dict[str, Any], city: str) -> str:
    """把每日预报 JSON 转成适合大模型阅读的文本。"""
    days = data.get("days") or []
    lines = [f"地点：{city}", f"未来 {len(days)} 天每日天气预报："]
    for day in days:
        start = (day.get("forecastStartTime") or "")[:10]
        daytime = day.get("daytime") or {}
        nighttime = day.get("nighttime") or {}
        day_condition = (daytime.get("condition") or {}).get("text") or "未知"
        night_condition = (nighttime.get("condition") or {}).get("text") or "未知"
        temp_max = day.get("temperatureMax") or {}
        temp_min = day.get("temperatureMin") or {}
        daytime_wind = daytime.get("wind") or {}
        wind_direction = (daytime_wind.get("direction") or {}).get("compass") or "未知"
        lines.append(
            f"{start}：白天 {day_condition}，夜间 {night_condition}，"
            f"气温 {temp_min.get('value', '?')}~{temp_max.get('value', '?')}"
            f"{temp_max.get('unit') or '°C'}，白天风力 {wind_direction} {daytime_wind.get('scale') or '未知'} 级"
        )
    return "\n".join(lines)
