import asyncio
import base64
import contextlib
import logging
from typing import Any

import dashscope

from ....config import settings

logger = logging.getLogger(__name__)

# 初始化 API Key（与 tts / embeddings 保持一致，避免依赖其导入顺序）
dashscope.api_key = settings.DASHSCOPE_API_KEY  # type: ignore[assignment]
Event = tuple[str, str]  # ("delta", 文本) / ("done", 最终文本) / ("error", 信息)


def _delta(value: Any) -> Event | None:
    """构造增量转写事件，空文本返回 None。"""
    text = str(value or "")
    if not text:
        return None
    return ("delta", text)


def _extract_item_text(item: Any) -> str:
    """从单个消息 item 里抽取转写文本（兼容多种结构）。"""
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("transcript") or part.get("text") or ""))
        return "".join(parts)
    return str(item.get("transcript") or item.get("text") or "")


def _extract_output_text(output: Any) -> str:
    """从 ``response.output`` 列表里抽取完整转写文本。"""
    if not isinstance(output, list):
        return ""
    return "".join(_extract_item_text(item) for item in output)


def parse_transcript_event(message: Any) -> Event | None:
    """把 omni 实时回调里的一条事件解析成内部事件。"""
    if not isinstance(message, dict):
        return None
    event_type = message.get("type")

    if event_type == "error":
        error = message.get("error") or {}
        detail = error.get("message") if isinstance(error, dict) else str(error or "")
        return ("error", str(detail or event_type))

    if event_type == "conversation.item.input_audio_transcription.text":
        # text 为已确认部分，stash 为未确认尾音，完整文本 = text + stash
        text = str(message.get("text") or "")
        stash = str(message.get("stash") or "")
        return _delta(text + stash)

    if event_type == "conversation.item.input_audio_transcription.completed":
        return ("done", _extract_item_text(message.get("item")))
    if event_type == "response.done":
        response = message.get("response") or {}
        return ("done", _extract_output_text(response.get("output")))
    return None


class RealtimeASRTranscriptor:
    """Qwen3-ASR 实时识别会话，桥接 dashscope 回调线程与 asyncio 事件循环。"""

    def __init__(self, model: str, loop: asyncio.AbstractEventLoop) -> None:
        self._model = model
        self._loop = loop
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._conversation: Any = None
        self._alive = False
        self._frame_count = 0

    def _clear_events(self) -> None:
        """丢弃队列里残留的识别事件，避免上一轮结果串到新会话。"""
        while True:
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                return

    def start(self) -> None:
        """建立 WebSocket 会话并下发识别配置（阻塞，调用方需放到线程池）。"""
        from dashscope.audio.qwen_omni import MultiModality, OmniRealtimeCallback, OmniRealtimeConversation
        from dashscope.audio.qwen_omni.omni_realtime import TranscriptionParams

        owner = self

        class _Callback(OmniRealtimeCallback):
            """把 dashscope 线程里的识别事件投递到 asyncio 队列。"""

            def on_event(self, message: Any) -> None:  # noqa: N802
                event = parse_transcript_event(message)
                if event is not None:
                    owner._loop.call_soon_threadsafe(owner._events.put_nowait, event)
                elif isinstance(message, dict):
                    logger.info("[qwen3-asr] 收到事件: %s", message.get("type"))

        self._clear_events()
        self._frame_count = 0
        conversation = OmniRealtimeConversation(model=self._model, callback=_Callback())
        conversation.connect()
        conversation.update_session(
            output_modalities=[MultiModality.TEXT],
            enable_input_audio_transcription=False,
            enable_turn_detection=settings.VOICE_ASR_TURN_MODE == "server_vad",
            turn_detection_type="server_vad",
            turn_detection_silence_duration_ms=settings.VOICE_ASR_SILENCE_DURATION_MS,
            transcription_params=TranscriptionParams(
                language=settings.VOICE_ASR_LANGUAGE,
                sample_rate=16000,
                input_audio_format="pcm",
            ),
        )
        self._conversation = conversation
        self._alive = True

    def append_audio(self, frame: bytes) -> bool:
        """送入一帧裸 PCM 音频，返回 False 表示会话未就绪或已失效。"""
        if not self._alive:
            return False
        try:
            self._conversation.append_audio(base64.b64encode(frame).decode("ascii"))
            self._frame_count += 1
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("实时识别发送音频失败，标记失效: %s", exc)
            self._alive = False
            return False

    def commit(self) -> None:
        """落定已缓存的音频（manual 模式由前端 end_turn 驱动）。"""
        if not self._alive:
            return
        try:
            self._conversation.commit()
            logger.info("已提交音频，落定本句（累计 %d 帧）", self._frame_count)
        except Exception as exc:  # noqa: BLE001
            logger.warning("提交音频失败，标记失效: %s", exc)
            self._alive = False

    def stop(self) -> None:
        """结束会话并关闭连接（可能阻塞，调用方需放到线程池）。"""
        self._alive = False
        if self._conversation is not None:
            with contextlib.suppress(Exception):
                self._conversation.end_session_async()
            with contextlib.suppress(Exception):
                self._conversation.close()
            self._conversation = None

    async def get_event(self) -> Event | None:
        """非阻塞取一条识别事件。"""
        try:
            return self._events.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def wait_event(self, timeout_seconds: float = 0.1) -> Event | None:
        """阻塞等待一条识别事件，超时返回 None。"""
        try:
            return await asyncio.wait_for(self._events.get(), timeout_seconds)
        except TimeoutError:
            return None
