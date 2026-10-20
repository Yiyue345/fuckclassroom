# FuckClassroom 插件开发与发布指南（Registry v1）

本指南覆盖一个真实插件从 **开发 → 本地构建/验证 → GitHub Release → Registry 收录 → 应用内安装/更新** 的完整流程。开发接口细节见 [Plugin API v1](plugin-api.md)，插件架构与运行时规则见 [插件架构](plugins.md)。

> 模板不是一个已经发布的插件：请先复制模板、修改唯一 ID、创建自己的仓库。插件源代码由插件作者维护，Registry 维护元数据和不可变的版本索引。

## 0. 获取公开模板并创建新仓库

模板：[examples/plugin-template](../examples/plugin-template/README.md)。将 **该目录内所有文件（包括隐藏的 .github 目录）** 复制到你自己新建的 GitHub 仓库根目录；不要连最外面的 plugin-template 文件夹一起压入插件包。

目录：

~~~text
plugin.json
plugin.py
scripts/build_registry_artifact.py
tests/test_package.py
.github/workflows/ci.yml
.github/workflows/release.yml
README.md
.gitignore
~~~

模板是一套可复制的代码目录，当前并没有设置成 GitHub 专用的 “Use this template” 仓库，因此应使用复制文件而非点击不存在的模板按钮。

## 1. 开发插件

最小示例提供一个 GET /sample-plugin/hello 路由。启动 FuckClassroom 并启用插件后，可以在主程序的同源地址访问该接口。

修改下列信息并保持一致：

| 文件 | 必须检查 |
| --- | --- |
| plugin.json | 唯一 ID、名称、描述、基础版本、API 版本、app_requires、requires、Python 依赖 |
| plugin.py | PluginSpec.id/name/requires 与清单一致；按需修改路由、Service、Lifecycle |
| tests/test_package.py | 根据新增模块或路由补充断言；测试与插件真实代码保持一致 |
| scripts/build_registry_artifact.py | 新增 Python 模块、模板、静态资源时，**显式加入 ZIP 内容和检查** |
| .github/workflows/ci.yml | 改动基础版本号时同步调整用于打包测试的版本 |

必须满足的基础声明（省略展示字段）：

~~~json
{
  "id": "sample_plugin",
  "version": "0.1.0",
  "api_version": 1,
  "app_requires": ">=0.1,<0.3",
  "requires": ["core_ui"],
  "python_requires": [],
  "execution": "in_process",
  "entry": "plugin.py",
  "route_prefix": null,
  "rpc_api_version": null,
  "rpc_permissions": []
}
~~~

- ID 采用小写字母、数字、下划线或短横线且不能与已有插件冲突；它必须与 PluginSpec.id 完全一致。
- Plugin API v1 是主要兼容条件。进程插件另需匹配 Plugin RPC API。app_requires 表达宿主版本期望；现有客户端将宿主版本不满足展示为**警告**，并不会在 API 兼容时直接硬阻断。
- 使用其他插件的能力时填写 requires，不能将“插件集 Collection”当成技术依赖。
- 模板采用 in_process，因此 RPC 字段为 null / 空数组；不能只修改 execution 为 process 就认为已经完成进程隔离。进程插件请按 [Plugin API v1 文档](plugin-api.md) 的 RPC 约定实现独立 Worker、route_prefix 与权限声明。
- 请只依赖公开稳定的 Plugin API；不要在模块 import 时启动线程或长期任务。

## 2. 本地测试与打包

模板的发布检查仅使用 Python 3.11+ 标准库：

~~~bash
python -m unittest discover -s tests -v
python scripts/build_registry_artifact.py 0.1.0-beta.1
cd dist
sha256sum -c -- *.sha256
~~~

结果：

~~~text
dist/sample_plugin-0.1.0-beta.1.zip
dist/sample_plugin-0.1.0-beta.1.zip.sha256
~~~

规范：

1. 文件名必须为 <plugin-id>-<SemVer>.zip，Tag 为 v<SemVer>。
2. ZIP **根目录**直接包含 plugin.json 和 plugin.py；不要包在 sample_plugin/ 外层目录下。
3. 打包版本的 plugin.json.version 必须与 Tag 和 ZIP 文件名匹配。
4. ZIP 大小上限 100 MiB（Registry）；禁止路径穿越、符号链接等不安全 ZIP 条目。
5. .zip.sha256 是便于发布者与 CI 校验的伴随文件；**Registry 会自行读取 ZIP 原始字节重新计算 size 和 SHA-256**，不会直接相信上传的摘要文件。
6. 本地单元测试通过并不代表运行环境已验证。还要在真实 FuckClassroom 里安装、启用并测试 API/依赖/卸载和重载路径。

建议先发布 Beta 验证，再发布稳定版。

## 3. 配置 GitHub Actions 并创建 Release

1. 新建插件仓库，推送模板（含 .github/workflows）。
2. 确认 Actions 可运行；Release 工作流使用 contents: write 发布 Tag 和 Release。
3. 每次 PR / main push 会跑 Validate plugin package。
4. 在 Actions → Publish Registry v1 release → Run workflow 输入版本，例如 0.1.0-beta.1。
5. 工作流运行测试、生成 ZIP 与 SHA-256、校验摘要，并创建 v0.1.0-beta.1 Tag 和 Release；Beta 后缀会将 Release 标为 prerelease。
6. 不要覆盖已发布版本，不要修改已有 Tag 指向的提交；需要修复时递增版本重新发布。模板工作流若发现已存在 Tag/Release 会失败退出。

基础版本号（major.minor.patch）需要与源 plugin.json.version 一致；发布工作流会将 ZIP 内的版本升级为指定的完整 SemVer（例如 Beta 后缀）。

**签名可选：** 当前模板默认生成未签名 ZIP + SHA-256。如果要使用 Ed25519，先由 Registry Publisher 登记 signing_keys，再按 Registry 的 ed25519-sha256-v1 协议为 SHA-256 消息生成签名，上传同名 .zip.sig；Registry 导入及校验会拒绝未知 key_id 或不匹配的签名。私钥不得提交到代码库或 Registry。

## 4. 在 Registry 建立插件条目（维护者操作）

官方 Registry 仓库：[f-classroom-plugin-website](https://github.com/Yiyue345/f-classroom-plugin-website)。

Registry 仓库当前若为私有仓库，外部作者**不能假设自己可以直接提交 PR**；请向维护者提供公开插件仓库地址、ID、描述、Publisher、Release URL、许可证和权限说明，由有权限的维护者执行下面的收录步骤。

~~~bash
pnpm install --frozen-lockfile
pnpm registry:new-plugin
~~~

根据交互提示选择已存在的 Publisher，输入唯一插件 ID、中文/英文名称及简介、类别、许可证和真实仓库地址。该操作在 registry/plugins/<id>/ 创建 metadata.json、README.md、README.en.md、icon 等草稿资料，并将 ID 加入 registry/registry.json 的 drafts 列表。

- 如果还没有 Publisher，须由维护者先在 registry/publishers/ 增加经过验证的发布者信息，并将其 ID 添加到 Registry 源清单的 publishers 列表。
- 检查仓库地址是否与 GitHub Release 的 OWNER/REPO 完全一致。
- 草稿**不会进入公开 API index**，不能视为已经上架。

## 5. 从真实 GitHub Release 导入版本

请使用 Release **页面 URL**，不要传 ZIP 下载直链：

~~~bash
pnpm registry:import-release -- sample_plugin https://github.com/OWNER/REPO/releases/tag/v0.1.0-beta.1
pnpm registry:verify-releases
pnpm check
pnpm build
~~~

导入器会：

1. 读取 GitHub Release、Tag、提交 SHA 和资源列表；确认版本不是 draft。
2. 寻找名称精确匹配 sample_plugin-0.1.0-beta.1.zip 的 **唯一** Asset。
3. 下载 ZIP，验证根目录 plugin.json、入口文件、安全路径与运行时声明，计算大小和 SHA-256。
4. 若存在同名 .zip.sig，按 Publisher 的公钥验证 Ed25519 签名；未知密钥或无效签名直接失败。
5. 生成 registry/plugins/sample_plugin/versions/0.1.0-beta.1.json，关联 GitHub Release Changelog URL。
6. 将首次成功导入的插件从 drafts 移到 plugins；下一次构建将向网站及 /api/v1/index.json 公布。

再次检查展示用图标、中英文 README、权限、许可证和主程序兼容性。提交 Registry 变更后由 CI 验证 Schema、不可变版本身份、已收录 Release 和 Astro 构建；合并并成功部署后才算正式上线。

规范要求：版本身份字段原则上不可变；撤回使用 yanked + reason，不通过覆盖同一 Release ZIP 来修补版本。

## 6. 在 FuckClassroom 中安装与启用

1. 打开插件管理 → 在线插件，刷新 Registry 列表。
2. 如果 Release 是 0.1.0-beta.1 等测试版，先开启 Beta 渠道；默认一般只选择 stable。
3. 客户端根据 Plugin API、RPC API、应用版本声明和 yanked 状态筛选版本。若没有兼容的版本，显示具体状态。
4. 点击安装。客户端验证 ZIP 文件名、大小、SHA-256、可选 Ed25519 签名以及安装包内部 manifest；新安装插件**默认停用**。
5. 在已安装插件页面启用插件，并由现有 Runtime 切换机制载入插件。
6. 对本示例访问 /sample-plugin/hello 验证路由。出现依赖缺失时，先安装或修复依赖。

**安装和启用是两个独立操作**。Registry 收录不等于主程序自动安装，下载成功也不等于插件已启用。

## 7. 发布更新并升级

发布者：

1. 修改代码和测试；需要改变 Plugin API/RPC API 时先检查宿主兼容规则。
2. 运行测试，使用新的完整 SemVer 创建新 Release，例如 0.1.0-beta.2。
3. 维护者将新 Release 导入 Registry，校验并部署。
4. 对于废弃/撤回版本，通过 Registry 的 yanked 机制处理，不删除不可变历史。

使用者：

1. 刷新在线插件列表查看已安装版本及可安装版本。
2. **先停用要更新的插件，以及依赖它且仍在运行的插件**。
3. 点击更新。安装器使用临时目录和同文件系统替换，并在失败时尽量保留旧版本。
4. 更新成功后手动重新启用，启用流程会请求现有 Runtime 热重载。当前不承诺“运行中的插件自动无感升级”。

此阶段版本详情的 Changelog 在 Registry 中链接到 GitHub Release 页面；应用内尚不保证完整展示更新说明，具体内容请查看 Release。

## 8. 常见发布失败及排查

| 错误 | 检查项 |
| --- | --- |
| Expected exactly one Release asset | ZIP 是否按 <id>-<version>.zip 命名；是否误上传多份同名资源 |
| plugin.json must be at ZIP root | 打包时是否额外包了一层插件目录 |
| version / id / runtime differs from Registry | 检查 plugin.json、插件入口 PluginSpec 与 Release SemVer |
| Publisher / repository mismatch | Registry metadata.repository 是否和 Release 的 OWNER/REPO 一致 |
| SHA-256 / size mismatch | Release ZIP 是否被修改、截断，或 Registry 记录是否来自另一个版本 |
| signature references unknown publisher key | 注册 Publisher signing_keys，检查 key_id 和签名格式 |
| CI rejected immutable Release | 不可修改既有版本身份；请发布新 SemVer |
| 插件市场找不到 Beta | 开启 Beta 渠道，并确认 Registry 已成功部署 |
| 更新按钮不可用 | 先停用目标插件及其运行中依赖者 |
| 安装成功但功能不可用 | 检查是否已启用、必需依赖、Runtime 日志与 Plugin API / RPC API 兼容性 |

不要把 GitHub Token、登录 Cookie、私钥或任何用户数据打入插件 ZIP、公开 Release 或 Registry。

## 9. 发布审核清单

- [ ] ID、PluginSpec、依赖和 Plugin API 声明一致
- [ ] 单测、Package 测试及真实宿主运行测试完成
- [ ] ZIP 根目录、SemVer、SHA-256 和可选签名符合 Registry v1
- [ ] GitHub Release 已发布且资产可下载
- [ ] 中英文展示元数据、许可证、依赖及权限已审核
- [ ] Registry 收录、verify-releases、check 与 build 通过
- [ ] 网站部署生效，插件管理器可发现并安装
- [ ] 启用、停用、更新与失败恢复路径已测试

其余接口和生命周期细节见 [Plugin API v1 文档](plugin-api.md)。
