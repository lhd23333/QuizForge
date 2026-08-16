"""联网能力的配置孔位；初版只启用离线实现。

业务代码只通过本模块选择导出后端。以后接授权、更新或云端编译时，在这里增加
实现并切换 mode，题库、页面和现有 exporter 不需要跟着改。当前模块不导入 HTTP
客户端，也不会主动建立任何网络连接。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging

import config
import exporter
import handout_exporter
import license_manager
import word_exporter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServicePorts:
    """三个未来服务的稳定配置边界。"""

    license_mode: str = "offline_signed"
    license_base_url: str = ""
    update_mode: str = "disabled"
    update_manifest_url: str = ""
    export_mode: str = "local"
    export_base_url: str = ""


_DEFAULTS = ServicePorts()
_VALID_MODES = {
    "license_mode": {"offline_signed", "remote"},
    "update_mode": {"disabled", "remote"},
    "export_mode": {"local", "remote"},
}


def load() -> ServicePorts:
    """读取服务配置；缺失、损坏或非法值一律保守回落到纯离线。"""
    path = config.SERVICE_PORTS_PATH
    if not path.is_file():
        return _DEFAULTS
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("联网服务配置不可读，保持纯离线：%s", exc)
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
            logger.warning("联网服务配置 %s=%r 非法，保持纯离线", key, values[key])
            return _DEFAULTS
    return ServicePorts(**values)


def status() -> dict[str, object]:
    """给桌面壳/未来设置页展示，不触发任何联网探测。"""
    ports = load()
    license_state = license_manager.load()
    return {
        "license": {"mode": ports.license_mode, **license_state.to_dict()},
        "updates": {"mode": ports.update_mode, "enabled": False},
        "export": {"mode": ports.export_mode, "enabled": ports.export_mode == "local"},
    }


def _require_local_export() -> None:
    """所有导出能力共用同一许可证与服务模式门控。"""
    ports = load()
    if ports.export_mode != "local":
        raise exporter.ExportError("当前版本尚未启用远程导出服务，请切回本地导出")
    if license_manager.is_enforced() and not license_manager.export_allowed():
        state = license_manager.load()
        raise exporter.ExportError(
            f"{state.summary}。请到“设置 → 软件授权”导入有效的 .qflicense 文件"
        )


def export_document(*args, **kwargs):
    """统一试卷导出入口；许可证通过后再选择本地语义导出器。"""
    _require_local_export()
    if kwargs.get("fmt", "pdf") == "docx":
        return word_exporter.export(*args, **kwargs)
    return exporter.export(*args, **kwargs)


def export_handout_document(*args, **kwargs):
    """讲义工作台导出入口，与既有导出共享许可证边界。"""
    _require_local_export()
    return handout_exporter.export(*args, **kwargs)


def render_handout_question(*args, **kwargs):
    """把单个讲义题卡编译为矢量预览，同样属于本地导出能力。"""
    _require_local_export()
    return handout_exporter.render_question(*args, **kwargs)
