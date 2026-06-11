# Fuck Classroom

桂电智慧课堂本地辅助工具。当前支持：

- 浏览器手动登录并保存本地会话；
- 按学期筛选课程，列出课次资源；
- 缓存课程名、课次和视频/课件资源链接，减少重复访问平台接口；
- 在下载中心查看本地已下载文件；
- 下载课堂录像、课件和平台转写；
- 从平台转写 `.docx` 提取文本；
- 对图片型 `.pptx` 使用 OCR 提取课件文字；
- 将转写和课件 OCR 合并后发送给 AI 生成课程总结；
- 在页面中保存 AI、OCR、默认学期和提取完成后跳转设置。

## 启动

```powershell
.\.venv\Scripts\python.exe main.py
```

默认访问：

```text
http://127.0.0.1:8765
```

首页会先显示系统选择：

- 课程录播助手：`/classroom`
- WebVPN 选课辅助：`/selection`

可选端口：

```powershell
$env:FUCKCLASSROOM_PORT="8766"
.\.venv\Scripts\python.exe main.py
```

## OCR

推荐在页面右上角进入“设置”修改 OCR 配置。配置会保存到：

```text
data/settings.json
```

默认 OCR 引擎为 `auto`：

1. 优先使用 `rapidocr-onnxruntime`；
2. 其次使用系统 `tesseract`；
3. 最后在配置了 AI key 时使用 AI 视觉 OCR。

可选配置：

```powershell
$env:FUCKCLASSROOM_OCR_ENGINE="rapidocr"
$env:FUCKCLASSROOM_OCR_MAX_IMAGES="120"
```

`FUCKCLASSROOM_OCR_ENGINE` 可取：

- `auto`
- `rapidocr`
- `tesseract`
- `ai`

## AI 总结

AI 接口兼容 OpenAI Chat Completions。

推荐在“设置”页面填写 Base URL、模型和 API Key。也可以用环境变量临时覆盖：

```powershell
$env:FUCKCLASSROOM_AI_API_KEY="你的 API Key"
$env:FUCKCLASSROOM_AI_BASE_URL="https://api.openai.com/v1"
$env:FUCKCLASSROOM_AI_MODEL="gpt-4o-mini"
$env:FUCKCLASSROOM_OCR_MODEL="gpt-4o-mini"
```

不要把真实 API Key 写入代码或提交到仓库。

## 进度页

点击“提取文字”或“生成 AI 总结”后，会进入任务进度页。进度页会展示当前阶段和百分比；如果在设置中启用“提取文字完成后自动打开提取页”，提取成功后会自动跳转到结果页。

## 缓存

课程列表、学期列表和课程详情会缓存到：

```text
data/cache/
```

设置页可以清除缓存。清除缓存不会删除登录会话、已下载文件或文字提取结果。

## 下载文件

课件和平台转写会保存到：

```text
data/downloads/
```

下载中心 `/downloads` 可以查看和删除本地已下载文件，并使用 KB/MB/GB 显示文件大小。课件或转写文件已存在时，课程详情页会显示“已下载”，再次点击下载会直接返回本地文件，不会重复请求平台下载。

## WebVPN 选课辅助

校外访问本科教务系统时，可以使用独立的 WebVPN 会话：

```powershell
.\.venv\Scripts\python.exe select_course.py login
```

这个命令会打开 `https://v.guet.edu.cn/`，需要手动完成 WebVPN 登录。会话保存到：

```text
data/webvpn/storage_state.json
```

它和课程录播平台的登录会话 `data/auth/storage_state.json` 是分开的。

打开本科教务首页：

```powershell
.\.venv\Scripts\python.exe select_course.py open
```

辅助定位并人工确认选课：

```powershell
.\.venv\Scripts\python.exe select_course.py assist --name "课程名关键字"
```

也可以组合课程代码或教师名：

```powershell
.\.venv\Scripts\python.exe select_course.py assist --code "课程代码" --teacher "教师名"
```

脚本不会绕过验证码、二次确认或学校认证，也不会做高频抢课轮询。`assist` 命令只会在你进入选课页面后查找候选课程，并在你输入 `YES` 后点击一次候选行里的选课/选择/提交按钮。

也可以使用可视化页面：

```text
http://127.0.0.1:8765/selection
```

页面版支持打开 WebVPN 登录窗口、打开本科教务系统，以及在进入选课页面后高亮匹配课程。页面版不会自动提交选课。

页面版还支持抓取选课相关 XHR/fetch 接口：

```text
http://127.0.0.1:8765/selection/captures
```

抓包记录会保存到 `data/webvpn/captures/`，并会对 token、cookie、password 等敏感字段做基础脱敏。

页面中的“执行一次选课”只在找到唯一匹配课程时点击一次候选行中的选课按钮；若没有匹配或匹配多条，只会高亮候选，不会点击。
