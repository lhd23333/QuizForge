# QuizForge 第三方组件声明

QuizForge 自身按 `GPL-3.0-or-later` 发布，完整条款见仓库根目录及安装目录
`licenses/LICENSE`。本文件记录随软件分发的主要第三方组件及相应许可证。

## Pandoc 3.9.0.2

- 上游项目：https://pandoc.org/
- 源码：https://github.com/jgm/pandoc/tree/3.9.0.2
- 许可证：GNU General Public License，版本 2 或更高版本。
- QuizForge 随包放置的是官方未修改 Windows x86_64 可执行文件，作为独立程序通过命令行调用；QuizForge 自身不是 Pandoc 的派生作品。
- 完整许可证与上游版权说明位于 `runtime/pandoc/licenses/`。
- 对应版本源码包随软件放在 `runtime/pandoc/source/pandoc-3.9.0.2.tar.gz`，用户可以复制、研究和重新构建 Pandoc。

Pandoc 与 QuizForge 均不对 Pandoc 提供任何额外担保。Pandoc 继续适用其自身许可证，作为独立程序与 QuizForge 一同分发。

## pypdf 6.10.0

- 上游项目：https://pypdf.readthedocs.io/
- 许可证：BSD 3-Clause。
- 用途：仅在用户选择“多份试卷合集”时，于本机读取 PDF 书签并拆分页面；不联网。

Copyright (c) 2006-2008, Mathieu Fenniak  
Some contributions copyright (c) 2007, Ashish Kulkarni <kulkarni.ashish@gmail.com>  
Some contributions copyright (c) 2014, Steve Witham <switham_github@mac-guyver.com>

All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

- Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
- Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
- The name of the author may not be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS “AS IS” AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

## 讲义编辑器的 MIT 组件

讲义编排工作台在开发期使用 npm 构建，发行包只包含编译后的本地 JavaScript/CSS/字体，不需要 Node.js，也不会在运行时从 CDN 下载资源。下列生产依赖均采用 MIT 许可证：

- Tiptap `3.27.4`（`@tiptap/core`、`@tiptap/pm`、`@tiptap/markdown`、`@tiptap/starter-kit`、`@tiptap/extension-mathematics`）及 StarterKit 锁定的 Tiptap `3.30.0` 子扩展，Copyright © 2023-present ueberdosis GmbH；
- ProseMirror（model/state/view/transform/history/commands/inputrules/keymap/dropcursor/gapcursor/schema-list/tables/changeset）及 `orderedmap`、`rope-sequence`、`w3c-keyname`，Copyright © Marijn Haverbeke 与各贡献者；
- KaTeX `0.16.22`，Copyright © 2013-2020 Khan Academy and other contributors；
- marked `17.0.6`、linkifyjs `4.3.3`、commander `8.3.0`，版权归各自作者与贡献者所有。

上述组件共同适用的 MIT 许可证全文：

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
