import unittest

import export_tables
import exporter


class ExportTableBoundaryTests(unittest.TestCase):
    def test_html_rows_decode_entities_strip_tags_and_keep_colspan(self):
        inner = (
            '<tr><td>地区</td><td colspan="2">平均分</td></tr>'
            '<tr><td>甲&lt;乙<br>组</td><td>$x$</td><td>4</td></tr>'
        )

        expected = [
            [("地区", 1), ("平均分", 2)],
            [("甲<乙 组", 1), ("$x$", 1), ("4", 1)],
        ]
        self.assertEqual(export_tables.html_table_rows(inner), expected)
        self.assertEqual(exporter._html_table_rows(inner), expected)

    def test_pipe_rows_and_separator_share_the_same_parser(self):
        self.assertTrue(export_tables.PIPE_SEP_RE.match("| --- | :---: |"))
        self.assertEqual(
            export_tables.pipe_text_cells("| 方法一 | $x$ &amp; 1 |"),
            [("方法一", 1), ("$x$ & 1", 1)],
        )
        self.assertIs(exporter._PIPE_SEP_RE, export_tables.PIPE_SEP_RE)

    def test_pipe_inside_inline_math_does_not_split_the_cell(self):
        # 题库真实数据：$\displaystyle |a|=3$ 里的竖线是数学内容，不是单元格边界。
        row = r"| $\displaystyle a=3$ | $\displaystyle |a|=3$ |  |"

        self.assertEqual(
            export_tables.pipe_text_cells(row),
            [(r"$\displaystyle a=3$", 1), (r"$\displaystyle |a|=3$", 1), ("", 1)],
        )

    def test_escaped_pipe_inside_cell_stays_literal(self):
        self.assertEqual(
            export_tables.pipe_text_cells(r"| 甲 \| 乙 | 4 |"),
            [("甲 | 乙", 1), ("4", 1)],
        )

    def test_pipe_table_row_with_absolute_value_renders_one_row(self):
        source = (
            "| " + r"$\displaystyle A$" + " | " + r"$\displaystyle B$" + " |\n"
            "|:--:|:--:|\n"
            r"| $\displaystyle a=3$ | $\displaystyle |a|=3$ |" + "\n"
        )

        tex = exporter._expand_tables(exporter._stash_pipe_tables(source))

        self.assertIn(r"$\displaystyle |a|=3$", tex)
        self.assertNotIn(r"\textbackslash", tex)
        # 表头 + 表体各一行、每行两格，故每行恰好一个 " & "。
        self.assertEqual(tex.count(" & "), 2)


if __name__ == "__main__":
    unittest.main()
