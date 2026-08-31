"""不可信 TeX 模板的静态校验与受限编译。

自定义模板来自用户文件或模型输出，不能沿用内置模板的信任边界。本模块只接受
参数数组形式的命令，关闭 shell escape，限制 TeX 文件读写范围，并在超时后终止
整棵编译进程。危险模式只能跳过产品层确认，不能绕过这里的硬校验。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Iterable

import config


class TexSandboxError(RuntimeError):
    """模板不安全、工具执行失败或编译失败。"""

    def __init__(self, message: str, *, code: str = "tex_sandbox_error"):
        super().__init__(message)
        self.code = code


class TexToolUnavailable(TexSandboxError):
    """本机缺少真实预览所需的外部工具。"""

    def __init__(self, tool: str):
        label = "Pandoc" if tool == "pandoc" else "XeLaTeX"
        super().__init__(
            f"本机尚未安装 {label}，模板可以保存，但完成真实编译前不能启用。",
            code=f"{tool}_unavailable",
        )


TEXT_RESOURCE_SUFFIXES = frozenset({
    ".tex", ".sty", ".cls", ".bbx", ".cbx", ".def", ".cfg", ".bib",
})

# 对模板源文件做可解释的前置拒绝。openin/openout 与 no-shell-escape 是最终硬边界，
# 这里仍显式拒绝明显的文件、进程和 Lua 原语，让用户在上传阶段就能看到具体原因。
_FORBIDDEN_COMMANDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\\(?:immediate\s*)?write\s*18\b", re.I), r"\write18"),
    (re.compile(r"\\(?:openin|openout|read|readline|newread|newwrite)\b", re.I),
     "文件读写原语"),
    (re.compile(r"\\(?:directlua|latelua|luaescapestring|luadirect)\b", re.I),
     "Lua 执行原语"),
    (re.compile(r"\\(?:pdffiledump|pdfmdfivesum|pdffilemoddate|pdfximage|"
                r"XeTeXpdffile|XeTeXpicfile)\b", re.I),
     "底层文件读取原语"),
    (re.compile(r"\\(?:everyjob|everyeof|scantokens)\b", re.I),
     "动态输入原语"),
    (re.compile(r"\\(?:ShellEscape|DelayedShellEscape)\b", re.I),
     "shell escape 原语"),
    (re.compile(r"\\(?:batchmode|errorstopmode|scrollmode)\b", re.I),
     "编译模式改写"),
    (re.compile(r"\\(?:loop|repeat)\b", re.I), "无界循环原语"),
    (re.compile(r"\\(?:usepackage|RequirePackage)(?:\s*\[[^\]]*\])?\s*"
                r"\{[^}]*\b(?:minted|pythontex|sagetex|gnuplottex|shellesc|"
                r"catchfile|catchfilebetweentags)\b[^}]*\}", re.I),
     "可执行外部程序的宏包"),
)
_INPUT_COMMAND_RE = re.compile(r"\\(input|include)\b", re.I)
_LITERAL_INPUT_RE = re.compile(
    r"\\(?:input|include)\s*\{\s*([A-Za-z0-9_./ -]+)\s*\}", re.I)
_PATH_ARGUMENT_RE = re.compile(
    r"\\(?:includegraphics|bibliography|addbibresource|lstinputlisting)"
    r"(?:\s*\[[^\]]*\])?\s*\{\s*([^{}]+)\s*\}", re.I)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")

_TEX_ENV_KEYS = {
    "BIBINPUTS", "BSTINPUTS", "CLUAINPUTS", "CWEBINPUTS", "ENCFONTS",
    "GFFONTS", "GLYPHFONTS", "LUAINPUTS", "MFINPUTS", "MPINPUTS",
    "OFMFONTS", "OPENTYPEFONTS", "OVFFONTS", "OVPFONTS", "PKFONTS",
    "T1FONTS", "TEXCONFIG", "TEXFONTS", "TEXINPUTS", "TEXMFCNF",
    "TFMFONTS", "TRFONTS", "TTFONTS", "VFFONTS",
}


def strip_tex_comments(text: str) -> str:
    """删除未转义的 TeX 行注释，避免把注释示例误判成危险命令。"""
    lines = []
    for line in text.splitlines():
        cut = len(line)
        for match in re.finditer(r"%", line):
            index = match.start()
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                cut = index
                break
        lines.append(line[:cut])
    return "\n".join(lines)


def _safe_relative_path(raw: str) -> str | None:
    value = str(raw or "").strip().replace("\\", "/")
    if (not value or value.startswith(("/", "//", "|", "`"))
            or _WINDOWS_DRIVE_RE.match(value)):
        return None
    parts = value.split("/")
    if any(part in {"", ".", ".."} or "\x00" in part for part in parts):
        return None
    return "/".join(parts)


def validate_tex_text(text: str, *, source_name: str = "模板",
                      package_files: Iterable[str] = ()) -> None:
    """校验一份 TeX 源码；只允许静态、包内的文件引用。"""
    if not isinstance(text, str) or "\x00" in text:
        raise TexSandboxError(f"{source_name} 不是有效的 UTF-8 TeX 文本",
                              code="invalid_tex")
    source = strip_tex_comments(text)
    for pattern, label in _FORBIDDEN_COMMANDS:
        if pattern.search(source):
            raise TexSandboxError(
                f"{source_name} 包含不允许的 {label}，不能进入编译沙箱。",
                code="dangerous_tex",
            )

    available = {str(item).replace("\\", "/").casefold()
                 for item in package_files}
    source_parent = Path(str(source_name).replace("\\", "/")).parent.as_posix()
    if source_parent == ".":
        source_parent = ""
    literal_spans = {match.span() for match in _LITERAL_INPUT_RE.finditer(source)}
    for match in _INPUT_COMMAND_RE.finditer(source):
        if not any(start <= match.start() < end for start, end in literal_spans):
            raise TexSandboxError(
                f"{source_name} 使用了动态 \\input/\\include；只允许花括号内的包内静态路径。",
                code="dangerous_tex",
            )
    for match in _LITERAL_INPUT_RE.finditer(source):
        raw = match.group(1).strip()
        rel = _safe_relative_path(raw)
        candidates = {str(rel or "").casefold()}
        if rel and not Path(rel).suffix:
            candidates.add(f"{rel}.tex".casefold())
        if rel and source_parent:
            nested = _safe_relative_path(f"{source_parent}/{rel}")
            if nested:
                candidates.add(nested.casefold())
                if not Path(nested).suffix:
                    candidates.add(f"{nested}.tex".casefold())
        if not rel or (available and not candidates.intersection(available)):
            raise TexSandboxError(
                f"{source_name} 引用了包外或不存在的 TeX 文件：{raw}",
                code="unsafe_resource_path",
            )

    for match in _PATH_ARGUMENT_RE.finditer(source):
        raw = match.group(1).strip()
        # Pandoc 可能生成文件名宏参数；模板源中只接受普通相对路径。生成后的
        # TeX 会再次校验，因此不存在“上传时安全、渲染后越界”的空档。
        if "$" in raw or "#" in raw:
            continue
        if _safe_relative_path(raw) is None:
            raise TexSandboxError(
                f"{source_name} 包含不安全的资源路径：{raw}",
                code="unsafe_resource_path",
            )


def validate_tex_package(root: Path, *, entrypoint: Path | None = None) -> None:
    """扫描模板包中的全部 TeX 类文本，拒绝链接、越界和危险命令。"""
    package_root = Path(root).resolve(strict=True)
    files: list[Path] = []
    for item in package_root.rglob("*"):
        if "_preview" in item.relative_to(package_root).parts:
            continue
        if item.is_symlink():
            raise TexSandboxError("模板包不能包含符号链接", code="symlink_file")
        if item.is_file() and item.suffix.casefold() in TEXT_RESOURCE_SUFFIXES:
            files.append(item)
    names = [item.relative_to(package_root).as_posix() for item in files]
    if entrypoint is not None:
        resolved_entrypoint = Path(entrypoint).resolve(strict=True)
        if resolved_entrypoint not in files:
            raise TexSandboxError("模板入口文件不存在", code="missing_entrypoint")
    for item in files:
        try:
            text = item.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise TexSandboxError(
                f"模板资源必须是 UTF-8 文本：{item.relative_to(package_root).as_posix()}",
                code="invalid_encoding",
            ) from exc
        validate_tex_text(
            text,
            source_name=item.relative_to(package_root).as_posix(),
            package_files=names,
        )


def _tool(command: str, tool: str) -> str:
    configured = str(command or "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_absolute() and candidate.is_file():
            return str(candidate)
        found = shutil.which(configured)
        if found:
            return found
    found = shutil.which(tool)
    if found:
        return found
    raise TexToolUnavailable(tool)


def pandoc_path() -> str:
    return _tool(getattr(config, "PANDOC", ""), "pandoc")


def xelatex_path() -> str:
    return _tool(getattr(config, "XELATEX", ""), "xelatex")


def tools_available() -> dict[str, bool]:
    result: dict[str, bool] = {}
    for name, getter in (("pandoc", pandoc_path), ("xelatex", xelatex_path)):
        try:
            getter()
        except TexToolUnavailable:
            result[name] = False
        else:
            result[name] = True
    return result


def _sandbox_env(cwd: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in _TEX_ENV_KEYS}
    env.update({
        "openin_any": "p",
        "openout_any": "p",
        "shell_escape": "f",
        "max_print_line": "1000",
        "TEXMFOUTPUT": str(cwd),
        # MiKTeX 不得在验证/导出过程中弹窗或联网补装宏包。
        "MIKTEX_ENABLE_INSTALLER": "0",
        "MIKTEX_DISABLE_INSTALLER": "1",
        "MIKTEX_AUTOINSTALL": "0",
        "MIKTEX_NO_REGISTRY": "1",
    })
    return env


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def run(command: list[str], *, cwd: Path, timeout: int, step: str) -> str:
    """在沙箱环境中执行外部工具，并返回合并后的 UTF-8 输出。"""
    work = Path(cwd).resolve(strict=True)
    kwargs: dict[str, object] = {
        "cwd": str(work),
        "env": _sandbox_env(work),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError as exc:
        raise TexSandboxError(f"{step} 无法启动：{exc}", code="tool_start_failed") from exc
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        raise TexSandboxError(
            f"{step} 超时（>{timeout}s），已终止整棵编译进程。",
            code="compile_timeout",
        ) from exc
    text = (output or b"").decode("utf-8", "replace")
    if process.returncode != 0:
        raise TexSandboxError(
            f"{step} 失败（退出码 {process.returncode}）：{error_excerpt(text)}",
            code="compile_failed",
        )
    return text


def error_excerpt(output: str) -> str:
    lines: list[str] = []
    for raw in str(output or "").splitlines():
        line = raw.strip()
        if (line.startswith("!") or line.startswith("l.")
                or "Error" in line or "Undefined control sequence" in line):
            lines.append(line[:500])
        if len(lines) >= 6:
            break
    if not lines:
        compact = " ".join(str(output or "").split())
        return compact[-1200:] or "未返回可识别的错误信息"
    return " / ".join(lines)


def run_pandoc(markdown: Path, output_tex: Path, template: Path,
               *, variables: Iterable[str] = (), timeout: int = 60) -> None:
    command = [pandoc_path(), markdown.name, "-o", output_tex.name,
               "--template", template.relative_to(markdown.parent).as_posix()]
    for variable in variables:
        command.extend(["-V", str(variable)])
    run(command, cwd=markdown.parent, timeout=timeout, step="Pandoc 模板渲染")
    if not output_tex.is_file():
        raise TexSandboxError("Pandoc 未生成 TeX 文件", code="missing_output")


def compile_xelatex(tex_file: Path, *, passes: int = 2,
                    timeout: int = 60) -> Path:
    tex_path = Path(tex_file).resolve(strict=True)
    work = tex_path.parent
    validate_tex_text(
        tex_path.read_text(encoding="utf-8-sig"),
        source_name=tex_path.name,
        package_files=[item.relative_to(work).as_posix()
                       for item in work.rglob("*") if item.is_file()],
    )
    executable = xelatex_path()
    command = [executable]
    if "miktex" in executable.casefold():
        command.append("--disable-installer")
    command.extend([
        "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error",
        "-file-line-error", tex_path.name,
    ])
    for index in range(max(1, int(passes))):
        output = run(command, cwd=work, timeout=timeout,
                     step=f"XeLaTeX 编译（第 {index + 1} 遍）")
        if "Missing character: There is no" in output:
            raise TexSandboxError(
                "XeLaTeX 编译完成但存在缺失字形；请为中文和公式配置可用字体。",
                code="missing_glyphs",
            )
    pdf = tex_path.with_suffix(".pdf")
    if not pdf.is_file():
        raise TexSandboxError("XeLaTeX 未生成 PDF", code="missing_output")
    return pdf


__all__ = [
    "TexSandboxError", "TexToolUnavailable", "TEXT_RESOURCE_SUFFIXES",
    "strip_tex_comments", "validate_tex_text", "validate_tex_package", "tools_available",
    "pandoc_path", "xelatex_path", "run", "run_pandoc", "compile_xelatex",
]
