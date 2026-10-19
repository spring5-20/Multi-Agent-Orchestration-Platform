"""语音合成（TTS）抽象层：统一接口 + 可切换实现。"""

import base64
import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import Protocol, runtime_checkable

import dashscope

from ....config import settings

logger = logging.getLogger(__name__)

# 初始化 API Key（与 core.embeddings 保持一致，避免依赖其导入顺序）
dashscope.api_key = settings.DASHSCOPE_API_KEY  # type: ignore[assignment]

_tts: "TTS | None" = None


def get_tts() -> "TTS":
    """获取语音合成单例（根据配置切换后端）。"""
    global _tts
    if _tts is None:
        _tts = build_tts()
    return _tts


@dataclass
class TTSResult:
    """语音合成结果。"""

    audio: bytes | None = None
    url: str | None = None
    fmt: str = "mp3"  # mp3 / wav / pcm

    def to_base64(self) -> str:
        """转成 base64，便于嵌入 JSON 返回给前端播放。"""
        return base64.b64encode(self.audio).decode("ascii") if self.audio else ""


@runtime_checkable
class TTS(Protocol):
    """语音合成统一接口。

    新后端只需实现本协议并在 :func:`build_tts` 注册，即可无缝替换。
    """

    def synthesize(self, text: str) -> TTSResult:
        """把文本合成为音频。"""
        ...


class DashScopeTTS:
    """基于阿里云百炼 Qwen-TTS 的语音合成（同步模式）。"""

    def __init__(self, model: str | None = None, voice: str | None = None) -> None:
        self._model = model or settings.VOICE_TTS_MODEL
        self._voice = voice or settings.VOICE_TTS_VOICE

    def synthesize(self, text: str) -> TTSResult:
        """把文本合成为音频，优先返回可播放的音频 URL。"""
        from typing import cast

        from dashscope.api_entities.dashscope_response import TextToSpeechResponse
        from dashscope.audio.qwen_tts import SpeechSynthesizer

        response = cast(
            TextToSpeechResponse,
            SpeechSynthesizer.call(
                model=self._model,
                text=text,
                voice=self._voice,
            ),
        )
        if response.status_code != HTTPStatus.OK:
            raise RuntimeError(f"语音合成失败: {response.message}")

        audio = response.output.audio if response.output else None
        if audio is None:
            raise RuntimeError("语音合成返回空音频")
        # 注意：dashscope 的 TextToSpeechOutput 未把 audio 转成对象，需按 dict 访问
        url = audio.get("url") if isinstance(audio, dict) else getattr(audio, "url", None)
        data = audio.get("data") if isinstance(audio, dict) else getattr(audio, "data", None)
        if url:
            return TTSResult(audio=None, url=url, fmt="wav")
        if data:
            raw = base64.b64decode(data)
            if not raw:
                raise RuntimeError("语音合成返回空音频")
            return TTSResult(audio=raw, url=None, fmt="wav")
        raise RuntimeError("语音合成返回空音频")


class DashScopeCosyVoiceTTS:
    """基于阿里云百炼 CosyVoice 2（tts_v2）的语音合成（同步模式）。

    返回 MP3 音频字节，相比 ``qwen-tts`` 是更新的 CosyVoice 系列，首包更稳。
    """

    def __init__(self, model: str | None = None, voice: str | None = None) -> None:
        self._model = model or settings.VOICE_TTS_MODEL
        self._voice = voice or settings.VOICE_TTS_VOICE

    def synthesize(self, text: str) -> TTSResult:
        """把文本合成为 MP3 音频字节。"""
        from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

        synthesizer = SpeechSynthesizer(
            model=self._model,
            voice=self._voice,
            format=AudioFormat.MP3_16000HZ_MONO_128KBPS,
        )
        audio = synthesizer.call(text)
        if not audio:
            raise RuntimeError("语音合成返回空音频")
        return TTSResult(audio=audio, url=None, fmt="mp3")


def build_tts(backend: str | None = None) -> TTS:
    """根据配置创建语音合成后端，实现 TTS 的平滑切换。"""
    backend = backend or settings.VOICE_TTS_BACKEND
    if backend == "dashscope":
        if settings.VOICE_TTS_MODEL.startswith("cosyvoice"):
            logger.info("语音合成后端：dashscope（CosyVoice 2）")
            return DashScopeCosyVoiceTTS()
        logger.info("语音合成后端：dashscope（qwen-tts）")
        return DashScopeTTS()
    raise ValueError(f"不支持的语音合成后端: {backend}")
