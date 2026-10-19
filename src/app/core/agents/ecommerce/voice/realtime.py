"""实时语音对话：Qwen3-ASR 实时识别桥接 WebSocket。

分句模式由 ``VOICE_ASR_TURN_MODE`` 决定（默认 manual）：前端检测到说完一段话
后发送 ``end_turn``，后端提交已缓存的音频落定本句，短暂等待最后的转写文本，
再走流式对话并逐句合成语音回复，降低首句播放延迟。会话建立完成后先回一个
``ready``，前端收到后才开始送音频，避免会话未就绪时丢帧。
"""

import asyncio
import contextlib
import json
import logging
import re
from typing import cast

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from ....config import settings
from ..session import new_conversation_id
from ..stream import stream_chat
from .asr import RealtimeASRTranscriptor
from .pool import WorkerPool
from .tts import TTSResult, get_tts

logger = logging.getLogger(__name__)

_SENTENCE_BOUNDARY = re.compile(r"[。！？!?；;\n]+")

# 识别建连/关闭与语音合成都是阻塞调用，统一放到各自的线程池，进程内共享单例
_asr_pool: WorkerPool | None = None
_tts_pool: WorkerPool | None = None


def _get_asr_pool() -> WorkerPool:
    """获取实时识别线程池（懒加载单例，限制建连与关闭的并发）。"""
    global _asr_pool
    if _asr_pool is None:
        _asr_pool = WorkerPool(
            "voice-asr",
            max_workers=settings.VOICE_ASR_MAX_WORKERS,
            max_concurrency=settings.VOICE_ASR_MAX_WORKERS,
        )
    return _asr_pool


def _get_tts_pool() -> WorkerPool:
    """获取语音合成线程池（懒加载单例，限制同时合成的句数）。"""
    global _tts_pool
    if _tts_pool is None:
        _tts_pool = WorkerPool(
            "voice-tts",
            max_workers=settings.VOICE_TTS_MAX_WORKERS,
            max_concurrency=settings.VOICE_TTS_MAX_WORKERS,
        )
    return _tts_pool


async def close_voice_pools() -> None:
    """关闭识别与合成线程池（幂等，供应用退出时调用）。"""
    global _asr_pool, _tts_pool
    pools = [pool for pool in (_asr_pool, _tts_pool) if pool is not None]
    _asr_pool = None
    _tts_pool = None
    for pool in pools:
        await pool.close()


def _split_sentences(text: str) -> tuple[list[str], str]:
    """按句末标点把文本切成完整句，返回 (完整句列表, 剩余未闭合文本)。"""
    sentences: list[str] = []
    last = 0
    for match in _SENTENCE_BOUNDARY.finditer(text):
        sentences.append(text[last : match.end()])
        last = match.end()
    return sentences, text[last:]


def _merge_transcript(current: str, incoming: str) -> str:
    """把一次识别事件合并进当前转写文本。

    实时识别回调存在两种语义，这里统一兼容：

    - 累计语义：``incoming`` 是「本句到目前为止」的完整文本，直接覆盖 ``current``；
    - 增量语义：``incoming`` 只是新增片段，追加到 ``current`` 之后。
    """
    incoming = incoming.strip()
    if not incoming:
        return current
    if not current:
        return incoming
    if incoming == current:
        return current
    if incoming.startswith(current):
        return incoming
    if current.startswith(incoming):
        return current
    return f"{current}{incoming}"


async def _synthesize(text: str) -> TTSResult:
    """合成一句语音（阻塞调用放 TTS 线程池，超时抛 TimeoutError）。"""
    return cast(
        TTSResult,
        await _get_tts_pool().run(get_tts().synthesize, text, timeout_s=settings.VOICE_TTS_TIMEOUT_S),
    )


async def _send_reply(
    websocket: WebSocket,
    user_id: int,
    conversation_id: str,
    user_text: str,
) -> None:
    """流式对话 + 逐句并行合成语音，边生成边回推音频。"""
    await websocket.send_json({"type": "reply_start", "user_text": user_text})

    sentences: list[str] = []
    buffer = ""
    tts_tasks: list[asyncio.Task[TTSResult]] = []

    try:
        async for chunk in stream_chat(
            user_id,
            user_text,
            conversation_id,
            skip_memory_write=settings.VOICE_SKIP_MEMORY_WRITE,
        ):
            buffer += chunk
            complete, buffer = _split_sentences(buffer)
            for sentence in complete:
                sentence = sentence.strip()
                if sentence:
                    sentences.append(sentence)
                    tts_tasks.append(asyncio.create_task(_synthesize(sentence)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("流式对话失败: %s", exc)
        await websocket.send_json(
            {"type": "reply", "user_text": user_text, "text": "抱歉，我暂时无法回答，请稍后再试。"},
        )
        return

    if buffer.strip():
        sentences.append(buffer.strip())
        tts_tasks.append(asyncio.create_task(_synthesize(buffer.strip())))

    full_text = "".join(sentences)

    # 按顺序取每句音频回推（TTS 已在后台并行合成）
    for task in tts_tasks:
        try:
            tts_result = await task
        except Exception as exc:  # noqa: BLE001
            logger.warning("语音合成失败，跳过该句: %s", exc)
            continue
        await websocket.send_json(
            {
                "type": "audio",
                "audio_url": tts_result.url,
                "audio_base64": tts_result.to_base64(),
                "audio_format": tts_result.fmt,
            },
        )

    await websocket.send_json({"type": "reply", "user_text": user_text, "text": full_text})


async def handle_realtime(websocket: WebSocket, user_id: int) -> None:
    """处理一次实时语音对话连接：识别 → 对话 → 合成 → 回推。"""
    loop = asyncio.get_running_loop()
    transcriptor = RealtimeASRTranscriptor(settings.VOICE_ASR_MODEL, loop)
    conversation_id = new_conversation_id()
    pending_text = ""
    end_turn = asyncio.Event()

    async def recv_loop() -> None:
        """持续接收音频帧与控制指令。"""
        while True:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                return
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes"):
                transcriptor.append_audio(message["bytes"])
            elif message.get("text"):
                try:
                    data = json.loads(message["text"])
                except json.JSONDecodeError:
                    logger.warning("收到非法 JSON 控制帧，已忽略")
                    continue
                kind = data.get("type")
                if kind == "close":
                    return
                if kind == "end_turn":
                    # manual 模式：前端检测到说完，落定本句
                    transcriptor.commit()
                    end_turn.set()
                # start_turn 无需处理

    recv_task = asyncio.create_task(recv_loop())

    try:
        try:
            await _get_asr_pool().run(transcriptor.start, timeout_s=settings.VOICE_ASR_TIMEOUT_S)
        except TimeoutError:
            await websocket.send_json({"type": "error", "message": "识别会话建立超时"})
            return
        except Exception as exc:  # noqa: BLE001
            await websocket.send_json({"type": "error", "message": f"识别会话建立失败：{exc}"})
            return

        # 会话就绪后通知前端，前端才开始送音频
        await websocket.send_json(
            {"type": "ready", "silence_duration_ms": settings.VOICE_ASR_SILENCE_DURATION_MS},
        )

        while True:
            # 一句话说完：短暂等待最后的转写文本落定，再触发对话
            if end_turn.is_set():
                end_turn.clear()
                deadline = loop.time() + 0.8
                last_change = loop.time()
                while loop.time() < deadline:
                    event = await transcriptor.wait_event(timeout_seconds=0.05)
                    if event is None:
                        if loop.time() - last_change > 0.3:
                            break
                        continue
                    kind, text = event
                    if kind == "delta":
                        pending_text = _merge_transcript(pending_text, text)
                    elif kind == "done":
                        if text:
                            pending_text = text
                    elif kind == "error":
                        await websocket.send_json({"type": "error", "message": text})
                    last_change = loop.time()
                user_text = pending_text.strip()
                pending_text = ""
                if user_text:
                    await _send_reply(websocket, user_id, conversation_id, user_text)
                else:
                    await websocket.send_json({"type": "idle"})
                continue

            event = await transcriptor.wait_event(timeout_seconds=0.1)
            if event is not None:
                kind, text = event
                if kind == "delta":
                    pending_text = _merge_transcript(pending_text, text)
                    await websocket.send_json({"type": "transcript", "text": pending_text})
                elif kind == "done":
                    if text:
                        pending_text = text
                elif kind == "error":
                    await websocket.send_json({"type": "error", "message": text})

            if recv_task.done():
                with contextlib.suppress(Exception):
                    recv_task.result()
                break
    except WebSocketDisconnect:
        logger.info("客户端已断开，结束实时对话")
    finally:
        recv_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await recv_task
        with contextlib.suppress(TimeoutError):
            await _get_asr_pool().run(transcriptor.stop, timeout_s=settings.VOICE_ASR_TIMEOUT_S)
