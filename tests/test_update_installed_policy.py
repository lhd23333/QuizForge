"""更新脚本的用户数据保留策略回归测试。"""

from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "update_installed.ps1"


class UpdateInstalledPolicyTests(unittest.TestCase):
    def test_protected_state_keeps_current_and_historical_user_configuration(self):
        text = SCRIPT.read_text(encoding="utf-8")
        protected_names = (
            ".enc_key",
            "license.qflicense",
            "device_identity.dat",
            "cloud_account.json",
            "activation.json",
            "mineru.json",
            "doc2x.json",
            "doc2x_local.json",
            "providers.json",
            "service_ports.json",
            "ui_prefs.json",
        )
        for name in protected_names:
            self.assertIn(f'    "{name}"', text)

    def test_update_hashes_state_before_and_after_install_and_startup(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("$protectedBefore = Get-ProtectedSnapshot", text)
        self.assertIn("$protectedAfterInstall = Get-ProtectedSnapshot", text)
        self.assertIn('Assert-SnapshotEqual $protectedBefore $protectedAfterInstall "Post-install"', text)
        self.assertIn("$stableBefore = Get-ProtectedSnapshot", text)
        self.assertIn("$stableAfterHealth = Get-ProtectedSnapshot", text)
        self.assertIn('Assert-SnapshotEqual $stableBefore $stableAfterHealth "Post-start"', text)


if __name__ == "__main__":
    unittest.main()
