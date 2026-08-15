"""MinerU / Doc2X 的统一凭证轮转与进程级 OCR 限流。

调度单位是一个 OCR 文档任务，而不是一个批次或一组题目。因此“题干 + 解析”在
组内并发时也会分别取凭证、分别占槽；多个批次同时运行仍共同受全局上限约束。

多份凭证主要用于负载均衡、失效切换和独立账号额度，不能假设同一账号下创建多个
Token/Key 就能增加服务端算力。服务端明确返回并发/队列限制时，保持原凭证并让整个
后端短暂冷却；只有凭证失效或额度不足时，才换另一份凭证重试一次。
"""

from __future__ import annotations

import threading
import time
from contextlib import ExitStack
import atexit
import os
import mmap
import struct

import config
import doc2x_store
import mineru_store


class _ProcessSemaphore:
    """Windows 用命名内核信号量跨窗口限流，其它平台退回进程内信号量。"""

    def __init__(self, name: str, limit: int):
        self.limit = max(1, int(limit))
        self._fallback = None
        self._handle = None
        if os.name != "nt":
            self._fallback = threading.BoundedSemaphore(self.limit)
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateSemaphoreW.argtypes = (
            wintypes.LPVOID, wintypes.LONG, wintypes.LONG, wintypes.LPCWSTR)
        kernel32.CreateSemaphoreW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseSemaphore.argtypes = (
            wintypes.HANDLE, wintypes.LONG, wintypes.LPVOID)
        kernel32.ReleaseSemaphore.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateSemaphoreW(
            None, self.limit, self.limit, f"Local\\QuizForge.{name}.v1")
        if not handle:
            raise OSError(ctypes.get_last_error(), "创建 OCR 跨进程信号量失败")
        self._kernel32 = kernel32
        self._handle = handle
        atexit.register(self.close)

    def __enter__(self):
        if self._fallback is not None:
            self._fallback.acquire()
            return self
        result = self._kernel32.WaitForSingleObject(self._handle, 0xFFFFFFFF)
        if result != 0:
            raise OSError("等待 OCR 跨进程信号量失败")
        return self

    def __exit__(self, *_args):
        if self._fallback is not None:
            self._fallback.release()
        elif not self._kernel32.ReleaseSemaphore(self._handle, 1, None):
            raise OSError("释放 OCR 跨进程信号量失败")

    def close(self):
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class _SharedRoundRobin:
    """跨窗口共享一个无凭据整数，只用于打散并发进程的候选起点。"""

    def __init__(self, name: str):
        self._local_lock = threading.Lock()
        self._local_value = 0
        self._mapping = None
        self._mutex = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel32
        self._mutex = kernel32.CreateMutexW(
            None, False, f"Local\\QuizForge.{name}.Mutex.v1")
        if not self._mutex:
            raise OSError(ctypes.get_last_error(), "创建 OCR 轮转锁失败")
        self._mapping = mmap.mmap(
            -1, 8, tagname=f"Local\\QuizForge.{name}.Counter.v1")
        atexit.register(self.close)

    def next(self, modulo: int) -> int:
        if modulo <= 1:
            return 0
        if self._mapping is None:
            with self._local_lock:
                result = self._local_value % modulo
                self._local_value += 1
                return result
        result = self._kernel32.WaitForSingleObject(self._mutex, 30000)
        if result not in (0, 0x80):
            raise TimeoutError("等待 OCR 轮转锁超时")
        try:
            self._mapping.seek(0)
            value = struct.unpack("<Q", self._mapping.read(8))[0]
            self._mapping.seek(0)
            self._mapping.write(struct.pack("<Q", (value + 1) % (1 << 64)))
            self._mapping.flush()
            return value % modulo
        finally:
            self._kernel32.ReleaseMutex(self._mutex)

    def close(self):
        if self._mapping is not None:
            self._mapping.close()
            self._mapping = None
        if self._mutex:
            self._kernel32.CloseHandle(self._mutex)
            self._mutex = None


class _SharedCooldown:
    """跨窗口共享后端冷却截止时间；只存一个浮点时间戳，不含任务或凭据。"""

    def __init__(self, name: str):
        self._local_lock = threading.Lock()
        self._local_until = 0.0
        self._mapping = None
        self._mutex = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel32
        self._mutex = kernel32.CreateMutexW(
            None, False, f"Local\\QuizForge.{name}.CooldownMutex.v1")
        if not self._mutex:
            raise OSError(ctypes.get_last_error(), "创建 OCR 冷却锁失败")
        self._mapping = mmap.mmap(
            -1, 8, tagname=f"Local\\QuizForge.{name}.Cooldown.v1")
        atexit.register(self.close)

    def _locked(self, callback):
        if self._mapping is None:
            with self._local_lock:
                return callback(None)
        result = self._kernel32.WaitForSingleObject(self._mutex, 30000)
        if result not in (0, 0x80):
            raise TimeoutError("等待 OCR 冷却锁超时")
        try:
            return callback(self._mapping)
        finally:
            self._kernel32.ReleaseMutex(self._mutex)

    def deadline(self) -> float:
        def read(mapping):
            if mapping is None:
                return self._local_until
            mapping.seek(0)
            return struct.unpack("<d", mapping.read(8))[0]
        return self._locked(read)

    def mark(self, deadline: float) -> None:
        def write(mapping):
            if mapping is None:
                self._local_until = max(self._local_until, deadline)
                return
            mapping.seek(0)
            current = struct.unpack("<d", mapping.read(8))[0]
            mapping.seek(0)
            mapping.write(struct.pack("<d", max(current, deadline)))
            mapping.flush()
        self._locked(write)

    def close(self):
        if self._mapping is not None:
            self._mapping.close()
            self._mapping = None
        if self._mutex:
            self._kernel32.CloseHandle(self._mutex)
            self._mutex = None


_GLOBAL_SLOTS = _ProcessSemaphore(
    "OCR.Total", getattr(config, "OCR_TOTAL_CONCURRENCY", 12))
_BACKEND_SLOTS = {
    "mineru": _ProcessSemaphore(
        "OCR.MinerU", getattr(config, "MINERU_CONCURRENCY", 6)),
    "doc2x": _ProcessSemaphore(
        "OCR.Doc2X", getattr(config, "DOC2X_CONCURRENCY", 8)),
}
_ROTATION = {
    "mineru": _SharedRoundRobin("OCR.MinerU.Rotation"),
    "doc2x": _SharedRoundRobin("OCR.Doc2X.Rotation"),
}
_SHARED_COOLDOWN = {
    "mineru": _SharedCooldown("OCR.MinerU"),
    "doc2x": _SharedCooldown("OCR.Doc2X"),
}
_RESOLVERS = {
    "mineru": mineru_store.resolve_all,
    "doc2x": doc2x_store.resolve_all,
}

_lock = threading.Lock()
_inflight: dict[str, dict[str, int]] = {"mineru": {}, "doc2x": {}}
_cooldown_until: dict[str, float] = {"mineru": 0.0, "doc2x": 0.0}


class _NoCredentialError(RuntimeError):
    """候选凭证为空；与 callback 自己抛出的 RuntimeError 严格区分。"""


def _error_code(exc: BaseException) -> str:
    """沿异常 cause 链取结构化错误码；旧异常没有属性时再看消息。"""
    current: BaseException | None = exc
    messages = []
    while current is not None:
        code = str(getattr(current, "code", "") or "").strip().lower()
        if code:
            return code
        messages.append(str(current).lower())
        current = current.__cause__
    text = " ".join(messages)
    for code in (
        "parse_task_limit_exceeded", "parse_concurrency_limit",
        "parse_quota_limit", "a0202", "a0211", "-60009", "-60018",
        "http_429",
    ):
        if code in text:
            return code
    if "http 429" in text or "并发已满" in text:
        return "http_429"
    return ""


def _error_action(exc: BaseException) -> str:
    code = _error_code(exc)
    if code == "resume_token_mismatch":
        # MinerU 的 batch 可能只允许提交它的账号查询。恢复状态只保存 Token
        # 指纹；这里依次尝试本机仍配置的候选，找到原 Token 前不会发起新 OCR。
        return "resume_switch"
    if code in {"a0202", "a0211", "parse_quota_limit", "-60018", "http_401"}:
        return "switch"
    if code in {
        "http_429", "parse_task_limit_exceeded", "parse_concurrency_limit",
        "-60007", "-60009", "-10001",
    }:
        return "cooldown"
    return "fail"


def _candidates(backend: str, fallback: str = "") -> list[str]:
    fallback = (fallback or "").strip()
    # 显式传入的凭证用于测试、CLI 和兼容调用，优先级必须高于设置页池；桌面/网页
    # 正常路径传空串，因此仍完全由池调度。否则测试会意外读到用户真实配置。
    values = ([fallback] if fallback
              and fallback != "placeholder-injected-by-quizforge" else [])
    values.extend(_RESOLVERS[backend]())
    out = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _acquire_credential(backend: str, fallback: str = "", *,
                        preferred: str = "", exclude: set[str] | None = None) -> str:
    candidates = _candidates(backend, fallback)
    exclude = exclude or set()
    usable = [value for value in candidates if value not in exclude]
    if not usable:
        service = "MinerU Token" if backend == "mineru" else "Doc2X API Key"
        raise _NoCredentialError(f"尚未配置 {service}，请先在「设置」页填入")
    with _lock:
        loads = _inflight[backend]
        # 选择与 +1 必须原子完成；否则同时起跑的线程可能都在 +1 前看见同一份
        # 凭证为 0，短时间把负载压到同一个账号上。
        if preferred and preferred in usable:
            chosen = preferred
        else:
            least = min(loads.get(value, 0) for value in usable)
            tied = [value for value in usable if loads.get(value, 0) == least]
            chosen = tied[_ROTATION[backend].next(len(tied))]
        loads[chosen] = loads.get(chosen, 0) + 1
    return chosen


def _release_credential(backend: str, credential: str) -> None:
    with _lock:
        loads = _inflight[backend]
        if credential in loads:
            loads[credential] = max(0, loads[credential] - 1)


def _wait_for_cooldown(backend: str) -> None:
    with _lock:
        local_deadline = _cooldown_until[backend]
    deadline = max(local_deadline, _SHARED_COOLDOWN[backend].deadline())
    delay = max(0.0, deadline - time.time())
    if delay:
        time.sleep(delay)


def _mark_cooldown(backend: str) -> None:
    seconds = max(0.0, float(getattr(config, "OCR_LIMIT_COOLDOWN_SECONDS", 15)))
    deadline = time.time() + seconds
    with _lock:
        _cooldown_until[backend] = max(
            _cooldown_until[backend], deadline)
    _SHARED_COOLDOWN[backend].mark(deadline)


def _run_once(backend: str, callback, *, fallback: str = "",
              preferred: str = "", exclude: set[str] | None = None):
    _wait_for_cooldown(backend)
    # 固定按“全局 -> 后端”顺序取槽，避免不同线程反向等待形成死锁。
    with ExitStack() as stack:
        stack.enter_context(_GLOBAL_SLOTS)
        stack.enter_context(_BACKEND_SLOTS[backend])
        credential = _acquire_credential(
            backend, fallback, preferred=preferred, exclude=exclude)
        try:
            return callback(credential), credential
        except Exception as exc:
            # 外层要据错误类型决定“同凭证冷却重试”还是“排除该凭证换号”；
            # 只挂进程内临时属性，不写日志、页面或任务快照，避免泄露凭证。
            setattr(exc, "_quizforge_ocr_credential", credential)
            raise
        finally:
            _release_credential(backend, credential)


def run(backend: str, callback, *, fallback: str = ""):
    """以最空闲凭证执行一个 OCR 文档任务；只对可恢复错误重试一次。"""
    if backend not in _BACKEND_SLOTS:
        raise ValueError(f"未知 OCR 后端：{backend}")
    fallback_value = (fallback or "").strip()
    preferred = (fallback_value if fallback_value
                 and fallback_value != "placeholder-injected-by-quizforge" else "")
    try:
        # 显式兼容参数来自本次调用，优先级高于设置页池；正常桌面请求传空串，
        # 仍按在途数与跨窗口轮转选择池内凭证。
        result, credential = _run_once(
            backend, callback, fallback=fallback, preferred=preferred)
        return result
    except Exception as first:
        credential = str(getattr(first, "_quizforge_ocr_credential", "") or "")
        action = _error_action(first)
        if action == "fail":
            raise
        if action == "resume_switch":
            # 普通额度/失效错误仍只换一次，避免把坏文件重复送给所有账号；只有
            # “本地恢复状态与当前 Token 指纹不符”完全不访问外部服务，可以安全
            # 穷举本机候选，确保多 Token 轮转后仍能找到原批次。
            excluded = {credential}
            mismatch = first
            while True:
                try:
                    result, _ = _run_once(
                        backend, callback, fallback=fallback,
                        exclude=excluded)
                    return result
                except _NoCredentialError:
                    raise mismatch
                except Exception as exc:
                    if _error_action(exc) != "resume_switch":
                        raise
                    candidate = str(getattr(
                        exc, "_quizforge_ocr_credential", "") or "")
                    if not candidate or candidate in excluded:
                        raise
                    excluded.add(candidate)
                    mismatch = exc
        if action == "cooldown":
            _mark_cooldown(backend)
            result, _ = _run_once(
                backend, callback, fallback=fallback, preferred=credential)
            return result
        # 凭证失效/额度不足才排除原凭证；没有第二份时保留第一次的原始错误。
        try:
            result, _ = _run_once(
                backend, callback, fallback=fallback, exclude={credential})
            return result
        except _NoCredentialError:
            raise first


def inflight_counts() -> dict[str, list[int]]:
    """测试/诊断只返回计数，不暴露任何明文凭证。"""
    with _lock:
        return {backend: list(loads.values())
                for backend, loads in _inflight.items()}
