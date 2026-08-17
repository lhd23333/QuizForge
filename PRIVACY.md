# QuizForge 隐私政策

最后更新：2026-08-18

QuizForge 是本地优先的开源 Windows 软件，不建立用户账号，不采集手机号，不包含广告、遥测或后台行为分析。题库、图片、界面设置和第三方 API 凭据保存在用户选择的本机目录或 `%LOCALAPPDATA%\QuizForge`，不会由 QuizForge 运营者集中托管。

## 可选联网行为

QuizForge 只在用户明确发起相应操作时联网：

- 点击“检查更新”时，请求 `api.quizforge.tech` 的公开版本清单。请求包含当前版本、Windows 平台信息和固定 User-Agent，不包含题库正文、文件路径或 API Key。服务器和网络基础设施可能在普通安全／访问日志中记录 IP 地址、请求时间和 HTTP 元数据。
- 点击 GitHub、Releases 或赞助链接时，由浏览器访问相应第三方网站，其数据处理适用该网站自己的隐私政策。
- 用户选择 MinerU、Doc2X 或其他 OpenAI 兼容服务进行识别时，软件会按用户配置把本次指定的文档或请求发送给该服务。QuizForge 运营者不会接收这些内容；第三方服务如何处理数据由用户选择的服务条款和隐私政策决定。
- 用户导出 TeX ZIP 并自行上传 Overleaf 时，上传行为发生在用户与 Overleaf 之间，不经过 QuizForge 服务器。

## 本地数据与删除

卸载程序只移除应用程序文件，不主动删除题库和 `%LOCALAPPDATA%\QuizForge` 中的配置，避免覆盖升级或误卸载造成资料丢失。用户可在确认不再需要后自行删除这些目录；第三方 API 数据和服务端访问日志应分别按对应服务的政策处理。

## Code signing policy privacy statement

This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it.

隐私或安全问题请通过仓库的 Private vulnerability reporting 联系维护者，不要在公开 Issue 中提交题库、真实 API Key 或其他私人内容。
