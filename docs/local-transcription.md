# 本地课堂语音转写

课程平台的“转写”导出可能为空、过短，或者只包含课程元信息。FuckClassroom 在存在课堂录像时会优先使用本地 `faster-whisper`，平台转写只在没有可用录像或本地 ASR 失败时作为兜底。

## 实现方式

- 使用 `faster-whisper`。
- 默认模型：`small`。
- 默认设备：CPU。
- 默认计算精度：CPU 使用 `int8`，CUDA 可使用 `float16`。
- 默认语言：中文（`zh`）。
- 默认启用 VAD 和 batched transcription。
- 默认 ASR CPU 线程数：4。
- 默认 ASR worker 数：2。
- 默认最多同时运行 2 个本地 ASR 任务。
- 默认音频预取队列：3。
- 转写结果会缓存在课次输出目录的 `local_transcript.txt`，再次提取时直接复用。
- 最终可读结果会同时写入 `transcript.md`。

## 远程录像处理

程序不会把数 GB 的完整课堂录像保存到本地。

对于较短录像或不支持 Range 的媒体地址，会直接把远程录像交给 PyAV/faster-whisper 流式解码。

对于较长录像，如果服务器确认支持 HTTP Range，则启用分块音频流水线：

- 每块约 10 分钟；
- 相邻块保留约 1.5 秒重叠；
- 使用 2 个解码 Worker 并行准备音频；
- 按设置中的“音频预取队列”提前准备后续块；
- 识别失败时会自动回退到整段流式解码。

因此即使不会把完整视频保存到磁盘，网络层仍然需要读取用于语音识别的媒体数据。

## 首次使用

安装项目依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

第一次进行本地转写时，faster-whisper 会下载所需模型到：

```text
data/models/faster-whisper/
```

模型下载完成后会在后续任务中复用。

## 配置

推荐直接在“设置 > 文字提取”中调整本地语音识别参数。

也可以使用环境变量覆盖：

```text
FUCKCLASSROOM_ASR_MODEL=small
FUCKCLASSROOM_ASR_DEVICE=cpu
FUCKCLASSROOM_ASR_COMPUTE_TYPE=int8
FUCKCLASSROOM_ASR_LANGUAGE=zh
FUCKCLASSROOM_ASR_CPU_THREADS=4
FUCKCLASSROOM_ASR_NUM_WORKERS=2
FUCKCLASSROOM_ASR_MAX_CONCURRENT_TASKS=2
FUCKCLASSROOM_AUDIO_PREFETCH_QUEUE_SIZE=3
FUCKCLASSROOM_ASR_BATCH_SIZE=8
FUCKCLASSROOM_ASR_USE_BATCHED=true
FUCKCLASSROOM_ASR_SIMPLIFIED_CHINESE=true
```

有可用 NVIDIA CUDA 环境时可以尝试：

```text
FUCKCLASSROOM_ASR_DEVICE=cuda
FUCKCLASSROOM_ASR_COMPUTE_TYPE=float16
```

模型也可以改成 `base`、`medium` 等 faster-whisper 支持的模型。模型越大通常准确率越高，但首次下载、显存/内存占用和推理时间也会增加。

## 平台转写兜底

如果没有可用录像，或本地 Whisper 运行失败，程序会尝试读取平台转写。

平台转写不会无条件采用。程序会去掉课程名称、教师、日期、节次等常见元信息后检查剩余正文长度；内容过短时会视为低质量结果并拒绝使用，避免把“只有标题和元数据”的文档当成有效课堂转写。
