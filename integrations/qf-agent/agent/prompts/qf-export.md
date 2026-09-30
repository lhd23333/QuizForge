---
description: 按条件导出题目（PDF / TeX / ZIP）
argument-hint: "<筛选条件> [pdf|tex|zip]"
---
按下面的要求导出：

要求：$ARGUMENTS

步骤：
1. 先把要求翻译成 `filter_questions` 的筛选条件，**把命中的题目列成表格给我确认**
   （id / 目录 / 题型 / 难度），并说明共几道。
2. 如果我没指定格式，问我一次（PDF / TeX / ZIP）。
3. 确认后用 `export_questions` 导出；如果要套特定模板，先用 `list_templates`
   列出来让我选。
4. 导出后告诉我文件名与落盘位置，不要贴文件内容。

如果命中 0 道题，停下来告诉我筛选条件可能有问题，不要擅自放宽条件。
