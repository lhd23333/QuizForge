# QuizForge 品牌资产登记

本目录登记 QuizForge 产品图标及 WIMath 母品牌署名图形的受控源文件。文件来自同级
`WIMath品牌` 1.1 交付包，复制时保留原始字节；`tools/build_app_icon.py` 会在桌面构建前
校验 SHA-256，并把产品图标同步到历史兼容路径 `assets/quizforge.png`、
`assets/quizforge.ico` 以及运行时的 `static/brand/`。

| 文件 | 用途 | SHA-256 |
| --- | --- | --- |
| `quizforge-app-icon.svg` | QuizForge 产品图标矢量源 | `8669569eca82aa4c117dc164f548728a140857542dd235c006394f60354491cd` |
| `quizforge-app-icon-1024.png` | 产品图标 1024 像素源 | `e27855c602196f235e17340bd6164b8497a8dd8834e20862db86e9b97d8c73b8` |
| `quizforge-app-icon.ico` | Windows 多尺寸图标源 | `87cea6cc26d3eb5f53706c6e50baf270df454a4cea944d88269f531daf58b414` |
| `wimath-mark-color.svg` | WIMath 品牌署名图形 | `8d37860ff6196fbe6a5a79b1c7008b9560aa057fb7a192572ed3379f1c4e64e4` |
| `wimath-mark.svg` | WIMath 品牌标志运行时兼容别名（与 mark-color 同字节） | `8d37860ff6196fbe6a5a79b1c7008b9560aa057fb7a192572ed3379f1c4e64e4` |
| `quizforge-by-wimath.svg` | QuizForge 与 WIMath 联合署名横版图形 | `6a916c0479b72bc479f2421c92503d3f5a7d58cf7ba9265f4a015af3dec7067b` |
| `wimath-mark-small-16.svg` | 16 像素窗口/状态栏母品牌标志 | `d01c854904ff06115cb5e85e1b4b9e036450803983b6f214b3075a9976819105` |

受控 SVG 的登记字节统一使用 `LF`；根目录 `.gitattributes` 固定源码与运行时镜像的检出换行，SHA-256 仍按原始字节计算。

产品图标 ICO 应包含 16、24、32、48、64、128 和 256 像素版本。产品图标与 WIMath
独立标志 SVG 为自包含路径；所有 SVG 均不引用远程资源或嵌入位图。`quizforge-by-wimath.svg`
的产品名和中文副标题按上游联名规范保留为文本，印刷或不可编辑交付时应先转为轮廓。
它用于关于页等联合署名场景，`wimath-mark-small-16.svg` 仅用于紧凑品牌标识。WIMath
标志用于品牌署名，不替代文件夹、导入、设置等功能图标。

这些文件是品牌/商标资产而非第三方软件依赖。公开发行前仍需确认项目维护者的使用
授权及商标清权；根目录 GPL 条款不会自动授予 WIMath 品牌资产的额外权利。
