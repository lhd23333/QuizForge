"""文件题库批量建题的顺序与扫描次数回归。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import filestore


class FilestoreBatchCreateTests(unittest.TestCase):
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

    def test_navigation_tree_only_expands_active_path(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            (Path(td) / "高考卷" / "2026" / "全国卷").mkdir(parents=True)
            (Path(td) / "高考卷" / "2025" / "北京卷").mkdir(parents=True)

            tree = filestore.list_navigation_tree("高考卷/2026")

        exam = tree[0]
        years = {row["name"]: row for row in exam["children"]}
        self.assertTrue(exam["children_loaded"])
        self.assertTrue(years["2026"]["children_loaded"])
        self.assertEqual([row["name"] for row in years["2026"]["children"]],
                         ["全国卷"])
        self.assertFalse(years["2025"]["children_loaded"])
        self.assertEqual(years["2025"]["children"], [])
        self.assertTrue(years["2025"]["has_children"])

    def test_navigation_tree_preloads_one_visible_hierarchy_level(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "BANK_DIR", Path(td)):
            (Path(td) / "高考卷" / "2026" / "全国卷").mkdir(parents=True)
            (Path(td) / "练习册" / "函数").mkdir(parents=True)

            tree = filestore.list_navigation_tree("")

        self.assertEqual([row["name"] for row in tree], ["练习册", "高考卷"])
        for row in tree:
            self.assertTrue(row["children_loaded"])
            self.assertEqual(len(row["children"]), 1)
            self.assertTrue(row["has_children"])
            self.assertFalse(row["children"][0]["children_loaded"])

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


if __name__ == "__main__":
    unittest.main()
