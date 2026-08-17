"""题库搜索语法与匹配语义回归。"""

import unittest

from search_query import SearchQueryError, matches_search, parse_search_query


class SearchQueryTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "body": "求二次函数的最小值",
            "solution": "使用配方法构造完全平方",
            "tags": ["高中数学", "函数"],
            "source": "2026 北京期中考试",
            "type": "解答题",
            "difficulty": "3",
            "starred": True,
        }

    def test_plain_text_searches_body_solution_tags_and_source(self):
        for text in ("二次函数", "配方法", "高中数学", "北京期中"):
            with self.subTest(text=text):
                self.assertTrue(matches_search(self.record, parse_search_query(text)))
        self.assertFalse(matches_search(self.record, parse_search_query("立体几何")))

    def test_structured_fields_and_quoted_value_are_combined_with_and(self):
        query = parse_search_query(
            'content:"二次函数" solution:配方法 tag:函数 '
            'source:期中 type:解答题 difficulty:3 starred:true')

        self.assertTrue(query.structured)
        self.assertTrue(matches_search(self.record, query))
        self.assertFalse(matches_search(
            dict(self.record, difficulty="4"), query))

    def test_unrecognized_prefix_is_plain_term_inside_structured_query(self):
        query = parse_search_query("tag:函数 author:北京")

        self.assertTrue(matches_search(
            dict(self.record, body="命题信息 author:北京"), query))
        self.assertFalse(matches_search(
            dict(self.record, source="上海期中考试"), query))

    def test_tag_resolver_adds_descendants_without_storing_parent_tag(self):
        record = dict(self.record, tags=["函数/二次函数"])
        query = parse_search_query("tag:函数")

        matched = matches_search(
            record, query,
            tag_resolver=lambda value: {"函数/一次函数", "函数/二次函数"}
            if value == "函数" else set(),
        )

        self.assertTrue(matched)

    def test_invalid_boolean_and_unclosed_quote_raise_readable_error(self):
        with self.assertRaisesRegex(SearchQueryError, "starred"):
            parse_search_query("starred:maybe")
        with self.assertRaisesRegex(SearchQueryError, "引号"):
            parse_search_query('content:"二次函数')

    def test_recognized_empty_or_out_of_range_value_is_rejected(self):
        with self.assertRaisesRegex(SearchQueryError, "tag"):
            parse_search_query("tag:")
        with self.assertRaisesRegex(SearchQueryError, "difficulty"):
            parse_search_query(
                "difficulty:9", allowed_difficulties={"1", "2", "3", "4", "5"})


if __name__ == "__main__":
    unittest.main()
