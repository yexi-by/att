# Formic 程序与源码来源

本目录提供 Formic 0.3.0 的 Windows x64 本机修订制品，运行时直接调用同目录的 `formic.exe`。

| 项目 | 当前制品 |
| --- | --- |
| 源码仓库 | [yexi-by/formic](https://github.com/yexi-by/formic) |
| 源码提交 | `45de311ab266ffa7cc606dffbb7463552a559ede` |
| 来源状态 | 本机源码仓库已提交，尚未推送远端 |
| 编译器 | Rust 1.97.1 |
| 构建目标 | `x86_64-pc-windows-msvc`，Release，静态 C Runtime |
| `formic.exe` SHA-256 | `1cfd69619d166214dc003aff972ea7419a72984cc029eadff478b08816282dd8` |

当前修订的精确源码保存在本机 Formic 源码仓库的上述提交中；上游远端尚未包含本次修订。

## 从对应源码构建

源码构建和规模实验在独立的 Formic 源码工程中执行。在持有上述提交的源码仓库中，检出该提交后运行：

```powershell
$env:RUSTFLAGS = '-C target-feature=+crt-static'
cargo +1.97.1 build --locked --release --target x86_64-pc-windows-msvc
```

产物位于该工程的 `target/x86_64-pc-windows-msvc/release/formic.exe`。ATT 随包目录提供预构建程序与用户文档，日常使用方法见[快速开始](README.md)。

Formic 使用 GNU AGPL v3，许可正文见 [LICENSE](LICENSE)。
