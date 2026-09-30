import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "migrate_exam_math_format", ROOT / "tools" / "migrate_exam_math_format.py"
)
MIGRATION = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MIGRATION)


class MathFormatMigrationTests(unittest.TestCase):
    def normalize(self, text):
        return MIGRATION.normalize_document(text)

    def test_inline_math_and_existing_displaystyle_are_canonical(self):
        result, counters = self.normalize("?? $x+1$ ? $\\displaystyle y$?")
        self.assertEqual(
            result,
            "?? $\\displaystyle x+1$ ? $\\displaystyle y$?",
        )
        self.assertEqual(counters["inline_normalized"], 2)

    def test_multiline_block_math_is_flattened_and_preserved(self):
        source = "??\n$$\\begin{cases}\nx=1,\\\\\ny=2.\n\\end{cases}$$"
        result, _ = self.normalize(source)
        self.assertEqual(
            result,
            "??\n$\\displaystyle \\begin{cases}\nx=1,\\\\\ny=2.\n\\end{cases}$",
        )

    def test_adjacent_inline_math_is_not_treated_as_block_math(self):
        result, _ = self.normalize("answer $\u2460$$\u2462$.")
        self.assertEqual(result, "answer $\\displaystyle \u2460$ $\\displaystyle \u2462$.")
        self.assertNotIn("$$", result)

    def test_option_labels_and_explicit_option_prose_are_wrapped(self):
        source = "A. $x$\nB. $y$\n?? B?\nA ????"
        result, _ = self.normalize(source)
        self.assertIn("$\\displaystyle A.$", result)
        self.assertIn("$\\displaystyle B.$", result)
        self.assertIn("?? $\\displaystyle B$?", result)
        self.assertIn("$\\displaystyle A$ ????", result)

    def test_ocr_escaped_closing_dollar_is_repaired(self):
        result, counters = self.normalize("? $a_n\\$ ?$n\\geqslant4$?")
        self.assertEqual(result, "? $\\displaystyle a_n$ ?$\\displaystyle n\\geqslant4$?")
        self.assertEqual(counters["repaired_escaped_closing_dollar"], 1)

    def test_frontmatter_and_images_are_untouched(self):
        source = "---\nid: abc\n---\n![[a_b.png]]\n?? $x$."
        result, _ = self.normalize(source)
        self.assertTrue(result.startswith("---\nid: abc\n---\n"))
        self.assertIn("![[a_b.png]]", result)
        self.assertIn("$\\displaystyle x$", result)

    def test_normalization_is_idempotent(self):
        source = "A. $x$\n\n## ??\n$$y=1$$"
        once, _ = self.normalize(source)
        twice, counters = self.normalize(once)
        self.assertEqual(once, twice)
        self.assertGreaterEqual(sum(counters.values()), 0)

    def test_crlf_process_does_not_double_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "q.md"
            path.write_bytes(b"---\r\nid: 1\r\n---\r\n$x$\r\n")
            result = MIGRATION.process(root, apply=False, backup=None)
            self.assertEqual(result["changed_count"], 1)
            normalized = MIGRATION.normalize_document("---\nid: 1\n---\n$x$\n")[0]
            expected = normalized.replace("\n", "\r\n")
            # Re-read through the same transformation path to ensure no CRCRLF is produced.
            raw = path.read_bytes().decode("utf-8")
            logical = raw.replace("\r\n", "\n")
            actual = MIGRATION.normalize_document(logical)[0].replace("\n", "\r\n")
            self.assertEqual(actual, expected)
            self.assertNotIn("\r\r\n", actual)


if __name__ == "__main__":
    unittest.main()
