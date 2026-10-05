"""来源文件哈希与输出版本登记。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

import config

_lock = threading.RLock()
logger = logging.getLogger(__name__)


class SourceVersionsCorruptError(RuntimeError):
    """版本账本无法安全读取；写入必须停止以保全原件。"""


def _quarantine_corrupt(path: Path, reason: str) -> None:
    backup = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
    try:
        shutil.copy2(path, backup)
    except OSError:
        logger.error("source versions corrupt; preservation failed (%s)", reason)
        raise SourceVersionsCorruptError("来源版本账本损坏，已阻止写入") from None
    logger.error("source versions corrupt; evidence copy created (%s)", reason)
    raise SourceVersionsCorruptError("来源版本账本损坏，已阻止写入")


def _read() -> list[dict]:
    path = Path(config.SOURCE_VERSIONS_PATH)
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _quarantine_corrupt(path, "invalid_json")
    except OSError:
        logger.error("source versions unreadable; writes blocked")
        raise SourceVersionsCorruptError("来源版本账本无法读取，已阻止写入") from None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        _quarantine_corrupt(path, "invalid_schema")
    return rows


def _write(rows: list[dict]) -> None:
    path = Path(config.SOURCE_VERSIONS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(rows, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def version_key(profile: str, source_path: Path, source_hash: str) -> str:
    canonical = "\0".join((
        str(profile).strip(),
        os.path.normcase(str(Path(source_path).expanduser().resolve())),
        str(source_hash).lower(),
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _infer_profile(output: Path) -> str:
    # Legacy callers did not pass profile; use the matching configured output path.
    import source_settings

    matches = [name for name, row in source_settings.load_profiles().items()
               if Path(row["output_dir"]).resolve() == output]
    if len(matches) > 1:
        raise ValueError("输出目录对应多个来源类型，请显式指定 profile")
    return matches[0] if matches else output.name


def reserve_version(output_dir: Path, base_name: str, *, is_directory: bool,
                    source_key: str, profile: str | None = None) -> dict:
    output = Path(output_dir).expanduser().resolve()
    if not output.is_dir():
        raise ValueError("输出目录不存在")
    base = Path(str(base_name)).name.strip()
    if not base or base in {".", ".."}:
        raise ValueError("版本名称无效")
    if profile is None:
        profile = _infer_profile(output)
    else:
        profile = str(profile).strip()
        if not profile:
            raise ValueError("来源类型不能为空")
    with _lock:
        rows = _read()
        # 幂等匹配只认三种"真实存在过"的状态。dismissed/interrupted 等标记行
        # 不能在这条路上返回，否则用户显式重试时 commit_version 会因为
        # "仅可提交有效的版本预留"永久卡死。
        dangling = None
        for row in rows:
            if row.get("source_key") != source_key \
                    or row.get("status") not in {"reserved", "committed",
                                                 "recycle_pending"}:
                continue
            if row.get("status") == "reserved" \
                    and Path(row.get("path") or "").exists():
                # 提交在"预留已登记、目标已存在"之间中断（发布成功但账本未写成，
                # 或发布前崩溃留下同名目标）。这条预留永远不会被提交，就地自愈
                # 删除，按新版本重新分配序号，避免同一指纹永久卡死。
                dangling = row
                break
            return dict(row)
        if dangling is not None:
            rows.remove(dangling)
            _write(rows)
        n = 1
        while True:
            name = base if n == 1 else f"{base}_{n}"
            candidate = output / (name if is_directory else f"{name}.md")
            if not candidate.exists() and not any(
                    Path(row.get("path", "")).resolve() == candidate
                    for row in rows if row.get("status") != "recycled"):
                break
            n += 1
        row = {
            "version_id": uuid.uuid4().hex,
            "profile": profile,
            "source_key": str(source_key),
            "path": str(candidate),
            "is_directory": bool(is_directory),
            "status": "reserved",
        }
        rows.append(row)
        _write(rows)
        return dict(row)


def commit_version(record: dict, manifest: dict) -> dict:
    if not isinstance(record, dict) or not record.get("version_id"):
        raise ValueError("版本预留记录无效")
    if not isinstance(manifest, dict):
        raise ValueError("版本清单必须是对象")
    with _lock:
        rows = _read()
        for row in rows:
            if row.get("version_id") == record["version_id"]:
                if row.get("status") == "committed":
                    return dict(row)
                if row.get("status") != "reserved":
                    raise ValueError("仅可提交有效的版本预留")
                row.update({"manifest": manifest, "status": "committed"})
                _write(rows)
                return dict(row)
    raise ValueError("版本预留记录不存在")


def validate_manifest(manifest: dict) -> dict:
    """验证输出清单的结构；文件哈希在 staged 根目录下再次核对。"""
    if not isinstance(manifest, dict):
        raise ValueError("版本清单必须是对象")
    source = manifest.get("source_path")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("版本清单缺少源文件")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("版本清单缺少输出文件")
    clean = []
    seen_paths = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("版本清单文件项无效")
        raw_path = item.get("path")
        digest = str(item.get("sha256") or "").lower()
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("版本清单路径无效")
        relative = Path(raw_path.replace("\\", "/"))
        if relative.is_absolute() or relative.name in {"", ".", ".."}:
            raise ValueError("版本清单不能包含绝对路径")
        if ".." in relative.parts:
            raise ValueError("版本清单路径越界")
        normalized_path = relative.as_posix()
        if normalized_path in seen_paths:
            raise ValueError("版本清单包含重复路径")
        seen_paths.add(normalized_path)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("版本清单哈希无效")
        clean.append({"path": normalized_path, "sha256": digest})
    return {**manifest, "source_path": str(Path(source)), "files": clean}


def _verify_tree(root: Path, files: list[dict]) -> None:
    """逐一复核清单文件存在且哈希一致（符号链接直接拒绝）。"""
    for item in files:
        file_path = root / item["path"]
        if (file_path.is_symlink() or not file_path.is_file()
                or sha256_file(file_path) != item["sha256"]):
            raise ValueError(f"输出文件校验失败: {item['path']}")


def _publish_item(staged_item: Path, target: Path, files: list[dict], *,
                  is_directory: bool, version_id: str) -> None:
    """把暂存产物发布到目标路径；跨卷安全，最终发布仍原子。

    暂存工作区与输出目录可能位于不同磁盘（桌面版运行态在 C 盘、题库常在 D
    盘），Windows 的 ``os.replace`` 跨卷会直接 ``WinError 17``。这里先把产物
    复制到目标父目录内的点开头隐藏临时名（同卷），复验哈希后 ``os.replace``
    到最终名——复制期间崩溃只会留下可识别的临时件，不会出现半个公开版本。
    """
    parent = target.parent
    tmp = parent / f".{target.name}.qf-tmp-{version_id}"
    # 只清理带本目标名前缀的旧残留（上次中断留下），不碰其他版本的临时件。
    for leftover in parent.glob(f".{target.name}.qf-tmp-*"):
        if leftover == tmp:
            continue
        try:
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover)
            else:
                leftover.unlink()
        except OSError:
            pass
    try:
        if tmp.is_dir() and not tmp.is_symlink():
            shutil.rmtree(tmp)
        elif tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        if is_directory:
            shutil.copytree(staged_item, tmp)
            # 目录清单 path 的第一段就是版本目录名，副本内要去掉这一段。
            for item in files:
                relative = Path(*Path(item["path"]).parts[1:])
                file_path = tmp / relative
                if (file_path.is_symlink() or not file_path.is_file()
                        or sha256_file(file_path) != item["sha256"]):
                    raise ValueError(f"输出文件复制后校验失败: {item['path']}")
        else:
            shutil.copy2(staged_item, tmp)
            item = files[0]
            if (tmp.is_symlink() or not tmp.is_file()
                    or sha256_file(tmp) != item["sha256"]):
                raise ValueError(f"输出文件复制后校验失败: {item['path']}")
        # 同一父目录内的 os.replace 必然同卷，保持发布动作本身的原子性。
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                if tmp.is_dir() and not tmp.is_symlink():
                    shutil.rmtree(tmp)
                else:
                    tmp.unlink()
            except OSError:
                pass


def commit_outputs(record: dict, staged_output: Path, manifest: dict) -> dict:
    """校验暂存输出、提交版本，再把源文件送入系统回收站。"""
    import source_recycle

    clean = validate_manifest(manifest)
    staged = Path(staged_output)
    if not staged.exists():
        raise FileNotFoundError(str(staged))
    _verify_tree(staged, clean["files"])
    source = Path(clean["source_path"])
    if not source.is_file():
        raise FileNotFoundError(str(source))
    target = Path(record.get("path") or "")
    if not target or target.exists():
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    if bool(record.get("is_directory")):
        top_levels = {Path(item["path"]).parts[0] for item in clean["files"]}
        if len(top_levels) != 1:
            raise ValueError("目录版本清单必须只有一个顶层目录")
        staged_item = staged / next(iter(top_levels))
        if not staged_item.is_dir() or staged_item.is_symlink():
            raise ValueError("目录版本输出根无效")
    else:
        if len(clean["files"]) != 1:
            raise ValueError("单卡版本必须只有一个输出文件")
        staged_item = staged / clean["files"][0]["path"]
        if staged_item.is_symlink() or not staged_item.is_file():
            raise ValueError("单卡版本输出无效")
    _publish_item(staged_item, target, clean["files"],
                  is_directory=bool(record.get("is_directory")),
                  version_id=str(record.get("version_id") or "v"))
    committed = commit_version(record, clean)
    try:
        source_recycle.send_to_recycle_bin(source)
    except Exception:
        return mark_recycle_pending(committed["version_id"], "SOURCE_RECYCLE_FAILED")
    return committed


def mark_recycle_pending(version_id: str, error: str) -> dict:
    with _lock:
        rows = _read()
        for row in rows:
            if row.get("version_id") == version_id:
                if row.get("status") != "committed":
                    raise ValueError("仅可标记已提交版本的回收失败")
                row["status"] = "recycle_pending"
                # 外部错误文本可能带本地路径或凭据，只留受控错误码。
                row["recycle_error"] = "SOURCE_RECYCLE_FAILED"
                if error:
                    logger.warning("source recycle failed; details omitted")
                _write(rows)
                return dict(row)
    raise ValueError("版本记录不存在")


def cancel_reservation(version_id: str) -> bool:
    """只释放仍未提交且输出目标尚不存在的预留。"""
    with _lock:
        rows = _read()
        for index, row in enumerate(rows):
            if row.get("version_id") != version_id:
                continue
            if row.get("status") != "reserved":
                raise ValueError("仅可释放未提交的版本预留")
            target = Path(row.get("path", ""))
            if target.exists():
                raise ValueError("版本目标已存在，不能释放预留")
            del rows[index]
            _write(rows)
            return True
    return False


def list_versions(profile: str | None = None) -> list[dict]:
    with _lock:
        rows = _read()
        return [
            dict(row) for row in rows
            if row.get("status") == "committed"
            and (profile is None or row.get("profile") == profile)
        ]


def retry_recycle(version_id: str) -> dict:
    """只重试源文件回收，不重新生成或分配版本。"""
    import source_recycle

    with _lock:
        rows = _read()
        target = None
        for row in rows:
            if row.get("version_id") == version_id:
                target = row
                break
        if target is None:
            raise ValueError("版本记录不存在")
        if target.get("status") == "committed":
            return dict(target)
        if target.get("status") != "recycle_pending":
            raise ValueError("版本不在待回收状态")
        manifest = target.get("manifest") or {}
        source = Path(str(manifest.get("source_path") or ""))
    if source.exists():
        source_recycle.send_to_recycle_bin(source)
    with _lock:
        rows = _read()
        for row in rows:
            if row.get("version_id") == version_id:
                row["status"] = "committed"
                row.pop("recycle_error", None)
                _write(rows)
                return dict(row)
    raise ValueError("版本记录不存在")
