import json
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...core.agents.qweather import agent as qweather_agent
from ...core.agents.qweather import client as qweather
from ...core.agents.qweather.advisor import generate_travel_advice

router = APIRouter(prefix="/qweather", tags=["天气管理"])


@router.get("/weather/now")
async def weather_now(location: str) -> dict[str, Any]:
    """查询指定经纬度位置（纬度,经度）的实时天气。"""
    data = await qweather.get_current_weather(location)
    return {"location": location, "weather": data}


@router.get("/weather/10d")
async def weather_10d(city: str, days: int = Query(10, ge=1, le=10)) -> dict[str, Any]:
    """根据城市名查询未来 N 天（默认 10 天）的每日天气预报。"""
    location = await qweather.lookup_city(city)
    latitude = float(location["lat"])
    longitude = float(location["lon"])
    forecast = await qweather.get_daily_forecast(latitude, longitude, days=days)
    return {
        "city": {
            "name": location.get("name"),
            "id": location.get("id"),
            "adm1": location.get("adm1"),
            "adm2": location.get("adm2"),
            "country": location.get("country"),
            "latitude": latitude,
            "longitude": longitude,
        },
        "days": forecast.get("days") or [],
    }


@router.get("/travel-advice")
async def travel_advice(location: str) -> dict[str, Any]:
    """查询实时天气并生成出行建议。"""
    data = await qweather.get_current_weather(location)
    weather_text = qweather.format_current_weather(data, location)
    advice = await generate_travel_advice(weather_text)
    return {"location": location, "weather": weather_text, "advice": advice}


class ChatRequest(BaseModel):
    """大模型问答请求体。"""

    question: str


@router.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    """大模型问答（流式 SSE）：模型自动调用天气工具并给出出行建议。"""

    async def event_stream() -> Any:
        async for event in qweather_agent.stream_events(req.question):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        yield 'data: {"type": "done"}\n\n'

    return StreamingResponse(event_stream(), media_type="text/event-stream")
