"""生成可重复构建的 QuizForge Windows 图标。"""

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SIZE = 1024


def _gradient() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE))
    pixels = image.load()
    top = (79, 70, 229)
    bottom = (2, 132, 199)
    for y in range(SIZE):
        ratio = y / (SIZE - 1)
        color = tuple(round(a + (b - a) * ratio) for a, b in zip(top, bottom)) + (255,)
        for x in range(SIZE):
            pixels[x, y] = color
    return image


def build() -> tuple[Path, Path]:
    ASSETS.mkdir(parents=True, exist_ok=True)
    image = _gradient()
    draw = ImageDraw.Draw(image)

    # 柔和内框让小尺寸图标仍有清楚轮廓。
    draw.rounded_rectangle((38, 38, 986, 986), radius=220,
                           outline=(255, 255, 255, 48), width=22)

    # 打开的题册：两页分别略向外张开，中缝和页线在 16px 下仍可辨认。
    left = [(182, 346), (492, 430), (492, 786), (183, 690)]
    right = [(532, 430), (842, 346), (841, 690), (532, 786)]
    draw.polygon(left, fill=(255, 255, 255, 245))
    draw.polygon(right, fill=(240, 249, 255, 245))
    draw.line((512, 426, 512, 805), fill=(183, 220, 244, 255), width=28)
    for y, inset in ((492, 0), (562, 12), (632, 24)):
        draw.line((244 + inset, y, 442, y + 54), fill=(74, 116, 180, 175), width=18)
        draw.line((582, y + 54, 780 - inset, y), fill=(74, 116, 180, 175), width=18)

    # 右上角的四向火花对应 Forge，也避免图标看起来只是普通阅读器。
    spark = [(790, 156), (827, 242), (916, 278), (827, 314),
             (790, 402), (753, 314), (665, 278), (753, 242)]
    draw.polygon(spark, fill=(251, 191, 36, 255))
    draw.ellipse((764, 252, 816, 304), fill=(255, 248, 220, 255))

    png_path = ASSETS / "quizforge.png"
    ico_path = ASSETS / "quizforge.ico"
    image.save(png_path, optimize=True)
    image.save(
        ico_path, format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
               (64, 64), (128, 128), (256, 256)],
    )
    return png_path, ico_path


if __name__ == "__main__":
    for output in build():
        print(f"[OK] {output}")
