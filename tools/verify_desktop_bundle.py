"""发行目录泄密与完整性检查；任何命中都以非零状态阻止继续制作安装包。"""

from __future__ import annotations

import argparse
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


FORBIDDEN_NAMES = {
    ".enc_key",
    ".env",
    "desktop.json",
    "device_identity.dat",
    "doc2x.json",
    "license.qflicense",
    "mineru.json",
    "providers.json",
    "selections.json",
    "service_ports.json",
    "conversion_tasks.json",
}
PRIVATE_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
)
REQUIRED_HANDOUT_RESOURCES = (
    "assets/wimath-logo-latex-black.pdf",
    "templates/handouts.html",
    "static/js/handout-editor.bundle.js",
    "static/js/katex/katex.min.css",
)


def scan(dist: Path, project_root: Path) -> list[str]:
    problems: list[str] = []
    if not (dist / "QuizForge.exe").is_file():
        problems.append("缺少 QuizForge.exe")
        return problems

    public_key_path = next(
        (candidate for candidate in (
            dist / "assets" / "license_public_key.pem",
            dist / "_internal" / "assets" / "license_public_key.pem",
        ) if candidate.is_file()),
        None,
    )
    if public_key_path is None:
        problems.append("缺少离线许可证公钥 assets/license_public_key.pem")
    else:
        try:
            key = serialization.load_pem_public_key(public_key_path.read_bytes())
            if not isinstance(key, Ed25519PublicKey):
                problems.append("许可证公钥不是 Ed25519 类型")
        except (OSError, ValueError, TypeError) as exc:
            problems.append(f"许可证公钥不可读：{exc}")

    resource_root = dist / "_internal" if (dist / "_internal").is_dir() else dist
    for relative in REQUIRED_HANDOUT_RESOURCES:
        if not (resource_root / relative).is_file():
            problems.append(f"缺少讲义工作台资源：{relative}")

    # 只拦截发行目录根部可能泄露的业务源码。第三方依赖内部也可能存在
    # app.py 等同名文件，按文件名全局匹配会误报 pywebview 之类的依赖。
    business_sources = {path.name.lower() for path in project_root.glob("*.py")}
    business_source_locations = business_sources | {
        f"_internal/{name}" for name in business_sources
    }
    for path in dist.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(dist).as_posix()
        name = path.name.lower()
        if name in FORBIDDEN_NAMES or name.endswith(".qflicense"):
            problems.append(f"包含运行数据或许可证：{rel}")
        if name.endswith((".key", ".pfx", ".p12")):
            problems.append(f"包含私密密钥文件：{rel}")
        if "private" in name and name.endswith((".pem", ".key")):
            problems.append(f"包含疑似私钥：{rel}")
        if path.suffix.lower() == ".py" and rel.lower() in business_source_locations:
            problems.append(f"包含 QuizForge Python 源码：{rel}")
        try:
            if path.stat().st_size <= 5 * 1024 * 1024:
                head = path.read_bytes()
                if any(marker in head for marker in PRIVATE_MARKERS):
                    problems.append(f"文件内容包含 PEM 私钥：{rel}")
        except OSError as exc:
            problems.append(f"无法扫描文件 {rel}：{exc}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 QuizForge 桌面发行目录")
    parser.add_argument("--dist", type=Path, required=True)
    args = parser.parse_args()
    dist = args.dist.resolve()
    project_root = Path(__file__).resolve().parent.parent
    problems = scan(dist, project_root)
    if problems:
        for problem in problems:
            print(f"[ERROR] {problem}")
        return 1
    files = sum(1 for path in dist.rglob("*") if path.is_file())
    print(f"[OK] release bundle scan: {files} files, no source/private data found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
