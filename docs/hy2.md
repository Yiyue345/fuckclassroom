# Hy2 运行与 Windows Smart App Control

FuckClassroom 的 Hy2 教务代理采用“预编译优先、本地构建兜底”的两级策略。

## 默认路径：预编译 runner

正常安装不需要 Rust。

CI 会为支持的平台构建 `hy2_serve`，并在仓库中保存对应 runner 和 manifest。程序使用前会检查：

- 当前平台是否有匹配的 runner；
- manifest 中记录的 Hy2 源码指纹是否与当前源码一致；
- runner 的 SHA-256 是否与 manifest 一致。

校验通过后，程序把 runner 安装到本地 `data/hy2/` 并在真正访问本科教务时启动。

## Windows Smart App Control

Windows Smart App Control 可能拒绝运行没有可信代码签名的 `hy2_serve.exe`。常见现象是 Windows 安全中心提示“无法确认谁发布了 hy2_serve.exe”，程序侧可能得到 WinError/os error 4551。

FuckClassroom 不会自动关闭 Smart App Control，也不会修改 Windows 安全策略。

检测到 4551 后，程序会进入第二条路径：本地 Rust 构建。

## 兜底路径：本地 Rust 1.96.0 构建

这个路径只在预编译 runner 被 Windows 应用控制拦截时自动触发。

如果机器已经有 rustup/Cargo，程序会重新编译 `rust_module/hy2_proxy`，把本地产物安装到 `data/hy2/`，然后再启动一次。Hy2 工程通过 `rust-toolchain.toml` 固定到 Rust 1.96.0，以避开此前在较新 Rust 工具链上遇到的 Windows Application Control 构建问题。

可以提前准备工具链：

```powershell
rustup toolchain install 1.96.0 --profile minimal
cd rust_module\hy2_proxy
cargo --version
```

应看到 Cargo 1.96.x。程序会自己运行构建命令，不需要手工执行 `cargo build`。

## 没有 Rust 时

如果预编译 runner 可以启动，不需要安装 Rust。

只有预编译 runner 被 Smart App Control 拦截时，本地兜底才需要 Cargo。若此时没有 Cargo，Hy2 状态会明确提示“正常使用预编译 Hy2 不需要 Rust；当前只有本地构建兜底需要 rustup/Cargo”。

不希望安装 Rust 时，可以关闭 Hy2；本科教务仍会继续尝试公网入口或 WebVPN。

## 本地产物仍被拦截

Smart App Control 的具体判定由 Windows 决定，本地重新编译并不能保证所有机器都一定放行。

如果本地构建出的 runner 仍然被应用控制阻止，FuckClassroom 会停止继续尝试，不会自动关闭安全功能。此时建议关闭 Hy2 并使用其他教务入口。

长期方案仍然是为 Windows runner 增加可信 Authenticode 代码签名；当前双路径主要用于在没有签名基础设施时提高可用性。
