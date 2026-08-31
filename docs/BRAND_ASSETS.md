# QuizForge 品牌资产与图标使用约定

本文规定 QuizForge 产品图标、WIMath 署名图形和运行时功能图标的边界。品牌源文件登记在 [`../assets/brand/README.md`](../assets/brand/README.md)，本文件补充产品页面、桌面壳和发行构建中的使用规则。

## 1. 品牌层级

QuizForge 是产品品牌，WIMath 是母品牌。两者的职责必须分开：

- `quizforge-app-icon` 是 QuizForge 的产品识别图形，用于应用图标、favicon、桌面标题栏、全局导航和关于/欢迎页。
- WIMath mark 和 `quizforge-by-wimath` 是署名或联名图形，只出现在关于页、品牌信息区或明确的发行说明中。
- WIMath 图形不能替代文件夹、导入、设置、删除、搜索等功能图标；功能图标使用统一的线性 `icon()` 宏。
- 工作区默认是中性灰/柔和石墨层级。WIMath 蓝用于品牌区和交互强调，不把母品牌深蓝铺满工作区。

## 2. 受控资产清单

正式源文件位于 `assets/brand/`，运行时镜像位于 `static/brand/`。下表中的哈希是当前受控版本（WIMath 1.1 交付包）；改动任何字节都必须先更新授权、登记和构建校验。

| 文件 | 用途 | SHA-256 |
| --- | --- | --- |
| `assets/brand/quizforge-app-icon.svg` | QuizForge 产品图标矢量源；运行时镜像到 `static/brand/` | `8669569eca82aa4c117dc164f548728a140857542dd235c006394f60354491cd` |
| `assets/brand/quizforge-app-icon-1024.png` | 产品图标 1024 像素源；用于构建和图像工具 | `e27855c602196f235e17340bd6164b8497a8dd8834e20862db86e9b97d8c73b8` |
| `assets/brand/quizforge-app-icon.ico` | Windows 多尺寸图标源（16/24/32/48/64/128/256） | `87cea6cc26d3eb5f53706c6e50baf270df454a4cea944d88269f531daf58b414` |
| `assets/brand/wimath-mark-color.svg` | WIMath 彩色署名图形；运行时镜像 | `8d37860ff6196fbe6a5a79b1c7008b9560aa057fb7a192572ed3379f1c4e64e4` |
| `assets/brand/wimath-mark.svg` | WIMath 运行时兼容别名；与 `mark-color` 同字节 | `8d37860ff6196fbe6a5a79b1c7008b9560aa057fb7a192572ed3379f1c4e64e4` |
| `assets/brand/quizforge-by-wimath.svg` | QuizForge/WIMath 联合署名横版图形；运行时镜像 | `6a916c0479b72bc479f2421c92503d3f5a7d58cf7ba9265f4a015af3dec7067b` |
| `assets/brand/wimath-mark-small-16.svg` | 16 像素紧凑母品牌标志；仅用于状态栏等品牌位置 | `d01c854904ff06115cb5e85e1b4b9e036450803983b6f214b3075a9976819105` |

受控 SVG 的登记字节统一使用 `LF`；根目录 `.gitattributes` 固定源码与运行时镜像的检出换行，SHA-256 仍按原始字节计算。

另有 [`../assets/wimath-logo-latex-black.pdf`](../assets/wimath-logo-latex-black.pdf)，它是 PDF/TeX 导出的黑色 WIMath 标志，不是网页运行时图标；不要把 PDF 转成临时 SVG 后提交。

## 3. 使用矩阵

| 场景 | 应使用 | 不应使用 |
| --- | --- | --- |
| 浏览器 favicon、桌面标题栏、全局导航 | `static/brand/quizforge-app-icon.svg` 或 `.ico` | WIMath mark、旧渐变 QF favicon |
| 关于/欢迎页产品识别 | `quizforge-app-icon.svg` | 把联名图形裁成产品图标 |
| 关于页或发行说明的联合署名 | `quizforge-by-wimath.svg` | 在每个设置组重复显示 |
| 紧凑品牌署名（约 16px） | `wimath-mark-small-16.svg` | 将母品牌图形当作操作按钮 |
| PDF/TeX 页眉或页脚 | `assets/wimath-logo-latex-black.pdf` | 浏览器 SVG 直接交给 XeLaTeX |
| 文件夹、设置、导入、删除、刷新等操作 | `icon()` 宏中的线性功能图标 | Unicode 字符或缩小品牌图形 |

历史兼容输出 `assets/quizforge.png`、`assets/quizforge.ico` 仍由构建脚本生成；新模板应优先引用 `static/brand/`，不要再创建新的兼容路径。

## 4. 视觉和文件规则

- 保持 SVG 的原始 viewBox、比例、路径和颜色，不裁切、旋转、描边重绘、加渐变或叠加阴影。需要尺寸时通过 CSS `width/height` 和 `object-fit: contain` 控制。
- SVG 必须自包含，不嵌入位图、远程资源或外部字体。产品图标 SVG 的标准 viewBox 是 `0 0 256 256`，联名图形是 `0 0 1000 260`，16 像素 mark 是 `0 0 16 16`。
- 小尺寸界面使用产品图标或专门的 16px mark，不把复杂母品牌图形压缩到 16px 功能按钮。图标旁的文字和 `alt`/`aria-label` 要说明产品或署名含义。
- 品牌区可以使用 WIMath 手册中的核心色：`#081A33`、`#2457D6`、`#63B3FF`、`#6F89A3`、`#F7F5EF`；工作区仍遵循 CSS 令牌，不直接复制这组颜色作为面板底色。

## 5. 构建和完整性

不要手工复制或重绘受控资产。更新产品图标或运行时镜像时执行：

```powershell
.venv\Scripts\python.exe tools\build_app_icon.py
```

该脚本会校验源文件 SHA-256、SVG 结构、PNG 尺寸和 ICO 多尺寸，再以原子替换方式生成：

- `assets/quizforge.png`、`assets/quizforge.ico`（历史兼容输出）；
- `static/brand/quizforge-app-icon.svg`、`.ico`；
- `static/brand/wimath-mark*.svg` 和 `quizforge-by-wimath.svg`（运行时镜像）。

桌面目录构建会自动调用此脚本。发行包还必须通过 [`../tools/verify_desktop_bundle.py`](../tools/verify_desktop_bundle.py)，确认 `assets/brand/` 和 `static/brand/` 的必需文件存在，且发行目录不包含私钥、凭据或运行数据。

如果品牌文件确需更新，先获得项目维护者和品牌权利人的授权，再同步修改 `assets/brand/README.md`、本文、`tools/build_app_icon.py` 中的期望哈希和相关发行说明。未经授权不得仅为“看起来更像”而替换二进制或 SVG。

## 6. 许可和发布边界

这些文件是品牌/商标资产，不是第三方软件依赖。QuizForge 的 `GPL-3.0-or-later` 不自动授予 WIMath 或 QuizForge 品牌的额外授权；发行包必须保留 [`../installer/THIRD_PARTY_NOTICES.md`](../installer/THIRD_PARTY_NOTICES.md) 中的品牌说明，公开发布前完成商标清权。

品牌资产不应进入题库、用户导出内容或用户自定义主题配置。用户可以改变界面强调色，但不能改变应用图标、联名署名或 PDF 标志的受控文件。

## 7. 新增品牌场景检查

1. 这是产品识别、母品牌署名还是普通功能？先选对资产层级。
2. 是否保持原始比例、viewBox、颜色和清晰空间，且没有复制出未经登记的变体？
3. 运行时是否引用 `static/brand/`，构建源是否仍来自 `assets/brand/`？
4. 是否为装饰图形提供空 `alt`/`aria-hidden`，为产品或署名提供可读替代文本？
5. 是否更新了资产登记、哈希校验、发行包扫描和变更文档？
