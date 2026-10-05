"""三个来源目录的稳定文件监听与任务调度。"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from pathlib import Path

import config
import source_profiles
import source_settings
import source_versions
import task_store

logger = logging.getLogger(__name__)


_EXTS = {
    "good_question": {".png", ".jpg", ".jpeg", ".webp", ".bmp"},
    "good_paper": {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".docx"},
    "good_material": {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".docx"},
}

# worker 的签退哨兵必须是模块级唯一对象。曾经用空字符串，stop() 给正在执行
# 任务的 worker 投的那一个没人消费（worker 回到循环头就因为 _stop 已置位而
# 直接退出），残留到下一次 start() 后把新 worker 一击签退——表现是"监控开着、
# 永远不执行任务"，与"图片不转换"的观感完全一样。
_SENTINEL = object()


def _now() -> float:
    return time.time()


def _readable(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            stream.read(1)
        return True
    except OSError:
        return False


def is_stable_file(path: Path, previous: dict | None, *, now: float) -> tuple[bool, dict]:
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        # 文件在两次观察之间被删除/改名/拒绝访问：视为不稳定并丢弃旧观察。
        # 不让 OSError 抛进扫描线程——线程死了只会表现为"监控莫名不动"。
        return False, {}
    current = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
               "observed_at": float(now)}
    if previous and previous.get("size") == stat.st_size \
            and previous.get("mtime_ns") == stat.st_mtime_ns \
            and _readable(path):
        return True, current
    return False, current


def _is_inside(path: Path, root: Path) -> bool:
    """path 等于 root 或位于 root 之内（pathlib 在 Windows 上大小写不敏感）。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _target_directory(payload: dict, source: Path) -> Path:
    """转换产物的目标目录。

    用户在监视目录里建的子文件夹结构只在**原地替换**（输入=输出）下延续：
    产物写回源文件所在的子目录，与源文件的组织方式一一对应。输入输出不同
    时不做镜像——相对结构只对"原地"有定义，输出目录里统一平铺在根下。
    缺 input_dir 的旧任务快照退回平铺（保持旧行为）。
    """
    output_dir = Path(str(payload.get("output_dir") or "")).expanduser()
    input_raw = str(payload.get("input_dir") or "").strip()
    if not input_raw:
        return output_dir
    try:
        input_dir = Path(input_raw).resolve()
        if input_dir != output_dir.resolve():
            return output_dir
        rel_parent = source.resolve().relative_to(input_dir).parent
    except (OSError, ValueError):
        return output_dir
    if not rel_parent.parts:
        return output_dir
    return output_dir / rel_parent


class SourceIngestService:
    def __init__(self, profiles: dict[str, dict] | None = None, *, interval: float = 2.0,
                 worker_count: int = 1):
        self.profiles = profiles or source_settings.load_profiles()
        self.interval = max(0.1, float(interval))
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._paused: set[str] = set()
        self._observations: dict[tuple[str, str], dict] = {}
        self._worker_count = max(1, int(worker_count))
        # 运行时状态：
        # - _known 是"这个源指纹已经见过"的集合（已排队/已转换/失败/中断都算），
        #   扫描器只对从未见过的指纹建任务，绝不自动重放可能计过费的调用；
        # - _tasks 是 source 任务的内存镜像，面板与导航徽标读它，避免每 5 秒
        #   轮询都解析一次 10MB 级快照文件；
        # - _digests 按 (size, mtime_ns) 缓存文件摘要，稳定文件不重复整读。
        # 三者都懒加载：构造发生在 import 路径上，不做任何文件 IO。
        self._state_lock = threading.RLock()
        self._state_loaded = False
        self._known: set[str] = set()
        self._tasks: dict[str, dict] = {}
        self._digests: dict[tuple[str, str], tuple[int, int, str]] = {}

    # ------------------------------------------------------------------
    # 运行时状态
    # ------------------------------------------------------------------

    def _ensure_state(self) -> None:
        with self._state_lock:
            if self._state_loaded:
                return
            self._state_loaded = True
            for task_id, payload in task_store.load("source"):
                if isinstance(payload.get("source_key"), str):
                    self._known.add(payload["source_key"])
                self._tasks[str(task_id)] = dict(payload)
            for row in self._all_version_rows():
                key = row.get("source_key")
                if isinstance(key, str):
                    self._known.add(key)

    def _save_task(self, task_id: str, payload: dict) -> None:
        """任务快照与内存镜像必须同步更新（镜像给面板/徽标用）。"""
        task_store.save("source", task_id, payload)
        with self._state_lock:
            self._tasks[str(task_id)] = dict(payload)

    def tasks_snapshot(self) -> list[tuple[str, dict]]:
        self._ensure_state()
        with self._state_lock:
            return [(task_id, dict(payload))
                    for task_id, payload in self._tasks.items()]

    def unfinished_count(self) -> int:
        """未处理（计入导航红心）的任务数。

        committed 是正常终态；dismissed 是用户显式"忽略"过的失败/中断记录，
        也算已处理。失败/中断本身仍是未处理——面板要给用户一个明确动作
        （重试或忽略），红心就是提醒他还欠这个动作。
        """
        self._ensure_state()
        with self._state_lock:
            return sum(1 for payload in self._tasks.values()
                       if payload.get("status") not in ("committed", "dismissed"))

    # ------------------------------------------------------------------
    # 线程生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # 清掉 stop() 可能残留在队列里的哨兵，真实任务 id 原样保留给新 worker。
        leftovers = self._drain_queue()
        for item in leftovers:
            self._queue.put(item)
        self._thread = threading.Thread(target=self._run_scan, name="qf-source-scan",
                                        daemon=True)
        self._thread.start()
        self._workers = [threading.Thread(target=self._run_worker,
                                          name=f"qf-source-worker-{i}", daemon=True)
                         for i in range(self._worker_count)]
        for worker in self._workers:
            worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1.0, self.interval + 1))
        # 还没开工的任务不能静默丢在队列里：队列是内存的，而快照还写着 queued，
        # 面板会永远显示"排队中"。统一标成中断，由用户显式重试。
        for task_id in self._drain_queue():
            self._interrupt_queued(task_id)
        for _ in self._workers:
            self._queue.put(_SENTINEL)
        for worker in self._workers:
            worker.join(timeout=2)
        self._workers.clear()

    def pause(self, profile: str) -> None:
        self._paused.add(str(profile))

    def resume(self, profile: str) -> None:
        self._paused.discard(str(profile))

    def _drain_queue(self) -> list[str]:
        pending = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not _SENTINEL:
                pending.append(item)
        return pending

    def _interrupt_queued(self, task_id: str) -> None:
        """把还没开工的 queued 任务标成中断；已开工或已终结的不动。"""
        rows = dict(task_store.load("source"))
        payload = rows.get(task_id)
        if not payload or payload.get("status") != "queued":
            return
        self._save_task(task_id, dict(
            payload, status="interrupted",
            error="监控已停止，任务未执行；可在任务面板重试"))

    def _run_scan(self):
        while not self._stop.wait(self.interval):
            try:
                self.scan_once()
            except Exception:
                # 单轮扫描无论如何不能带走线程：线程静默死掉 = 监控长期不动。
                logger.exception("来源扫描失败（本轮跳过）")

    def _run_worker(self):
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                return
            try:
                self._execute(item)
            except Exception:
                logger.exception("来源任务执行异常 task=%s", item)

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------

    def scan_once(self) -> list[dict]:
        self._ensure_state()
        ledger = self._all_version_rows()
        version_keys = {str(row.get("source_key")) for row in ledger
                        if row.get("source_key")}
        version_paths = [Path(str(row["path"])) for row in ledger
                         if row.get("path")]
        queued: list[dict] = []
        for profile, row in self.profiles.items():
            if not row.get("enabled") or profile in self._paused:
                continue
            input_dir = Path(str(row.get("input_dir") or "")).expanduser()
            output_dir = Path(str(row.get("output_dir") or "")).expanduser()
            try:
                input_root = input_dir.resolve()
                output_root = output_dir.resolve()
                input_root.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            # 只有"输出目录严格位于输入目录之内"时才需要整树排除输出树。
            # 两者相等（默认的原地替换语义）绝不能整树排除——那会把全部输入
            # 挡在门外，正是"图片放进目录没有任何反应"的根因；原地替换下
            # 改由版本账本排除已产物。
            try:
                output_nested = bool(output_root.relative_to(input_root).parts)
            except ValueError:
                output_nested = False
            protected = self._protected_roots(input_root)
            for path in sorted(input_root.rglob("*")):
                try:
                    queued.extend(self._scan_file(
                        path, profile, row, input_root, output_root,
                        output_nested, protected, version_keys, version_paths))
                except OSError:
                    # 单个文件判定失败（扫描间隙被删除、被占用、权限不足）
                    # 不能让整轮扫描失败。
                    logger.warning("来源扫描跳过文件：%s", path.name)
        return queued

    def _protected_roots(self, input_root: Path) -> list[Path]:
        """返回位于输入树内的应用保留目录（资产、工作区、状态、回收站……）。

        输入目录可以自定义（甚至设为 vault 根），这些目录里的图片/文件一旦
        被当成源文件，就会出现"把 OCR 产物再喂给 OCR"的循环。
        """
        candidates: list[Path] = []
        for name in ("ASSETS_DIR", "SOURCE_WORKSPACE_DIR", "BANK_STATE_DIR",
                     "TRASH_DIR", "HANDOUTS_DIR"):
            raw = getattr(config, name, None)
            if raw:
                candidates.append(Path(raw))
        bank_dir = getattr(config, "BANK_DIR", None)
        if bank_dir:
            candidates.append(Path(bank_dir) / "_backups")
        out = []
        for raw in candidates:
            try:
                root = raw.resolve()
            except OSError:
                continue
            if _is_inside(root, input_root):
                out.append(root)
        return out

    def _scan_file(self, path: Path, profile: str, profile_row: dict,
                   input_root: Path, output_root: Path, output_nested: bool,
                   protected: list[Path], version_keys: set[str],
                   version_paths: list[Path]) -> list[dict]:
        if not path.is_file() or path.suffix.lower() not in _EXTS.get(profile, set()):
            return []
        try:
            rel = path.relative_to(input_root)
        except ValueError:
            return []
        if any(part.startswith(".") for part in rel.parts):
            # 隐藏文件与隐藏目录（.trash、各类临时件）一律不是输入。
            return []
        path_resolved = path.resolve()
        if output_nested and _is_inside(path_resolved, output_root):
            return []
        for root in protected:
            if _is_inside(path_resolved, root):
                return []
        for registered in version_paths:
            # 已登记版本的产物（.md 卡片、好卷/好资料的版本目录及其内文件）
            # 不是新输入；原地替换（输入=输出）全靠这一步防止产物回归队列。
            if path_resolved == registered or _is_inside(path_resolved, registered):
                return []
        key = (profile, str(path_resolved))
        stable, observation = is_stable_file(
            path_resolved, self._observations.get(key), now=_now())
        self._observations[key] = observation
        if not stable:
            return []
        digest = self._digest_for(key, path_resolved, observation)
        source_key = source_versions.version_key(profile, path_resolved, digest)
        with self._state_lock:
            if source_key in self._known or source_key in version_keys:
                return []
            task_id = uuid.uuid4().hex
            payload = {"profile": profile, "source": str(path_resolved),
                       "source_hash": digest, "source_key": source_key,
                       "output_dir": str(output_root), "status": "queued",
                       "created_at": _now(),
                       # 记下输入根：原地替换时产物要写回源文件所在的子目录，
                       # 保住用户在监视目录里建的分类结构（见 _target_directory）。
                       "input_dir": str(input_root),
                       "normalize_with_llm": bool(
                           profile_row.get("normalize_with_llm"))}
            self._save_task(task_id, payload)
            self._known.add(source_key)
        self._queue.put(task_id)
        return [{"task_id": task_id, **payload}]

    def _digest_for(self, key: tuple[str, str], path: Path,
                    observation: dict) -> str:
        cached = self._digests.get(key)
        if cached and cached[0] == observation.get("size") \
                and cached[1] == observation.get("mtime_ns"):
            return cached[2]
        digest = source_versions.sha256_file(path)
        self._digests[key] = (observation.get("size"),
                              observation.get("mtime_ns"), digest)
        return digest

    def retry(self, task_id: str) -> dict:
        self._ensure_state()
        rows = dict(task_store.load("source"))
        payload = rows.get(task_id)
        if not payload:
            raise ValueError("来源任务不存在")
        # dismissed 也允许重试：忽略只是把它移出红心，不代表这份文件永远
        # 不能转了——在"连已完成一起看"视图里恢复处理是它的唯一出口。
        if payload.get("status") not in {"failed", "interrupted", "dismissed"}:
            raise ValueError("任务当前不可重试")
        payload = dict(payload, status="queued", error="")
        self._save_task(task_id, payload)
        self._known.add(payload["source_key"])
        self._queue.put(task_id)
        return payload

    def remove(self, task_id: str) -> dict | None:
        """移除一条已完成的监控任务记录（产物与源文件都不动）。

        只允许 committed：失败/中断任务保留在面板里等重试或忽略。记录删除
        不影响去重——该指纹早已写进版本账本，扫描器不会因为记录消失而再转
        一遍。任务已不存在时返回 None（"让它消失"的两次点击结果一致）。
        """
        self._ensure_state()
        rows = dict(task_store.load("source"))
        payload = rows.get(task_id)
        if not payload:
            return None
        if payload.get("status") != "committed":
            raise ValueError("仅可移除已完成的任务记录")
        task_store.delete("source", task_id)
        with self._state_lock:
            self._tasks.pop(str(task_id), None)
        return payload

    def dismiss(self, task_id: str) -> dict | None:
        """忽略一条失败/中断的监控任务记录（移出红心计数与默认列表）。

        刻意**不删记录**：source_key 指纹靠任务快照在重启后重建 _known，
        删掉它，输入目录里内容未变的源文件会被扫描器当成新文件自动重放
        （可能重复消耗 OCR 额度，而这是设计上绝不允许的）。改成
        status="dismissed" 既保住了防重放账本，又让用户能把"不打算再处理"
        的失败记录从红心里清掉；要恢复处理，可在「连已完成一起看」视图点
        重试（retry 接受 dismissed）。任务不存在返回 None；其它状态拒绝
        （committed 走 remove）。
        """
        self._ensure_state()
        rows = dict(task_store.load("source"))
        payload = rows.get(task_id)
        if not payload:
            return None
        if payload.get("status") not in {"failed", "interrupted"}:
            raise ValueError("仅可忽略失败或已中断的任务记录")
        payload = dict(payload, status="dismissed")
        self._save_task(task_id, payload)
        return payload

    def _all_version_rows(self) -> list[dict]:
        path = Path(config.SOURCE_VERSIONS_PATH)
        if not path.exists():
            return []
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            return rows if isinstance(rows, list) else []
        except (OSError, ValueError):
            return []

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def _execute(self, task_id: str) -> None:
        self._ensure_state()
        rows = dict(task_store.load("source"))
        payload = rows.get(task_id)
        if not payload:
            return
        source = Path(payload["source"])
        workspace = Path(config.SOURCE_WORKSPACE_DIR) / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        record: dict | None = None
        try:
            if source_versions.sha256_file(source) != payload["source_hash"]:
                raise ValueError("源文件在排队后发生变化")
            self._save_task(task_id, dict(payload, status="converting"))
            profile = payload["profile"]
            fn = {"good_question": source_profiles.convert_good_question,
                  "good_paper": source_profiles.convert_good_paper,
                  "good_material": source_profiles.convert_good_material}[profile]
            row = self.profiles.get(profile) or {}
            normalize = bool(row.get("normalize_with_llm"))
            provider = None
            if normalize:
                # 只有勾选"大模型规范化"才解析识别模型；默认机械链路完全不碰
                # LLM 配置（provider=None 会回落 .env，绝不能拿它冒充 no-AI）。
                import providers
                provider = providers.resolve_active()
            result = fn(source, workspace, ocr_backend="mineru", engine="block",
                        provider=provider, normalize=normalize)
            # 识别层的软警告（多题、页码缺失、LLM 回退等）在转换结果里，但
            # committed 后 result 会被版本记录覆盖——抽到任务顶层字段保存，
            # 面板才有"需人工校对"可见。
            warnings = []
            if isinstance(result, dict) and isinstance(result.get("warnings"), list):
                warnings = [str(item) for item in result["warnings"]
                            if str(item).strip()]
            self._save_task(task_id, dict(payload, status="validating",
                                          result=result, warnings=warnings))
            base = Path(result["cards"][0]).stem if profile == "good_question" \
                else Path(source).stem
            # 原地替换时写回源文件所在子目录（保住用户的文件夹结构）；
            # 输入输出不同则平铺在输出根。目录必然已存在（源文件就在里面），
            # mkdir 只作纵深防御。
            target_dir = _target_directory(payload, source)
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            record = source_versions.reserve_version(
                target_dir, base,
                is_directory=profile != "good_question",
                source_key=payload["source_key"], profile=profile)
            manifest = source_profiles.build_output_manifest(profile, source, workspace)
            committed = source_versions.commit_outputs(record, workspace, manifest)
            self._save_task(task_id, dict(payload, status="committed",
                                          result=committed, warnings=warnings))
        except Exception as exc:
            if record is not None:
                # 未提交的预留不能留给下一次重试当"已存在版本"卡死；提交已完成
                # 的预留 cancel 会拒绝，属正常，静默忽略。
                try:
                    source_versions.cancel_reservation(record.get("version_id"))
                except Exception:
                    pass
            logger.exception("来源任务失败 task=%s", task_id)
            # 面板要把失败原因展示给用户（"尚未配置大模型""跨盘"这类必须可见），
            # 不能再只存一个类名。快照本就是本机运行态文件，与 library 任务
            # 的 error 口径一致；超长错误截断，避免撑爆快照。
            self._save_task(task_id, dict(
                payload, status="failed",
                error=f"{type(exc).__name__}: {str(exc)[:500]}"))


_default_service: SourceIngestService | None = None


def default_service() -> SourceIngestService:
    """GUI、Agent 与 CLI 共用的进程级监听服务。"""
    global _default_service
    if _default_service is None:
        _default_service = SourceIngestService()
    return _default_service
