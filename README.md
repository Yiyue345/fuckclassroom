# Fuck Classroom

桂电智慧课堂本地辅助工具。当前支持：

- 浏览器手动登录并保存本地会话；
- 按学期筛选课程，列出课次资源；
- 缓存课程名、课次和视频/课件资源链接，减少重复访问平台接口；
- 在下载中心查看本地已下载文件；
- 下载课堂录像、课件和平台转写；
- 在选课批次开始后执行持久化定时选课任务；
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
- 本科教务选课辅助：`/selection`

可选端口：

```powershell
$env:FUCKCLASSROOM_PORT="8766"
.\.venv\Scripts\python.exe main.py
```
## 项目结构

Python 代码按职责放在 `fuckclassroom/` 下的子包中：

```text
fuckclassroom/
├── auth/               # CAS、登录会话和加密凭据
├── classroom/          # 录播平台接口、下载、OCR 和 AI 总结
├── core/               # 配置、缓存、设置和后台任务
├── course_selection/   # 教务接口、浏览器兼容、定时选课和 Hy2
└── web/                # FastAPI 应用和页面路由
```

选课模块进一步按职责拆分：

```text
course_selection/
├── assistant.py        # 对外统一入口
├── session.py          # 登录与会话管理
├── api.py              # 课程查询、选课和退课接口
├── browser.py          # 必要的浏览器兼容流程
├── automation.py       # 定时自动选课
├── models.py           # 数据模型
├── helpers.py          # 响应解析与通用辅助函数
└── proxy.py            # Hy2 本地代理
```

`rust_module/hy2_proxy/` 是项目内置的独立 Hy2 Rust crate，包含 `hy2_serve` 命令行组件及 MPL-2.0 许可证。

根目录下的 `config.py`、`settings.py`、`cache.py` 和 `tasks.py` 仅保留兼容导入，已有外部脚本无需立即修改。


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

## 本科教务选课辅助

可视化页面会参考 GUET Hub 的策略探测 `https://iw.guet.edu.cn/`：校园网内直接访问本科教务；校外启用 Hy2 时优先通过本地代理访问教务和 CAS，代理不可用时自动回退 WebVPN。网络入口切换后需要为当前入口重新登录，避免混用 Cookie 和选课 token。

Hy2 Rust 源码已经内置在项目中：

```text
rust_module/hy2_proxy
```

设置页不再要求填写 GUET Hub 或其他本地源码路径。安装 Rust 工具链后点击“构建内置组件”，产物会保存到已忽略的 `data/hy2/hy2_serve.exe`。运行时只在内存中读取随机本地 SOCKS5 凭据，并提供仅监听 `127.0.0.1` 的 HTTP CONNECT 桥接。仍可用环境变量 `FUCKCLASSROOM_HY2_ENABLED` 控制是否启用。

未启用 Hy2 或代理启动失败时，校外访问继续使用独立的 WebVPN 会话：


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

页面版登录会根据当前网络自动打开校园网直连、Hy2 代理直连或 WebVPN 本科教务入口，后续课程查询、人数刷新、选课和退课接口沿用同一入口。

课程列表可以把指定教学班加入“定时选课”队列：

- 任务保存在 `data/webvpn/auto_selection_jobs.json`，程序重启后会继续等待；
- 到达当前批次开始时间后才会通过接口提交，选上或批次结束后自动停止；
- 课程满额时进入候补队列，定时读取人数，检测到空位后再发起选课请求；
- 普通失败、会话恢复和满课人数监测间隔可在设置页独立配置，默认分别为 5、30、30 秒；
- 队列运行中会话失效时会使用已保存凭据自动重登；短信动态码仍通过本地页面弹窗确认；
- 可在 `/selection/auto` 查看尝试次数、平台返回信息，并取消、重试或删除任务。

校园网直连和 Hy2 登录从 `https://bkjwtest.guet.edu.cn/student/sso/login` 进入 CAS，`/student/home` 只用于登录后的会话验证。在设置页保存统一桂电账号和密码后，课程录播与本科教务会共用这份凭据，在启动检测或点击登录时优先静默恢复失效会话。短信动态码会在本地页面弹窗中完成；只有图形验证码、密码错误等确实需要操作登录页的情况才打开浏览器窗口。

凭据整体使用当前 Windows 用户的 DPAPI 加密，单独保存到 `data/account/credentials.json`，不会写入 `data/settings.json`，也不能被其他 Windows 用户直接解密。旧的 `data/webvpn/credentials.json` 会在启动时自动迁移，设置页可以随时清除这份共用凭据。

页面版还支持抓取选课相关 XHR/fetch 接口：

```text
http://127.0.0.1:8765/selection/captures
```

抓包记录会保存到 `data/webvpn/captures/`，并会对 token、cookie、password 等敏感字段做基础脱敏。

页面中的“执行一次选课”只在找到唯一匹配课程时点击一次候选行中的选课按钮；若没有匹配或匹配多条，只会高亮候选，不会点击。
