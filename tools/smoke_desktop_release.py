"""发行前验证随包 Pandoc 能生成可交给 Overleaf 的源码包。"""

from pathlib import Path
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import handouts
import service_ports


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="quizforge-release-smoke-") as td:
        config.OUTPUT_DIR = Path(td) / "output"
        result = service_ports.export_document(
            [{
                "id": "release-smoke",
                "body": "已知 $1+1=2$，请写出验证过程。",
                "type": "解答题",
                "solution": "等式两边相等。",
                "difficulty": "1",
            }],
            title="QuizForge 发行验证",
            fmt="zip",
            mode="list",
            solution_mode="separate",
        )
        if not result.is_file():
            raise RuntimeError("源码包未生成")
        with zipfile.ZipFile(result) as archive:
            names = archive.namelist()
            if not any(name.endswith(".tex") for name in names):
                raise RuntimeError("源码包中没有 .tex")
        block_id = handouts.new_block_id()
        metadata = handouts._default_metadata("QuizForge 讲义发行验证", columns=2)
        metadata["solution_default"] = "appendix"
        metadata["question_blocks"][block_id] = {
            "question_type": "解答题",
            "number_override": "例A",
        }
        handout_zip = service_ports.export_handout_document(
            metadata,
            "# 函数讲义\n\n" + handouts.question_marker(
                block_id,
                "已知 $f(x)=x^2$，求 $f'(x)$。",
                "$f'(x)=2x$。",
            ),
            fmt="zip",
        )
        with zipfile.ZipFile(handout_zip) as archive:
            handout_names = archive.namelist()
            tex_name = next((name for name in handout_names if name.endswith(".tex")), None)
            if tex_name is None:
                raise RuntimeError("讲义源码包中没有 .tex")
            tex = archive.read(tex_name).decode("utf-8")
            if "例A" not in tex or "参考解析" not in tex:
                raise RuntimeError("讲义源码包缺少自定义题号或文末解析")
        print(f"[OK] bundled pandoc: {config.PANDOC}")
        print(f"[OK] tex.zip entries: {', '.join(names)}")
        print(f"[OK] handout tex.zip entries: {', '.join(handout_names)}")


if __name__ == "__main__":
    main()
