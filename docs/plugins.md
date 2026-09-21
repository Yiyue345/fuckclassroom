# 插件架构

`pluginization` 分支把应用拆成一个始终启用的核心壳层，以及按需启用的功能插件。

## 当前内置插件

| 插件 ID | 功能 | 主要 Service | Lifecycle |
| --- | --- | --- | --- |
| `core_ui` | 通用 UI、设置、任务、共享认证基础设施 | settings/cache/task/credential/verification | 始终启用 |
| `classroom` | 课程录播、课件、OCR/ASR/AI、课堂派、直播/PPT/二维码 | classroom/auth/extraction/download/ketangpai | 启动时同步学期并验证会话 |
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
└── static/
```

职责固定为：

- `plugin.py`：只描述 `PluginSpec`、导航、设置面板以及 Route/Service/Lifecycle hook；
- `services.py`：创建并注册该插件自己的服务；
- `routes.py`：创建该插件自己的 `APIRouter` 和 HTTP handler；
- `lifecycle.py`：只处理 startup/shutdown 与后台任务；
- `templates/`、`static/`：只放插件自己的 UI 资源。

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

- `classroom` 的学期同步、录播会话验证、课堂派会话验证属于 startup；
- `course_selection` 的教务会话验证、缓存刷新、自动选课启动属于 startup；
- 自动选课停止和 Hy2 停止属于 shutdown。

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
