"""本地功能与公开更新服务的边界。

QuizForge 的核心功能全部在本机开放。服务器只提供公开更新清单，不参与授权、
账号、OCR 或 TeX；旧版配置文件仍可留在用户目录，但这里不会读取其中的授权字段。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging

import config
import exporter
import handout_exporter
import word_exporter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServicePorts:
    """联网更新开关；本地导出不再是可配置的联网服务。"""

    update_mode: str = "remote"
    update_manifest_url: str = ""


_DEFAULTS = ServicePorts(
    update_manifest_url=getattr(config, "UPDATE_MANIFEST_URL", ""),
)
_VALID_MODES = {
    "update_mode": {"disabled", "remote"},
}


def load() -> ServicePorts:
    """只读取更新配置；旧文件中的授权和云导出字段会被忽略。"""
    path = config.SERVICE_PORTS_PATH
    if not path.is_file():
        return _DEFAULTS
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("更新服务配置不可读，使用默认更新地址：%s", exc)
        return _DEFAULTS
    if not isinstance(raw, dict):
        return _DEFAULTS

    values = asdict(_DEFAULTS)
    for key in values:
        value = raw.get(key)
        if isinstance(value, str):
            values[key] = value.strip()
    for key, allowed in _VALID_MODES.items():
        if values[key] not in allowed:
            logger.warning("更新配置 %s=%r 非法，使用默认更新地址", key, values[key])
            return _DEFAULTS
    return ServicePorts(**values)


def status() -> dict[str, object]:
    """返回桌面状态，不主动访问网络，也不读取任何历史授权数据。"""
    ports = load()
    return {
        "license": {
            "mode": "open_source",
            "enabled": False,
            "enforced": False,
            "summary": "GPL-3.0-or-later",
        },
        "updates": {
            "mode": ports.update_mode,
            "enabled": bool(ports.update_manifest_url),
            "manifest_url_configured": bool(ports.update_manifest_url),
        },
        "export": {"mode": "local", "enabled": True},
        "cloud": {"enabled": False},
    }


def export_document(*args, **kwargs):
    """导出题卷；全部本地格式免费开放，云 TeX 明确停用。"""
    kwargs.pop("entitlement_feature", None)
    tex_backend = kwargs.pop("tex_backend", "local")
    fmt = kwargs.get("fmt", "pdf")
    if fmt == "docx":
        return word_exporter.export(*args, **kwargs)
    if fmt == "pdf" and tex_backend == "cloud":
        raise exporter.ExportError("云 TeX 已停用，请安装本机 TeX 或导出 tex.zip")
    return exporter.export(*args, **kwargs)


def export_handout_document(*args, **kwargs):
    """讲义导出完全在本机完成。"""
    return handout_exporter.export(*args, **kwargs)


def render_handout_question(*args, **kwargs):
    """讲义题卡的本地矢量预览。"""
    return handout_exporter.render_question(*args, **kwargs)
