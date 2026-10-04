"""三个来源目录的稳定文件监听与任务调度。"""

from __future__ import annotations

import hashlib
import os
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


_EXTS = {
    "good_question": {".png", ".jpg", ".jpeg", ".webp", ".bmp"},
    "good_paper": {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".docx"},
    "good_material": {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".docx"},
}


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
    stat = path.stat()
    current = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
               "observed_at": float(now)}
    if previous and previous.get("size") == stat.st_size \
            and previous.get("mtime_ns") == stat.st_mtime_ns \
            and _readable(path):
        return True, current
    return False, current


class SourceIngestService:
    def __init__(self, profiles: dict[str, dict] | None = None, *, interval: float = 2.0,
                 worker_count: int = 1):
        self.profiles = profiles or source_settings.load_profiles()
        self.interval = max(0.1, float(interval))
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._paused: set[str] = set()
        self._observations: dict[tuple[str, str], dict] = {}
        self._active: set[str] = set()
        self._worker_count = max(1, int(worker_count))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
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
        for _ in self._workers:
            self._queue.put("")
        for worker in self._workers:
            worker.join(timeout=2)
        self._workers.clear()

    def pause(self, profile: str) -> None:
        self._paused.add(str(profile))

    def resume(self, profile: str) -> None:
        self._paused.discard(str(profile))

    def scan_once(self) -> list[dict]:
        queued = []
        for profile, row in self.profiles.items():
            if not row.get("enabled") or profile in self._paused:
                continue
            input_dir = Path(row["input_dir"]).expanduser()
            output_dir = Path(row["output_dir"]).expanduser()
            try:
                input_root = input_dir.resolve()
                output_root = output_dir.resolve()
                input_root.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            for path in sorted(input_root.rglob("*")):
                if not path.is_file() or path.name.startswith(".") \
                        or path.suffix.lower() not in _EXTS.get(profile, set()):
                    continue
                try:
                    path_resolved = path.resolve()
                    path_resolved.relative_to(output_root)
                    continue
                except ValueError:
                    pass
                except OSError:
                    continue
                key = (profile, str(path_resolved))
                stable, observation = is_stable_file(
                    path_resolved, self._observations.get(key), now=_now())
                self._observations[key] = observation
                if not stable:
                    continue
                digest = source_versions.sha256_file(path_resolved)
                source_key = source_versions.version_key(profile, path_resolved, digest)
                if source_key in self._active:
                    continue
                if any(item.get("source_key") == source_key
                       for item in self._all_version_rows()):
                    continue
                task_id = uuid.uuid4().hex
                payload = {"profile": profile, "source": str(path_resolved),
                           "source_hash": digest, "source_key": source_key,
                           "output_dir": str(output_root), "status": "queued",
                           "created_at": _now()}
                task_store.save("source", task_id, payload)
                self._active.add(source_key)
                self._queue.put(task_id)
                queued.append({"task_id": task_id, **payload})
        return queued

    def retry(self, task_id: str) -> dict:
        rows = dict(task_store.load("source"))
        payload = rows.get(task_id)
        if not payload:
            raise ValueError("来源任务不存在")
        if payload.get("status") not in {"failed", "interrupted"}:
            raise ValueError("任务当前不可重试")
        payload = dict(payload, status="queued", error="")
        task_store.save("source", task_id, payload)
        self._active.add(payload["source_key"])
        self._queue.put(task_id)
        return payload

    def _all_version_rows(self) -> list[dict]:
        path = Path(config.SOURCE_VERSIONS_PATH)
        if not path.exists():
            return []
        try:
            import json
            rows = json.loads(path.read_text(encoding="utf-8"))
            return rows if isinstance(rows, list) else []
        except (OSError, ValueError):
            return []

    def _run_scan(self):
        while not self._stop.wait(self.interval):
            self.scan_once()

    def _run_worker(self):
        while not self._stop.is_set():
            task_id = self._queue.get()
            if not task_id:
                return
            self._execute(task_id)

    def _execute(self, task_id: str) -> None:
        rows = dict(task_store.load("source"))
        payload = rows.get(task_id)
        if not payload:
            return
        source = Path(payload["source"])
        workspace = Path(config.SOURCE_WORKSPACE_DIR) / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            if source_versions.sha256_file(source) != payload["source_hash"]:
                raise ValueError("源文件在排队后发生变化")
            task_store.save("source", task_id, dict(payload, status="converting"))
            profile = payload["profile"]
            fn = {"good_question": source_profiles.convert_good_question,
                  "good_paper": source_profiles.convert_good_paper,
                  "good_material": source_profiles.convert_good_material}[profile]
            result = fn(source, workspace, ocr_backend="mineru", engine="block")
            task_store.save("source", task_id, dict(payload, status="validating",
                                                     result=result))
            base = Path(result["cards"][0]).stem if profile == "good_question" \
                else Path(source).stem
            record = source_versions.reserve_version(
                Path(payload["output_dir"]), base,
                is_directory=profile != "good_question", source_key=payload["source_key"],
                profile=profile)
            manifest = source_profiles.build_output_manifest(profile, source, workspace)
            committed = source_versions.commit_outputs(record, workspace, manifest)
            task_store.save("source", task_id,
                            dict(payload, status="committed", result=committed))
        except Exception as exc:
            task_store.save("source", task_id,
                            dict(payload, status="failed", error=type(exc).__name__))
        finally:
            self._active.discard(payload.get("source_key", ""))
