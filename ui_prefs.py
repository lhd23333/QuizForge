"""界面外观偏好：深浅色模式、主题色、壁纸。

服务器版把这三项存在 `users` 表的 `theme_mode` / `theme_color` / `wallpaper`
三列上，单机单人没有用户表，改存一个 JSON（`config.UI_PREFS_PATH`）。

**为什么单机版也要这一套**：插件是把本应用嵌在 iframe 里，iframe 里是独立文档，
**不继承 Obsidian 自己的主题**——宿主切深色、这个面板还是刺眼的白底。所以深浅色
在插件场景下比在独立浏览器里更要紧，不是「宿主已经管了」。
"""

import json
import logging
import re
import threading

import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# 主题色只作为交互强调色；画布和品牌色由 CSS 设计令牌统一管理。
SWATCHES = ["#2457d6", "#63b3ff", "#2f9e72", "#d99a32", "#c66b9b", "#7c8491"]

# 新用户默认柔和石墨深色；load() 会继续保留旧偏好文件中的明确值。
DEFAULTS = {"theme_mode": "dark", "theme_color": "#63b3ff", "wallpaper": None}

# 主题模式各自有一个品牌默认强调色。偏好文件仍保留原来的三个字段，
# 这里通过“是否等于当前模式默认色”派生出自定义状态，避免为旧版本文件引入
# 迁移字段；旧文件里真正选过的其它颜色会继续保留。
DEFAULT_THEME_COLORS = {"dark": "#63b3ff", "light": "#2457d6"}

_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}\Z")

_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".m4v", ".ogv")


def default_theme_color(mode: str) -> str:
    """返回合法主题模式对应的品牌默认强调色。"""
    return DEFAULT_THEME_COLORS.get(mode, DEFAULT_THEME_COLORS["dark"])


def is_custom_theme_color(mode: str, color: object) -> bool:
    """判断颜色是否应覆盖当前模式的默认强调色。"""
    if not isinstance(color, str) or not _HEX_COLOR_RE.fullmatch(color):
        return False
    return color.lower() != default_theme_color(mode)


def effective_theme_color(mode: str, color: object) -> str:
    """取得设置控件和预览应显示的有效颜色。"""
    if is_custom_theme_color(mode, color):
        return str(color).lower()
    return default_theme_color(mode)


def accent_foreground(color: object) -> str:
    """选择在强调色上使用的高对比度前景色（深色或白色）。

    按 WCAG 相对亮度选择较高对比度的一方；按钮等控件不能假设所有自定义
    颜色都适合白字。
    """
    if not isinstance(color, str) or not _HEX_COLOR_RE.fullmatch(color):
        color = DEFAULT_THEME_COLORS["dark"]
    dark_ratio = _contrast_ratio(color, "#101214")
    light_ratio = _contrast_ratio(color, "#ffffff")
    # #101214 比纯黑更贴合石墨基线；若处于临界亮度，退回纯黑保证 AA。
    if dark_ratio >= light_ratio and dark_ratio >= 4.5:
        return "#101214"
    if light_ratio >= 4.5:
        return "#ffffff"
    black_ratio = _contrast_ratio(color, "#000000")
    return "#000000" if black_ratio >= light_ratio else "#ffffff"


def _relative_luminance(color: str) -> float:
    """返回十六进制颜色的 WCAG 相对亮度。"""
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.03928
              else ((value + 0.055) / 1.055) ** 2.4
              for value in channels]
    return (0.2126 * linear[0] + 0.7152 * linear[1]
            + 0.0722 * linear[2])


def _contrast_ratio(foreground: str, background: str) -> float:
    """返回两个不透明颜色之间的 WCAG 对比度。"""
    first = _relative_luminance(foreground)
    second = _relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def accent_text_color(color: object, mode: str) -> str:
    """派生可用于链接和文字图标的强调色。

    用户选择的原色继续用于填充、边框和焦点环；文字场景需要同时面对面板、
    抬升层等中性背景，因此对过亮/过暗的自定义色向黑或白渐进混合，直到
    所有基线背景达到 4.5:1。返回值仍是合法的六位十六进制颜色。
    """
    if not isinstance(color, str) or not _HEX_COLOR_RE.fullmatch(color):
        color = default_theme_color(mode)
    color = color.lower()
    if mode == "light":
        backgrounds = ("#f5f6f8", "#ffffff", "#f0f1f3")
        target = (0, 0, 0)
    else:
        backgrounds = ("#202124", "#292a2d", "#323338")
        target = (255, 255, 255)
    if all(_contrast_ratio(color, background) >= 4.5
           for background in backgrounds):
        return color
    source = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
    for step in range(1, 101):
        amount = step / 100
        candidate = "#" + "".join(
            f"{round(channel * (1 - amount) + goal * amount):02x}"
            for channel, goal in zip(source, target)
        )
        if all(_contrast_ratio(candidate, background) >= 4.5
               for background in backgrounds):
            return candidate
    return "#101214" if mode == "light" else "#f1f3f4"


def load() -> dict:
    """读全部偏好，缺键补默认值（所以调用方可以直接下标取）。"""
    data = dict(DEFAULTS)
    if not config.UI_PREFS_PATH.exists():
        return data
    try:
        raw = json.loads(config.UI_PREFS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("ui_prefs.json 解析失败，用默认外观")
        return data
    if isinstance(raw, dict):
        # 只认识的键才收，避免手改文件塞进奇怪内容后模板里出意外。
        # 主题色单独在下面按最终模式归一化，不能先套用深色默认值。
        if raw.get("theme_mode") is not None:
            data["theme_mode"] = raw["theme_mode"]
        if raw.get("wallpaper") is not None:
            # 壁纸文件名会进入模板和路径拼接，只接受字符串，避免损坏偏好导致整页渲染失败。
            if isinstance(raw["wallpaper"], str) and raw["wallpaper"].strip():
                data["wallpaper"] = raw["wallpaper"]
    if data["theme_mode"] not in ("light", "dark"):
        data["theme_mode"] = "dark"
    # 偏好文件是本机可编辑文件，但其值会进入 html 的 CSS 自定义属性；
    # 只接受完整的十六进制颜色，避免异常内容污染页面或形成 CSS 注入。
    raw_color = raw.get("theme_color") if isinstance(raw, dict) else None
    if not isinstance(raw_color, str) or not _HEX_COLOR_RE.fullmatch(raw_color):
        data["theme_color"] = default_theme_color(data["theme_mode"])
    else:
        data["theme_color"] = raw_color.lower()
    return data


def _save(data: dict):
    config.UI_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.UI_PREFS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def update(**kwargs):
    """局部更新（只写传进来的键）。wallpaper 传 None 表示移除。"""
    with _lock:
        data = load()
        for k, v in kwargs.items():
            if k == "theme_mode":
                data[k] = v if v in ("light", "dark") else "dark"
            elif k == "theme_color":
                data[k] = (v.lower() if isinstance(v, str)
                           and _HEX_COLOR_RE.fullmatch(v)
                           else default_theme_color(data["theme_mode"]))
            elif k in DEFAULTS:
                data[k] = v
        _save(data)


def is_video_wallpaper(name: str | None) -> bool:
    return isinstance(name, str) and bool(name) and name.lower().endswith(_VIDEO_EXTS)
