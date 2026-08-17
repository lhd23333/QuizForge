# 离线排版组件

此目录是发行构建的外部工具挂载点。`runtime/pandoc/` 在本地发行环境中放置
Pandoc 3.9.0.2，因体积较大由 `.gitignore` 排除，但 `build_desktop.ps1` 会自动把它
装入桌面目录和安装包。V0.1 不捆绑 TeX。

支持的目录结构：

```text
runtime/
  pandoc/
    pandoc.exe
    licenses/COPYING.md
    licenses/COPYRIGHT
    source/pandoc-3.9.0.2.tar.gz
  tex/bin/windows/xelatex.exe
  tex/bin/windows/dvisvgm.exe
```

Pandoc 以未经修改的独立命令行程序聚合分发；发行目录必须同时含 GPL 文本、版权
声明和该二进制版本的对应源码归档，不得只复制 `pandoc.exe`。总体第三方说明见
`installer/THIRD_PARTY_NOTICES.md`。

干净的 GitHub Actions Runner 通过 `tools/prepare_ci_runtime.ps1` 从 Pandoc 官方
Release、上游源码仓库和 Hackage 下载固定版本，并在写入本目录前校验每个文件的
SHA-256。哈希不匹配时立即停止构建，不回退到浮动版本或其他镜像。

TeX 仍兼容 `runtime/tex/bin/windows/` 与 MiKTeX 风格的
`runtime/tex/miktex/bin/x64/`。只有完成体积、更新和许可证审计后，才能随商业包加入。
未附带时，QuizForge 自动使用 `QUIZFORGE_PANDOC` / `QUIZFORGE_XELATEX` /
`QUIZFORGE_DVISVGM` 环境变量、系统 `PATH` 或常见的 Pandoc/MiKTeX 安装位置。
