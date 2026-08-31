"""更新脚本的用户数据保留策略回归测试。"""

from pathlib import Path
import tempfile
import unittest

from tools import verify_desktop_bundle


SCRIPT = Path(__file__).resolve().parents[1] / "update_installed.ps1"
INSTALLER = Path(__file__).resolve().parents[1] / "installer" / "QuizForge.iss"


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
        self.assertIn('$protectedTreeNames = @("history")', text)
        self.assertIn('$file.Extension -eq ".md"', text)
        self.assertIn('$protectedContentTreeNames = @("agent_templates")', text)
        self.assertIn('($protectedContentTreeNames -contains $treeName)', text)

    def test_update_hashes_state_before_and_after_install_and_startup(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("PYTHONPYCACHEPREFIX", text)
        self.assertIn("quizforge-update-pycache-", text)
        self.assertIn("$protectedBefore = Get-ProtectedSnapshot", text)
        self.assertIn("$protectedAfterInstall = Get-ProtectedSnapshot", text)
        self.assertIn('Assert-SnapshotEqual $protectedBefore $protectedAfterInstall "Post-install"', text)
        self.assertIn("$stableBefore = Get-ProtectedSnapshot", text)
        self.assertIn("$stableAfterHealth = Get-ProtectedSnapshot", text)
        self.assertIn('Assert-SnapshotEqual $stableBefore $stableAfterHealth "Post-start"', text)

    def test_upgrade_removes_only_known_obsolete_program_files(self):
        script = SCRIPT.read_text(encoding="utf-8")
        installer = INSTALLER.read_text(encoding="utf-8")
        obsolete_files = (
            r"licenses\preview-license.txt",
            r"licenses\THIRD_PARTY_NOTICES-preview.md",
            r"_internal\assets\cloud_entitlement_public_key.pem",
            r"_internal\assets\license_public_key.pem",
            r"_internal\static\js\activation.js",
        )
        self.assertIn("function Remove-ObsoleteProgramFiles", script)
        self.assertIn(
            'Assert-ObsoleteProgramFilesAbsent $InstallDir "Installed application"',
            script,
        )
        self.assertGreater(
            script.index("Remove-ObsoleteProgramFiles $InstallDir"),
            script.index("Copy-Item -Destination $InstallDir"),
        )
        self.assertIn("[InstallDelete]", installer)
        install_delete = installer.split("[InstallDelete]", 1)[1].split("[Icons]", 1)[0]
        self.assertNotIn("*", install_delete)
        for relative_path in obsolete_files:
            self.assertIn(f'    "{relative_path}"', script)
            self.assertIn(
                f'Type: files; Name: "{{app}}\\{relative_path}"',
                install_delete,
            )

    def test_release_scanner_rejects_obsolete_program_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary)
            (dist / "QuizForge.exe").write_bytes(b"MZ")
            obsolete = dist / "_internal" / "static" / "js" / "activation.js"
            obsolete.parent.mkdir(parents=True)
            obsolete.write_text("legacy", encoding="utf-8")
            problems = verify_desktop_bundle.scan(
                dist, Path(__file__).resolve().parent.parent
            )
        self.assertTrue(
            any("_internal/static/js/activation.js" in item for item in problems)
        )


if __name__ == "__main__":
    unittest.main()
