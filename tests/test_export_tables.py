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


if __name__ == "__main__":
    unittest.main()
