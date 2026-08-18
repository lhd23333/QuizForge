"""题卡拖拽 JSON 路由的最小契约回归。"""

import unittest
from unittest import mock

import app as app_module


class QuestionDragRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        self.headers = {"X-CSRF-Token": app_module._WRITE_TOKEN}

    def test_add_one_copy_returns_created_result_contract(self):
        source = {"id": "q1", "folder": "来源"}
        with mock.patch.object(
                app_module.filestore, "get_collection",
                return_value={"id": "目标", "name": "目标"}), \
                mock.patch.object(
                app_module.filestore, "get_question",
                return_value=source), \
                mock.patch.object(
                    app_module.filestore, "copy_to_collection",
                    return_value=["q2"]) as copy, \
                mock.patch.object(
                    app_module.filestore, "move_to_collection") as move:
            response = self.client.post(
                "/collections/目标/add_one", headers=self.headers,
                json={"question_id": "q1", "mode": "copy"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "copy")
        self.assertEqual(payload["created"], ["q2"])
        self.assertEqual(payload["created_id"], "q2")
        self.assertEqual(payload["moved"], [])
        self.assertEqual(payload["source_collection"], "来源")
        self.assertEqual(payload["target_collection"], "目标")
        copy.assert_called_once_with(["q1"], "目标")
        move.assert_not_called()

    def test_add_one_move_reuses_batch_move_and_relative_route_is_separate(self):
        source = {"id": "q1", "folder": "来源"}
        with mock.patch.object(
                app_module.filestore, "get_collection",
                return_value={"id": "目标", "name": "目标"}), \
                mock.patch.object(
                app_module.filestore, "get_question",
                return_value=source), \
                mock.patch.object(
                    app_module.filestore, "move_to_collection",
                    return_value=["q1"]) as move:
            response = self.client.post(
                "/collections/目标/add_one", headers=self.headers,
                json={"question_id": "q1", "mode": "move"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["mode"], "move")
        self.assertEqual(payload["moved"], ["q1"])
        self.assertEqual(payload["created"], [])
        move.assert_called_once_with(["q1"], "目标")

        result = {
            "question_id": "q1", "collection": "目标",
            "anchor_id": "q3", "placement": "before",
            "order": 1.5, "normalized": False, "changed": True,
        }
        with mock.patch.object(
                app_module.filestore, "reorder_relative",
                return_value=result) as reorder:
            response = self.client.post(
                "/reorder/relative", headers=self.headers,
                json={"question_id": "q1", "collection": "目标",
                      "anchor_id": "q3", "placement": "before"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["order"], 1.5)
        reorder.assert_called_once_with("q1", "目标", "q3", "before")

    def test_index_enables_reorder_only_for_unfiltered_existing_leaf(self):
        contexts = []

        def render(_template, **context):
            contexts.append(context)
            return "ok"

        with mock.patch.object(
                app_module.filestore, "list_collection_children",
                return_value=[]), \
                mock.patch.object(
                    app_module.filestore, "get_collection",
                    side_effect=lambda cid: (
                        {"id": cid, "name": cid}
                        if cid == "叶子" else None)), \
                mock.patch.object(
                    app_module.filestore, "collection_records_snapshot",
                    return_value=[]), \
                mock.patch.object(
                    app_module.filestore, "list_navigation_tree",
                    return_value=[]), \
                mock.patch.object(
                    app_module.filestore, "list_papers", return_value=[]), \
                mock.patch.object(
                    app_module.filestore, "count_selected", return_value=0), \
                mock.patch.object(app_module, "render_template", side_effect=render):
            for query in (
                    {"collection": "叶子"},
                    {"collection": "叶子", "all": "1"},
                    {"collection": "叶子", "all": "true"},
                    {"collection": "叶子", "all": "on"},
                    {"collection": "叶子", "recursive": "1"},
                    {"collection": "叶子", "q": "函数"},
                    {"collection": "不存在"}):
                response = self.client.get("/", query_string=query)
                self.assertEqual(response.status_code, 200)

        self.assertTrue(contexts[0]["question_reorder_enabled"])
        self.assertEqual(contexts[0]["question_card_sort"], "custom")
        for context in contexts[1:]:
            self.assertFalse(context["question_reorder_enabled"])
            self.assertEqual(context["question_card_sort"], "browse")


if __name__ == "__main__":
    unittest.main()
