"""LLM 识别模型配置：JSON 文件存储，可存多套、按用途切换当前生效的那套。

替代 quizbank 的 llm_providers 表 + llm_provider.py。存储位置见
`config.PROVIDERS_PATH`（data/providers.json），cc-switch 风格：每个用途一个
active id + 一份共享的配置列表。api_key 只存 Fernet 密文（crypto_utils），
设置页不回显明文。

用途分两条（与服务器版 `db.PURPOSES` 对齐）：

- `md`   —— 导入识别/规范化，要的是长上下文的**文本**模型；
- `redraw` —— 配图重绘（草图 → TikZ），**必须支持图片输入**，纯文本模型
  （deepseek-chat 之类）会在 `chat_vision` 那一步直接报错。

一套凭据可以同时服务两条路径（两个开关都点亮），也可以各配一套。

没有启用项时 resolve() 返回 None，调用方（converter.py）回落到 project-alpha
的默认 DeepSeek 客户端——与加这个模块之前的行为一致。
"""

import dataclasses
import json
import logging
import os
import threading

import config
import crypto_utils

logger = logging.getLogger(__name__)

_lock = threading.Lock()

#: 合法用途。表单传进来的 purpose 一律先过 `_active_key()` 校验，别直接当键用。
PURPOSES = ("md", "redraw")

# 预设只用于辅助填写新配置，不写入 providers.json，也不会修改已有配置。
# context_label 是服务商公布的模型能力提示；实际请求只使用 max_tokens。
LLM_PROVIDER_PRESETS = (
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "models": (
            {
                "id": "deepseek-v4-flash",
                "label": "DeepSeek V4 Flash",
                "context_label": "1M",
                "recommended_max_tokens": 32768,
                "supports_vision": False,
            },
            {
                "id": "deepseek-v4-pro",
                "label": "DeepSeek V4 Pro",
                "context_label": "1M",
                "recommended_max_tokens": 65536,
                "supports_vision": False,
            },
        ),
    },
    {
        "id": "qwen",
        "label": "阿里云百炼（Qwen）",
        "name": "Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": (
            {
                "id": "qwen3.7-flash",
                "label": "Qwen3.7 Flash",
                "context_label": "以百炼控制台为准",
                "recommended_max_tokens": 32768,
                "supports_vision": False,
            },
            {
                "id": "qwen3.7-plus",
                "label": "Qwen3.7 Plus",
                "context_label": "以百炼控制台为准",
                "recommended_max_tokens": 32768,
                "supports_vision": False,
            },
            {
                "id": "qwen3.8-max",
                "label": "Qwen3.8 Max",
                "context_label": "以百炼控制台为准",
                "recommended_max_tokens": 65536,
                "supports_vision": False,
            },
            {
                "id": "qwen3.5-omni-plus",
                "label": "Qwen3.5 Omni Plus（支持图片）",
                "context_label": "以百炼控制台为准",
                "recommended_max_tokens": 16384,
                "supports_vision": True,
            },
        ),
    },
)

#: 用途 → JSON 里存 active id 的键名。`md` 沿用老键 `active`，这样**已有的
#: data/providers.json 不需要迁移**，老配置读出来天然就是「导入识别」那一套。
_ACTIVE_KEY = {"md": "active", "redraw": "active_redraw"}


def _active_key(purpose: str) -> str:
    """把用途映射成 JSON 键名；未知用途直接抛。

    purpose 来自表单，所有「按用途取键」的地方只能从这个字典拿值，
    不允许调用方把字符串直接当键用（对齐服务器版 `db._active_col` 的约定）。
    """
    try:
        return _ACTIVE_KEY[purpose]
    except KeyError:
        raise ValueError(f"未知的 LLM 用途: {purpose!r}") from None


@dataclasses.dataclass(frozen=True)
class ProviderConfig:
    """解析结果。api_key 是解密后的明文，只在内存里存在，别写日志。"""
    id: str
    name: str
    base_url: str
    api_key: str
    model: str
    max_tokens: int

    @property
    def label(self) -> str:
        return self.name


def _empty() -> dict:
    return {"active": None, "active_redraw": None, "providers": []}


def _load() -> dict:
    if not config.PROVIDERS_PATH.exists():
        return _empty()
    try:
        data = json.loads(config.PROVIDERS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("providers.json 解析失败，视为空配置")
        return _empty()
    # 老文件没有 active_redraw 键。**不要顺手把它填成 active** —— resolve()
    # 已经有 redraw→md 的回落，填了反而把「没单独配视觉模型」这个事实抹掉，
    # 设置页就显示不出该提示了。
    data.setdefault("active", None)
    data.setdefault("active_redraw", None)
    data.setdefault("providers", [])
    return data


def _save(data: dict):
    path = config.PROVIDERS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def list_llm_providers() -> list[dict]:
    """返回列表（不含解密后的明文 key，仅供设置页展示）。

    每条附上 `active_md` / `active_redraw` 两个布尔位，模板照这两位画开关，
    不用再自己跟顶层的 active id 比对。
    """
    data = _load()
    out = []
    for p in data["providers"]:
        row = dict(p)
        # 老配置没有这个字段，默认按纯文本模型处理。这样快速切换区不会把
        # deepseek-chat 一类模型误列为视觉模型；用户在编辑页可手动补标记。
        row["supports_vision"] = bool(p.get("supports_vision", False))
        row["active_md"] = (p["id"] == data.get("active"))
        row["active_redraw"] = (p["id"] == data.get("active_redraw"))
        # 老模板读的是 is_active，保留它指向「导入识别」那一路，别断掉。
        row["is_active"] = row["active_md"]
        out.append(row)
    return out


def get_llm_provider(pid: str) -> dict | None:
    for p in _load()["providers"]:
        if p["id"] == pid:
            return p
    return None


def _to_config(row: dict) -> ProviderConfig | None:
    try:
        api_key = crypto_utils.decrypt_token(row["api_key_enc"])
    except crypto_utils.CryptoError:
        logger.warning("LLM 配置 id=%s 的 api_key 解密失败，已跳过", row["id"])
        return None
    if not api_key:
        return None
    return ProviderConfig(
        id=row["id"],
        name=row["name"],
        base_url=row["base_url"],
        api_key=api_key,
        model=row["model"],
        max_tokens=int(row["max_tokens"]),
    )


def get_active_llm_provider(purpose: str = "md") -> dict | None:
    data = _load()
    active = data.get(_active_key(purpose))
    if not active:
        return None
    return get_llm_provider(active)


def _resolve_one(purpose: str) -> ProviderConfig | None:
    row = get_active_llm_provider(purpose)
    return _to_config(row) if row is not None else None


def resolve(purpose: str = "md") -> ProviderConfig | None:
    """解析这次该用的 LLM 配置；没有启用项则返回 None。

    purpose='redraw' 解析不到时**回落到 'md' 那套**：没专门配视觉模型也能先点
    一下试试，而不是被一句「请先去配置」挡住。撞上纯文本模型时 `chat_vision`
    的报错已经点明「需要多模态模型」。反方向不回落 —— md 规范化拿视觉模型去跑
    没有意义。（与服务器版 `llm_provider.resolve` 同一条规则。）
    """
    cfg = _resolve_one(purpose)
    if cfg is None and purpose == "redraw":
        cfg = _resolve_one("md")
        if cfg is not None:
            logger.info("重绘没有专属配置，回落到规范化那套: %s", cfg.model)
    return cfg


# 与 quizbank 的 llm_provider.resolve_active() 对齐，供 converter.py 沿用同一调用方式。
resolve_active = resolve


def add_llm_provider(name: str, base_url: str, api_key_enc: str,
                      model: str, max_tokens: int,
                      purposes: tuple[str, ...] = ("md",),
                      supports_vision: bool = False) -> str:
    """新增一套配置，返回 id。

    `purposes` 里的每条用途，若当前**还没有**生效配置，就顺手把这一套点亮
    ——否则用户填完还要再回表格里点一次「启用」才生效（与服务器版
    `db.add_llm_provider` 同一行为）。已经有生效配置的用途不动，避免静默顶掉
    用户正在用的那套。
    """
    import uuid

    keys = [_active_key(p) for p in purposes]  # 先校验，别等写盘时才炸
    with _lock:
        data = _load()
        pid = uuid.uuid4().hex[:8]
        data["providers"].append({
            "id": pid,
            "name": name.strip(),
            "base_url": base_url.strip(),
            "api_key_enc": api_key_enc,
            "model": model.strip(),
            "max_tokens": int(max_tokens),
            "supports_vision": bool(supports_vision),
        })
        for key in keys:
            if not data.get(key):
                data[key] = pid
        _save(data)
        return pid


def update_llm_provider(pid: str, *, name: str, base_url: str,
                        model: str, max_tokens: int,
                        api_key_enc: str | None = None,
                        supports_vision: bool | None = None) -> bool:
    """编辑配置；API Key 留空时保留原密文，永不把明文送回页面。"""
    with _lock:
        data = _load()
        row = next((item for item in data["providers"] if item["id"] == pid), None)
        if row is None:
            return False
        row.update(name=name.strip(), base_url=base_url.strip(), model=model.strip(),
                   max_tokens=int(max_tokens))
        if supports_vision is not None:
            row["supports_vision"] = bool(supports_vision)
        if api_key_enc:
            row["api_key_enc"] = api_key_enc
        _save(data)
        return True


def deactivate_llm_providers(purpose: str | None = None):
    """清空启用项（不删除配置）。purpose=None 表示两条用途一起清。

    md 被清空后识别回落到 project-alpha 的老行为；redraw 被清空后
    resolve('redraw') 会回落到 md 那套。
    """
    keys = [_active_key(purpose)] if purpose else list(_ACTIVE_KEY.values())
    with _lock:
        data = _load()
        for key in keys:
            data[key] = None
        _save(data)


def set_active_llm_provider(pid: str, purpose: str = "md"):
    """把 pid 设为该用途当前生效的配置。pid 不存在则无操作。

    只动这一个用途的键——两条路径各配一套是支持的用法，别顺手把另一条也改了。
    """
    key = _active_key(purpose)
    with _lock:
        data = _load()
        if not any(p["id"] == pid for p in data["providers"]):
            return
        data[key] = pid
        _save(data)


def remove_llm_provider(pid: str):
    """删除一套配置。删掉的正好是某个用途的启用项时那个用途就空了，解析自动回落。"""
    with _lock:
        data = _load()
        data["providers"] = [p for p in data["providers"] if p["id"] != pid]
        for key in _ACTIVE_KEY.values():
            if data.get(key) == pid:
                data[key] = None
        _save(data)
