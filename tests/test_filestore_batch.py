"""文件题库批量建题的顺序与扫描次数回归。"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import config
import filestore


class FilestoreBatchCreateTests(unittest.TestCase):
    def test_filename_is_title_and_custom_frontmatter_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            path = Path(td) / "用户旧名称.md"
            filestore._write_raw(path, {
                "id": "legacy-id", "source": "旧题源",
                "title": "用户自己的 frontmatter 标题",
            }, "旧题干")
            before = path.read_text(encoding="utf-8")

            row = filestore.get_question("legacy-id")
            after = path.read_text(encoding="utf-8")

        self.assertEqual(row["title"], "用户旧名称")
        self.assertEqual(row["name"], "用户旧名称")
        self.assertEqual(before, after)
        self.assertIn("title: 用户自己的 frontmatter 标题", after)

    def test_default_titles_follow_source_number_and_batch_sequence(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            batch = filestore.create_questions_batch([
                {"body": "无题号一", "source": "函数专题"},
                {"body": "无题号二", "source": "函数专题"},
            ], "题集")
            numbered = filestore.create_question(
                "原卷第八题", source="北京卷", number=8, folder="题集")
            single = filestore.create_question(
                "单题", source="校本例题", folder="题集")
            explicit = filestore.create_question(
                "显式名称", source="不会覆盖名称", number=9,
                folder="题集", title="自定义题卡")
            rows = {row["id"]: row for row in
                    filestore.collection_records_snapshot("题集", recursive=False)}

        self.assertEqual(rows[batch[0]]["title"], "函数专题第1题")
        self.assertEqual(rows[batch[1]]["title"], "函数专题第2题")
        self.assertEqual(rows[numbered]["title"], "北京卷第8题")
        self.assertEqual(rows[single]["title"], "校本例题")
        self.assertEqual(rows[explicit]["title"], "自定义题卡")
        self.assertEqual(
            {Path(rows[qid]["path"]).stem for qid in rows},
            {row["title"] for row in rows.values()})

    def test_title_conflicts_use_readable_suffix_and_temp_index_only_grows(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            first = filestore.create_question(
                "一", folder="临时卡片", temporary=True)
            second = filestore.create_question(
                "二", folder="临时卡片", temporary=True)
            conflict = filestore.create_question(
                "重名", folder="临时卡片", title="临时卡2")
            filestore.create_question("三", folder="临时卡片", title="临时卡4")
            rows = {row["id"]: row for row in
                    filestore.collection_records_snapshot(
                        "临时卡片", recursive=False)}
            next_title = filestore.next_temporary_question_title()

        self.assertEqual(rows[first]["title"], "临时卡1")
        self.assertEqual(rows[second]["title"], "临时卡2")
        self.assertEqual(rows[conflict]["title"], "临时卡2_2")
        self.assertEqual(next_title, "临时卡5")

    def test_rename_question_keeps_id_and_references_and_avoids_collision(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            filestore.create_question("占位", folder="题集", title="目标名称")
            qid = filestore.create_question(
                "题干\n\n![[q-stable_1.png]]", solution="解析",
                source="原题源", folder="题集", title="旧名称")
            old_path = Path(td) / filestore.get_question(qid)["path"]
            before = old_path.read_bytes()

            renamed = filestore.rename_question(qid, "目标名称")
            new_path = Path(td) / renamed["path"]
            old_exists = old_path.exists()
            new_exists = new_path.is_file()
            after = new_path.read_bytes()

        self.assertEqual(renamed["id"], qid)
        self.assertEqual(renamed["title"], "目标名称_2")
        self.assertEqual(renamed["name"], "目标名称_2")
        self.assertEqual(renamed["source"], "原题源")
        self.assertIn("![[q-stable_1.png]]", renamed["body"])
        self.assertFalse(old_exists)
        self.assertTrue(new_exists)
        self.assertEqual(after, before)

    def test_note_round_trip_and_body_update_preserves_existing_note(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            qid = filestore.create_question(
                "原题干", solution="原解析", note="容易忽略定义域")
            created = filestore.get_question(qid)
            filestore.update_question(
                qid, "修改后的题干", solution="修改后的解析")
            preserved = filestore.get_question(qid)
            filestore.update_question(
                qid, preserved["body"], solution=preserved["solution"], note="新备注")
            updated = filestore.get_question(qid)
            raw = (Path(td) / updated["path"]).read_text(encoding="utf-8")

        self.assertEqual(created["note"], "容易忽略定义域")
        self.assertEqual(preserved["note"], "容易忽略定义域")
        self.assertEqual(updated["note"], "新备注")
        self.assertEqual(raw.count("## 备注"), 1)

    def test_batch_scans_order_once_and_keeps_sequence(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            original = filestore._top_order
            with mock.patch.object(
                    filestore, "_top_order", wraps=original) as top_order:
                ids = filestore.create_questions_batch([
                    {"body": "第一题", "type": "填空题", "number": 1},
                    {"body": "第二题", "type": "填空题", "number": 2},
                    {"body": "第三题", "type": "填空题", "number": 3},
                ], "某卷")
            rows = filestore.list_questions(collection="某卷")

        self.assertEqual(len(ids), 3)
        self.assertEqual(top_order.call_count, 1)
        self.assertEqual([row["number"] for row in rows], [1, 2, 3])
        self.assertEqual([row["order"] for row in rows], [1.0, 2.0, 3.0])

    def test_batch_order_only_reads_target_folder(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            filestore.create_questions_batch(
                [{"body": "其它年份题", "number": 1}], "高考卷/2025/卷甲")
            with mock.patch.object(
                    filestore, "_all_records",
                    side_effect=AssertionError("导入排序不应解析整座题库")):
                ids = filestore.create_questions_batch([
                    {"body": "第一题", "number": 1},
                    {"body": "第二题", "number": 2},
                ], "高考卷/2026/卷乙")
                rows = filestore.collection_records_snapshot(
                    "高考卷/2026/卷乙", recursive=False)

        self.assertEqual([row["id"] for row in rows], ids)
        self.assertEqual([row["order"] for row in rows], [1.0, 2.0])

    def test_source_sort_is_natural_stable_and_puts_empty_source_last(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache(folder_structure=True)
            ids = filestore.create_questions_batch([
                {"body": "卷10第二题", "source": "模拟卷10", "number": 99},
                {"body": "卷2第二题", "source": "模拟卷2", "number": 88},
                {"body": "卷2第一题", "source": "模拟卷2", "number": 77},
                {"body": "无题源", "source": "", "number": 1},
                {"body": "卷10第一题", "source": "模拟卷10", "number": 66},
            ], "来源")
            # 自定义顺序才是题源组内基准，题号故意逆向设置，防止回退为按题号排序。
            filestore.reorder([
                ids[2], ids[1], ids[0], ids[4], ids[3]])

            rows = filestore.list_questions(
                sort="source", records=filestore.all_records_snapshot())

        self.assertEqual(
            [(row["source"], row["body"]) for row in rows],
            [
                ("模拟卷2", "卷2第一题"),
                ("模拟卷2", "卷2第二题"),
                ("模拟卷10", "卷10第二题"),
                ("模拟卷10", "卷10第一题"),
                ("", "无题源"),
            ])
        self.assertEqual(rows[-1]["id"], ids[3])

    def test_cold_single_question_lookup_does_not_parse_whole_bank(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            qid = filestore.create_question("冷启动定向读取", folder="试卷")
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            with mock.patch.object(
                    filestore, "_all_records",
                    side_effect=AssertionError("读取单题不应解析整座题库")):
                row = filestore.get_question(qid)

        self.assertEqual(row["id"], qid)
        self.assertEqual(row["body"], "冷启动定向读取")

    def test_batch_move_appends_in_source_order_without_global_scan(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache(folder_structure=True)
            source_ids = filestore.create_questions_batch([
                {"body": "第一题", "number": 1},
                {"body": "第二题", "number": 2},
                {"body": "第三题", "number": 3},
            ], "来源")
            seed = filestore.create_question("目标既有题", folder="目标", number=9)

            with mock.patch.object(
                    filestore, "_all_records",
                    side_effect=AssertionError("批量移动不应解析整座题库")):
                moved = filestore.move_to_collection(
                    [source_ids[2], source_ids[0]], "目标")
                target = filestore.list_questions(records=
                    filestore.collection_records_snapshot(
                        "目标", recursive=False))

        self.assertEqual(moved, [source_ids[0], source_ids[2]])
        self.assertEqual([row["id"] for row in target],
                         [seed, source_ids[0], source_ids[2]])
        self.assertEqual([row["order"] for row in target], [1.0, 2.0, 3.0])

    def test_copy_keeps_content_metadata_and_creates_independent_ids(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache(folder_structure=True)
            first = filestore.create_question(
                "第一题 ![[图.png]]", solution="第一题解析", note="第一题备注",
                qtype="单选题", source="原卷", difficulty="3", tags=["重点"],
                folder="来源", number=1)
            second = filestore.create_question(
                "第二题", solution="第二题解析", folder="来源", number=2)
            first_path = Path(td) / filestore.get_question(first)["path"]
            meta, body = filestore._read_raw(first_path)
            meta.update({
                "img_split": "opts", "img_layouts": [{"i": 0, "w": 40}],
                "custom_field": "保留我", "_quizforge_import_scope": "旧任务",
                "_quizforge_import_index": 0,
            })
            filestore._write_raw(first_path, meta, body)
            (Path(td) / "目标").mkdir()

            with mock.patch.object(
                    filestore, "_all_records",
                    side_effect=AssertionError("批量复制不应解析整座题库")):
                created = filestore.copy_to_collection([second, first], "目标")
                copies = filestore.list_questions(records=
                    filestore.collection_records_snapshot(
                        "目标", recursive=False))

            original = filestore.get_question(first)
            copied_meta, _copied_body = filestore._read_raw(
                Path(td) / copies[0]["path"])

        self.assertEqual(len(created), 2)
        self.assertEqual([row["number"] for row in copies], [1, 2])
        self.assertEqual([row["id"] for row in copies], created)
        self.assertNotIn(first, created)
        self.assertEqual(original["folder"], "来源")
        for key in ("body", "solution", "note", "type", "source", "difficulty",
                    "tags", "img_split", "img_layouts"):
            self.assertEqual(copies[0][key], original[key], key)
        self.assertEqual(copied_meta["custom_field"], "保留我")
        self.assertNotIn("_quizforge_import_scope", copied_meta)
        self.assertNotIn("_quizforge_import_index", copied_meta)

    def test_automatic_batch_scope_is_persistent_and_idempotent(self):
        items = [
            {"body": "第一题", "solution": "第一题解析",
             "type": "填空题", "source": "专题一", "number": 1},
            {"body": "第二题", "solution": "第二题解析",
             "type": "解答题", "source": "专题一", "number": 2},
        ]
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            partial = filestore.create_questions_batch(
                items[:1], "专题一", idempotency_scope="batch:b1:group:0")
            # 模拟只写完第一题、进程就退出；新进程没有任何内存标记，只能依靠
            # 题目 frontmatter 认回第一题并继续补第二题。
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            completed = filestore.create_questions_batch(
                items, "专题一", idempotency_scope="batch:b1:group:0")
            # 任务状态再次丢失时，整组重跑仍不得新增第三份文件。
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            retried = filestore.create_questions_batch(
                items, "专题一", idempotency_scope="batch:b1:group:0")
            paths = sorted((Path(td) / "专题一").glob("*.md"))
            metas = [filestore._read_raw(path)[0] for path in paths]

        self.assertEqual(completed[0], partial[0])
        self.assertEqual(retried, completed)
        self.assertEqual(len(paths), 2)
        self.assertEqual(
            {meta["_quizforge_import_scope"] for meta in metas},
            {"batch:b1:group:0"})
        self.assertEqual(
            sorted(meta["_quizforge_import_index"] for meta in metas), [0, 1])

    def test_automatic_batch_failure_rolls_back_only_current_scope_writes(self):
        items = [
            {"body": "第一题", "number": 1},
            {"body": "第二题", "number": 2},
        ]
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            existing = filestore.create_questions_batch(
                [{"body": "用户既有题", "number": 9}], "专题一")
            original_write = filestore._write_raw
            calls = 0

            def fail_second(path, meta, body):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("模拟第二题写盘失败")
                return original_write(path, meta, body)

            with mock.patch.object(filestore, "_write_raw", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "模拟第二题写盘失败"):
                    filestore.create_questions_batch(
                        items, "专题一",
                        idempotency_scope="batch:b2:group:0")
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            rows = filestore.list_questions(collection="专题一")

        self.assertEqual([row["id"] for row in rows], existing)
        self.assertEqual([row["body"] for row in rows], ["用户既有题"])

    def test_safe_refresh_preserves_identity_and_user_metadata(self):
        scope = "batch:refresh:group:0"
        old = [
            {"body": "旧第一题", "solution": "旧解析一", "type": "单选题",
             "source": "专题", "number": 1},
            {"body": "旧第二题", "solution": "旧解析二", "type": "解答题",
             "source": "专题", "number": 2},
        ]
        new = [
            {"body": "新第一题含完整选项", "solution": "新解析一", "type": "单选题",
             "source": "专题", "number": 1},
            {"body": "新第二题", "solution": "新解析二", "type": "解答题",
             "source": "专题", "number": 2},
        ]
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            ids = filestore.create_questions_batch(
                old, "专题", idempotency_scope=scope)
            first = filestore.get_question(ids[0])
            path = Path(td) / first["path"]
            meta, body = filestore._read_raw(path)
            meta.update({"difficulty": "难", "tags": ["保留"],
                         "starred": True, "custom_field": "用户值"})
            filestore._write_raw(path, meta, body)
            filestore._cache.clear()
            filestore.invalidate_scan_cache()

            refreshed = filestore.refresh_questions_batch(
                new, old, "专题", idempotency_scope=scope)
            rows = {row["id"]: row for row in
                    filestore.list_questions(collection="专题")}
            saved_meta, _ = filestore._read_raw(path)

        self.assertEqual(ids, refreshed)
        self.assertEqual("新第一题含完整选项", rows[ids[0]]["body"])
        self.assertEqual("新解析一", rows[ids[0]]["solution"])
        self.assertEqual("难", rows[ids[0]]["difficulty"])
        self.assertEqual(["保留"], rows[ids[0]]["tags"])
        self.assertTrue(rows[ids[0]]["starred"])
        self.assertEqual("用户值", saved_meta["custom_field"])

    def test_safe_refresh_rejects_user_edit_before_any_write(self):
        scope = "batch:refresh-edited:group:0"
        old = [
            {"body": "旧第一题", "solution": "旧解析一", "type": "单选题",
             "source": "专题", "number": 1},
            {"body": "旧第二题", "solution": "旧解析二", "type": "解答题",
             "source": "专题", "number": 2},
        ]
        new = [dict(item, body=f"新{index + 1}题")
               for index, item in enumerate(old)]
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            ids = filestore.create_questions_batch(
                old, "专题", idempotency_scope=scope)
            filestore.update_question(
                ids[0], "用户已经修改第一题", "旧解析一",
                "单选题", source="专题")
            before = {row["id"]: (row["body"], row["solution"])
                      for row in filestore.list_questions(collection="专题")}

            with self.assertRaisesRegex(ValueError, "入库后被编辑"):
                filestore.refresh_questions_batch(
                    new, old, "专题", idempotency_scope=scope)
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            after = {row["id"]: (row["body"], row["solution"])
                     for row in filestore.list_questions(collection="专题")}

        self.assertEqual(before, after)

    def test_safe_refresh_rolls_back_whole_group_when_second_replace_fails(self):
        scope = "batch:refresh-rollback:group:0"
        old = [
            {"body": "旧第一题", "solution": "旧解析一", "type": "单选题",
             "source": "专题", "number": 1},
            {"body": "旧第二题", "solution": "旧解析二", "type": "解答题",
             "source": "专题", "number": 2},
        ]
        new = [dict(item, body=f"新第{index + 1}题")
               for index, item in enumerate(old)]
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            ids = filestore.create_questions_batch(
                old, "专题", idempotency_scope=scope)
            paths = [Path(td) / filestore.get_question(qid)["path"]
                     for qid in ids]
            before = {path: path.read_bytes() for path in paths}
            real_replace = filestore.os.replace
            calls = 0

            def fail_second_replace(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("模拟第二题替换失败")
                return real_replace(source, target)

            with mock.patch.object(
                    filestore.os, "replace", side_effect=fail_second_replace):
                with self.assertRaisesRegex(OSError, "模拟第二题替换失败"):
                    filestore.refresh_questions_batch(
                        new, old, "专题", idempotency_scope=scope)
            after = {path: path.read_bytes() for path in paths}
            leftovers = list(Path(td).rglob("*.tmp"))

        self.assertEqual(before, after)
        self.assertEqual([], leftovers)

    def test_safe_refresh_preserves_external_edit_during_validation_window(self):
        scope = "batch:refresh-external-edit:group:0"
        old = [{"body": "旧题正文", "solution": "旧解析", "type": "单选题",
                "source": "专题", "number": 1}]
        new = [dict(old[0], body="新识别正文")]
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            qid = filestore.create_questions_batch(
                old, "专题", idempotency_scope=scope)[0]
            path = Path(td) / filestore.get_question(qid)["path"]
            meta, _body = filestore._read_raw(path)
            external = filestore._render_raw(
                meta, filestore._join_sections(
                    "用户在 Obsidian 中刚保存的正文", "旧解析", []),
            ).encode("utf-8")
            real_read_bytes = Path.read_bytes
            reads = 0

            def race_read_bytes(current):
                nonlocal reads
                if current.resolve() == path.resolve():
                    reads += 1
                    if reads == 2:
                        path.write_bytes(external)
                return real_read_bytes(current)

            with mock.patch.object(Path, "read_bytes", race_read_bytes):
                with self.assertRaisesRegex(ValueError, "外部编辑"):
                    filestore.refresh_questions_batch(
                        new, old, "专题", idempotency_scope=scope)
            saved = path.read_bytes()
            leftovers = list(Path(td).rglob("*.tmp"))

        self.assertEqual(external, saved)
        self.assertEqual([], leftovers)

    def test_index_helpers_reuse_one_records_snapshot(self):
        records = [
            {"folder": "年份/试卷甲", "tags": ["高考", "甲"], "order": 1},
            {"folder": "年份/试卷乙", "tags": ["高考"], "order": 2},
        ]
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)), \
                mock.patch.object(filestore, "_all_records",
                                  side_effect=AssertionError("不应重复扫描题库")):
            (Path(td) / "年份" / "试卷甲").mkdir(parents=True)
            (Path(td) / "年份" / "试卷乙").mkdir(parents=True)
            tags = filestore.all_tags(records)
            tree = filestore.list_collections_tree(records)
            flat = filestore.all_collections(tree)
            rows = filestore.list_questions(collection="年份", records=records)

        self.assertEqual(tags, ["高考", "甲"])
        self.assertEqual(len(flat), 3)
        self.assertEqual(len(rows), 2)

    def test_global_tags_reuse_sidebar_cache(self):
        records = [{"tags": ["高考"]}]
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)), \
                mock.patch.object(filestore, "_all_records",
                                  return_value=records) as scan:
            filestore.invalidate_scan_cache()
            first = filestore.all_tags()
            second = filestore.all_tags()

        self.assertEqual(first, ["高考"])
        self.assertEqual(second, first)
        scan.assert_called_once_with()

    def test_scan_snapshot_avoids_rewalking_within_short_window(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            filestore.create_questions_batch([{"body": "题目", "number": 1}])
            real_rglob = Path.rglob
            calls = []

            def counted_rglob(path, pattern):
                calls.append((path, pattern))
                return real_rglob(path, pattern)

            with mock.patch.object(Path, "rglob", counted_rglob):
                self.assertEqual(len(filestore.all_records_snapshot()), 1)
                self.assertEqual(len(filestore.all_records_snapshot()), 1)
                self.assertEqual(len(calls), 1)
                filestore.invalidate_scan_cache()
                self.assertEqual(len(filestore.all_records_snapshot()), 1)
                self.assertEqual(len(calls), 2)

    def test_collection_children_lists_only_one_level(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            (Path(td) / "高考卷" / "2026" / "全国卷").mkdir(parents=True)
            (Path(td) / "高考卷" / "2025").mkdir(parents=True)

            children = filestore.list_collection_children("高考卷")

        self.assertEqual([row["name"] for row in children], ["2025", "2026"])
        self.assertFalse(children[0]["has_children"])
        self.assertTrue(children[1]["has_children"])
        self.assertNotIn("全国卷", [row["name"] for row in children])

    def test_collection_snapshot_does_not_scan_sibling_year(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            filestore.create_questions_batch(
                [{"body": "2026题", "number": 1}], "高考卷/2026/卷甲")
            filestore.create_questions_batch(
                [{"body": "2025题", "number": 1}], "高考卷/2025/卷乙")

            rows = filestore.collection_records_snapshot("高考卷/2026")

        self.assertEqual([row["body"] for row in rows], ["2026题"])

    def test_question_path_snapshot_uses_natural_order_and_reads_only_chunk(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            paper = Path(td) / "高考卷" / "2020" / "测试卷"
            paper.mkdir(parents=True)
            for name in ("第10题.md", "第2题.md", "第1题.md"):
                (paper / name).write_text(name.removesuffix(".md"), encoding="utf-8")
            filestore._cache.clear()

            paths = filestore.list_question_paths("高考卷/2020")
            self.assertEqual(
                [Path(path).name for path in paths],
                ["第1题.md", "第2题.md", "第10题.md"],
            )
            original_read = filestore._read_raw
            with mock.patch.object(
                    filestore, "_read_raw", wraps=original_read) as read_raw:
                rows = filestore.records_from_paths(paths[1:])

        self.assertEqual([row["body"] for row in rows], ["第2题", "第10题"])
        self.assertEqual(read_raw.call_count, 2)

    def test_question_path_snapshot_reuses_identity_without_reopening_files(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            root = Path(td)
            (root / "题卡.md").write_text(filestore._render_raw({
                "id": "question-id", "quizforge_kind": "question",
            }, "题目"), encoding="utf-8", newline="\n")
            (root / "资料.md").write_text(filestore._render_raw({
                "id": "document-id", "quizforge_kind": "document",
            }, "资料"), encoding="utf-8", newline="\n")
            filestore._identity_cache.clear()

            with mock.patch.object(
                    filestore, "_peek_question_identity",
                    wraps=filestore._peek_question_identity) as peek:
                first = filestore.list_question_paths()
            self.assertEqual(first, ["题卡.md"])
            self.assertEqual(peek.call_count, 2)
            first_generation = filestore._question_paths_generation

            with mock.patch.object(
                    filestore, "_peek_question_identity",
                    side_effect=AssertionError("缓存命中后不应再次快读")), \
                    mock.patch.object(
                        Path, "open",
                        side_effect=AssertionError("缓存命中后不应再次打开文件")), \
                    mock.patch.object(
                        Path, "rglob",
                        side_effect=AssertionError("TTL 内应直接复用完整快照")):
                second = filestore.list_question_paths()

            future = (time.monotonic()
                      + filestore._QUESTION_PATHS_CACHE_SECONDS + 1)
            with mock.patch.object(filestore.time, "monotonic", return_value=future), \
                    mock.patch.object(
                        filestore, "_peek_question_identity",
                        side_effect=AssertionError("指纹命中后不应再次快读")), \
                    mock.patch.object(
                        Path, "open",
                        side_effect=AssertionError("指纹命中后不应再次打开文件")):
                after_ttl = filestore.list_question_paths()

        self.assertEqual(second, first)
        self.assertEqual(after_ttl, first)
        self.assertEqual(filestore._question_paths_generation, first_generation)

    def test_question_identity_cache_invalidates_after_external_edit(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            path = Path(td) / "资料.md"
            path.write_text(filestore._render_raw({
                "id": "same-id", "quizforge_kind": "document",
            }, "短文"), encoding="utf-8", newline="\n")
            filestore._identity_cache.clear()
            self.assertEqual(filestore.list_question_paths(), [])
            before = path.stat()

            # 模拟 Obsidian 直接修改，不调用 QuizForge 的缓存失效入口；正文长度变化
            # 保证即使极端文件系统没有推进 mtime，size 也会让身份缓存失效。
            path.write_text(filestore._render_raw({
                "id": "same-id", "quizforge_kind": "question",
            }, "修改后的题目正文，长度明显不同"), encoding="utf-8", newline="\n")
            after = path.stat()
            self.assertNotEqual(
                (before.st_mtime_ns, before.st_size),
                (after.st_mtime_ns, after.st_size))
            future = (time.monotonic()
                      + filestore._QUESTION_PATHS_CACHE_SECONDS + 1)
            with mock.patch.object(filestore.time, "monotonic", return_value=future), \
                    mock.patch.object(
                        filestore, "_peek_question_identity",
                        wraps=filestore._peek_question_identity) as peek:
                refreshed = filestore.list_question_paths()

        self.assertEqual(refreshed, ["资料.md"])
        self.assertEqual(peek.call_count, 1)

    def test_parsed_question_record_backfills_identity_cache(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            path = Path(td) / "题卡.md"
            path.write_text(filestore._render_raw({
                "id": "question-id", "quizforge_kind": "question",
            }, "题目"), encoding="utf-8", newline="\n")
            filestore._cache.clear()
            filestore._identity_cache.clear()

            rows = filestore.records_from_paths(["题卡.md"])
            with mock.patch.object(
                    filestore, "_peek_question_identity",
                    side_effect=AssertionError("完整解析已回填身份缓存")):
                paths = filestore.list_question_paths()

        self.assertEqual([row["id"] for row in rows], ["question-id"])
        self.assertEqual(paths, ["题卡.md"])

    def test_document_identity_is_excluded_from_every_question_read_path(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            root = Path(td)
            folder = root / "资料"
            folder.mkdir()
            (folder / "旧题.md").write_text("旧题正文", encoding="utf-8")
            (folder / "新题.md").write_text(filestore._render_raw({
                "id": "question-id", "quizforge_kind": "question",
            }, "新题正文"), encoding="utf-8", newline="\n")
            for name, kind in (("文档", "document"), ("讲义", "handout"),
                               ("未来类型", "worksheet")):
                (folder / f"{name}.md").write_text(filestore._render_raw({
                    "id": f"{kind}-id", "quizforge_kind": kind,
                }, f"{name}正文 ![[{name}.png]]"), encoding="utf-8", newline="\n")
            # 复杂标量强制 records_from_ids 走完整扫描回退，结果仍须遵守同一身份规则。
            (folder / "复杂题.md").write_text(
                "---\nid: complex-question\nquizforge_kind: >-\n  question\n---\n\n复杂题",
                encoding="utf-8", newline="\n")
            (folder / "复杂文档.md").write_text(
                "---\nid: complex-document\nquizforge_kind: >-\n  document\n---\n\n复杂文档",
                encoding="utf-8", newline="\n")
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            expected = {"旧题", "question-id", "complex-question"}

            self.assertEqual(
                {row["id"] for row in filestore.all_records_snapshot()}, expected)
            self.assertEqual(
                {row["id"] for row in filestore.collection_records_snapshot("资料")},
                expected)
            paths = filestore.list_question_paths("资料")
            self.assertEqual({Path(path).stem for path in paths},
                             {"旧题", "新题", "复杂题"})
            self.assertEqual(
                {row["id"] for row in filestore.records_from_paths([
                    path.relative_to(root).as_posix() for path in folder.glob("*.md")
                ])}, expected)
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            rows = filestore.records_from_ids([
                "旧题", "question-id", "document-id", "handout-id",
                "worksheet-id", "complex-question", "complex-document",
            ])

            self.assertEqual({row["id"] for row in rows}, expected)
            with mock.patch.object(
                    filestore, "_registered_bank_roots",
                    return_value=[root.resolve()]):
                live, _stats = filestore._registered_live_refs()
            self.assertIn("文档.png", live)

    def test_new_questions_write_kind_and_temp_index_ignores_documents(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            folder = Path(td) / "临时卡片"
            folder.mkdir()
            (folder / "临时卡2.md").write_text("旧题", encoding="utf-8")
            (folder / "临时卡99.md").write_text(filestore._render_raw({
                "id": "document-id", "quizforge_kind": "document",
            }, "普通文档"), encoding="utf-8", newline="\n")
            filestore._cache.clear()
            filestore.invalidate_scan_cache()

            self.assertEqual(filestore.next_temporary_question_title(), "临时卡3")
            qid = filestore.create_question(
                "新题", folder="临时卡片", temporary=True)
            row = filestore.get_question(qid)
            meta, _body = filestore._read_raw(Path(td) / row["path"])

        self.assertEqual(row["title"], "临时卡3")
        self.assertEqual(meta["quizforge_kind"], "question")

    def test_document_markdown_stays_out_of_recycle_and_paper_panels(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)), \
                mock.patch.object(config, "TRASH_DIR", Path(td) / ".trash"):
            root = Path(td)
            config.TRASH_DIR.mkdir()
            document = config.TRASH_DIR / "资料.md"
            document.write_text(filestore._render_raw({
                "id": "document-id", "quizforge_kind": "document",
                "_trash_deleted_at": "2026-01-01T00:00:00",
            }, "资料"), encoding="utf-8", newline="\n")
            (config.TRASH_DIR / "题目.md").write_text(filestore._render_raw({
                "id": "question-id", "quizforge_kind": "question",
                "_trash_deleted_at": "2026-01-02T00:00:00",
            }, "题目"), encoding="utf-8", newline="\n")
            (root / "资料.md").write_text("资料", encoding="utf-8")
            (root / "旧资料.markdown").write_text("旧资料", encoding="utf-8")
            (root / "原卷.pdf").write_bytes(b"pdf")

            self.assertEqual(
                [row["id"] for row in filestore.list_deleted_questions()],
                ["question-id"])
            with self.assertRaises(KeyError):
                filestore.restore_question("document-id")
            filestore.purge_question("document-id")
            self.assertTrue(document.exists())
            self.assertEqual(
                [row["filename"] for row in filestore.list_papers("")],
                ["原卷.pdf"])
            self.assertIsNone(filestore.paper_abspath("资料.md"))
            self.assertIsNone(filestore.paper_abspath("旧资料.markdown"))

    def test_navigation_tree_only_expands_active_path(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            (Path(td) / "高考卷" / "2026" / "全国卷").mkdir(parents=True)
            (Path(td) / "高考卷" / "2025" / "北京卷").mkdir(parents=True)

            tree = filestore.list_navigation_tree("高考卷/2026")

        exam = tree[0]
        years = {row["name"]: row for row in exam["children"]}
        self.assertTrue(exam["children_loaded"])
        self.assertFalse(years["2026"]["children_loaded"])
        self.assertEqual(years["2026"]["children"], [])
        self.assertTrue(years["2026"]["has_children"])
        self.assertFalse(years["2025"]["children_loaded"])
        self.assertEqual(years["2025"]["children"], [])
        self.assertTrue(years["2025"]["has_children"])

    def test_navigation_tree_keeps_unrelated_roots_collapsed(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            (Path(td) / "高考卷" / "2026" / "全国卷").mkdir(parents=True)
            (Path(td) / "练习册" / "函数").mkdir(parents=True)

            tree = filestore.list_navigation_tree("")

        self.assertEqual([row["name"] for row in tree], ["练习册", "高考卷"])
        for row in tree:
            self.assertFalse(row["children_loaded"])
            self.assertEqual(row["children"], [])
            self.assertTrue(row["has_children"])

    def test_loaded_question_updates_without_global_rescan(self):
        """题卡已经出现在页面后，单题按钮只能重读该题文件。"""
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            qid = filestore.create_question(
                "求下列式子的值。\n\n![[示意图.png]]",
                qtype="填空题", folder="试卷")
            # 页面渲染会把当前批次放进 mtime 缓存，模拟用户随后点击分栏按钮。
            rows = filestore.collection_records_snapshot("试卷")
            self.assertEqual([row["id"] for row in rows], [qid])

            with mock.patch.object(
                    filestore, "_all_records",
                    side_effect=AssertionError("单题更新不应扫描全库")):
                self.assertEqual(filestore.get_question(qid)["id"], qid)
                filestore.set_img_split(qid, "opts")
                updated = filestore.get_question(qid)

        self.assertEqual(updated["img_split"], "opts")

    def test_backups_directory_is_excluded_from_questions_and_tree(self):
        """修复前副本可留在题库中，但不能再次参与全选与导出。"""
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache(folder_structure=True)
            qid = filestore.create_question(
                "正式题目", qtype="填空题", folder="试卷")
            backup = Path(td) / "_backups" / "试卷"
            backup.mkdir(parents=True)
            source = next((Path(td) / "试卷").glob("*.md"))
            (backup / source.name).write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8")
            filestore.invalidate_scan_cache(folder_structure=True)

            rows = filestore.list_questions()
            paths = filestore.list_question_paths()
            tree = filestore.list_collections_tree(rows)

        self.assertEqual([row["id"] for row in rows], [qid])
        self.assertEqual(len(paths), 1)
        self.assertNotIn("_backups", [node["name"] for node in tree])

    def test_collection_lookup_validates_directory_without_question_scan(self):
        """移动题目前只需确认目标目录存在，不需要统计整库题数。"""
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)), \
                mock.patch.object(
                    filestore, "_all_records",
                    side_effect=AssertionError("目录校验不应扫描题目")):
            (Path(td) / "高考卷" / "2026").mkdir(parents=True)
            row = filestore.get_collection("高考卷/2026")

        self.assertEqual(row["name"], "2026")
        self.assertEqual(row["parent_id"], "高考卷")

    def test_question_metadata_write_keeps_directory_tree_cache(self):
        """题卡按钮不改变目录结构，不能让下一页重走整棵目录树。"""
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            (Path(td) / "高考卷" / "2026" / "测试卷").mkdir(parents=True)
            qid = filestore.create_question(
                "带图填空题\n\n![[x.png]]", qtype="填空题",
                folder="高考卷/2026/测试卷")
            filestore.collection_records_snapshot("高考卷/2026/测试卷")
            filestore.invalidate_scan_cache(folder_structure=True)
            real_iterdir = Path.iterdir
            calls = []

            def counted_iterdir(path):
                calls.append(path)
                return real_iterdir(path)

            with mock.patch.object(Path, "iterdir", counted_iterdir):
                directory_tree = filestore.list_collections_tree([])
                first_calls = len(calls)
                filestore.set_img_split(qid, "opts")
                self.assertIs(filestore.list_collections_tree([]), directory_tree)
                self.assertEqual(len(calls), first_calls)
                filestore.create_collection("2025", "高考卷")
                filestore.list_collections_tree([])

        self.assertGreater(len(calls), first_calls)

    def test_directory_only_tree_does_not_poison_counted_tree(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            (Path(td) / "高考卷" / "2026").mkdir(parents=True)
            filestore.invalidate_scan_cache(folder_structure=True)
            directory_tree = filestore.list_collections_tree([])
            counted_tree = filestore.list_collections_tree([
                {"folder": "高考卷/2026"},
            ])

        self.assertEqual(directory_tree[0]["cnt"], 0)
        self.assertEqual(counted_tree[0]["cnt"], 1)
        self.assertEqual(counted_tree[0]["children"][0]["cnt"], 1)

    def test_plain_search_includes_question_source(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache(folder_structure=True)
            expected = filestore.create_question(
                "普通题干", source="2026 北京期中考试", folder="甲卷")
            filestore.create_question(
                "另一题", source="上海月考", folder="甲卷")

            rows = filestore.list_questions(search="北京期中")

        self.assertEqual([row["id"] for row in rows], [expected])

    def test_structured_search_intersects_existing_question_scope(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache(folder_structure=True)
            expected = filestore.create_question(
                "二次函数压轴题", solution="配方法", qtype="解答题",
                source="北京期中", difficulty="3", tags=["函数"], folder="甲卷")
            filestore.set_starred_many([expected], True)
            filestore.create_question(
                "二次函数基础题", solution="配方法", qtype="解答题",
                source="北京期中", difficulty="2", tags=["函数"], folder="甲卷")
            filestore.create_question(
                "二次函数压轴题", solution="配方法", qtype="解答题",
                source="北京期中", difficulty="3", tags=["函数"], folder="乙卷")

            rows = filestore.list_questions(
                tags=["函数"], qtype="解答题", difficulty="3", starred=True,
                collection="甲卷",
                search="content:压轴 source:北京 starred:true")

        self.assertEqual([row["id"] for row in rows], [expected])

    def test_relative_reorder_writes_only_dragged_card_with_midpoint(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache(folder_structure=True)
            ids = filestore.create_questions_batch([
                {"body": "第一题"}, {"body": "第二题"},
                {"body": "第三题"}, {"body": "第四题"},
            ], "甲卷")
            other = filestore.create_question("其它题", folder="乙卷")
            original_write = filestore._write_raw
            with mock.patch.object(
                    filestore, "_all_records",
                    side_effect=AssertionError("局部排序不应扫描整座题库")), \
                    mock.patch.object(
                        filestore, "_write_raw", wraps=original_write) as write:
                result = filestore.reorder_relative(
                    ids[3], "甲卷", ids[1], "before")

            rows = filestore.list_questions(records=
                filestore.collection_records_snapshot(
                    "甲卷", recursive=False))
            other_row = filestore.get_question(other)

        self.assertTrue(result["changed"])
        self.assertFalse(result["normalized"])
        self.assertEqual(result["order"], 1.5)
        self.assertEqual(write.call_count, 1)
        self.assertEqual([row["id"] for row in rows],
                         [ids[0], ids[3], ids[1], ids[2]])
        self.assertEqual(other_row["order"], 1.0)

    def test_relative_reorder_normalizes_only_target_when_gap_is_exhausted(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache(folder_structure=True)
            ids = filestore.create_questions_batch([
                {"body": "第一题"}, {"body": "第二题"},
                {"body": "第三题"},
            ], "甲卷")
            for qid, order in zip(ids, (1.0, 1.0 + 5e-10, 3.0)):
                row = filestore.get_question(qid)
                path = Path(td) / row["path"]
                meta, body = filestore._read_raw(path)
                meta["order"] = order
                filestore._write_raw(path, meta, body)
            filestore._cache.clear()
            filestore.invalidate_scan_cache()
            original_write = filestore._write_raw
            with mock.patch.object(
                    filestore, "_all_records",
                    side_effect=AssertionError("归一化不应扫描其它题集")), \
                    mock.patch.object(
                        filestore, "_write_raw", wraps=original_write) as write:
                result = filestore.reorder_relative(
                    ids[2], "甲卷", ids[1], "before")

            rows = filestore.list_questions(records=
                filestore.collection_records_snapshot(
                    "甲卷", recursive=False))
            written_parents = {
                Path(call.args[0]).parent.resolve()
                for call in write.call_args_list
            }

        self.assertTrue(result["normalized"])
        self.assertEqual([row["id"] for row in rows],
                         [ids[0], ids[2], ids[1]])
        self.assertEqual([row["order"] for row in rows], [1.0, 2.0, 3.0])
        self.assertEqual(written_parents, {(Path(td) / "甲卷").resolve()})

    def test_relative_reorder_rejects_parent_and_non_direct_anchor(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            filestore._cache.clear()
            filestore.invalidate_scan_cache(folder_structure=True)
            parent_q = filestore.create_question("父级题", folder="父级")
            parent_anchor = filestore.create_question("父级锚点", folder="父级")
            filestore.create_question("子级题", folder="父级/子级")
            leaf_q = filestore.create_question("叶子题", folder="叶子")
            other_anchor = filestore.create_question("其它锚点", folder="其它")
            with mock.patch.object(filestore, "_write_raw") as write:
                with self.assertRaisesRegex(ValueError, "叶子题集"):
                    filestore.reorder_relative(
                        parent_q, "父级", parent_anchor, "after")
                with self.assertRaisesRegex(ValueError, "必须直属"):
                    filestore.reorder_relative(
                        leaf_q, "叶子", other_anchor, "after")

        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
