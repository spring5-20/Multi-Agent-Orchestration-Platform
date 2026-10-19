/**
 * 实时语音采集用的 AudioWorklet。
 *
 * 浏览器每 128 个采样点回调一次 process()，这里把它们攒成固定长度的帧后
 * 回传主线程；主线程再转成 16bit PCM 发往后端（后端要求 16kHz 单声道 PCM）。
 */
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const frameSize = (options && options.processorOptions && options.processorOptions.frameSize) || 1024;
    this._frameSize = frameSize;
    this._buffer = new Float32Array(frameSize);
    this._offset = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) {
      return true;
    }
    for (let i = 0; i < channel.length; i += 1) {
      this._buffer[this._offset] = channel[i];
      this._offset += 1;
      if (this._offset === this._frameSize) {
        this.port.postMessage(this._buffer.slice(0));
        this._offset = 0;
      }
    }
    // 返回 true 保持节点常驻，否则静音几帧后浏览器会回收该处理器
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);
