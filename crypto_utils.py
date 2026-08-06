r"""识别模型 API key 的加密存储（Fernet 对称加密）。

本地单机版，没有 `.env`，也不该要求用户手工生成密钥，所以首次使用时自动在
`data/.enc_key` 生成一份并复用。

**换掉/删掉 `data/.enc_key` 是不可逆的**：已存的 API key 永久解不开，
只能在设置页重新填一次。该文件不进 git（`data/` 已在 .gitignore 里）。
"""

import logging

from cryptography.fernet import Fernet, InvalidToken

import config

logger = logging.getLogger(__name__)

_KEY_PATH = config.ENC_KEY_PATH


class CryptoError(Exception):
    """加解密失败（如密钥错误、数据损坏）。"""


def _load_key() -> bytes:
    if _KEY_PATH.exists():
        return _KEY_PATH.read_bytes().strip()
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    _KEY_PATH.write_bytes(key)
    logger.info("已生成本机加密密钥: %s（删掉它已存的 API key 就解不开了）", _KEY_PATH)
    return key


def _fernet() -> Fernet:
    try:
        return Fernet(_load_key())
    except (ValueError, TypeError) as e:
        raise CryptoError(
            f"加密密钥文件 {_KEY_PATH} 格式不正确：{e}。"
            f"删掉它会重新生成（代价是已存的 API key 需要重新填）"
        ) from e
    except OSError as e:
        raise CryptoError(f"读写加密密钥文件 {_KEY_PATH} 失败: {e}") from e


def encrypt_token(plain: str) -> str:
    """明文 API key -> 加密串（可直接存 providers.json）。"""
    return _fernet().encrypt(plain.strip().encode()).decode()


def decrypt_token(enc: str) -> str:
    """加密串 -> 明文 key。密钥错误或数据损坏时抛 CryptoError。"""
    try:
        return _fernet().decrypt(enc.encode()).decode()
    except InvalidToken as e:
        raise CryptoError(
            f"API key 解密失败，可能是 {_KEY_PATH} 被更换或删除过。"
            f"请在设置页重新填一次 API key"
        ) from e
