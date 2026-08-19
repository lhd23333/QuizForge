"""独立桌面产品壳与更新服务边界的回归。"""

import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import config
import desktop
import desktop_product
import exporter
import filestore
import service_ports


class ServicePortsTests(unittest.TestCase):
    def test_missing_config_is_fully_offline(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "missing.json"
            with mock.patch.object(config, "SERVICE_PORTS_PATH", path):
                ports = service_ports.load()
                self.assertEqual(ports.update_mode, "remote")

    def test_invalid_mode_falls_back_to_offline(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "services.json"
            path.write_text(json.dumps({"update_mode": "unknown"}), encoding="utf-8")
            with mock.patch.object(config, "SERVICE_PORTS_PATH", path):
                ports = service_ports.load()
                self.assertEqual(ports.update_mode, "remote")
                self.assertEqual(ports.update_manifest_url, config.UPDATE_MANIFEST_URL)

    def test_legacy_service_fields_are_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "services.json"
            path.write_text(json.dumps({
                "license_mode": "remote", "export_mode": "remote",
                "update_mode": "disabled",
            }), encoding="utf-8")
            with (mock.patch.object(config, "SERVICE_PORTS_PATH", path),
                  mock.patch.object(
                      service_ports.exporter, "export", return_value=Path("x.pdf")
                  )):
                ports = service_ports.load()
                result = service_ports.export_document([])
            self.assertEqual(ports.update_mode, "disabled")
            self.assertEqual(result, Path("x.pdf"))

    def test_local_gateway_delegates_to_existing_exporter(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "missing.json"
            with (mock.patch.object(config, "SERVICE_PORTS_PATH", path),
                  mock.patch.object(service_ports.exporter, "export", return_value=Path("x.pdf")) as call):
                result = service_ports.export_document([{"id": "q1"}], title="测试")
            self.assertEqual(result, Path("x.pdf"))
            call.assert_called_once_with([{"id": "q1"}], title="测试")

    def test_local_gateway_dispatches_docx_without_license_gate(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "missing.json"
            with (mock.patch.object(config, "SERVICE_PORTS_PATH", path),
                  mock.patch.object(
                      service_ports.word_exporter, "export",
                      return_value=Path("x.docx")) as call):
                result = service_ports.export_document(
                    [{"id": "q1"}], title="测试", fmt="docx")
            self.assertEqual(result, Path("x.docx"))
            call.assert_called_once_with(
                [{"id": "q1"}], title="测试", fmt="docx")


class DesktopSettingsTests(unittest.TestCase):
    def test_desktop_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "desktop.json"
            desktop._write_desktop_config(path, {"bank_dir": "D:/QuizForge"})
            self.assertEqual(
                desktop._load_desktop_config(path), {"bank_dir": "D:/QuizForge"}
            )
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_legacy_single_bank_config_migrates_to_deduplicated_bank_list(self):
        with tempfile.TemporaryDirectory() as td:
            bank = Path(td) / "旧题库"
            bank.mkdir()
            normalized = desktop._normalized_desktop_config({
                "bank_dir": str(bank),
                "banks": [str(bank), {"name": "重复记录", "path": str(bank)}],
                "last_version": "0.6.0-beta",
            })
            self.assertEqual(normalized["bank_dir"], str(bank.resolve()))
            self.assertEqual(
                normalized["banks"],
                [{"name": "旧题库", "path": str(bank.resolve()),
                  "subject": "math"}],
            )
            self.assertEqual(desktop._saved_bank(normalized), bank.resolve())
            self.assertEqual(
                Path(normalized["assets_dir"]), bank.resolve() / "_assets")
            self.assertEqual(normalized["last_version"], "0.6.0-beta")

    def test_native_window_is_not_exposed_as_public_js_api_attribute(self):
        api = desktop.DesktopApi(Path("D:/bank"), Path("D:/data"), Path("D:/data/desktop.json"))
        self.assertFalse(hasattr(api, "window"))
        self.assertIsNone(api._window)

    def test_open_local_file_only_opens_a_regular_file_inside_bank(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "bank"
            root.mkdir()
            target = root / "卷子.pdf"
            target.write_bytes(b"pdf")
            api = desktop.DesktopApi(root, Path(td) / "data", Path(td) / "desktop.json")
            with mock.patch.object(desktop.os, "startfile", create=True) as startfile:
                self.assertEqual({"ok": True}, api.open_local_file("卷子.pdf"))
            startfile.assert_called_once_with(str(target.resolve()))
            self.assertFalse(api.open_local_file("../outside.pdf")["ok"])
            self.assertFalse(api.open_local_file(str(target))["ok"])

    def test_bank_directory_can_be_browsed_then_saved_and_verified(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            current = root / "current"
            target = root / "target"
            config_path = root / "data" / "desktop.json"
            current.mkdir()
            target.mkdir()
            api = desktop.DesktopApi(current, root / "data", config_path)
            api._window = mock.Mock()
            api._window.create_file_dialog.return_value = (str(target),)

            browsed = api.browse_bank_directory()
            self.assertTrue(browsed["ok"])
            self.assertEqual(Path(browsed["bank_dir"]), target.resolve())
            self.assertFalse(config_path.exists())

            saved = api.set_bank_directory(browsed["bank_dir"])
            self.assertTrue(saved["ok"])
            self.assertTrue(target.is_dir())
            self.assertEqual(
                desktop._load_desktop_config(config_path)["bank_dir"],
                str(target.resolve()),
            )
            saved_config = desktop._load_desktop_config(config_path)
            self.assertEqual(
                [entry["path"] for entry in saved_config["banks"]],
                [str(target.resolve()), str(current.resolve())],
            )
            self.assertEqual(list(target.glob(".quizforge_write_test_*")), [])

    def test_bank_subject_is_saved_and_same_path_subject_change_restarts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bank = root / "物理题库"
            bank.mkdir()
            config_path = root / "data" / "desktop.json"
            api = desktop.DesktopApi(bank, root / "data", config_path, "math")

            saved = api.set_bank_directory(str(bank), "physics")

            self.assertTrue(saved["ok"])
            self.assertTrue(saved["restart_required"])
            self.assertEqual(api.restart_subject, "physics")
            value = desktop._load_desktop_config(config_path)
            self.assertEqual(value["banks"][0]["subject"], "physics")
            restarted = desktop.DesktopApi(bank, root / "data", config_path, "physics")
            self.assertEqual(restarted._bank_list()[0]["subject_label"], "物理")

    def test_switch_restart_uses_selected_bank_instead_of_inherited_environment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            current = root / "current"
            target = root / "target"
            data = root / "data"
            current.mkdir()
            target.mkdir()
            api = desktop.DesktopApi(current, data, data / "desktop.json")

            result = api.set_bank_directory(str(target))

            self.assertTrue(result["restart_required"])
            self.assertEqual(api.restart_bank_dir, target.resolve())
            with mock.patch.object(desktop.subprocess, "Popen") as popen, \
                    mock.patch.dict("os.environ", {"QUIZFORGE_BANK": str(current)}):
                desktop._launch_bank_process(api.restart_bank_dir, data)
            launched_env = popen.call_args.kwargs["env"]
            self.assertEqual(launched_env["QUIZFORGE_BANK"], str(target.resolve()))
            self.assertEqual(launched_env["QUIZFORGE_DATA_DIR"], str(data.resolve()))
            self.assertEqual(launched_env["QUIZFORGE_SUBJECT"], "math")
            self.assertEqual(
                launched_env["QUIZFORGE_ASSETS_DIR"],
                str((current.resolve() / "_assets")),
            )
            self.assertEqual(
                launched_env["QUIZFORGE_BANK_STATE_DIR"],
                str(desktop._bank_state_dir(data, target)),
            )

    def test_shared_assets_directory_merges_registered_banks_without_deleting_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            math = root / "数学"
            physics = root / "物理"
            target = root / "_assets"
            data = root / "data"
            for path in (math / "_assets", physics / "_assets", target):
                path.mkdir(parents=True)
            (math / "_assets" / "math.png").write_bytes(b"math")
            (physics / "_assets" / "physics.png").write_bytes(b"physics")
            config_path = data / "desktop.json"
            desktop._write_desktop_config(config_path, {
                "bank_dir": str(math),
                "assets_dir": str(math / "_assets"),
                "banks": [{"path": str(math)}, {"path": str(physics)}],
            })
            api = desktop.DesktopApi(
                math, data, config_path, assets_dir=math / "_assets")

            result = api.set_assets_directory(str(target))

            self.assertTrue(result["ok"])
            self.assertTrue(result["restart_required"])
            self.assertEqual(result["copied"], 2)
            self.assertEqual((target / "math.png").read_bytes(), b"math")
            self.assertEqual((target / "physics.png").read_bytes(), b"physics")
            self.assertTrue((math / "_assets" / "math.png").is_file())
            self.assertEqual(
                desktop._load_desktop_config(config_path)["assets_dir"],
                str(target.resolve()),
            )

    def test_shared_assets_directory_rejects_same_name_with_different_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            one = root / "one"
            two = root / "two"
            target = root / "shared"
            data = root / "data"
            for path in (one / "_assets", two / "_assets", target):
                path.mkdir(parents=True)
            (one / "_assets" / "same.png").write_bytes(b"one")
            (two / "_assets" / "same.png").write_bytes(b"two")
            config_path = data / "desktop.json"
            desktop._write_desktop_config(config_path, {
                "bank_dir": str(one),
                "assets_dir": str(one / "_assets"),
                "banks": [{"path": str(one)}, {"path": str(two)}],
            })
            api = desktop.DesktopApi(one, data, config_path, assets_dir=one / "_assets")

            result = api.set_assets_directory(str(target))

            self.assertFalse(result["ok"])
            self.assertIn("同名图片内容冲突", result["error"])
            self.assertFalse((target / "same.png").exists())
            self.assertEqual(
                desktop._load_desktop_config(config_path)["assets_dir"],
                str((one / "_assets").resolve()),
            )

    def test_bank_opens_in_independent_process_without_switching_current_window(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            current = root / "current"
            target = root / "target"
            data = root / "data"
            current.mkdir()
            target.mkdir()
            api = desktop.DesktopApi(current, data, data / "desktop.json")
            process = mock.Mock(pid=4321)

            with mock.patch.object(desktop, "_launch_bank_process",
                                   return_value=process) as launch:
                result = api.open_bank_in_new_window(str(target))

            self.assertTrue(result["ok"])
            self.assertEqual(result["pid"], 4321)
            self.assertEqual(api.bank_dir, current.resolve())
            self.assertIsNone(api.restart_bank_dir)
            launch.assert_called_once_with(target.resolve(), data, "math")
            saved = desktop._load_desktop_config(data / "desktop.json")
            self.assertEqual(saved["bank_dir"], str(current.resolve()))
            self.assertIn(str(target.resolve()), [entry["path"] for entry in saved["banks"]])

    def test_secondary_window_does_not_replace_default_when_registering_or_removing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            default = root / "default"
            secondary = root / "secondary"
            third = root / "third"
            for path in (default, secondary, third):
                path.mkdir()
            data = root / "data"
            config_path = data / "desktop.json"
            desktop._write_desktop_config(config_path, {
                "bank_dir": str(default),
                "banks": [
                    {"name": "default", "path": str(default)},
                    {"name": "secondary", "path": str(secondary)},
                ],
            })
            api = desktop.DesktopApi(secondary, data, config_path)
            process = mock.Mock(pid=9)

            with mock.patch.object(desktop, "_launch_bank_process",
                                   return_value=process):
                self.assertTrue(api.open_bank_in_new_window(str(third))["ok"])
            self.assertEqual(
                desktop._load_desktop_config(config_path)["bank_dir"],
                str(default.resolve()),
            )
            self.assertTrue(api.remove_bank_directory(str(third))["ok"])
            self.assertEqual(
                desktop._load_desktop_config(config_path)["bank_dir"],
                str(default.resolve()),
            )

    def test_each_bank_uses_separate_webview_storage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            one = root / "one"
            two = root / "two"
            one.mkdir()
            two.mkdir()
            self.assertNotEqual(
                desktop._webview_storage_dir(root, one),
                desktop._webview_storage_dir(root, two),
            )
            self.assertNotEqual(
                desktop._bank_state_dir(root, one),
                desktop._bank_state_dir(root, two),
            )

    def test_legacy_runtime_state_migrates_only_to_original_default_bank(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            current = root / "current"
            other = root / "other"
            data.mkdir()
            current.mkdir()
            other.mkdir()
            (data / "conversion_tasks.json").write_text('{"old": true}', encoding="utf-8")
            (data / "selections.json").write_text('["q1"]', encoding="utf-8")
            config_path = data / "desktop.json"
            value = {"bank_dir": str(current), "banks": [{"path": str(current)}]}
            desktop._write_desktop_config(config_path, value)

            migrated = desktop._migrate_legacy_bank_state(
                config_path, data, value, current)

            state = desktop._bank_state_dir(data, current)
            self.assertEqual((state / "conversion_tasks.json").read_text(encoding="utf-8"),
                             '{"old": true}')
            self.assertEqual((state / "selections.json").read_text(encoding="utf-8"),
                             '["q1"]')
            self.assertEqual(migrated["bank_state_migrated"], str(current.resolve()))
            second = desktop._migrate_legacy_bank_state(
                config_path, data, migrated, other)
            self.assertEqual(second, migrated)
            self.assertFalse(desktop._bank_state_dir(data, other).exists())

    def test_bank_can_be_created_and_noncurrent_record_removed_without_deleting_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            current = root / "current"
            remembered = root / "remembered"
            data = root / "data"
            config_path = data / "desktop.json"
            current.mkdir()
            remembered.mkdir()
            desktop._write_desktop_config(config_path, {
                "bank_dir": str(current),
                "banks": [
                    {"name": "current", "path": str(current)},
                    {"name": "remembered", "path": str(remembered)},
                ],
            })
            api = desktop.DesktopApi(current, data, config_path)

            removed = api.remove_bank_directory(str(remembered))
            self.assertTrue(removed["ok"])
            self.assertFalse(removed["files_deleted"])
            self.assertTrue(remembered.is_dir())
            self.assertNotIn(
                str(remembered.resolve()),
                [entry["path"] for entry in desktop._load_desktop_config(config_path)["banks"]],
            )
            rejected = api.remove_bank_directory(str(current))
            self.assertFalse(rejected["ok"])
            self.assertIn("当前题库不能移除", rejected["error"])

            created = api.create_bank_directory(str(root), "新题库")
            self.assertTrue(created["ok"])
            target = root / "新题库"
            self.assertTrue(target.is_dir())
            saved = desktop._load_desktop_config(config_path)
            self.assertEqual(saved["bank_dir"], str(target.resolve()))
            self.assertIn(str(current.resolve()), [entry["path"] for entry in saved["banks"]])

    def test_new_bank_name_rejects_traversal_reserved_and_duplicate_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            current = root / "current"
            current.mkdir()
            api = desktop.DesktopApi(current, root, root / "desktop.json")
            for name in ("..", "a/b", "CON", "bad."):
                with self.subTest(name=name):
                    self.assertFalse(api.create_bank_directory(str(root), name)["ok"])
            (root / "已有").mkdir()
            duplicate = api.create_bank_directory(str(root), "已有")
            self.assertFalse(duplicate["ok"])
            self.assertIn("同名文件夹", duplicate["error"])

    def test_bank_directory_accepts_string_picker_result_and_rejects_relative_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            api = desktop.DesktopApi(root, root, root / "desktop.json")
            api._window = mock.Mock()
            api._window.create_file_dialog.return_value = str(root)

            self.assertEqual(
                Path(api.browse_bank_directory()["bank_dir"]), root.resolve()
            )
            rejected = api.set_bank_directory("relative/bank")
            self.assertFalse(rejected["ok"])
            self.assertIn("绝对路径", rejected["error"])

    def test_bank_switch_dialog_has_manual_path_fallback(self):
        from app import app

        with mock.patch.dict("os.environ", {"QUIZFORGE_DESKTOP": "1"}):
            html = app.test_client().get("/").get_data(as_text=True)
        self.assertIn('id="desktop-bank-dialog"', html)
        self.assertIn('id="desktop-bank-path"', html)
        self.assertIn('id="desktop-bank-list"', html)
        self.assertIn('id="desktop-bank-create-form"', html)
        self.assertIn("browse_bank_directory", html)
        self.assertIn("browse_bank_parent_directory", html)
        self.assertIn("set_bank_directory", html)
        self.assertIn("create_bank_directory", html)
        self.assertIn("remove_bank_directory", html)
        self.assertIn("open_bank_in_new_window", html)
        self.assertIn("create_bank_in_new_window", html)
        self.assertIn("在新窗口打开", html)

    def test_frameless_window_controls_delegate_to_native_window(self):
        api = desktop.DesktopApi(Path("D:/bank"), Path("D:/data"), Path("D:/data/desktop.json"))
        api._window = mock.Mock()
        api._window.native = None

        self.assertTrue(api.window_minimize()["ok"])
        api._window.minimize.assert_called_once_with()

        maximized = api.window_toggle_maximize()
        self.assertTrue(maximized["maximized"])
        api._window.maximize.assert_called_once_with()

        restored = api.window_toggle_maximize()
        self.assertFalse(restored["maximized"])
        api._window.restore.assert_called_once_with()

        with mock.patch.object(desktop.threading, "Timer") as timer:
            self.assertTrue(api.window_close()["ok"])
            timer.assert_called_once_with(0.05, api._window.destroy)
            timer.return_value.start.assert_called_once_with()

    def test_frameless_window_keeps_native_taskbar_controls(self):
        source = (Path(__file__).resolve().parent.parent / "desktop.py").read_text(
            encoding="utf-8")
        self.assertIn("native.ShowInTaskbar = True", source)
        self.assertIn("native.MinimizeBox = True", source)
        self.assertIn("native.MaximizeBox = True", source)
        self.assertIn(
            "native.MaximizedBounds = Screen.FromHandle(native.Handle).WorkingArea",
            source)

    def test_active_taskbar_button_click_minimizes_frameless_window(self):
        api = desktop.DesktopApi(
            Path("D:/bank"), Path("D:/data"), Path("D:/data/desktop.json"))
        api._window = mock.Mock()
        api._native_handle = 123
        api._taskbar_button_rect = (10, 20, 110, 120)
        posted_messages = []

        class FakeUser32:
            calls = 0

            def GetAsyncKeyState(self, _button):
                self.calls += 1
                if self.calls == 1:
                    return 0x8000
                api._taskbar_monitor_stop.set()
                return 0

            @staticmethod
            def GetCursorPos(pointer):
                coordinates = desktop.ctypes.cast(
                    pointer,
                    desktop.ctypes.POINTER(desktop.ctypes.c_long * 2),
                ).contents
                coordinates[0] = 50
                coordinates[1] = 60
                return True

            @staticmethod
            def GetForegroundWindow():
                return 123

            @staticmethod
            def IsIconic(_handle):
                return False

            @staticmethod
            def PostMessageW(handle, message, command, parameter):
                posted_messages.append((handle, message, command, parameter))
                return True

        fake_windll = mock.Mock(user32=FakeUser32())
        with mock.patch.object(desktop.ctypes, "windll", fake_windll):
            api._taskbar_click_monitor()

        self.assertEqual(posted_messages, [(123, 0x0112, 0xF020, 0)])
        api._window.minimize.assert_not_called()

    def test_taskbar_rect_monitor_never_reads_winforms_from_worker(self):
        api = desktop.DesktopApi(
            Path("D:/bank"), Path("D:/data"), Path("D:/data/desktop.json"))
        api._native_handle = 123
        api._taskbar_monitor_stop = mock.Mock()
        api._taskbar_monitor_stop.is_set.side_effect = [False, True]

        with mock.patch.object(
                api, "_find_taskbar_button_rect",
                return_value=(10, 20, 110, 120)) as find_rect:
            api._taskbar_rect_monitor()

        find_rect.assert_called_once_with(desktop.APP_NAME)
        self.assertEqual(api._taskbar_button_rect, (10, 20, 110, 120))

    def test_frameless_window_resize_clamps_size_and_fixes_opposite_corner(self):
        from webview.window import FixPoint

        api = desktop.DesktopApi(Path("D:/bank"), Path("D:/data"), Path("D:/data/desktop.json"))
        api._window = mock.Mock()

        resized = api.window_resize(800, 500, "nw")
        self.assertEqual(resized, {"ok": True, "width": 1024, "height": 680})
        api._window.resize.assert_called_once_with(
            1024, 680, FixPoint.NORTH | FixPoint.WEST)
        self.assertFalse(api.window_resize(1200, 800, "bad-corner")["ok"])

    def test_pdf_capture_maps_css_viewport_to_client_pixels(self):
        box = desktop._client_css_capture_box(
            (-1920, 40, 1500, 900),
            {"x": 100, "y": 50, "width": 200, "height": 100,
             "viewport_width": 1000, "viewport_height": 600},
        )

        self.assertEqual(box, (-1770, 115, 300, 150))

    def test_pdf_capture_writes_short_token_to_current_bank_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bank = root / "bank"
            data = root / "data"
            bank.mkdir()
            api = desktop.DesktopApi(bank, data, data / "desktop.json")
            api._native_handle = 123
            request_rect = {
                "x": 10, "y": 20, "width": 120, "height": 80,
                "viewport_width": 1000, "viewport_height": 600,
            }

            def fake_capture(_box, target):
                target.write_bytes(b"png")

            with (mock.patch.object(
                    desktop, "_window_client_screen_rect",
                    return_value=(0, 0, 1000, 600)),
                  mock.patch.object(
                    desktop, "_capture_screen_region_png",
                    side_effect=fake_capture)):
                result = api.capture_client_rect(request_rect)

            self.assertTrue(result["ok"])
            self.assertRegex(result["name"], r"^library-card-[0-9a-f]{32}\.png$")
            capture = (desktop._bank_state_dir(data, bank)
                       / "uploads" / "batch" / result["name"])
            self.assertEqual(capture.read_bytes(), b"png")
            self.assertTrue(api.discard_client_capture(result["name"])["ok"])
            self.assertFalse(capture.exists())

    def test_desktop_shell_has_all_frameless_resize_handles(self):
        from app import app

        with mock.patch.dict("os.environ", {"QUIZFORGE_DESKTOP": "1"}):
            html = app.test_client().get("/").get_data(as_text=True)
        for edge in ("n", "ne", "e", "se", "s", "sw", "w", "nw"):
            self.assertIn(f'data-resize-edge="{edge}"', html)
        self.assertIn("window_resize", html)


class DesktopFirstRunTests(unittest.TestCase):
    def test_pyinstaller_version_resource_matches_product_version(self):
        root = Path(__file__).resolve().parent.parent
        raw = (root / "installer" / "pyinstaller-version.txt").read_text(
            encoding="utf-8"
        )
        numeric = desktop_product.PRODUCT_VERSION.split("-", 1)[0]
        parts = tuple(int(part) for part in numeric.split(".")) + (0,)
        self.assertIn(f"filevers={parts}", raw)
        self.assertIn(f"prodvers={parts}", raw)
        self.assertIn(
            f"StringStruct('ProductVersion', '{desktop_product.PRODUCT_VERSION}')",
            raw,
        )

    def test_all_release_metadata_matches_product_version(self):
        root = Path(__file__).resolve().parent.parent
        product_version = desktop_product.PRODUCT_VERSION
        numeric = product_version.split("-", 1)[0]
        file_version = f"{numeric}.0"
        expected = {
            "build_desktop.ps1": (
                f'[string]$Version = "{product_version}"',
                f'[string]$FileVersion = "{file_version}"',
                '--file-version=$FileVersion',
                '--product-version=$FileVersion',
            ),
            "build_installer.ps1": (
                f'[string]$Version = "{product_version}"',
                f'[string]$FileVersion = "{file_version}"',
            ),
            "installer/QuizForge.iss": (
                f'#define MyAppVersion "{product_version}"',
                f'#define MyFileVersion "{file_version}"',
            ),
            "installer/pyinstaller-version.txt": (
                f"StringStruct('FileVersion', '{file_version}')",
                f"StringStruct('ProductVersion', '{product_version}')",
            ),
            "package.json": (f'"version": "{product_version}"',),
            "package-lock.json": (f'"version": "{product_version}"',),
            "LICENSE": ("GNU GENERAL PUBLIC LICENSE",),
            "installer/THIRD_PARTY_NOTICES.md": ("QuizForge 第三方组件声明",),
        }
        for relative, markers in expected.items():
            raw = (root / relative).read_text(encoding="utf-8")
            for marker in markers:
                with self.subTest(file=relative, marker=marker):
                    self.assertIn(marker, raw)

        installer = (root / "installer" / "QuizForge.iss").read_text(
            encoding="utf-8"
        )
        self.assertIn("LicenseFile=..\\LICENSE", installer)
        self.assertIn('Source: "..\\LICENSE"', installer)

    def test_empty_bank_gets_original_demo_only_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with (mock.patch.object(config, "BANK_DIR", root),
                  mock.patch.object(config, "TRASH_DIR", root / ".trash"),
                  mock.patch.object(config, "ASSETS_DIR", root / "_assets")):
                filestore.invalidate_scan_cache(folder_structure=True)
                self.assertTrue(desktop_product.seed_demo_bank(root))
                files = sorted(root.rglob("*.md"))
                self.assertEqual(len(files), 3)
                self.assertTrue(all("QuizForge 内置原创示例" in p.read_text(encoding="utf-8")
                                    for p in files))
                self.assertFalse(desktop_product.seed_demo_bank(root))
                self.assertEqual(len(list(root.rglob("*.md"))), 3)
                filestore.invalidate_scan_cache(folder_structure=True)

    def test_report_accepts_missing_xelatex_with_overleaf_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bank = root / "bank"
            data = root / "data"
            pandoc = root / "pandoc.exe"
            bank.mkdir()
            data.mkdir()
            pandoc.write_bytes(b"placeholder")
            with (mock.patch.object(config, "BANK_DIR", bank),
                  mock.patch.object(config, "DATA_DIR", data),
                  mock.patch.object(config, "PANDOC", str(pandoc)),
                  mock.patch.object(config, "XELATEX", "missing-xelatex")):
                report = desktop_product.environment_report()
            rows = {row["name"]: row for row in report["checks"]}
            self.assertTrue(report["ready"])
            self.assertEqual(rows["Pandoc"]["status"], "ok")
            self.assertEqual(rows["XeLaTeX"]["status"], "warn")
            self.assertIn("Overleaf", rows["XeLaTeX"]["detail"])

    def test_export_tool_errors_are_actionable(self):
        with mock.patch.object(exporter.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(exporter.ExportError, "重新安装 QuizForge"):
                exporter._run(["pandoc"], Path.cwd(), "pandoc")
            with self.assertRaisesRegex(exporter.ExportError, "Overleaf"):
                exporter._run(["xelatex"], Path.cwd(), "xelatex")

    def test_about_and_welcome_pages_render(self):
        from app import app

        fake_report = {
            "version": desktop_product.PRODUCT_VERSION, "desktop": True, "frozen": True,
            "bank_dir": "D:/bank", "data_dir": "D:/data", "log_dir": "D:/data/logs",
            "checks": [], "ready": True, "services": {},
            "account": {
                "logged_in": False, "entitlement_valid": False,
                "entitlement_error": "", "user": {},
            },
        }
        with mock.patch.object(desktop_product, "environment_report", return_value=fake_report):
            client = app.test_client()
            self.assertIn("关于 QuizForge", client.get("/about").get_data(as_text=True))
            welcome = client.get("/welcome?demo=1").get_data(as_text=True)
            self.assertIn("欢迎使用 QuizForge", welcome)
            self.assertIn("已加入 3 道示例题", welcome)
            self.assertNotIn("导入许可证", welcome)
            self.assertIn("desktop-titlebar", welcome)
            self.assertIn("data-window-action=\"close\"", welcome)

    def test_desktop_shell_is_present_in_first_html_frame(self):
        from app import app

        with mock.patch.dict("os.environ", {"QUIZFORGE_DESKTOP": "1"}):
            html = app.test_client().get("/").get_data(as_text=True)
        self.assertIn('class="desktop-host"', html)
        self.assertIn('id="desktop-titlebar">', html)
        self.assertIn('id="desktop-bank-button"', html)
        self.assertNotIn('id="desktop-titlebar" hidden', html)

        with mock.patch.dict("os.environ", {"QUIZFORGE_DESKTOP": "0"}):
            browser_html = app.test_client().get("/").get_data(as_text=True)
        self.assertNotIn('class="desktop-host"', browser_html)
        self.assertIn('id="desktop-titlebar" hidden', browser_html)

    def test_desktop_question_workspace_keeps_browser_layout_boundary(self):
        from app import app

        root = Path(__file__).resolve().parent.parent
        card_template = (root / "templates" / "_question_card.html").read_text(
            encoding="utf-8"
        )
        stylesheet = (root / "static" / "style.css").read_text(encoding="utf-8")
        with mock.patch.dict("os.environ", {"QUIZFORGE_DESKTOP": "1"}):
            desktop_html = app.test_client().get("/").get_data(as_text=True)
        with mock.patch.dict("os.environ", {"QUIZFORGE_DESKTOP": "0"}):
            browser_html = app.test_client().get("/").get_data(as_text=True)

        self.assertIn('data-sidebar-tab="files"', desktop_html)
        self.assertIn('data-sidebar-tab="filters"', desktop_html)
        self.assertIn("quizforge.sidebar.activePanel", desktop_html)
        self.assertIn("static/favicon.svg", desktop_html)
        self.assertIn("desktop-card-controls", card_template)
        self.assertIn("legacy-card-controls", card_template)
        self.assertIn("card-more-trigger", card_template)
        self.assertIn("html.desktop-host .desktop-card-controls", stylesheet)
        self.assertIn("html.desktop-host .legacy-card-controls", stylesheet)
        self.assertNotIn('class="desktop-host"', browser_html)

    def test_tool_page_header_does_not_inherit_sticky_navigation(self):
        root = Path(__file__).resolve().parent.parent
        stylesheet = (root / "static" / "style.css").read_text(encoding="utf-8")

        sidebar = re.search(
            r"(?ms)^\.app-sidebar \{(.*?)^\}", stylesheet
        ).group(1)
        page_header = re.search(
            r"(?ms)^\.app-page-head \{(.*?)^\}", stylesheet
        ).group(1)
        self.assertIn("position: fixed", sidebar)
        self.assertNotIn("position: sticky", page_header)
        self.assertNotRegex(stylesheet, r"(?m)^header \{")
        self.assertNotRegex(stylesheet, r"(?m)^html\.desktop-host header \{")


if __name__ == "__main__":
    unittest.main()
