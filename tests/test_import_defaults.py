import unittest

import import_defaults


class ImportDefaultsTests(unittest.TestCase):
    def test_math_choice_defaults_distinguish_single_multi_and_pair(self):
        pair_applies = lambda body, _qtype: body.startswith("PAIR")

        self.assertEqual(
            import_defaults.import_image_defaults(
                "单选题", "![[one.png]]", subject="math",
                pair_applies=pair_applies),
            ("opts", [], "column"),
        )
        self.assertEqual(
            import_defaults.import_image_defaults(
                "单选题", "![[one.png]]\n![[two.png]]", subject="math",
                pair_applies=pair_applies),
            ("between", [], "row"),
        )
        self.assertEqual(
            import_defaults.import_image_defaults(
                "单选题", "PAIR\n![[a.png]]\n![[b.png]]\n![[c.png]]\n![[d.png]]",
                subject="math", pair_applies=pair_applies),
            ("pair", [], "column"),
        )

    def test_physics_defaults_and_manual_values_are_preserved(self):
        never_pair = lambda _body, _qtype: False

        self.assertEqual(
            import_defaults.import_image_defaults(
                "填空题", "![[a.png]]\n![[b.png]]", subject="physics",
                pair_applies=never_pair),
            ("between", [], "row"),
        )
        self.assertEqual(
            import_defaults.import_image_defaults(
                "解答题", "![[a.png]]", subject="physics",
                pair_applies=never_pair),
            ("after", [{"i": 0, "align": "center"}], "row"),
        )
        self.assertEqual(
            import_defaults.import_image_defaults(
                "解答题", "![[a.png]]\n![[b.png]]", subject="physics",
                pair_applies=never_pair, requested_mode="full",
                requested_flow="column"),
            ("full", [{"i": 0, "align": "center", "stack": True}], "column"),
        )

    def test_solution_images_use_full_mode_and_stack_multiple_images(self):
        self.assertEqual(
            import_defaults.import_solution_image_defaults("没有图片"),
            (None, []),
        )
        self.assertEqual(
            import_defaults.import_solution_image_defaults(
                "![[a.png]]\n![[b.png]]"),
            ("full", [{"i": 0, "stack": True}]),
        )


if __name__ == "__main__":
    unittest.main()
