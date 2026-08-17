"""独立桌面产品的首次启动、演示题库与环境诊断。"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import config
import filestore
import service_ports
import mineru_store
import doc2x_store


PRODUCT_VERSION = "1.0.0"
DEMO_FOLDER = "QuizForge 示例题库"


_DEMO_QUESTIONS = [
    {
        "body": "已知函数 $f(x)=x^2-2x+3$，则 $f(2)=$（　　）\n\nA. $1$\nB. $2$\nC. $3$\nD. $4$",
        "solution": "代入得 $f(2)=2^2-2\\times2+3=3$，故选 C。",
        "type": "单选题",
        "source": "QuizForge 内置原创示例",
        "difficulty": "1",
        "tags": ["示例", "函数"],
        "number": 1,
    },
    {
        "body": "曲线 $y=x^3-3x$ 在 $x=1$ 处的切线斜率为 ______。",
        "solution": "$y'=3x^2-3$，所以 $y'|_{x=1}=0$。",
        "type": "填空题",
        "source": "QuizForge 内置原创示例",
        "difficulty": "2",
        "tags": ["示例", "导数"],
        "number": 2,
    },
    {
        "body": "在等差数列 $\\{a_n\\}$ 中，$a_1=2$，公差 $d=3$。\n\n（1）求 $a_{10}$；\n\n（2）求前 $10$ 项和 $S_{10}$。",
        "solution": "（1）$a_{10}=a_1+9d=29$。\n\n（2）$S_{10}=\\dfrac{10(a_1+a_{10})}{2}=155$。",
        "type": "解答题",
        "source": "QuizForge 内置原创示例",
        "difficulty": "2",
        "tags": ["示例", "数列"],
        "number": 3,
    },
]


def seed_demo_bank(bank_dir: Path) -> bool:
    """仅为空题库写入三道原创示例；已有任意 Markdown 时保持原目录不动。"""
    bank_dir = bank_dir.resolve()
    if bank_dir != config.BANK_DIR.resolve():
        raise ValueError("演示题库目标与当前题库目录不一致")
    bank_dir.mkdir(parents=True, exist_ok=True)
    if next(bank_dir.rglob("*.md"), None) is not None:
        return False
    folder = filestore.get_or_create_collection(DEMO_FOLDER)
    filestore.create_questions_batch(_DEMO_QUESTIONS, folder)
    return True


def _resolve_executable(configured: str) -> Path | None:
    value = str(configured or "").strip()
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute() or candidate.parent != Path("."):
        try:
            return candidate.resolve() if candidate.is_file() else None
        except OSError:
            return None
    found = shutil.which(value)
    return Path(found).resolve() if found else None


def _path_check(name: str, path: Path) -> dict[str, str]:
    try:
        exists = path.is_dir()
        writable = exists and os.access(path, os.W_OK)
    except OSError:
        exists = writable = False
    if writable:
        return {"name": name, "status": "ok", "summary": "可用", "detail": str(path)}
    return {
        "name": name,
        "status": "error",
        "summary": "不可写" if exists else "不存在",
        "detail": str(path),
    }


def _tool_check(name: str, configured: str, missing: str,
                required: bool = True) -> dict[str, str]:
    executable = _resolve_executable(configured)
    if executable is None:
        return {
            "name": name,
            "status": "error" if required else "warn",
            "summary": "未找到",
            "detail": missing,
        }
    runtime_root = (config.BASE_DIR / "runtime").resolve()
    try:
        bundled = executable.is_relative_to(runtime_root)
    except (OSError, ValueError):
        bundled = False
    return {
        "name": name,
        "status": "ok",
        "summary": "随软件附带" if bundled else "使用本机安装",
        "detail": str(executable),
    }


def environment_report() -> dict[str, object]:
    """返回关于页所需的只读诊断，不发网络请求、不运行外部转换。"""
    checks = [
        _path_check("题库目录", config.BANK_DIR),
        _path_check("运行数据目录", config.DATA_DIR),
        _tool_check(
            "Pandoc", config.PANDOC,
            "安装包损坏或不完整；重新安装后才能导出 TeX、ZIP 和 PDF。",
        ),
        _tool_check(
            "XeLaTeX", config.XELATEX,
            "仍可导出 tex.zip 上传 Overleaf；本机直接生成 PDF 需安装 MiKTeX。",
            required=False,
        ),
    ]
    checks.insert(0, {
        "name": "运行模式",
        "status": "ok",
        "summary": "开源本地",
        "detail": "核心功能直接使用；联网仅在用户主动检查更新时使用。",
    })
    checks.append({
        "name": "MinerU Token",
        "status": "ok" if mineru_store.has_token() else "warn",
        "summary": "已配置" if mineru_store.has_token() else "未配置",
        "detail": "未配置时仍可手动导入 Markdown；需要 OCR 时在设置页添加自己的 Token。",
    })
    checks.append({
        "name": "Doc2X API Key",
        "status": "ok" if doc2x_store.has_key() else "warn",
        "summary": "已配置" if doc2x_store.has_key() else "未配置",
        "detail": "多份 Key 会按忙闲和轮转使用；未配置时不影响题库编辑和本地导出。",
    })
    try:
        free_gib = shutil.disk_usage(config.DATA_DIR).free / (1024 ** 3)
        checks.append({
            "name": "数据盘剩余空间",
            "status": "ok" if free_gib >= 2 else "warn",
            "summary": f"{free_gib:.1f} GiB",
            "detail": "建议至少保留 2 GiB 供上传件、识别中间产物和导出文件使用。",
        })
    except OSError as exc:
        checks.append({
            "name": "数据盘剩余空间", "status": "warn", "summary": "无法读取",
            "detail": str(exc),
        })
    services = service_ports.status()
    return {
        "version": PRODUCT_VERSION,
        "desktop": os.environ.get("QUIZFORGE_DESKTOP") == "1",
        "frozen": bool(getattr(sys, "frozen", False)),
        "bank_dir": str(config.BANK_DIR),
        "data_dir": str(config.DATA_DIR),
        "log_dir": str(config.DATA_DIR / "logs"),
        "checks": checks,
        "ready": all(row["status"] != "error" for row in checks),
        "services": services,
    }
