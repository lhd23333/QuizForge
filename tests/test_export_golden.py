"""导出 golden 基线：8 个 mode × 3 个解析行为的 build_markdown 输出快照。

用途（2026-10-05 导出抽屉重构 P0）：
  重构引入「版式/分类/页面/比例」四维度组合，但硬约束是**现有 8 mode 的输出
  必须逐字节不变**。本测试把重构前 8 mode 的 build_markdown 全量输出冻结为
  fixtures/export_golden.json；之后每次改动逐字符比对——任何差异都必须单独
  解释，防止「顺手改坏旧路径」这种看不见的回归。
  为什么不用断言内嵌：24 份快照总量在数十 KB 量级，内嵌会让测试文件不可读；
  外部 JSON 同时便于人工抽查与 diff。

更新快照：确需变更旧行为时（属重大决策，提交信息里必须说明），在仓库根用
  QF_UPDATE_EXPORT_GOLDEN=1 重新运行本测试生成，再跑默认模式验证。
  默认只读，绝不静默改写。
"""

import json
import os
import unittest
from pathlib import Path

import exporter

GOLDEN_PATH = Path(__file__).with_name("fixtures") / "export_golden.json"
UPDATE = os.environ.get("QF_UPDATE_EXPORT_GOLDEN") == "1"

# 固定输入（与快照配套）：一旦要改这里的任何值，快照必须连带更新。
TITLE = "题_集 A"
KEYPOINTS = "一、函数单调性\n二、数形结合与分类讨论"
FULLPAGE_IDS = ("g4",)
STD_OPTS = {
    "subject": "数学",
    "info_bar": True,
    "secret_notice": "本试卷共 8 题，满分 100 分。",
    "exam_notes": "考试时间 120 分钟。",
    "section_points": {"single": "5", "multi": "5", "blank": "5", "solve": "10"},
}


def _fixture_questions():
    """8 题覆盖四题型 × 有/无解析 × 图片字段，纯 dict（不触磁盘）。

    图片引用 ![[...]] 不经过 _stage_images（那一步在 export() 里、需要真实文件），
    build_markdown 层只冻结「无暂存文件名列表」时对图片引用的文本处理。
    """
    return [
        {"id": "g1", "type": "单选题",
         "body": "1. 已知集合 $A=\\{1,2\\}$，则 $A$ 的子集个数为（  ）\n"
                 "A. 2\nB. 3\nC. 4\nD. 5"},
        {"id": "g2", "type": "多选题",
         "body": "2. 下列说法正确的是（  ）\nA. 甲\nB. 乙\nC. 丙\nD. 丁",
         "solution": "【解析】由定义逐项判断，甲、丙正确。"},
        {"id": "g3", "type": "填空题",
         "body": "3. 若 $x^{2}=4$，则 $x=$ ______。",
         "solution": "【解析】由平方根定义得 $x=\\pm 2$。"},
        {"id": "g4", "type": "解答题",
         "body": "4. 设函数 $f(x)=x^{2}+2x$。\n"
                 "(1) 求 $f(x)$ 的最小值；\n(2) 若 $f(x)=3$，求 $x$ 的值。",
         "solution": "【解析】(1) $f(x)=(x+1)^{2}-1$，最小值为 $-1$。\n"
                     "(2) 解方程得 $x=1$ 或 $x=-3$。"},
        {"id": "g5", "type": "解答题",
         "body": "5. 在 $\\triangle ABC$ 中，角 $A,B,C$ 的对边分别为 $a,b,c$，"
                 "且 $a=2$，$b=3$，$C=\\frac{\\pi}{3}$。\n"
                 "(1) 求 $c$ 的长；\n(2) 求 $\\triangle ABC$ 的面积。"},
        {"id": "g6", "type": "单选题",
         "body": "6. 如图，$AB$ 为 $\\odot O$ 的直径，则 $\\angle ACB=$（  ）\n\n"
                 "![[geom-circle.png]]\n\n"
                 "A. $30^\\circ$\nB. $45^\\circ$\nC. $60^\\circ$\nD. $90^\\circ$",
         "img_width": 0.4},
        {"id": "g7", "type": "填空题",
         "body": "7. 计算 $\\int_{0}^{1} x\\,\\mathrm{d}x=$ ______。",
         "solution": "【解析】原式 $=\\frac{1}{2}$。\n\n![[sol-step.png]]",
         "sol_img_split": "full"},
        {"id": "g8", "type": "单选题",
         "body": "8. 下列图形中，是中心对称图形的是（  ）\n"
                 "A. ![[opt-a.png]]\nB. ![[opt-b.png]]\n"
                 "C. ![[opt-c.png]]\nD. ![[opt-d.png]]",
         "img_split": "opts"},
    ]


def _first_diff(a: str, b: str) -> tuple:
    """返回 (首个不同位置, a 侧片段, b 侧片段)，用于失败时精确定位。"""
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i]:
            lo = max(0, i - 40)
            return i, a[lo:i + 60], b[lo:i + 60]
    if len(a) != len(b):
        lo = max(0, limit - 40)
        return limit, a[lo:limit + 60], b[lo:limit + 60]
    return -1, "", ""


class ExportGoldenTests(unittest.TestCase):
    """基线：重构期间 8 mode 的输出必须逐字节冻结。"""

    def _render_all(self) -> dict:
        questions = _fixture_questions()
        rendered = {}
        for mode in sorted(exporter.SUPPORTED_MODES):
            for sol in ("none", "inline", "separate"):
                md = exporter.build_markdown(
                    questions, TITLE, mode=mode,
                    keypoints=KEYPOINTS, fullpage_ids=set(FULLPAGE_IDS),
                    solution_mode=sol, std_opts=dict(STD_OPTS))
                rendered[f"{mode}__{sol}"] = md
        return rendered

    def test_golden_matches(self):
        rendered = self._render_all()

        if UPDATE:
            GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(GOLDEN_PATH, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(rendered, fh, ensure_ascii=False, indent=1,
                          sort_keys=True)
            self.skipTest(f"已更新 golden 快照（{len(rendered)} 份）：{GOLDEN_PATH}")

        self.assertTrue(GOLDEN_PATH.is_file(),
                        f"缺少 golden 快照文件 {GOLDEN_PATH}；"
                        "首次生成请设 QF_UPDATE_EXPORT_GOLDEN=1 运行本测试")
        golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

        self.assertEqual(set(golden), set(rendered),
                         "快照键集合与当前 SUPPORTED_MODES×解析行为不一致")
        for key in sorted(golden):
            expected, actual = golden[key], rendered[key]
            if expected == actual:
                continue
            pos, a_frag, b_frag = _first_diff(expected, actual)
            self.fail(
                f"{key} 的 build_markdown 输出与 golden 基线不一致"
                f"（首处差异 @ {pos}）。\n"
                f"  基线: …{a_frag!r}…\n"
                f"  当前: …{b_frag!r}…\n"
                "若这是有意的旧行为变更，必须在提交信息中说明理由，并用 "
                "QF_UPDATE_EXPORT_GOLDEN=1 更新快照；若是无意改动，请修复。")


if __name__ == "__main__":
    unittest.main()
