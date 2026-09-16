# 本地课堂语音转写

课程平台的“转写”导出可能为空。FuckClassroom 会优先尝试平台转写；如果平台未提供、返回空内容或导出失败，会自动使用课堂录像执行本地语音识别。

## 实现方式

- 使用 `faster-whisper`。
- 默认模型：`small`。
- 默认设备：CPU。
- 默认计算精度：CPU 使用 `int8`，CUDA 使用 `float16`。
- 默认语言：中文（`zh`）。
- 启用 VAD，减少静音片段的无效推理。
- 直接把课堂录像 URL 交给 PyAV/faster-whisper 解码，不把数 GB 的完整录像保存到本地。
- 同一时间只运行一个本地 ASR 任务，避免多个 Whisper 模型任务同时占满内存或显存。
- 转写结果会缓存在课次输出目录的 `local_transcript.txt`，再次提取时直接复用。

> 注意：对于音视频复用在同一个 MP4 中的录像，虽然不会在磁盘保存整个视频，网络层仍可能需要读取大部分媒体数据；如果平台提供独立音轨/HLS 音频轨，后续还可以进一步降低网络流量。

## 首次使用

重新安装项目依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

第一次进行本地转写时，faster-whisper 会下载所需模型到：

```text
data/models/faster-whisper/
```

## 可选环境变量

```text
FUCKCLASSROOM_ASR_MODEL=small
FUCKCLASSROOM_ASR_DEVICE=cpu
FUCKCLASSROOM_ASR_COMPUTE_TYPE=int8
FUCKCLASSROOM_ASR_LANGUAGE=zh
```

有可用 NVIDIA CUDA 环境时可以尝试：

```text
FUCKCLASSROOM_ASR_DEVICE=cuda
FUCKCLASSROOM_ASR_COMPUTE_TYPE=float16
```

模型也可以改成 `base`、`medium` 等 faster-whisper 支持的模型。模型越大通常准确率更高，但首次下载、内存占用和推理时间也会增加。
