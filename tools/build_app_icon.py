"""生成并校验可重复构建的 QuizForge Windows 图标。

图标源文件登记在 ``assets/brand``。历史构建接口仍然输出
``assets/quizforge.png`` 与 ``assets/quizforge.ico``，但不再运行时重绘渐变，
避免源码构建、桌面包和安装器之间出现不同图标。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
BRAND_ASSETS = ASSETS / "brand"
STATIC_BRAND = ROOT / "static" / "brand"

SOURCE_SVG = BRAND_ASSETS / "quizforge-app-icon.svg"
SOURCE_PNG = BRAND_ASSETS / "quizforge-app-icon-1024.png"
SOURCE_ICO = BRAND_ASSETS / "quizforge-app-icon.ico"
SOURCE_WIMATH_MARK = BRAND_ASSETS / "wimath-mark-color.svg"
SOURCE_WIMATH_MARK_ALIAS = BRAND_ASSETS / "wimath-mark.svg"
SOURCE_QUIZFORGE_BY_WIMATH = BRAND_ASSETS / "quizforge-by-wimath.svg"
SOURCE_WIMATH_SMALL_MARK = BRAND_ASSETS / "wimath-mark-small-16.svg"

RUNTIME_SVG = STATIC_BRAND / "quizforge-app-icon.svg"
RUNTIME_ICO = STATIC_BRAND / "quizforge-app-icon.ico"
RUNTIME_WIMATH_MARK = STATIC_BRAND / "wimath-mark-color.svg"
RUNTIME_WIMATH_MARK_ALIAS = STATIC_BRAND / "wimath-mark.svg"
RUNTIME_QUIZFORGE_BY_WIMATH = STATIC_BRAND / "quizforge-by-wimath.svg"
RUNTIME_WIMATH_SMALL_MARK = STATIC_BRAND / "wimath-mark-small-16.svg"
LEGACY_PNG = ASSETS / "quizforge.png"
LEGACY_ICO = ASSETS / "quizforge.ico"

ICON_SIZES = frozenset({
    (16, 16), (24, 24), (32, 32), (48, 48),
    (64, 64), (128, 128), (256, 256),
})

# 这些哈希对应已经审核过的 WIMath 1.1 交付资产。若要更新品牌文件，
# 必须先获得授权、更新资产登记和哈希，再提交代码，不能静默替换二进制。
EXPECTED_SHA256 = {
    SOURCE_SVG: "8669569eca82aa4c117dc164f548728a140857542dd235c006394f60354491cd",
    SOURCE_PNG: "e27855c602196f235e17340bd6164b8497a8dd8834e20862db86e9b97d8c73b8",
    SOURCE_ICO: "87cea6cc26d3eb5f53706c6e50baf270df454a4cea944d88269f531daf58b414",
    SOURCE_WIMATH_MARK: "8d37860ff6196fbe6a5a79b1c7008b9560aa057fb7a192572ed3379f1c4e64e4",
    SOURCE_WIMATH_MARK_ALIAS: "8d37860ff6196fbe6a5a79b1c7008b9560aa057fb7a192572ed3379f1c4e64e4",
    SOURCE_QUIZFORGE_BY_WIMATH: "6a916c0479b72bc479f2421c92503d3f5a7d58cf7ba9265f4a015af3dec7067b",
    SOURCE_WIMATH_SMALL_MARK: "d01c854904ff06115cb5e85e1b4b9e036450803983b6f214b3075a9976819105",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_svg(path: Path, expected_view_box: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"无法读取品牌 SVG：{path}") from exc
    if not text.lstrip().startswith("<svg"):
        raise RuntimeError(f"品牌文件不是 SVG：{path}")
    if f'viewBox="{expected_view_box}"' not in text:
        raise RuntimeError(f"品牌 SVG viewBox 不符合约定：{path}")
    if "<image" in text.lower() or "href=\"http" in text.lower():
        raise RuntimeError(f"品牌 SVG 不得嵌入位图或远程资源：{path}")


def _validate_sources() -> None:
    for path, expected in EXPECTED_SHA256.items():
        if not path.is_file():
            raise RuntimeError(f"缺少受控品牌资产：{path}")
        actual = _sha256(path)
        if actual != expected:
            raise RuntimeError(
                f"受控品牌资产哈希不匹配：{path}（期望 {expected}，实际 {actual}）"
            )

    _validate_svg(SOURCE_SVG, "0 0 256 256")
    _validate_svg(SOURCE_WIMATH_MARK, "0 0 256 256")
    _validate_svg(SOURCE_WIMATH_MARK_ALIAS, "0 0 256 256")
    _validate_svg(SOURCE_QUIZFORGE_BY_WIMATH, "0 0 1000 260")
    _validate_svg(SOURCE_WIMATH_SMALL_MARK, "0 0 16 16")

    try:
        with Image.open(SOURCE_PNG) as image:
            if image.format != "PNG" or image.size != (1024, 1024):
                raise RuntimeError(f"产品图标 PNG 必须为 1024x1024：{SOURCE_PNG}")
            if image.mode not in ("RGBA", "RGB"):
                raise RuntimeError(f"产品图标 PNG 色彩模式不受支持：{image.mode}")
            image.load()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"无法读取产品图标 PNG：{SOURCE_PNG}") from exc

    try:
        with Image.open(SOURCE_ICO) as image:
            sizes = set(image.info.get("sizes", ()))
            if image.format != "ICO" or not ICON_SIZES.issubset(sizes):
                raise RuntimeError(
                    f"产品图标 ICO 必须包含尺寸 {sorted(ICON_SIZES)}：{SOURCE_ICO}"
                )
            image.seek(0)
            image.load()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"无法读取产品图标 ICO：{SOURCE_ICO}") from exc


def _copy_exact(source: Path, target: Path) -> Path:
    """以临时文件写入，避免构建中断时留下半个品牌文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def build() -> tuple[Path, Path]:
    _validate_sources()
    png_path = _copy_exact(SOURCE_PNG, LEGACY_PNG)
    ico_path = _copy_exact(SOURCE_ICO, LEGACY_ICO)
    _copy_exact(SOURCE_SVG, RUNTIME_SVG)
    _copy_exact(SOURCE_ICO, RUNTIME_ICO)
    _copy_exact(SOURCE_WIMATH_MARK, RUNTIME_WIMATH_MARK)
    _copy_exact(SOURCE_WIMATH_MARK_ALIAS, RUNTIME_WIMATH_MARK_ALIAS)
    _copy_exact(SOURCE_QUIZFORGE_BY_WIMATH, RUNTIME_QUIZFORGE_BY_WIMATH)
    _copy_exact(SOURCE_WIMATH_SMALL_MARK, RUNTIME_WIMATH_SMALL_MARK)
    return png_path, ico_path


if __name__ == "__main__":
    for output in build():
        print(f"[OK] {output}")
