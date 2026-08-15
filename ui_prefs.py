"""界面外观偏好：深浅色模式、主题色、壁纸。

服务器版把这三项存在 `users` 表的 `theme_mode` / `theme_color` / `wallpaper`
三列上，单机单人没有用户表，改存一个 JSON（`config.UI_PREFS_PATH`）。

**为什么单机版也要这一套**：插件是把本应用嵌在 iframe 里，iframe 里是独立文档，
**不继承 Obsidian 自己的主题**——宿主切深色、这个面板还是刺眼的白底。所以深浅色
在插件场景下比在独立浏览器里更要紧，不是「宿主已经管了」。
"""

import json
import logging
import threading

import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# 与服务器版 settings.html 的色板一致
SWATCHES = ["#0ea5e9", "#6366f1", "#16a34a", "#f59e0b", "#ec4899", "#64748b"]

DEFAULTS = {"theme_mode": "light", "theme_color": "#0ea5e9", "wallpaper": None}

_VIDEO_EXTS = (".mp4", ".webm", ".mov", ".m4v", ".ogv")


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
        # 只认识的键才收，避免手改文件塞进奇怪内容后模板里出意外
        for k in DEFAULTS:
            if raw.get(k) is not None:
                data[k] = raw[k]
    if data["theme_mode"] not in ("light", "dark"):
        data["theme_mode"] = "light"
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
            if k in DEFAULTS:
                data[k] = v
        _save(data)


def is_video_wallpaper(name: str | None) -> bool:
    return bool(name) and name.lower().endswith(_VIDEO_EXTS)
