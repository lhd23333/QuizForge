"""QuizForge 自定义 Pandoc/TeX 模板契约、迁移与真实预览。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import tex_sandbox


CATALOG_SCHEMA = 2
MANIFEST_SCHEMA = 1
CONTRACT = "quizforge-pandoc-v1"
MANIFEST_NAME = "quizforge-template.json"
PREVIEW_DIR = "_preview"
SUPPORTED_MODES = (
    "list", "note", "lecture", "slides", "practice", "exam",
    "exam_std", "handout",
)
DEFAULT_SINGLE_FILE_MODES = ("list",)
_MAX_MANIFEST_BYTES = 64 * 1024

RESOURCE_SUFFIXES = frozenset({
    ".tex", ".sty", ".cls", ".bbx", ".cbx", ".def", ".cfg", ".bib",
    ".png", ".jpg", ".jpeg", ".webp", ".pdf", ".svg", ".eps",
    ".ttf", ".otf", ".txt", ".md", ".json", ".yaml", ".yml",
})

_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_RUNTIME_COMMON = frozenset({
    "qopen", "qclose", "qsubopen", "qsubitem", "qsubclose",
    "qfig", "qfigwrap", "qwrapclear", "qfigflexbox", "qpairitem",
})
_RUNTIME_BY_MODE = {
    "list": frozenset(),
    "note": frozenset({"qslotopen", "qslotclose", "qslotpagerel"}),
    "lecture": frozenset({"qslotopen", "qslotclose", "qslotpagerel"}),
    "handout": frozenset({"qslotopen", "qslotclose", "qslotpagerel"}),
    "exam": frozenset({"qslotopen", "qslotclose", "qslotpagerel"}),
    "exam_std": frozenset({"qslotopen", "qslotclose", "qslotpagerel", "qnotebox"}),
    "practice": frozenset({"qpracticebegin", "qpracticeend"}),
    "slides": frozenset({"qslidecover", "qslidehead"}),
}
_COMMAND_DEFINITION_RE = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand)\*?\s*\{?\\([A-Za-z@]+)"
    r"|\\(?:long|outer|global|protected)*\s*def\s*\\([A-Za-z@]+)",
    re.I,
)
_ENV_DEFINITION_RE = re.compile(
    r"\\(?:newenvironment|renewenvironment)\*?\s*\{([A-Za-z@]+)\}", re.I)
_NEWIF_RE = re.compile(r"\\newif\s*\\if([A-Za-z@]+)", re.I)

# 1x1 PNG。固定样例不依赖题库图片目录，仍能验证模板的图片链路。
_SAMPLE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
    "YGD4DwABBAEAX+XDSwAAAABJRU5ErkJggg=="
)


class TemplatePipelineError(ValueError):
    """模板包结构、契约或编译结果无效。"""

    def __init__(self, message: str, *, code: str = "invalid_template",
                 status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details or {})


def _safe_relative(raw: Any) -> str | None:
    value = str(raw or "").replace("\\", "/")
    if (not value or "\x00" in value or value.startswith(("/", "//"))
            or re.match(r"^[A-Za-z]:", value)):
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    if any(part.casefold() == PREVIEW_DIR.casefold() for part in path.parts):
        return None
    for part in path.parts:
        if (part.startswith(".") or re.search(r'[<>:"|?*]', part)
                or part.endswith((".", " "))
                or Path(part).stem.upper() in _WINDOWS_RESERVED_NAMES):
            return None
    return path.as_posix()


def _manifest_bytes(entrypoint: str,
                    supported_modes: Iterable[str] = DEFAULT_SINGLE_FILE_MODES) -> bytes:
    payload = {
        "schema": MANIFEST_SCHEMA,
        "contract": CONTRACT,
        "entrypoint": entrypoint,
        "supported_modes": list(supported_modes),
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n").encode("utf-8")


def single_tex_package(filename: str, raw: bytes) -> list[tuple[str, bytes]]:
    """为单 .tex 上传补齐标准 manifest。"""
    safe = _safe_relative(Path(filename).name)
    if not safe or Path(safe).suffix.casefold() != ".tex":
        raise TemplatePipelineError("模板入口必须是 .tex 文件", code="invalid_entrypoint")
    return [(safe, bytes(raw)), (MANIFEST_NAME, _manifest_bytes(safe))]


def _parse_manifest(raw: bytes) -> dict[str, Any]:
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise TemplatePipelineError("模板清单过大", code="manifest_too_large")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TemplatePipelineError("quizforge-template.json 不是有效的 UTF-8 JSON",
                                    code="invalid_manifest") from exc
    if not isinstance(payload, dict):
        raise TemplatePipelineError("模板清单必须是 JSON 对象", code="invalid_manifest")
    if payload.get("schema") != MANIFEST_SCHEMA:
        raise TemplatePipelineError(
            f"模板清单 schema 必须为 {MANIFEST_SCHEMA}", code="unsupported_schema")
    if payload.get("contract") != CONTRACT:
        raise TemplatePipelineError(
            f"模板 contract 必须为 {CONTRACT}", code="unsupported_contract")
    entrypoint = _safe_relative(payload.get("entrypoint"))
    if not entrypoint or Path(entrypoint).suffix.casefold() != ".tex":
        raise TemplatePipelineError("模板入口必须是包内 .tex 相对路径",
                                    code="invalid_entrypoint")
    raw_modes = payload.get("supported_modes")
    if not isinstance(raw_modes, list) or not raw_modes:
        raise TemplatePipelineError("supported_modes 必须是非空数组",
                                    code="invalid_supported_modes")
    modes: list[str] = []
    for raw_mode in raw_modes:
        mode = str(raw_mode or "").strip()
        if mode not in SUPPORTED_MODES:
            raise TemplatePipelineError(f"模板不支持未知导出模式：{mode or '(空)'}",
                                        code="invalid_supported_modes")
        if mode not in modes:
            modes.append(mode)
    return {
        **payload,
        "schema": MANIFEST_SCHEMA,
        "contract": CONTRACT,
        "entrypoint": entrypoint,
        "supported_modes": modes,
    }


def _source_hash(files: Iterable[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, raw in sorted(files, key=lambda item: item[0].casefold()):
        encoded_name = name.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _runtime_definitions(sources: Iterable[str]) -> set[str]:
    text = "\n".join(tex_sandbox.strip_tex_comments(source) for source in sources)
    found: set[str] = set()
    for match in _COMMAND_DEFINITION_RE.finditer(text):
        found.add((match.group(1) or match.group(2) or "").casefold())
    found.update(match.group(1).casefold() for match in _ENV_DEFINITION_RE.finditer(text))
    # \newif\ifqslotpagerel 会生成 qslotpagereltrue/false，导出器正是调用这两个命令。
    found.update(match.group(1).casefold() for match in _NEWIF_RE.finditer(text))
    return found


def inspect_files(files: Iterable[tuple[str, bytes]]) -> dict[str, Any]:
    """验证尚未落盘的可执行模板包并返回规范化契约信息。"""
    entries: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for raw_name, raw in files:
        name = _safe_relative(raw_name)
        if not name:
            raise TemplatePipelineError("模板资源路径无效", code="path_traversal")
        key = name.casefold()
        if key in seen:
            raise TemplatePipelineError(f"模板包存在重复文件：{name}",
                                        code="duplicate_member")
        seen.add(key)
        if Path(name).suffix.casefold() not in RESOURCE_SUFFIXES:
            raise TemplatePipelineError(f"模板资源类型不受支持：{name}",
                                        code="unsupported_file")
        entries.append((name, bytes(raw)))
    manifests = [raw for name, raw in entries if name == MANIFEST_NAME]
    if len(manifests) != 1:
        raise TemplatePipelineError(
            "TeX ZIP 根目录必须且只能包含一个 quizforge-template.json",
            code="missing_manifest" if not manifests else "duplicate_manifest",
        )
    manifest = _parse_manifest(manifests[0])
    by_name = {name.casefold(): (name, raw) for name, raw in entries}
    entry = by_name.get(manifest["entrypoint"].casefold())
    if entry is None:
        raise TemplatePipelineError("模板清单指定的入口文件不存在",
                                    code="missing_entrypoint")
    try:
        entry_text = entry[1].decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TemplatePipelineError("TeX 模板必须是 UTF-8 文本",
                                    code="invalid_encoding") from exc
    body_count = entry_text.count("$body$")
    if body_count != 1:
        raise TemplatePipelineError(
            f"模板入口必须包含且仅包含一个 $body$（当前 {body_count} 个）",
            code="invalid_body_placeholder",
        )
    if ("\\documentclass" not in entry_text
            or "\\begin{document}" not in entry_text
            or "\\end{document}" not in entry_text):
        raise TemplatePipelineError(
            "模板入口必须包含 documentclass 和完整 document 环境",
            code="invalid_document",
        )

    tex_sources: list[str] = []
    package_names = [name for name, _raw in entries]
    for name, raw in entries:
        if Path(name).suffix.casefold() not in tex_sandbox.TEXT_RESOURCE_SUFFIXES:
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise TemplatePipelineError(f"模板资源必须是 UTF-8 文本：{name}",
                                        code="invalid_encoding") from exc
        try:
            tex_sandbox.validate_tex_text(text, source_name=name,
                                          package_files=package_names)
        except tex_sandbox.TexSandboxError as exc:
            raise TemplatePipelineError(str(exc), code=exc.code) from exc
        tex_sources.append(text)

    required = set(_RUNTIME_COMMON)
    for mode in manifest["supported_modes"]:
        required.update(_RUNTIME_BY_MODE.get(mode, ()))
    definitions = _runtime_definitions(tex_sources)
    missing = sorted(name for name in required if name.casefold() not in definitions)
    if missing:
        raise TemplatePipelineError(
            "模板缺少 QuizForge 运行时宏：" + "、".join(f"\\{name}" for name in missing),
            code="missing_runtime_macros",
        )
    return {
        "manifest": manifest,
        "entrypoint": manifest["entrypoint"],
        "supported_modes": list(manifest["supported_modes"]),
        "source_hash": _source_hash(entries),
        "files": [name for name, _raw in entries],
        "fields": _extract_fields(entry_text),
    }


def _extract_fields(tex: str) -> list[str]:
    found: set[str] = set()
    for pattern in (
        r"\$([A-Za-z_][A-Za-z0-9_.-]*)\$",
        r"\$if\(\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\)\$",
        r"\$for\(\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\)\$",
    ):
        found.update(re.findall(pattern, tex))
    found.difference_update({"body", "if", "endif", "for", "endfor", "else", "sep"})
    return sorted(found)


def directory_files(root: Path) -> list[tuple[str, bytes]]:
    package_root = Path(root).resolve(strict=True)
    entries: list[tuple[str, bytes]] = []
    for item in sorted(package_root.rglob("*")):
        rel_path = item.relative_to(package_root)
        if PREVIEW_DIR in rel_path.parts:
            continue
        if item.is_symlink():
            raise TemplatePipelineError("模板包不能包含符号链接", code="symlink_file")
        if not item.is_file():
            continue
        rel = rel_path.as_posix()
        if not _safe_relative(rel):
            raise TemplatePipelineError("模板资源路径无效", code="path_traversal")
        if item.suffix.casefold() not in RESOURCE_SUFFIXES:
            raise TemplatePipelineError(f"模板资源类型不受支持：{rel}",
                                        code="unsupported_file")
        try:
            entries.append((rel, item.read_bytes()))
        except OSError as exc:
            raise TemplatePipelineError(f"模板资源无法读取：{rel}",
                                        code="source_not_found", status=404) from exc
    return entries


def inspect_directory(root: Path) -> dict[str, Any]:
    package_root = Path(root).resolve(strict=True)
    info = inspect_files(directory_files(package_root))
    entrypoint = (package_root / PurePosixPath(info["entrypoint"])).resolve(strict=True)
    if package_root not in entrypoint.parents:
        raise TemplatePipelineError("模板入口路径越界", code="path_traversal")
    try:
        tex_sandbox.validate_tex_package(package_root, entrypoint=entrypoint)
    except tex_sandbox.TexSandboxError as exc:
        raise TemplatePipelineError(str(exc), code=exc.code) from exc
    return {**info, "root": package_root, "entrypoint_path": entrypoint}


def package_root(entrypoint: Path) -> Path:
    current = Path(entrypoint).resolve(strict=True).parent
    for _ in range(16):
        if (current / MANIFEST_NAME).is_file():
            return current
        if current.parent == current:
            break
        current = current.parent
    raise TemplatePipelineError("模板包缺少 quizforge-template.json",
                                code="missing_manifest")


def ensure_legacy_manifest(root: Path, row: dict[str, Any]) -> bool:
    """为 schema v1 的旧 TeX 目录补清单；不删除或改写任何旧资源。"""
    directory = Path(root).resolve(strict=True)
    manifest_path = directory / MANIFEST_NAME
    if manifest_path.exists():
        return False
    tex_files = [item for item in sorted(directory.rglob("*.tex"))
                 if item.is_file() and not item.is_symlink()
                 and PREVIEW_DIR not in item.relative_to(directory).parts]
    if not tex_files:
        raise TemplatePipelineError("旧模板目录中没有可迁移的 .tex 文件",
                                    code="missing_entrypoint")
    source_file = Path(str(row.get("source_file") or "")).name.casefold()
    chosen = next((item for item in tex_files if item.name.casefold() == source_file),
                  tex_files[0])
    rel = chosen.relative_to(directory).as_posix()
    tmp = manifest_path.with_name(f".{MANIFEST_NAME}.{secrets.token_hex(6)}.tmp")
    try:
        with tmp.open("xb") as handle:
            handle.write(_manifest_bytes(rel))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, manifest_path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return True


def ensure_mode(info: dict[str, Any], mode: str) -> None:
    if mode not in info.get("supported_modes", []):
        raise TemplatePipelineError(
            f"该模板不支持 {mode} 导出模式",
            code="template_mode_unsupported", status=409,
        )


def copy_package(source_root: Path, destination: Path) -> Path:
    """按白名单把模板包复制到独占工作区，返回复制后的入口路径。"""
    info = inspect_directory(source_root)
    target_root = Path(destination).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    for rel, raw in directory_files(info["root"]):
        target = (target_root / PurePosixPath(rel)).resolve()
        if target != target_root and target_root not in target.parents:
            raise TemplatePipelineError("模板资源路径越界", code="path_traversal")
        if target.exists() or target.is_symlink():
            raise TemplatePipelineError(f"模板资源与导出工作文件重名：{rel}",
                                        code="resource_collision")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return (target_root / PurePosixPath(info["entrypoint"])).resolve(strict=True)


def _sample_questions(image_name: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "quizforge-template-sample-choice",
            "type": "单选题",
            "body": (
                "固定中文样例：设 $f(x)=x^2+1$，则 $f(2)$ 等于（　　）\n\n"
                "A. $3$　B. $4$　C. $5$　D. $6$\n\n"
                "| 符号 | 数值 |\n|---|---:|\n| $f(2)$ | $5$ |\n\n"
                "QFIGSLOT0"
            ),
            "solution": "【解析】代入 $x=2$，得到 $f(2)=2^2+1=5$。",
            "_img_files": [image_name],
        },
        {
            "id": "quizforge-template-sample-solve",
            "type": "解答题",
            "body": "已知 $a+b=3$。\n\n（1）求 $(a+b)^2$；\n\n（2）说明计算过程。",
            "solution": "【解析】（1）结果为 $9$；（2）使用完全平方公式。",
        },
    ]


def compile_preview(root: Path) -> dict[str, Any]:
    """用固定中文、公式、表格、图片和解析样例真实编译声明的全部模式。"""
    info = inspect_directory(root)
    # 先探测两个工具，使“缺工具”与“模板编译失败”拥有稳定、可读的状态。
    tex_sandbox.pandoc_path()
    tex_sandbox.xelatex_path()
    modes: dict[str, dict[str, Any]] = {}
    preview_bytes = b""
    preview_mode = info["supported_modes"][0]
    with tempfile.TemporaryDirectory(prefix="quizforge-template-") as raw_work:
        work = Path(raw_work).resolve()
        entrypoint = copy_package(info["root"], work)
        token = secrets.token_hex(10)
        image_name = f"quizforge-validation-{token}.png"
        (work / image_name).write_bytes(_SAMPLE_PNG)

        # 延迟导入，避免 exporter -> template_pipeline 的正常导出接线形成循环导入。
        import exporter

        for mode in info["supported_modes"]:
            markdown = work / f"validation-{token}-{mode}.md"
            output_tex = work / f"validation-{token}-{mode}.tex"
            questions = _sample_questions(image_name)
            # 固定样例直接使用已在独占工作区内的图片，因此只需补齐正式导出在
            # _stage_images 中执行的表格 staging；否则 Pandoc 会生成不能进入
            # minipage/multicols 的 longtable，验证链路反而偏离真实导出。
            for question in questions:
                question["body"] = exporter._stash_tables(question.get("body", ""))
                question["solution"] = exporter._stash_tables(
                    question.get("solution", "")
                )
            markdown.write_text(
                exporter.build_markdown(
                    questions, "QuizForge 模板固定样例",
                    mode=mode, solution_mode="separate",
                    std_opts={
                        "subject": "数学", "info_bar": True,
                        "secret_notice": "模板验证样例",
                        "exam_notes": "请检查中文、公式、表格、图片和解析。",
                        "section_points": {
                            "single": "5", "multi": "5", "blank": "5", "solve": "12",
                        },
                    },
                    bank_subject="math",
                ),
                encoding="utf-8",
            )
            variables = []
            if mode == "slides":
                variables.append("slides=1")
            elif mode == "practice":
                variables.append("practice=1")
            try:
                tex_sandbox.run_pandoc(markdown, output_tex, entrypoint,
                                       variables=variables, timeout=60)
                pdf = tex_sandbox.compile_xelatex(output_tex, passes=1, timeout=60)
            except tex_sandbox.TexSandboxError as exc:
                modes[mode] = {"status": "failed", "message": str(exc)}
                raise TemplatePipelineError(
                    f"{mode} 模式真实编译失败：{exc}", code=exc.code,
                    details={"modes": modes, "failed_mode": mode},
                ) from exc
            payload = pdf.read_bytes()
            modes[mode] = {"status": "passed", "pdf_bytes": len(payload)}
            if mode == preview_mode:
                preview_bytes = payload
    if not preview_bytes:
        raise TemplatePipelineError("模板预览未生成 PDF", code="missing_output")
    return {
        "status": "valid",
        "source_hash": info["source_hash"],
        "preview_mode": preview_mode,
        "preview_pdf": preview_bytes,
        "modes": modes,
        "validated_at": round(time.time(), 3),
    }


def write_preview(root: Path, preview: dict[str, Any]) -> Path:
    directory = Path(root).resolve(strict=True) / PREVIEW_DIR
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "preview.pdf"
    tmp = directory / f".preview.{secrets.token_hex(8)}.tmp"
    raw = bytes(preview.get("preview_pdf") or b"")
    if not raw.startswith(b"%PDF-"):
        raise TemplatePipelineError("模板预览不是有效 PDF", code="invalid_preview")
    try:
        with tmp.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return target


def preview_path(root: Path) -> Path:
    target = Path(root).resolve(strict=True) / PREVIEW_DIR / "preview.pdf"
    if target.is_symlink() or not target.is_file():
        raise TemplatePipelineError("模板尚未生成真实预览", code="preview_not_found",
                                    status=404)
    if not target.read_bytes()[:5] == b"%PDF-":
        raise TemplatePipelineError("模板预览文件无效", code="invalid_preview",
                                    status=404)
    return target


def migrate_catalog(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """把旧目录无损提升到 schema v2；旧可执行模板必须重新真实验证。"""
    changed = int(data.get("version", 1) or 1) < CATALOG_SCHEMA
    rows = data.get("templates") if isinstance(data.get("templates"), list) else []
    active = str(data.get("active_id") or "")
    for row in rows:
        if not isinstance(row, dict) or row.get("schema_version") == CATALOG_SCHEMA:
            continue
        changed = True
        fmt = str(row.get("format") or "tex").casefold()
        reference_only = fmt == "pdf"
        row["schema_version"] = CATALOG_SCHEMA
        row["reference_only"] = reference_only
        row["executable"] = not reference_only and bool(row.get("source_file"))
        row["manifest"] = dict(row.get("manifest") or {})
        row["entrypoint"] = str(row.get("entrypoint") or "")
        row["supported_modes"] = list(row.get("supported_modes") or [])
        row["source_hash"] = str(row.get("source_hash") or "")
        row["validation_hash"] = ""
        row["validation"] = {
            "status": "reference_only" if reference_only else "pending",
            "message": (
                "PDF 仅作为版式参考，不能进入导出编译。" if reference_only
                else "旧模板已保留，需按 schema v2 重新校验并真实编译。"
            ),
            "modes": {},
        }
        row["status"] = "reference_only" if reference_only else "pending"
        row["enabled"] = False
        row["selected"] = False
        preview = dict(row.get("preview") or {})
        preview.update({"status": "reference_only" if reference_only else "pending",
                        "rendered": reference_only})
        row["preview"] = preview
        if str(row.get("id") or "") == active:
            active = ""
    data["templates"] = rows
    data["active_id"] = active or None
    data["version"] = CATALOG_SCHEMA
    return data, changed


__all__ = [
    "CATALOG_SCHEMA", "MANIFEST_SCHEMA", "CONTRACT", "MANIFEST_NAME",
    "PREVIEW_DIR", "SUPPORTED_MODES", "RESOURCE_SUFFIXES",
    "TemplatePipelineError", "single_tex_package", "inspect_files",
    "directory_files", "inspect_directory", "package_root",
    "ensure_legacy_manifest", "ensure_mode", "copy_package", "compile_preview",
    "write_preview", "preview_path", "migrate_catalog",
]
