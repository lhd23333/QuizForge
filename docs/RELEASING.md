# QuizForge Windows 发布与一键更新手册

本文供项目维护者使用。普通用户只需从 GitHub Releases 安装新版，或在软件“设置”／“关于”页点击“检查更新”。

## 一键更新是怎样工作的

QuizForge 不在后台自动更新。只有用户主动点击后，客户端才读取 `https://api.quizforge.tech/api/v1/updates/windows/latest` 的公开 JSON 清单；请求只包含版本、平台和固定 User-Agent，不包含题库、路径或 API Key。

发现更高版本后，客户端仍不会直接运行网络文件，而是按顺序完成四道检查：下载地址必须为 HTTPS、安装包不得超过 512 MiB、文件 SHA-256 必须与清单一致、Windows Authenticode 签名必须有效且证书指纹与清单一致。全部通过后，独立更新进程等待 QuizForge 退出，使用 Inno Setup 静默覆盖原安装目录，再启动新版。

安装器的 `AppId` 固定，`UsePreviousAppDir=yes`。程序文件在安装目录中覆盖，题库与 `%LOCALAPPDATA%\QuizForge` 中的配置、加密密钥、任务状态和历史兼容文件不属于安装包覆盖范围。

```text
GitHub Release 中的已签名 Setup
        ↓ HTTPS 下载
更新清单：版本 + URL + SHA-256 + 证书指纹
        ↓ 用户点击“检查更新”
客户端复验下载、哈希和签名
        ↓
静默覆盖原安装目录并重启
```

没有可信代码签名证书时，只能在本机生成未签名候选用于构建和隔离安装验收，**不得上传为公开 Setup，也不能发布一键更新清单**。不要为省略证书而删除或放宽客户端校验。

## Windows 代码签名证书是什么

Windows 代码签名证书是包含发布者身份和私钥控制证明的 Authenticode 证书。发布时用私钥给 `QuizForge.exe` 与 Setup 签名，并附加可信时间戳；Windows 和更新器据此确认文件由同一发布者提供，且签名后没有被修改。证书私钥不得进入仓库、安装包或服务器明文配置。

公开发行应使用受 Windows 信任的 OV／EV 代码签名证书，或通过 SignPath 等面向开源项目的托管签名服务完成签名。自签名证书只适合本机流程测试，不能替代正式发行证书，也不会消除普通用户看到的未知发布者警告。QuizForge 当前选择申请 SignPath Foundation，公开政策见 [`CODE_SIGNING_POLICY.md`](CODE_SIGNING_POLICY.md)，申请准备清单见 [`SIGNPATH_APPLICATION.md`](SIGNPATH_APPLICATION.md)。

## 发布前准备

使用本地 OV／EV 证书时，发布机需要 Windows、项目虚拟环境、Node.js、Inno Setup 6、Windows SDK 的 `signtool.exe`，以及安装在当前用户或计算机证书存储中的代码签名证书。私钥和证书密码不得进入仓库、脚本参数示例或 GitHub Release。

SignPath 路径使用 `.github/workflows/windows-release-candidate.yml` 证明公开源码到未签名候选的构建来源。该工作流只有 `contents: read` 权限，不读取仓库 Secret，不创建 tag 或 Release；Pandoc 从固定官方地址下载并逐文件校验 SHA-256。SignPath 审核通过前，它上传的 14 天候选只能用于构建验证，不得公开分发。审核通过后再按 SignPath 分配的组织、项目和 Artifact Configuration 接入官方签名步骤，不能预先虚构配置标识。

正式版本使用三段式版本号，例如 `2.0.0`，不带 `beta` 后缀。版本选择遵循根级 `docs/VERSIONING.md`：新增用户能力提升 MINOR，只有修复与文案调整才提升 PATCH。

## 发布步骤

### 1. 确认版本与工作区

先确认本次发布包含哪些提交，逐项检查未跟踪文件，不使用 `git add -A`：

```powershell
git status --short --branch
git diff --check
git log --oneline --decorate -10
```

把 `desktop_product.py` 中的 `PRODUCT_VERSION` 更新为目标版本，并将 `CHANGELOG.md` 的 `[Unreleased]` 内容定版为 `[vX.Y.Z] - YYYY-MM-DD`，再在顶部留下新的空 `[Unreleased]`。

### 2. 完整验证源码

```powershell
$version = '2.0.0'
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File tools\verify_release.ps1 -SkipBundleScan `
  -ExpectedTag "v$version"
```

随后执行日常目录版覆盖，核对真实安装目录能启动，并确认题库登记、API 配置、界面设置、任务状态和历史兼容文件未变化：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File update_installed.ps1 -DirectBundle
```

### 3. 构建并签名安装包

#### 本地 OV／EV 证书

把代码签名证书的 SHA-1 指纹放入当前 PowerShell 进程环境，不要写进脚本：

```powershell
$env:QUIZFORGE_SIGNING_CERT_THUMBPRINT = '<40 位证书指纹>'
$version = '2.0.0'
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File build_installer.ps1 `
  -Version $version `
  -FileVersion "$version.0"
```

`build_installer.ps1` 会构建桌面目录版，签名 `QuizForge.exe`，编译 Setup，再签名安装包。正式三段式版本没有证书时脚本会直接中止；`-AllowUnsigned` 只供本地实验，不得用于公开一键更新。

#### SignPath Foundation

推送公开提交后，先确认 [Windows release candidate](https://github.com/lhd23333/QuizForge/actions/workflows/windows-release-candidate.yml) 完整通过。当前工作流只证明构建可复现并产生未签名候选；SignPath 审核通过后，正式流程必须依次让 `QuizForge.exe` 和最终 Setup 获得受信任签名，且每次签名请求由 Approver 手动批准。只签外层 Setup、让内层主程序保持未签名，不算完成。

最终产物无论来自本地证书还是 SignPath，都继续执行下列发行扫描、签名与摘要检查；签名服务成功状态不能替代本机 `Get-AuthenticodeSignature` 复验。

对最终目录版再次运行包含发行扫描的完整检查：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File tools\verify_release.ps1 `
  -ExpectedTag "v$version"
```

再确认安装包签名与摘要：

```powershell
$setup = "build\installer\QuizForge-$version-Setup.exe"
Get-AuthenticodeSignature -LiteralPath $setup | Format-List Status,StatusMessage,SignerCertificate
Get-FileHash -Algorithm SHA256 -LiteralPath $setup
```

### 4. 验收覆盖升级

在 Windows 沙盒、虚拟机或其他隔离用户目录中先安装上一个公开版本，建立最小题库并填写假的测试配置，然后运行新 Setup 覆盖安装。至少核对：

- 新版 EXE 的 ProductVersion 与 FileVersion 正确，快捷方式和卸载器仍可用；
- 原题库、题库登记、界面设置和任务状态未变化；
- 测试用 OCR／LLM 配置仍能读取，历史兼容文件字节未变化；
- 软件能健康启动，“关于”页显示目标版本，手动导出和本次主要功能可用。

只看到安装器退出码 0 不算完成覆盖验收。

### 5. 提交、打 tag 并创建 GitHub Release

按主题显式暂存文件，完成定版提交后创建 annotated tag：

```powershell
git add <本次确认过的文件>
git commit
git tag -a "v$version" -m "QuizForge v$version：<本次主题>"
git push origin master
git push origin "v$version"
```

在 GitHub 的 Releases 页面选择刚推送的 tag，创建正式 Release，上传 `QuizForge-$version-Setup.exe`。Release 说明从对应 CHANGELOG 段落整理，至少写清新增、修复、兼容影响与已知限制，并包含 `Free code signing provided by SignPath.io, certificate by SignPath Foundation` 以及 [Code signing policy](CODE_SIGNING_POLICY.md) 链接。也可以在安装好 GitHub CLI 后使用 `gh release create`，但不要把签名私钥交给 GitHub Actions。

发布后从 GitHub 实际下载一次安装包，重新计算 SHA-256 并复验 Authenticode。用于更新清单的必须是用户最终下载到的那个文件，而不是上传前的临时副本。

### 6. 发布更新清单

取得 GitHub Release 的安装包 HTTPS 地址、SHA-256 和签名证书指纹后，在 `quizforge-cloud` 服务器运行其 `tools/publish_update_manifest.py`。完整命令与生产路径见 `quizforge-cloud/deploy/README.md`；工具会校验字段并原子替换清单，无需重启服务。

清单应包含：

```json
{
  "latest_version": "2.0.0",
  "download_url": "https://github.com/lhd23333/QuizForge/releases/download/v2.0.0/QuizForge-2.0.0-Setup.exe",
  "sha256": "<64 位 SHA-256>",
  "signer_thumbprint": "<40 位证书指纹>",
  "notes": "<简短更新说明>",
  "published_at": "<UTC ISO 8601 时间>"
}
```

### 7. 验收真实一键更新

保留一台安装上一个公开版本的测试机，在软件“关于”页点击“检查更新”。确认它能发现目标版本、下载、校验、自动关闭、覆盖并重启；随后再次核对版本号与用户数据。最后分别检查：

```text
https://api.quizforge.tech/healthz
https://api.quizforge.tech/api/v1/updates/windows/latest
```

至此才算正式发布完成。GitHub Release 存在但更新清单未发布时，用户仍可手动下载；更新清单已发布但真实覆盖未验收时，不应对外宣布一键更新可用。

## 出现问题时

发现严重问题时，先停止分发更新清单并保留原文件备份，使接口暂时回到“尚未发布可用更新”；不要替换同一 Release URL 下的安装包，否则已经记录的 SHA-256 会失去意义。修复后提升 PATCH 版本，重新构建、签名、发布 Release 与清单。已经公开的 tag 不重写、不强推。

用户端更新失败时，优先查看 `%LOCALAPPDATA%\QuizForge\updates` 下的状态与安装日志。哈希或签名失败必须中止，不能提供跳过校验的开关；用户仍可从 GitHub Releases 手动下载并覆盖安装。
