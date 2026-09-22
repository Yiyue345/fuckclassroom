# 插件 API v1 开发手册

本文面向 FuckClassroom 第三方插件作者，说明插件在 **Plugin API v1** 中可以使用的接口、运行顺序、服务容器、路由、模板、后台任务和兼容性约束。

> 当前 `PLUGIN_API_VERSION = 1`。本文描述的是稳定插件契约，不等同于“仓库里所有 Python 对象都可以长期依赖”。

## 1. 最小插件长什么样

一个最小外部插件只需要两个文件：

```text
hello_plugin/
├── plugin.json
└── plugin.py
```

`plugin.json`：

```json
{
  "id": "hello_plugin",
  "name": "Hello Plugin",
  "version": "0.1.0",
  "api_version": 1,
  "app_requires": ">=0.1,<0.2",
  "description": "最小示例插件",
  "requires": [],
  "entry": "plugin.py"
}
```

`plugin.py`：

```python
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from fuckclassroom.core.plugins import (
    PLUGIN_API_VERSION,
    PluginContext,
    PluginSpec,
)


def build_routes(context: PluginContext) -> APIRouter:
    router = APIRouter()

    @router.get("/hello-plugin")
    def hello() -> JSONResponse:
        return JSONResponse(
            {
                "message": "hello",
                "plugin_id": context.plugin_id,
            }
        )

    return router


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="hello_plugin",
        name="Hello Plugin",
        api_version=PLUGIN_API_VERSION,
        route_factory=build_routes,
    )
```

把目录或 ZIP 包添加到插件管理页后，FuckClassroom 会读取 `plugin.json`、检查兼容性和依赖，再在插件真正启用时调用 `build_plugin()`。

---

## 2. 稳定 API 从哪里导入

Plugin API v1 的核心类型位于：

```python
from fuckclassroom.core.plugins import (
    PLUGIN_API_VERSION,
    AccountPanel,
    NavigationGroup,
    NavigationItem,
    PluginContext,
    PluginRegistry,
    PluginSpec,
    ServiceContainer,
    SettingsPanel,
)
```

第三方插件应优先依赖这些对象。

以下对象属于 runtime 实现细节，不应直接调用：

- `PluginRuntime._context()`
- `PluginRuntime.attach_router()`
- `PluginRuntime._routers`
- `PluginRuntime._background_tasks`
- `PluginContext._runtime`
- `PluginManager` 的私有方法
- 任何以下划线开头的模块级函数、属性或字段

如果未来必须破坏本文列出的稳定契约，`PLUGIN_API_VERSION` 会递增。

---

## 3. `plugin.json` 清单

外部插件首先由 `plugin.json` 描述。

常用字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 插件唯一 ID，必须以小写字母开头，只能包含小写字母、数字、`_`、`-` |
| `name` | string | 展示名称 |
| `version` | string | 插件自身版本 |
| `api_version` | integer | FuckClassroom Plugin API 版本；当前为 `1` |
| `app_requires` | string | 可选，PEP 440 应用版本约束，例如 `>=0.1,<0.2` |
| `description` | string | 插件描述 |
| `requires` | string[] | 依赖的其他插件 ID |
| `python_requires` | array | Python 运行依赖 |
| `entry` | string | 插件入口，默认 `plugin.py` |
| `order` | integer | 插件管理页的展示顺序，默认 `100` |

`python_requires` 可以直接写 requirement：

```json
{
  "python_requires": [
    "httpx>=0.27"
  ]
}
```

也可以提供更完整的信息：

```json
{
  "python_requires": [
    {
      "requirement": "orjson>=3.10",
      "import": "orjson",
      "label": "orjson",
      "optional": true
    }
  ]
}
```

其中：

- `requirement`：pip requirement；
- `import`：安装后用于探测的 Python import 名；
- `label`：插件管理页展示名；
- `optional`：是否属于可选增强。

外部插件不能在 `python_requires` 中使用 URL、本地路径或 pip 参数。

---

## 4. `build_plugin() -> PluginSpec`

插件入口必须导出：

```python
def build_plugin() -> PluginSpec:
    ...
```

`PluginSpec` 是插件向 runtime 声明能力的主要接口。

### PluginSpec 字段

| 字段 | 作用 |
| --- | --- |
| `id` | 插件 ID，必须与 `plugin.json.id` 一致 |
| `name` | 插件名称 |
| `order` | runtime hook 执行和 UI 聚合时的排序值 |
| `api_version` | 插件 API 版本，外部插件必须与清单一致 |
| `requires` | 依赖插件，必须与 `plugin.json.requires` 一致 |
| `navigation_groups` | 向左侧导航注册分组与入口 |
| `settings_panels` | 向设置页注册模板面板 |
| `account_panels` | 向“账户与认证”页注册账户卡片 |
| `sidebar_templates` | 向侧栏底部注入状态模板 |
| `topbar_status_templates` | 向顶栏会话状态区域注入模板 |
| `system_labels` | 面包屑中的 system 名称映射 |
| `stylesheets` | 插件启用时自动注入到 `<head>` 的 CSS URL |
| `scripts` | 插件启用时在 core `app.js` 之后自动加载的 JS URL |
| `ui_assets` | `UIAsset` 声明的按页面加载资源，兼容原有全局资源 |
| `ui_slots` | `UISlot` 声明的页面模板贡献 |
| `template_dir` | 插件 Jinja 模板目录 |
| `static_dir` | 插件静态资源目录 |
| `service_factory` | 服务注册 hook |
| `route_factory` | 路由构建 hook |
| `startup` | 应用启动 hook |
| `shutdown` | 应用退出 hook |
| `settings_saved` | 全局设置持久化后通知当前插件；适合刷新运行时配置 |
| `clear_session` | Core 请求清除登录状态时，由插件自行清理自己的会话 |

推荐让 `plugin.py` 保持轻量，只放元数据和 lazy wrapper：

```python
from pathlib import Path

from fuckclassroom.core.plugins import (
    PLUGIN_API_VERSION,
    PluginContext,
    PluginSpec,
)


PLUGIN_DIR = Path(__file__).resolve().parent


def setup_services(context: PluginContext):
    from .services import setup_services as setup
    return setup(context)


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


def startup(context: PluginContext):
    from .lifecycle import startup as hook
    return hook(context)


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="my_plugin",
        name="My Plugin",
        api_version=PLUGIN_API_VERSION,
        order=100,
        service_factory=setup_services,
        route_factory=build_routes,
        startup=startup,
        template_dir=PLUGIN_DIR / "templates",
        static_dir=PLUGIN_DIR / "static",
    )
```

这样插件被发现但未启用时，不会提前 import 重型依赖。

---

## 5. Hook 的执行顺序

当前 API v1 的装配顺序是：

```text
读取 plugin.json
    ↓
兼容性 / Python 依赖检查
    ↓
import plugin.py
    ↓
build_plugin()
    ↓
PluginRegistry
    ↓
service_factory(context)
    ↓
创建全局 Jinja templates service
    ↓
route_factory(context)
    ↓
挂载 static_dir
    ↓
挂载 APIRouter
    ↓
FastAPI lifespan 启动
    ↓
startup(context)
    ↓
应用运行
    ↓
取消 context.create_task() 创建的后台任务
    ↓
shutdown(context)   ← 按插件顺序反向执行
```

插件 hook 按 `PluginSpec.order`，再按插件 ID 排序执行。

### 一个重要限制

API v1 **不会根据 `requires` 做拓扑排序**。

因此，如果插件 A 的 `service_factory` 要直接获取插件 B 注册的 service：

1. A 必须在 `requires` 中声明 B；
2. A 的 `PluginSpec.order` 应大于 B。

外部插件默认 `order=100`，目前高于所有内置功能插件，通常可以安全读取内置插件 service。

---

## 6. `PluginContext`

每一个 hook 都会收到：

```python
PluginContext
```

### `context.plugin_id`

当前插件 ID：

```python
context.plugin_id
# "my_plugin"
```

不要在共享代码里硬编码插件 ID，优先使用这个字段。

### `context.config`

当前应用配置对象。目前实际类型是 `AppConfig`。

适合读取公共路径，例如：

```python
data_dir = context.config.data_dir
cache_dir = context.config.cache_dir
downloads_dir = context.config.downloads_dir
outputs_dir = context.config.outputs_dir
```

应把 `context.config` 当作只读对象。

诸如 ASR、OCR、Hy2 等功能专用字段属于具体功能实现，不建议第三方插件把它们当作通用 Plugin API。

### `context.services`

全局 `ServiceContainer`，用于插件之间显式交换对象：

```python
task_manager = context.services.get("task_manager")
```

详见下一节。

### `context.logger`

应用 logger。可以使用常见 logging 方法：

```python
context.logger.debug("debug")
context.logger.info("plugin started")
context.logger.warning("something unusual")
context.logger.error("operation failed")
context.logger.exception("operation failed")
```

插件不应自行修改全局 logging 配置。

### `context.registry`

当前已启用插件的只读视图：

```python
if context.registry.is_enabled("classroom"):
    ...

plugin_ids = context.registry.plugin_ids
spec = context.registry.get("classroom")
```

建议只使用：

- `is_enabled(plugin_id)`
- `get(plugin_id)`
- `plugin_ids`
- `plugins`

不要在插件运行期间动态 `register()` 插件。

### `context.create_task(awaitable)`

创建由 runtime 管理的 asyncio 后台任务：

```python
async def polling_loop():
    while True:
        await refresh()
        await asyncio.sleep(30)


async def startup(context: PluginContext):
    context.create_task(polling_loop())
```

这些任务会在应用退出时被 runtime 自动取消并等待回收。

它适用于**长生命周期 async 任务**。

不要用它直接运行阻塞 I/O 或 CPU 重任务；这类工作应交给 `task_manager`。

---

## 7. `ServiceContainer`

`ServiceContainer` 是插件间依赖注入的核心接口。

### 注册 service

```python
def setup_services(context: PluginContext):
    client = MyClient()
    context.services.add("my_plugin.client", client)
```

推荐第三方插件使用命名空间键：

```text
my_plugin.client
my_plugin.store
my_plugin.scheduler
```

避免使用过于通用的 `client`、`store`，因为 service key 全局唯一。

重复注册会抛出 `ValueError`。

### 必须存在的 service

```python
service = context.services.get("task_manager")
```

不存在时抛出 `KeyError`。

### 可选 service

```python
service = context.services.maybe("classroom_client")

if service is not None:
    ...
```

也可以：

```python
if context.services.has("classroom_client"):
    ...
```

### 查看当前 service key

```python
keys = context.services.keys
```

这个能力适合诊断，不建议用“扫描 keys 猜功能”的方式建立依赖；应使用 `requires`。

---

## 8. Core 提供的 service

`core_ui` 永远启用，因此下列基础 service 在正常插件运行环境中存在。

| key | 当前类型 | 用途 |
| --- | --- | --- |
| `settings_store` | `SettingsStore` | FuckClassroom 全局设置 |
| `cache_store` | `JsonCache` | 简单 JSON 缓存 |
| `task_manager` | `TaskManager` | 可视化后台任务 |
| `credential_store` | `SelectionCredentialStore` | 统一登录凭据存储 |
| `verification_broker` | `LoginVerificationBroker` | 登录短信验证协调 |
| `login_lock` | `threading.Lock` | 登录流程串行化 |
| `templates` | `Jinja2Templates` | 页面模板渲染；仅 route 阶段及之后可用 |

### 稳定性建议

第三方插件优先使用：

- `task_manager`
- `cache_store`
- `templates`

`credential_store`、`verification_broker`、`login_lock` 属于 FuckClassroom 登录实现细节；除非插件明确扩展桂电认证流程，否则不要依赖。

`settings_store` 当前保存的是 FuckClassroom 内置 `AppSettings`。API v1 尚未提供“第三方插件自定义设置 schema”机制，因此第三方插件不要往全局 `AppSettings` 强塞字段。

---

## 9. 后台任务接口

对于会持续几秒以上、需要进度、日志或取消功能的同步任务，推荐使用：

```python
task_manager = context.services.get("task_manager")
```

### 创建任务

```python
def worker(progress):
    progress(10, "开始处理")
    progress.log("读取输入", "info")

    for index in range(10):
        progress.raise_if_cancelled()
        do_work(index)
        progress((index + 1) * 10, f"处理 {index + 1}/10")

    return "/my-plugin/result"


task = task_manager.start(
    "My Plugin · 处理任务",
    worker,
)
```

worker 返回值如果是字符串，会作为任务的 `result_url`。

如果任务完成后允许任务中心自动打开结果页，可以由创建任务的插件显式声明：

```python
task = task_manager.start(
    "My Plugin · 处理任务",
    worker,
    auto_open_result=True,
)
```

`auto_open_result` 是任务元数据；Core 不会根据任务标题或插件类型猜测这个行为。

### `TaskProgressReporter`

worker 收到的 `progress` 对象支持：

```python
progress(35, "正在处理")
progress.detail(60, "当前子步骤")
progress.clear_detail()

progress.log("普通日志", "info")
progress.log("调试日志", "debug")
progress.log("警告", "warning")
progress.log("错误", "error")

if progress.cancel_requested:
    ...

progress.raise_if_cancelled()
```

取消是**协作式取消**。插件需要在循环、分片、网络请求之间主动调用 `raise_if_cancelled()`。

### HTTP 路由返回任务页面

FuckClassroom 当前提供：

```python
from fuckclassroom.web.responses import task_started_response
```

用法：

```python
@router.post("/my-plugin/run")
def run(request: Request):
    task = task_manager.start("My Plugin · Run", worker)
    return task_started_response(
        request,
        task,
        "/my-plugin/result",
    )
```

浏览器请求会跳转到任务详情页；接受 JSON 的客户端会得到：

```json
{
  "task_id": "...",
  "status_url": "/api/tasks/...",
  "result_url": "/my-plugin/result"
}
```

---

## 10. 路由接口

`route_factory` 应返回 FastAPI `APIRouter`：

```python
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse


def build_router(context: PluginContext) -> APIRouter:
    router = APIRouter()
    templates = context.services.get("templates")

    @router.get("/my-plugin", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "my_plugin/home.html",
            {
                "plugin_id": context.plugin_id,
            },
        )

    return router
```

runtime 会把 router 中的 routes 挂到主 FastAPI 应用。

### 路由命名建议

外部插件建议使用唯一 URL 前缀：

```text
/my-plugin/...
/api/my-plugin/...
```

API v1 不会自动检测不同插件之间的 URL 冲突。

---

## 11. 模板与静态资源

### `template_dir`

在 `PluginSpec` 中声明：

```python
template_dir=PLUGIN_DIR / "templates"
```

之后 route factory 可以通过：

```python
templates = context.services.get("templates")
```

渲染插件模板。

建议模板文件名带插件命名空间：

```text
templates/
└── my_plugin/
    ├── home.html
    └── status.html
```

并在路由中使用完整模板名：

```python
templates.TemplateResponse(request, "my_plugin/home.html", {...})
```

这样不同插件即使都有 `home.html`、`settings.html` 也不会发生 Jinja 模板名冲突。

避免与其他插件或 core 模板重名。

### `static_dir`

声明：

```python
static_dir=PLUGIN_DIR / "static"
```

### 插件级 CSS / JS

如果某个样式或脚本属于整个插件，而不是某一张页面，可以在 `PluginSpec` 中声明：

```python
PluginSpec(
    ...,
    static_dir=PLUGIN_DIR / "static",
    stylesheets=(
        "/plugins/my_plugin/static/plugin.css?v=1",
    ),
    scripts=(
        "/plugins/my_plugin/static/plugin.js?v=1",
    ),
)
```

`base.html` 通过 `page_assets(ui_page, kind)` 聚合当前已启用插件的全局与页面资源。因此：

- 插件禁用后，对应 CSS/JS 不会进入页面；
- core 不需要知道具体功能插件的资源文件名；
- 插件脚本会在 core `/static/app.js` 之后加载，可以使用 core 已初始化的通用浏览器 helper；
- 页面专属资源仍可以继续通过 Jinja 的 `head_extra` / `scripts` block 按页加载。

新插件建议用 `UIAsset` 声明页面资源、用 `UISlot` 扩展宿主页面，并复用 Jinja 表单组件。接口、加载顺序、插槽上下文和示例见 [UI 设计系统](ui-system.md)。

第三方插件应尽量避免把功能专属样式写进 core `/static/app.css`，也不要修改 core `/static/app.js`。


runtime 会自动挂载：

```text
/plugins/<plugin_id>/static
```

例如：

```html
<script src="/plugins/my_plugin/static/app.js"></script>
<link rel="stylesheet" href="/plugins/my_plugin/static/app.css">
```

插件无需自行调用 `app.mount()`。

---

## 12. 导航接口

插件可以向侧栏注册导航。

```python
from fuckclassroom.core.plugins import NavigationGroup, NavigationItem


NavigationGroup(
    key="my_plugin",
    label="我的插件",
    aria_label="我的插件导航",
    system="my_plugin",
    order=70,
    items=(
        NavigationItem(
            key="home",
            label="插件首页",
            href="/my-plugin",
            icon="settings",
            active_keys=("my-plugin-home",),
        ),
    ),
)
```

### `NavigationItem`

| 字段 | 说明 |
| --- | --- |
| `key` | 导航项唯一 key |
| `label` | 显示名称 |
| `href` | 跳转地址 |
| `icon` | core `static/icons.svg` 中的 symbol ID |
| `active_keys` | 哪些模板 `active_nav` 值会高亮此项 |
| `badge_template` | 可选，导航项右侧 badge 模板 |

API v1 的导航图标只能引用 core SVG sprite 中已有的 symbol。自定义导航图标目前没有独立注册接口。

### `NavigationGroup`

`status_template` 可以向导航分组标题右侧插入轻量状态，例如登录状态。

该模板会出现在全局侧栏，应该避免执行昂贵操作。

---

## 13. 设置面板接口

插件可以注册：

```python
def build_settings_context(services, request):
    client = services.get("my_plugin.client")
    return {"status": client.status()}


SettingsPanel(
    key="my-plugin-settings",
    label="My Plugin",
    template="my_plugin/settings.html",
    order=70,
    context_factory=build_settings_context,
)
```

字段：

| 字段 | 说明 |
| --- | --- |
| `key` | HTML anchor / 面板 key |
| `label` | 设置页左侧导航名称 |
| `template` | Jinja 模板 |
| `order` | 排序 |
| `in_form` | 是否放入 core 的 `/settings` form |
| `show_in_nav` | 是否显示在设置页左侧导航 |
| `checkbox_fields` | core settings form 中需要处理“未提交即 false”的 checkbox 名 |
| `context_factory` | 可选；页面请求时调用，参数为 `ServiceContainer` 和 request，返回当前面板上下文 |

面板模板会拿到统一变量 `panel_context`，值就是 `context_factory` 返回的字典：

```jinja2
<div>{{ panel_context.status }}</div>
```

如果不需要额外数据，可以省略 `context_factory`，此时 `panel_context` 为空字典。factory 只会为当前启用插件注册的面板执行；不要在其中做长时间阻塞工作。

### 第三方插件的重要限制

当前 `SettingsStore` 只认识 FuckClassroom 内置 `AppSettings` 字段。

因此第三方插件注册 `SettingsPanel` **不会自动获得任意字段持久化能力**。

第三方插件需要自定义设置时，推荐：

1. 设置模板自己提交到插件路由；
2. 插件自己负责验证与持久化；
3. 不修改 core `AppSettings` schema。

未来如果提供插件 config schema API，会作为新的正式契约单独文档化。

---

## 14. 账户面板接口

插件可以把自己的登录、令牌或连接状态卡片注册到统一的“账户与认证”页面，而不需要修改 core 模板：

```python
from fuckclassroom.core.plugins import AccountPanel


def build_account_context(services, request):
    client = services.get("my_plugin.client")
    return {
        "session": client.get_session_status(),
        "notices": (),
    }


AccountPanel(
    key="my_plugin",
    template="my_plugin/account_panel.html",
    order=70,
    context_factory=build_account_context,
    shortcut_label="My Plugin",
    shortcut_href="/my-plugin",
    shortcut_icon="shield-check",
)
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `key` | 面板唯一 key |
| `template` | Jinja 模板名，建议使用插件命名空间 |
| `order` | 页面中的排序 |
| `context_factory` | 页面请求时调用，参数为 `ServiceContainer` 与当前 request，返回该面板上下文字典 |
| `shortcut_label` | 可选，账户页顶部快捷入口文案 |
| `shortcut_href` | 可选，账户页顶部快捷入口地址；为空时不显示 |
| `shortcut_icon` | 可选，core SVG sprite 中的 icon ID |
| `shortcut_primary` | 是否使用主按钮样式 |

面板模板会拿到统一变量 `account`，值就是 `context_factory` 返回的字典。例如：

```jinja2
<article class="surface account-session-panel">
  <strong>{{ account.session.message }}</strong>
</article>
```

如果需要在账户页顶部显示成功或错误提示，可以在返回值中提供 `notices`：

```python
{
    "notices": (
        {
            "kind": "error",
            "icon": "circle-alert",
            "message": "登录已过期",
        },
    ),
}
```

`context_factory` 只会为当前已启用插件的面板执行，因此停用插件后，它的账户 UI 和账户数据读取都会一起退出页面。不要在 factory 中执行长时间阻塞操作；昂贵登录或网络动作应由插件路由或后台任务处理。

---

## 15. Sidebar / Topbar 模板

`PluginSpec` 还可以提供：

```python
sidebar_templates=("my_plugin/sidebar_status.html",),
topbar_status_templates=("my_plugin/topbar_status.html",),
```

这些模板会直接包含在全局 `base.html` 中。

适合显示：

- 登录状态；
- 连接状态；
- 简单 badge。

不适合：

- 发网络请求；
- 扫描大量文件；
- 执行数据库重查询；
- 修改全局状态。

复杂状态应该由 service/lifecycle 提前计算，模板只读结果。

---

## 16. Lifecycle

插件可以同时提供同步或异步 hook：

```python
def startup(context: PluginContext):
    ...


async def shutdown(context: PluginContext):
    ...
```

### startup

适合：

- 验证会话；
- 读取插件自己的持久化状态；
- 启动轮询任务；
- 恢复调度器。

### shutdown

适合：

- 关闭连接；
- 停止线程/进程；
- flush 数据；
- 释放外部资源。

使用 `context.create_task()` 创建的 asyncio task 会在 shutdown hook **之前**由 runtime 统一取消。

shutdown 按插件顺序反向执行，使高层插件先退出、底层依赖后退出。

### settings_saved

当 Core 设置页完成持久化后，会调用已启用插件注册的 `settings_saved`。插件可以在这里把新设置应用到已经创建的 service，而不需要 Core 知道具体 service：

```python
def settings_saved(context: PluginContext, settings, form):
    service = context.services.get("my_plugin.service")
    service.configure(settings)
```

该 hook 可以是同步或异步函数。异常会被 Core 记录并反馈到设置页，但不会回滚已经写入磁盘的设置。

### clear_session

如果插件持有登录会话，可以注册 `clear_session`：

```python
def clear_session(context: PluginContext):
    context.services.get("my_plugin.client").clear_session()
```

用户执行“清除插件登录状态”时，Runtime 只遍历当前启用且声明了此 hook 的插件。Core 不应直接引用具体业务插件的登录 service。

---

## 17. 依赖其他插件

如果需要另一个插件的能力，必须在两处声明相同的 `requires`。

`plugin.json`：

```json
{
  "requires": ["classroom"]
}
```

`PluginSpec`：

```python
PluginSpec(
    ...,
    requires=("classroom",),
)
```

两边不一致会拒绝加载。

之后可以：

```python
if context.registry.is_enabled("classroom"):
    classroom = context.services.get("classroom_client")
```

插件管理器启用当前插件时会自动补齐 `requires`。

停用底层插件时，也会同时停用依赖它的插件。

---

## 18. 当前内置功能插件暴露的 service

下面这些 service 当前可以通过 `context.services` 获取，但它们属于**具体功能插件接口**，稳定性低于 core Plugin API。

使用前必须在 `requires` 中声明对应插件。

| 插件 | service key | 当前对象 |
| --- | --- | --- |
| `classroom` | `classroom_client` | `ClassroomClient` |
| `classroom` | `semester_sync` | `SemesterSyncService` |
| `classroom` | `auth_service` | `LoginSessionService` |
| `classroom` | `download_library` | `DownloadLibrary` |
| `processing` | `extraction_service` | `LessonExtractionService` |
| `transcription` | `transcription_service` | `LocalTranscriptionService` |
| `ocr` | `ocr_service` | `OcrService` |
| `ai_summary` | `ai_summary_service` | `AIClient` |
| `attendance` | `ketangpai_client` | `KetangpaiClient` |
| `course_selection` | `course_selection` | `CourseSelectionAssistant` |
| `course_selection` | `auto_selection` | `AutoSelectionService` |
| `course_selection` | `hy2_proxy` | `Hy2ProxyManager` |

例如：

```python
def build_router(context: PluginContext) -> APIRouter:
    classroom = context.services.get("classroom_client")
    ...
```

如果插件只想在对应功能存在时增强，而不是强制依赖：

```python
classroom = context.services.maybe("classroom_client")
if classroom is not None:
    ...
```

但是不要把这种可选探测代替 `requires` 用于核心功能依赖。

---

## 19. 兼容性规则

Plugin API v1 使用两层兼容检查。

### API 版本

`plugin.json`：

```json
{
  "api_version": 1
}
```

`PluginSpec`：

```python
PluginSpec(
    ...,
    api_version=PLUGIN_API_VERSION,
)
```

两者必须一致，并且必须等于 runtime 当前的 `PLUGIN_API_VERSION`。

### FuckClassroom 应用版本

可以额外声明：

```json
{
  "app_requires": ">=0.1,<0.2"
}
```

不满足时，插件会：

- 继续保留在插件目录；
- 继续显示在插件管理页；
- 不能启用；
- 不会 import `plugin.py`；
- 不会执行第三方插件代码。

如果状态文件中已经把不兼容插件设置为启用，启动时会跳过该插件和依赖它的插件，保证 core UI 可以继续启动。

---

## 20. API v1 中不要做的事情

为了让插件可以跨 FuckClassroom 版本工作，应避免：

### 不要直接访问 runtime 私有对象

错误：

```python
context._runtime._routers
```

正确：

```python
return APIRouter()
```

交给 `route_factory`。

### 不要在 import 阶段启动任务

错误：

```python
threading.Thread(...).start()
```

写在模块顶层。

正确：放到 `startup` 或由用户操作触发。

### 不要假设所有插件都启用

错误：

```python
context.services.get("ocr_service")
```

但没有声明 `requires=("ocr",)`。

### 不要直接修改 FastAPI 主 app

插件 API v1 没有把 `FastAPI app` 暴露为稳定对象。

应该使用：

- `route_factory`
- `template_dir`
- `static_dir`
- 导航 / 设置 panel 注册

### 不要依赖私有模块路径

例如：

```python
from fuckclassroom.plugins.processing.extraction import _some_private_helper
```

以下划线开头的符号和兼容 shim 不属于插件 API。

---

## 21. 一个较完整的插件骨架

```text
demo_plugin/
├── plugin.json
├── plugin.py
├── services.py
├── routes.py
├── lifecycle.py
├── templates/
│   └── demo_plugin/
│       └── home.html
└── static/
    └── app.js
```

`plugin.py`：

```python
from pathlib import Path

from fuckclassroom.core.plugins import (
    PLUGIN_API_VERSION,
    NavigationGroup,
    NavigationItem,
    PluginContext,
    PluginSpec,
)


PLUGIN_DIR = Path(__file__).resolve().parent


def setup_services(context: PluginContext):
    from .services import setup_services as setup
    return setup(context)


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


def startup(context: PluginContext):
    from .lifecycle import startup as hook
    return hook(context)


async def shutdown(context: PluginContext):
    from .lifecycle import shutdown as hook
    return await hook(context)


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="demo_plugin",
        name="Demo Plugin",
        api_version=PLUGIN_API_VERSION,
        order=100,
        requires=("classroom",),
        service_factory=setup_services,
        route_factory=build_routes,
        startup=startup,
        shutdown=shutdown,
        template_dir=PLUGIN_DIR / "templates",
        static_dir=PLUGIN_DIR / "static",
        navigation_groups=(
            NavigationGroup(
                key="demo_plugin",
                label="Demo",
                aria_label="Demo Plugin 导航",
                system="demo_plugin",
                order=70,
                items=(
                    NavigationItem(
                        key="demo-home",
                        label="Demo 首页",
                        href="/demo-plugin",
                        icon="settings",
                        active_keys=("demo-home",),
                    ),
                ),
            ),
        ),
        system_labels=(("demo_plugin", "Demo"),),
    )
```

`services.py`：

```python
from fuckclassroom.core.plugins import PluginContext


class DemoService:
    def __init__(self, classroom):
        self.classroom = classroom


def setup_services(context: PluginContext) -> None:
    classroom = context.services.get("classroom_client")
    context.services.add(
        "demo_plugin.service",
        DemoService(classroom),
    )
```

`routes.py`：

```python
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fuckclassroom.core.plugins import PluginContext


def build_router(context: PluginContext) -> APIRouter:
    router = APIRouter()
    templates = context.services.get("templates")
    demo = context.services.get("demo_plugin.service")
    task_manager = context.services.get("task_manager")

    @router.get("/demo-plugin", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "demo_plugin/home.html",
            {
                "service": demo,
            },
        )

    @router.post("/demo-plugin/run")
    def run(request: Request):
        from fuckclassroom.web.responses import task_started_response

        def worker(progress):
            progress(10, "开始")
            progress.log("Demo task started")
            # do work...
            progress(100, "完成")
            return "/demo-plugin"

        task = task_manager.start("Demo Plugin · Run", worker)
        return task_started_response(request, task, "/demo-plugin")

    return router
```

`lifecycle.py`：

```python
import asyncio

from fuckclassroom.core.plugins import PluginContext


async def _poll(context: PluginContext):
    while True:
        context.logger.debug("demo plugin heartbeat")
        await asyncio.sleep(60)


def startup(context: PluginContext) -> None:
    context.create_task(_poll(context))


async def shutdown(context: PluginContext) -> None:
    context.logger.info("demo plugin stopped")
```

---

## 22. 发布插件前检查清单

发布 ZIP 或目录前至少确认：

- `plugin.json.id == PluginSpec.id`
- `plugin.json.requires == PluginSpec.requires`
- `plugin.json.api_version == PluginSpec.api_version == PLUGIN_API_VERSION`
- `app_requires` 覆盖你真正测试过的 FuckClassroom 版本
- 重型依赖没有在 `plugin.py` 顶层 import
- 所需 Python 包已经写入 `python_requires`
- service key 使用插件命名空间，避免碰撞
- 使用其他插件 service 时声明了 `requires`
- route URL 有自己的命名空间
- template 名称有自己的命名空间
- static 文件通过 `/plugins/<plugin_id>/static/` 引用
- 长同步任务使用 `task_manager`
- 长 async 任务使用 `context.create_task()`
- worker 定期检查 `progress.raise_if_cancelled()`
- shutdown 能安全重复执行且不会因为单个资源关闭失败而卡死
- 插件禁用时不会留下 import 阶段启动的线程、进程或全局副作用

---

## 23. 当前 API v1 的已知限制

Plugin API v1 目前有几个明确限制：

1. `requires` 不做运行时拓扑排序，service hook 仍依赖 `PluginSpec.order`。
2. 第三方插件没有独立的声明式配置 schema；`SettingsPanel` 只负责 UI 组合。
3. Navigation API 只能使用 core SVG sprite 中已有图标。
4. 插件启用/停用需要重启应用，暂不支持安全热卸载。
5. 内置功能插件 service 的具体 Python 类型尚未纳入与 core API 同级的稳定性保证。
6. 外部插件安装后会执行 Python 代码，目前没有进程级 sandbox。

这些限制如果在未来版本中通过新的稳定接口解决，会同步更新 Plugin API 版本和本文档。

---

## 24. 如何判断一个接口能不能依赖

可以按下面的优先级判断：

**最稳定：**

```text
fuckclassroom.core.plugins 中本文明确列出的 API
        ↓
core_ui 提供的基础 service
        ↓
requires 对应内置插件暴露的 service
        ↓
公开但未纳入本文的模块
        ↓
下划线私有符号 / runtime 私有字段
```

如果一个能力只能通过最后两层实现，优先提交新的 Plugin API，而不是让第三方插件直接耦合内部实现。
