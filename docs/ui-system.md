# UI 设计系统与插件扩展

本轮在 Jinja + 原生 JavaScript 上统一视觉与组件，保留已有账户面板、设置上下文、设置保存和会话清理接口。业务页面继续归属于各插件的命名空间目录。

## 样式与组件

- `static/tokens.css` 是颜色、字体、间距、圆角、控件高度和明暗主题的统一入口。
- `static/app.css` 提供外壳与共享基础组件。业务布局、响应式规则保留在各插件的 `static/` 目录。
- `templates/ui/components.html` 提供 `field`、`select`、`checkbox`、`section`、`notice`、`icon` 和 `page_header`。输入值保持 Jinja 自动转义，字段名与既有后端表单一致。
- `static/shell.js` 提供窄屏导航、焦点管理、设置分类跟随和未保存提示。760px 以下使用导航抽屉；长表格在容器内滚动。

```jinja
{% import "ui/components.html" as ui %}
{% call ui.section("demo-settings", "示例设置", "按需调整插件行为。") %}
  {{ ui.field("worker_count", "并发数", settings.worker_count,
              type="number", min=1, max=8, step=1) }}
  {{ ui.checkbox("feature_enabled", "启用功能", settings.feature_enabled) }}
{% endcall %}
```

宏只负责渲染。第三方设置的验证、存储与提交端点仍由插件负责；未新增自动持久化 schema。

## 页面资源

`UIAsset` 是 API v1 的增量接口。已有 `stylesheets` 和 `scripts` 仍作为全局资源加载，无须迁移已有外部插件。

```python
from fuckclassroom.core.plugins import UIAsset

ui_assets=(
    UIAsset("detail.css?v=1", "style", ("demo_detail",)),
    UIAsset("detail.js?v=1", pages=("demo_detail",)),
)
```

页面在顶层设置 `{% set ui_page = "demo_detail" %}`，未设置时使用 `active_nav`。相对路径自动解析到 `/plugins/<id>/static/`，`/` 开头的路径指向应用共享资源。只支持本地 URL；`kind` 为 `style` 或 `script`。`pages=()` 表示该插件启用时所有页面加载。

资源先加载原有全局声明，再按插件 `order` 和各插件声明顺序加载页面资源，完全相同的 URL 去重。宿主脚本必须排在依赖它的扩展之前；当前课堂 HLS、播放器、处理扩展、签到扩展按此顺序加载。插件停用后其资源自动消失。

## 页面插槽

```python
from fuckclassroom.core.plugins import UISlot

ui_slots=(UISlot("classroom.lesson.actions", "demo/lesson_action.html", order=120),)
```

宿主通过 `registry.slot_templates(name)` 获取模板并在当前上下文中 include。贡献按插槽 `order` 排序，同序保持注册表的插件顺序。贡献只来自已启用的插件。依赖宿主的插件应声明 `requires`。

| 插槽 | 提供的上下文 | 用途 |
| --- | --- | --- |
| `classroom.lesson.transcript` | `detail`、`lesson`、`media`、`plugin_registry` | 非直播课次的转写入口；无贡献时显示默认文案 |
| `classroom.lesson.actions` | `detail`、`lesson`、`media`、`plugin_registry` | 课次操作按钮 |
| `processing.output.actions` | `course_id`、`lesson_id`、`outputs` | 提取结果页的扩展操作 |

这些插槽属于本项目的页面契约；模板不能假设其他可选插件一定启用。共享 HTML 类和 data 属性承载交互，扩展应保留它们。

## 任务 UI 事件

共享内联任务轮询器在读取任务状态后触发 `academic:task-progress` 文档事件，`event.detail` 为 `{ task, statusBox }`。扩展只更新该 `statusBox`，不得覆盖全局 `fetch`。核心轮询器统一处理失败、成功和取消终态；处理插件订阅该事件显示模型下载进度。

## 验证

`tests/test_ui_contract.py` 覆盖资源加载与去重、模板编译、自动转义、字段值和限制、插件停用后的按钮与资源消失。既有 UI、账户、设置与任务测试继续保留。

视觉验收至少检查桌面、平板和手机宽度，浅色/深色主题、导航键盘焦点、长表格滚动和未保存提示。测试数据与真实账户数据分开。
