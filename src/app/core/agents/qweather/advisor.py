"""出行建议生成：把天气信息交给大模型生成出行建议。"""

from ...deepseek import stream_chat


async def generate_travel_advice(weather_text: str) -> str:
    """根据天气信息调用 DeepSeek 生成中文出行建议。

    Args:
        weather_text: 天气工具返回的文本描述。

    Returns:
        大模型生成的出行建议文本。
    """
    system_prompt = (
        "你是一个贴心的出行助手。请根据用户提供的天气信息，"
        "用简洁、实用的中文给出出行建议，可涵盖穿衣、防晒/防雨、带伞、"
        "出行方式、户外活动注意事项等，并结合体感温度给出提醒。"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"以下是当前天气信息：\n{weather_text}\n\n请给我出行建议。"},
    ]

    chunks: list[str] = []
    async for chunk in stream_chat(messages):
        chunks.append(chunk)
    return "".join(chunks)
