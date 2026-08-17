"""公开签名政策和可验证 Windows 构建边界回归。"""

from pathlib import Path
import re
import unittest

from ruamel.yaml import YAML


ROOT = Path(__file__).resolve().parents[1]
SIGNPATH_CREDIT = (
    "Free code signing provided by SignPath.io, "
    "certificate by SignPath Foundation"
)
PRIVACY_SENTENCE = (
    "This program will not transfer any information to other networked "
    "systems unless specifically requested by the user or the person "
    "installing or operating it."
)


class ReleasePolicyTests(unittest.TestCase):
    def test_homepages_link_public_signing_and_privacy_policies(self):
        for name in ("README.md", "README.en.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(SIGNPATH_CREDIT, text)
            self.assertIn("docs/CODE_SIGNING_POLICY.md", text)
            self.assertIn("PRIVACY.md", text)

    def test_policy_contains_required_roles_and_privacy_statement(self):
        policy = (ROOT / "docs" / "CODE_SIGNING_POLICY.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(SIGNPATH_CREDIT, policy)
        self.assertIn(PRIVACY_SENTENCE, policy)
        self.assertIn("Committer and reviewer", policy)
        self.assertIn("Approver", policy)
        self.assertIn("multi-factor authentication", policy)
        self.assertIn("manual approval", policy)

        privacy = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")
        self.assertIn(PRIVACY_SENTENCE, privacy)
        self.assertIn("api.quizforge.tech", privacy)

    def test_candidate_workflow_is_read_only_and_uses_pinned_actions(self):
        path = ROOT / ".github" / "workflows" / "windows-release-candidate.yml"
        raw = path.read_text(encoding="utf-8")
        data = YAML(typ="safe").load(raw)

        self.assertEqual({"contents": "read"}, data["permissions"])
        self.assertIn("pull_request", data["on"])
        self.assertIn("workflow_dispatch", data["on"])
        self.assertNotIn("secrets.", raw)
        self.assertNotRegex(raw, r"\bgh\s+release\b")
        self.assertNotIn("release-action", raw)

        verify_steps = [
            step
            for step in data["jobs"]["build"]["steps"]
            if step.get("name") in {"Verify source", "Verify release bundle"}
        ]
        self.assertEqual(2, len(verify_steps))
        for step in verify_steps:
            self.assertEqual("${{ runner.temp }}", step["env"]["TEMP"])
            self.assertEqual("${{ runner.temp }}", step["env"]["TMP"])

        uses = re.findall(r"^\s*uses:\s*([^\s#]+)", raw, re.MULTILINE)
        self.assertGreaterEqual(len(uses), 4)
        for action in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_ci_runtime_sources_and_hashes_are_fixed(self):
        script = (ROOT / "tools" / "prepare_ci_runtime.ps1").read_text(
            encoding="ascii"
        )
        self.assertIn("pandoc-3.9.0.2-windows-x86_64.zip", script)
        self.assertIn("hackage.haskell.org/package/pandoc-", script)
        self.assertIn(
            "C97542F2800F446E788D9F74237856D995421AD1BB3CC8324286840C5F272D3A",
            script,
        )
        self.assertIn(
            "E83F8354C0F507222B5684797B9C5AE766F03889785995D14AAC27816EC456BA",
            script,
        )
        self.assertNotIn("latest", script.lower())


if __name__ == "__main__":
    unittest.main()
