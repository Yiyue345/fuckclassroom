# 插件架构

第三方插件作者需要查看可调用接口、Hook、ServiceContainer、后台任务和完整示例时，请阅读 [`docs/plugin-api.md`](plugin-api.md)。

`pluginization` 分支把应用拆成一个始终启用的核心壳层，以及按需启用的功能插件。

## 当前内置插件

| 插件 ID | 功能 | 主要 Service | Lifecycle |
| --- | --- | --- | --- |
| `core_ui` | 通用 UI、设置、任务、共享认证基础设施 | settings/cache/task/credential/verification | 始终启用 |
| `classroom` | 课程、直播、回放、PPT 与下载基础能力 | classroom/auth/download | 启动时同步学期并验证录播会话 |
| `processing` | 课次文字处理编排与输出页面 | extraction | 无 |
| `transcription` | faster-whisper 本地语音转写 | transcription | 无 |
| `ocr` | PPT 图片 OCR | ocr | 无 |
| `ai_summary` | AI 课程总结 | ai_summary | 无 |
| `attendance` | 畅课/课堂派二维码签到 | ketangpai/QR scanner | 启动时验证课堂派会话 |
| `course_selection` | 本科选课、全校开课、定时选课、Hy2 | course_selection/auto_selection/hy2 | 启动时验证会话并启动自动选课；退出时停止后台服务 |

## 启用插件

默认不设置环境变量时启用全部内置插件。

PowerShell 只启用课程录播：

```powershell
$env:FUCKCLASSROOM_PLUGINS="classroom"
python main.py
```

只启用本科选课：

```powershell
$env:FUCKCLASSROOM_PLUGINS="course_selection"
python main.py
```

同时启用：

```powershell
$env:FUCKCLASSROOM_PLUGINS="classroom,course_selection"
python main.py
```

设置为空字符串时只启动 `core_ui`。`core_ui` 无论如何都会启用，因为它提供共享设置、任务和认证基础设施。

插件依赖会自动补齐。例如只指定 `transcription` 时，会自动启用 `processing` 和 `classroom`；只指定 `attendance` 时会自动启用 `classroom`，但不会加载文字提取、Whisper、OCR 或 AI 总结。

测试或嵌入运行时也可以直接调用：

```python
app = create_app(enabled_plugins={"classroom"})
```

## 插件目录约定

每个功能插件使用相同的物理结构：

```text
fuckclassroom/plugins/<plugin_id>/
├── __init__.py
├── plugin.py
├── services.py
├── routes.py
├── lifecycle.py      # 只有需要启停逻辑的插件才需要
├── templates/
│   └── <plugin_id>/   # 完整页面使用命名空间目录
└── static/
```

职责固定为：

- `plugin.py`：只描述 `PluginSpec`、导航、设置面板以及 Route/Service/Lifecycle hook；
- `services.py`：创建并注册该插件自己的服务；
- `routes.py`：创建该插件自己的 `APIRouter` 和 HTTP handler；
- `lifecycle.py`：只处理 startup/shutdown 与后台任务；
- `templates/`、`static/`：只放插件自己的 UI 资源；完整页面使用 `<plugin_id>/...` 命名空间避免模板重名；
- 插件级 CSS/JS 通过 `PluginSpec.stylesheets` / `PluginSpec.scripts` 注册，core `base.html` 只负责聚合已启用插件的资源。

`fuckclassroom/web/app.py` 现在只是 composition root：创建 registry/runtime、模板环境、挂载静态资源并启动 lifecycle，不包含具体功能路由。

## Route 插件化

每个功能拥有独立 `APIRouter`。路由先注册到所属 router，最后由 `PluginRuntime` 根据已启用插件决定是否挂载到 FastAPI。

因此禁用 `course_selection` 后：

- `/selection/*` 和 `/api/selection/*` 不会进入应用路由表；
- 选课插件静态资源不会挂载；
- QR/直播等课堂扩展路由只在 `classroom` 启用时注册。

共享路由（设置、账户、任务、健康检查、统一验证码 broker）属于 `core_ui`。

## Service 插件化

`ServiceContainer` 是显式的运行时服务容器。每个 `PluginSpec.service_factory` 只在插件启用时执行。

插件通过：

```python
context.services.add("my_service", service)
dependency = context.services.get("task_manager")
```

注册或获取服务。重复注册会直接报错，避免插件之间静默覆盖依赖。

例如禁用本科选课后，以下对象不会创建：

- `Hy2ProxyManager`
- `CourseSelectionAssistant`
- `AutoSelectionService`

禁用课程录播后，`ClassroomClient`、`LessonExtractionService`、`DownloadLibrary` 和课堂派 client 也不会创建。

## Lifecycle 插件化

`PluginSpec` 支持：

```python
PluginSpec(
    ...,
    service_factory=setup_services,
    route_factory=build_routes,
    startup=startup,
    shutdown=shutdown,
)
```

`PluginRuntime` 在 FastAPI lifespan 中按插件顺序执行 startup，并在退出时按相反顺序执行 shutdown。

插件通过 `context.create_task(...)` 创建的后台任务由 runtime 统一追踪；应用退出时会先取消并回收这些任务，再执行各插件 shutdown，避免服务已经关闭而后台任务仍在访问资源。

当前：

- `classroom` 的学期同步与录播会话验证属于 startup；
- `attendance` 的课堂派会话验证属于 startup；
- `course_selection` 的教务会话验证、缓存刷新、自动选课启动属于 startup；
- 自动选课停止和 Hy2 停止属于 shutdown。

## UI 资源插件化

UI 与后端使用相同的所有权边界。

当前根目录 `templates/` 只保留应用壳层 `base.html`；具体页面全部归所属插件。功能页面已经物理迁移到所属插件：

```text
core_ui/templates/core/...
classroom/templates/classroom/...
processing/templates/processing/...
course_selection/templates/selection/...
```

路由使用完整命名空间模板名，例如：

```python
templates.TemplateResponse(request, "classroom/course_detail.html", context)
templates.TemplateResponse(request, "selection/catalog.html", context)
```

功能专属前端资源也由插件所有：

```text
classroom/static/classroom.css
classroom/static/downloads.js
processing/static/processing.css
course_selection/static/selection.css
course_selection/static/selection_ui.js
core_ui/static/task_status.css
core_ui/static/task_status.js
```

`PluginRegistry.stylesheets` 和 `PluginRegistry.scripts` 只聚合当前已启用插件声明的资源，`base.html` 不硬编码功能插件。禁用插件后，它的路由、service、lifecycle、模板目录、静态挂载以及插件级 CSS/JS 会一起退出应用。

## 插件管理页面

应用内置 `/plugins` 插件管理页，统一管理内置和外部插件。

- 启用插件时会自动补齐 `requires` 依赖。
- 停用基础依赖时，会同时停用所有依赖它的插件。
- 插件配置保存到 `data/plugin_state.json`。
- 插件路由、服务、静态挂载和 lifecycle 在应用启动时完成，因此启停变更保存后需要重启应用。
- 若设置了 `FUCKCLASSROOM_PLUGINS`，或代码通过 `create_app(enabled_plugins=...)` 显式指定插件，管理页会进入只读模式，避免网页配置与启动参数互相覆盖。

外部插件安装到 `data/plugins/<plugin_id>/`。管理页既支持填写本机插件目录/ZIP 路径，也支持直接选择 ZIP 文件上传安装。新安装的外部插件默认保持停用。

最小外部插件包：

```text
my_plugin/
├── plugin.json
└── plugin.py
```

`plugin.json` 示例：

```json
{
  "id": "my_plugin",
  "name": "示例插件",
  "version": "0.1.0",
  "api_version": 1,
  "app_requires": ">=0.1,<0.2",
  "description": "插件说明",
  "requires": ["classroom"],
  "python_requires": ["httpx>=0.27"],
  "entry": "plugin.py"
}
```

`plugin.py` 需要导出 `build_plugin() -> PluginSpec`，并且 `PluginSpec.id`、`PluginSpec.requires`、`PluginSpec.api_version` 必须与 `plugin.json` 一致。外部插件会执行 Python 代码，只应安装可信来源的插件。

## 插件运行依赖

Python 依赖已经按插件拆分：

```text
requirements/
├── core.txt
├── classroom.txt
├── processing.txt
├── transcription.txt
├── ocr.txt
├── attendance.txt
├── course_selection.txt
└── all.txt
```

根目录 `requirements.txt` 只安装 core 依赖。完整开发/测试环境使用：

```powershell
python -m pip install -r requirements/all.txt
```

插件管理页会读取每个插件的依赖声明并显示已安装版本、缺失的必需依赖和缺失的可选增强。点击“安装/修复依赖”会创建后台任务，使用当前 Python 解释器执行 pip 安装；Playwright 安装完成后还会自动执行 `python -m playwright install chromium`。签到插件的 wxscan PyO3 扩展属于可选增强，编译失败只会记录 warning，仍可使用 OpenCV fallback。

内置插件当前依赖关系：

- `classroom`：Playwright + Chromium
- `processing`：Pillow
- `transcription`：faster-whisper、huggingface-hub、OpenCC
- `ocr`：rapidocr-onnxruntime
- `attendance`：cryptography、NumPy、OpenCV；wxscan 原生扩展为可选
- `course_selection`：Playwright + Chromium
- `ai_summary`：无额外 Python 包

外部插件可在 `plugin.json` 中声明：

```json
{
  "python_requires": [
    "httpx>=0.27",
    {
      "requirement": "orjson>=3.10",
      "import": "orjson",
      "label": "orjson",
      "optional": true
    }
  ]
}
```

外部插件依赖仅接受正常 PyPI requirement，不接受 URL、本地路径或 pip 参数。缺少必需依赖时外部插件不能启用；如果状态文件中残留了这种配置，启动时会跳过该插件及依赖它的插件，而不会阻止 core UI 启动。

## 插件 API 与版本兼容

核心插件 API 版本由 `fuckclassroom.core.plugins.PLUGIN_API_VERSION` 定义，当前为：

```text
PLUGIN_API_VERSION = 1
```

外部插件建议在 `plugin.json` 中显式声明：

```json
{
  "api_version": 1,
  "app_requires": ">=0.1,<0.2"
}
```

- `api_version`：插件接口版本。涉及 `PluginSpec`、生命周期 hook、ServiceContainer、Route/Static/Template 注册契约的破坏性变更时才递增。
- `app_requires`：可选的 PEP 440 FuckClassroom 版本约束，例如 `>=0.1,<0.2`。
- 旧插件若没有声明 `api_version`，当前按 API v1 兼容处理。
- `plugin.py` 返回的 `PluginSpec.api_version` 必须与 `plugin.json.api_version` 一致。
- API 版本不匹配或 `app_requires` 不满足时，插件可以继续保留在 `data/plugins/` 中并显示在管理页面，但不能启用，也不会执行插件入口代码。
- 如果状态文件中已经把不兼容插件标记为启用，启动时会安全跳过该插件以及依赖它的插件，保证 `core_ui` 仍可启动并展示具体兼容性错误。

当前应用版本来自 `fuckclassroom.__version__`，插件管理页会同时显示当前 FuckClassroom 版本与插件 API 版本。

## Manifest 懒加载

`plugin.py` 只保留插件元数据以及轻量 hook。Service/Lifecycle/Route 实现都在 hook 被 runtime 调用时再 import，因此禁用插件不会仅仅因为 registry 枚举了全部内置插件就加载其实现模块。

这意味着例如只启用 `classroom` 时，Whisper、OCR、AI 总结、课堂派签到和选课实现不会因为 import `fuckclassroom.plugins` 而被提前加载。

`processing` 也只在顶层依赖 `transcription/errors.py` 与 `ai_summary/errors.py` 这类轻量异常契约；Whisper engine 和 AI client 不会因为只启用课次处理而被连带 import。OCR 的 AI client 同样只在实际选择 AI OCR 时懒加载。

## 源码归属与兼容路径

功能实现现在以插件目录为唯一主路径：

```text
processing/extraction.py     课次处理与 PPT 文本编排
transcription/engine.py      faster-whisper
ocr/engine.py                RapidOCR / Tesseract 后端
ocr/compat.py                RapidOCR 兼容补丁
ai_summary/client.py         AI API client
attendance/scanner.py        二维码识别
attendance/ketangpai.py      课堂派 client
```

旧的 `fuckclassroom/classroom/extraction.py`、`transcription.py`、`ai.py`、`qr_scan.py`、`ketangpai.py` 和 `rapidocr_compat.py` 仅保留兼容 re-export，仓库内部代码和测试不再把这些旧路径当作实现入口。

`classroom/__init__.py` 也不会再无条件安装 OCR 兼容补丁或 failure guard，因此只启用 `classroom` 时不会初始化 OCR/ASR/AI/签到实现。

## 新增插件的最小流程

1. 在 `fuckclassroom/plugins/<plugin_id>/` 创建插件目录。
2. 在 `build_plugin()` 返回一个 `PluginSpec`。
3. 用 `service_factory` 注册插件自己的服务。
4. 将页面/API 路由放入插件 router，并由 runtime 按插件 ID 挂载。
5. 需要后台任务时实现 startup/shutdown，不要在 import 阶段启动线程或任务。
6. 模板放在插件 `templates/`，静态资源放在 `static/`。
7. 在 `fuckclassroom/plugins/__init__.py` 中加入插件构造器。
8. 增加“插件启用/禁用后路由和服务是否存在”的测试。

这个阶段的原则是：**插件未启用，不仅 UI 不显示，对应 Route 不注册、Service 不实例化、Lifecycle 不执行。**
