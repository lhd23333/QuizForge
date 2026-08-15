"""旧版多份 MinerU token 调度器，仅供历史测试兼容。

生产转换链已经统一改走 ``ocr_pool.py``；新代码不得再引用本模块，否则会绕过
MinerU／Doc2X 共享的进程级总并发、后端冷却和结构化错误分流。

设置页可以存多份 token（`mineru_store`）。多份的用处不是「切换」而是**并行**：
方式四同时跑 config.BATCH_CONVERT_CONCURRENCY 组，单个 MinerU 账号的并发和额度
都有限，几组挤同一个 token 会互相排队甚至报超额。

调度策略：按「当前并发数」选最空闲的一个 token（进程内计数器）。调用方必须在
任务结束（成功或失败）后调用 `release()`，否则计数器只增不减，后续调度会一直
避开那个其实已经空闲的 token——**必须写在 finally 里**。

失败重试：MinerU 报错（token 失效/超额等）时换下一个最空闲的 token 重试一次；
仍失败就把原始异常抛给调用方。只在**存了多份**时才重试：只存一份的人，那份报错
就是真报错，用同一个 token 再跑一遍只是白等一次。

与服务器版 mineru_pool 的区别：那边的池子是「管理员加的 ∪ 白名单用户自己填的」，
要查库、要判 `mineru_whitelisted`、还要区分「自己的 token」和「借来的 token」
（借来的才重试，自己的不重试，免得偷偷花别人的额度）。本地单用户没有别人，
所有 token 都是自己的，那套区分整个消失：池子就是 mineru_store 里的全部。
"""

import logging
import threading

import mineru_store

logger = logging.getLogger(__name__)

# token(明文) -> 当前并发数。仅进程内有效，重启归零（可接受：计数只影响
# 「挑哪个」，不影响正确性）。
_inflight: dict[str, int] = {}
_lock = threading.Lock()


def _acquire_least_busy(candidates: list[str], exclude: set[str] | None = None) -> str | None:
    """从候选里选当前并发数最少的一个（排除 exclude 里的），计数 +1 后返回。"""
    exclude = exclude or set()
    usable = [t for t in candidates if t not in exclude]
    if not usable:
        return None
    with _lock:
        chosen = min(usable, key=lambda t: _inflight.get(t, 0))
        _inflight[chosen] = _inflight.get(chosen, 0) + 1
    return chosen


def acquire() -> str:
    """取一个该用的 token；没存过任何 token 时返回空字符串。

    返回空串是正常路径，不是错误：converter 会回落到
    `vendor/project_alpha/.env` 里的 `MINERU_API_TOKEN`（见 mineru_store 顶部）。
    空串**不需要** release（release 对空串无害，照写 finally 也行）。
    """
    return _acquire_least_busy(mineru_store.resolve_all()) or ""


def acquire_retry(exclude: str) -> str | None:
    """上一个 token 失败后换一个最空闲的重试；排除刚失败的那个。

    取不到候选（只存了一份 token）时返回 None——调用方应把原始异常照常抛出，
    不要静默吞掉。
    """
    return _acquire_least_busy(mineru_store.resolve_all(), exclude={exclude})


def release(token: str):
    """任务结束（成功/失败）后归还并发计数。**必须放在 finally 里。**

    对空串、以及计数器里没有的 token 调用都安全跳过。
    """
    if not token:
        return
    with _lock:
        if token in _inflight:
            _inflight[token] = max(0, _inflight[token] - 1)


def inflight_snapshot() -> dict[str, int]:
    """调试/自检用：各 token 当前并发数。**键是明文 token，不要往页面上送。**"""
    with _lock:
        return dict(_inflight)
