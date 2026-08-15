"""MinerU API Token 的加密存储（OCR 环节用）。

与 `providers.py` 平行的一份小存储，但**刻意不合进那个文件**：MinerU 的多份
token 是「一起用、轮着调度」，不是 providers.json 那种「存多套、点一个启用」，
塞进那边的 `active` + 列表结构里语义会变形（谁是 active？停用 MinerU 是什么意思？）。

存储位置见 `config.MINERU_TOKEN_PATH`（data/mineru.json），密文用的是
`crypto_utils` 那同一把 `data/.enc_key`。设置页只写不读明文，页面上永远
只显示备注名和「（已设置）」，不回显 token 本身。

**可以存多份 token**（一个账号的额度用完了、或想让几组并发各走一个账号）。
调度不在这里，在 `ocr_pool`：这里只管落盘与解密。文件格式：

    {"tokens": [{"id": "...", "label": "备注", "token_enc": "...", "added": "..."}]}

老格式 `{"token_enc": "..."}`（单 token）仍能读，加载时就地升格成列表——不改写
文件，读旧文件的老代码路径没有了，但没必要为此多一次写盘。

一份 token 都没存时 `resolve()` 返回空字符串，`converter._load_config_for_user`
会回落到 `vendor/project_alpha/.env` 里的 `MINERU_API_TOKEN`（老行为）。
"""

import json
import logging
import threading
import uuid
from datetime import datetime

import config
import crypto_utils

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _load() -> dict:
    if not config.MINERU_TOKEN_PATH.exists():
        return {}
    try:
        return json.loads(config.MINERU_TOKEN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("mineru.json 解析失败，视为未配置")
        return {}


def _save(data: dict):
    config.MINERU_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.MINERU_TOKEN_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _entries() -> list[dict]:
    """全部 token 条目（保序）。老的单 token 格式就地升格，不写回文件。"""
    data = _load()
    items = data.get("tokens")
    if isinstance(items, list):
        return [it for it in items if isinstance(it, dict) and it.get("token_enc")]
    enc = data.get("token_enc")
    if enc:
        return [{"id": "legacy", "label": "", "token_enc": enc, "added": ""}]
    return []


def list_tokens() -> list[dict]:
    """设置页用：[{id, label, added}]，**不含密文、更不含明文**。"""
    return [{"id": it.get("id") or "", "label": it.get("label") or "",
             "added": it.get("added") or ""} for it in _entries()]


def has_token() -> bool:
    """设置页用：只判断有没有，不解密。密文坏了也算「有」——
    这样用户看到的是「已设置」+ 转换时的解密报错，而不是「没设置」这种误导。"""
    return bool(_entries())


def add_token(plain: str, label: str = "") -> bool:
    """追加一份 token（加密后落盘）。空串不动，返回是否真加了。"""
    plain = plain.strip()
    if not plain:
        return False
    enc = crypto_utils.encrypt_token(plain)
    with _lock:
        items = _entries()
        items.append({"id": uuid.uuid4().hex, "label": label.strip(),
                      "token_enc": enc,
                      "added": datetime.now().isoformat(timespec="seconds")})
        _save({"tokens": items})
    return True


def set_token(plain: str, label: str = ""):
    """存入明文 token，**替换掉全部已存的**。传空字符串等于清空。

    保留这个名字与「留空提交＝清除」的语义，是因为设置页那个单框表单一直是
    这么用的；要加第二份 token 请走 add_token，别把这个函数改成追加——
    那会让老表单一提交就悄悄多一份，用户以为自己在改、其实在攒。
    """
    with _lock:
        if not plain.strip():
            _save({})
            return
        _save({"tokens": [{"id": uuid.uuid4().hex, "label": label.strip(),
                           "token_enc": crypto_utils.encrypt_token(plain),
                           "added": datetime.now().isoformat(timespec="seconds")}]})


def remove_token(token_id: str) -> bool:
    """按 id 删一份。返回是否删掉了（id 不存在返回 False）。"""
    with _lock:
        items = _entries()
        kept = [it for it in items if (it.get("id") or "") != token_id]
        if len(kept) == len(items):
            return False
        _save({"tokens": kept})
    return True


def clear_token():
    with _lock:
        _save({})


def resolve_all() -> list[str]:
    """全部能解开的明文 token（保序、去重）。解不开的跳过并记日志。

    调度侧（ocr_pool）要的是候选集合，不是单个值，所以给列表。这里同样
    **不抛异常**，理由见 resolve()。
    """
    out: list[str] = []
    seen = set()
    for it in _entries():
        try:
            tok = crypto_utils.decrypt_token(it["token_enc"])
        except crypto_utils.CryptoError:
            logger.warning("MinerU token 解密失败（.enc_key 可能被换过），已跳过该条")
            continue
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def resolve() -> str:
    """返回一个明文 token；没配过或解不开都返回空字符串（调用方回落 .env）。

    存了多份时返回第一份。**要按忙闲轮转请走 ocr_pool.run()**——这个函数
    只在「不需要调度」的场合用（比如设置页自检），转换链路走池子。

    **不要把解密失败改成抛异常**：这个函数在后台转换线程里调用，抛出来只会
    让整组任务报一个与 MinerU 无关的错；返回空字符串会让 converter 走回落路径，
    真配置缺失时它自己会报「尚未配置 MinerU token」那条明确的提示。
    """
    toks = resolve_all()
    return toks[0] if toks else ""
