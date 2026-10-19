"""和风天气查询的 LangChain 工具封装，供 Agent / LangGraph 调用。"""

from langchain_core.tools import tool

from .client import (
    format_current_weather,
    format_daily_forecast,
    get_current_weather,
    get_daily_forecast,
    lookup_city,
)


@tool
async def query_qweather_now(city: str) -> str:
    """查询指定城市的实时天气，返回天气状况、温度、体感、风力、湿度等信息。

    当用户询问某地当前天气，或需要根据当前天气决定出行、穿衣、是否带伞时使用。

    Args:
        city: 城市名，例如「北京」「杭州」。
    """
    location = await lookup_city(city)
    latitude = float(location["lat"])
    longitude = float(location["lon"])
    data = await get_current_weather(f"{latitude},{longitude}")
    return format_current_weather(data, city)


@tool
async def query_qweather_forecast(city: str, days: int = 10) -> str:
    """查询指定城市未来 N 天的每日天气预报，返回每天白天/夜间的天气和最高最低气温。

    当用户询问未来几天天气、几号适合出行、多日出行计划时使用。

    Args:
        city: 城市名，例如「杭州」。
        days: 预报天数，取值范围 1-10，默认 10。
    """
    location = await lookup_city(city)
    latitude = float(location["lat"])
    longitude = float(location["lon"])
    data = await get_daily_forecast(latitude, longitude, days=days)
    return format_daily_forecast(data, city)


qweather_tools = [query_qweather_now, query_qweather_forecast]
