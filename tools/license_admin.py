"""QuizForge 发布者本地许可证台账。

台账只服务发布者，不进入桌面发行包。默认放在“文档/QuizForgePublisher”，也可用
``--publisher-dir`` 指定仓库外目录；签名私钥、客户记录和已签发文件都不得放进源码库。
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(TOOLS_DIR))

import device_identity
import license_manager
import license_signer


DEFAULT_PUBLISHER_DIR = Path.home() / "Documents" / "QuizForgePublisher"
PRIVATE_KEY_NAME = "keys/license_private.pem"
PUBLIC_KEY_NAME = "keys/license_public.pem"
DB_NAME = "publisher.db"


def _publisher_dir(value: str) -> Path:
    raw = value.strip() or os.environ.get("QUIZFORGE_PUBLISHER_DIR", "").strip()
    path = Path(raw) if raw else DEFAULT_PUBLISHER_DIR
    resolved = path.expanduser().resolve()
    try:
        inside_project = resolved.is_relative_to(PROJECT_DIR.resolve())
    except AttributeError:  # pragma: no cover - Python 3.9 兼容分支
        inside_project = PROJECT_DIR.resolve() in resolved.parents
    if inside_project:
        raise ValueError("发布者目录必须位于 QuizForge 源码仓库之外")
    return resolved


def _connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / DB_NAME)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS licenses (
            license_id TEXT PRIMARY KEY,
            licensee TEXT NOT NULL,
            edition TEXT NOT NULL,
            device_id TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            not_before TEXT NOT NULL,
            expires_at TEXT,
            updates_until TEXT,
            features_json TEXT NOT NULL,
            status TEXT NOT NULL,
            replaces_license_id TEXT,
            file_path TEXT NOT NULL,
            file_sha256 TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (replaces_license_id) REFERENCES licenses (license_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_licenses_licensee ON licenses (licensee)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_licenses_status ON licenses (status)"
    )
    connection.commit()
    return connection


def _key_paths(root: Path) -> tuple[Path, Path]:
    return root / PRIVATE_KEY_NAME, root / PUBLIC_KEY_NAME


def _adopt_key(args, root: Path) -> None:
    """把客户端已经采用的密钥对复制到仓库外台账；只用于不中断现有 beta。"""
    source_private = args.private_key_source.expanduser().resolve()
    source_public = args.public_key_source.expanduser().resolve()
    private_raw = source_private.read_bytes()
    public_raw = source_public.read_bytes()
    password = license_signer._password_from_args(args)
    private_key = license_signer._load_private(source_private, password)
    public_key = serialization.load_pem_public_key(public_raw)
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("来源公钥不是 Ed25519 类型")
    derived = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    supplied = public_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if derived != supplied:
        raise ValueError("来源私钥与公钥不匹配")

    target_private, target_public = _key_paths(root)
    if target_private.exists() or target_public.exists():
        raise FileExistsError("发布者目录已经存在密钥；为防误换钥，拒绝覆盖")
    target_private.parent.mkdir(parents=True, exist_ok=True)
    private_tmp = target_private.with_suffix(".tmp")
    public_tmp = target_public.with_suffix(".tmp")
    try:
        private_tmp.write_bytes(private_raw)
        public_tmp.write_bytes(public_raw)
        private_tmp.replace(target_private)
        public_tmp.replace(target_public)
    finally:
        private_tmp.unlink(missing_ok=True)
        public_tmp.unlink(missing_ok=True)


def _require_keys(root: Path) -> tuple[Path, Path]:
    private_key, public_key = _key_paths(root)
    if not private_key.is_file() or not public_key.is_file():
        raise ValueError(f"发布密钥尚未初始化，请先运行 init（目录：{root}）")
    return private_key, public_key


def _now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _record(
    connection: sqlite3.Connection,
    root: Path,
    document: dict,
    output: Path,
    *,
    replaces: str = "",
    note: str = "",
) -> None:
    payload = document["payload"]
    relative_output = output.resolve().relative_to(root)
    connection.execute(
        """
        INSERT INTO licenses (
            license_id, licensee, edition, device_id, issued_at, not_before,
            expires_at, updates_until, features_json, status,
            replaces_license_id, file_path, file_sha256, note, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
        """,
        (
            payload["license_id"], payload["licensee"], payload["edition"],
            payload["device_id"], payload["issued_at"], payload["not_before"],
            payload["expires_at"], payload["updates_until"],
            json.dumps(payload["features"], ensure_ascii=False),
            replaces or None, relative_output.as_posix(),
            sha256(output.read_bytes()).hexdigest(), note.strip(), _now_text(),
        ),
    )
    if replaces:
        connection.execute(
            "UPDATE licenses SET status = 'superseded' WHERE license_id = ?",
            (replaces,),
        )
    connection.commit()


def _find(connection: sqlite3.Connection, license_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM licenses WHERE license_id = ?", (license_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"台账中不存在许可证 {license_id}")
    return row


def _signer_args(
    args,
    private_key: Path,
    *,
    licensee: str,
    device_id: str,
    edition: str,
    features: list[str],
) -> argparse.Namespace:
    return argparse.Namespace(
        private_key=private_key,
        output=Path("unused.qflicense"),
        licensee=licensee,
        device_id=device_id,
        license_id="",
        edition=edition,
        issued=getattr(args, "issued", ""),
        not_before=getattr(args, "not_before", ""),
        valid_days=getattr(args, "valid_days", license_signer.DEFAULT_VALID_DAYS),
        expires=getattr(args, "expires", ""),
        perpetual=getattr(args, "perpetual", False),
        updates_until=getattr(args, "updates_until", ""),
        feature=features,
        password_env=getattr(args, "password_env", ""),
        no_password=getattr(args, "no_password", False),
    )


def _issue(
    args,
    root: Path,
    connection: sqlite3.Connection,
    *,
    licensee: str,
    device_id: str,
    edition: str,
    features: list[str],
    replaces: str = "",
) -> dict:
    private_key, _ = _require_keys(root)
    normalized_device = device_identity.normalize_device_id(device_id)
    signer_args = _signer_args(
        args,
        private_key,
        licensee=licensee.strip(),
        device_id=normalized_device,
        edition=edition.strip(),
        features=features,
    )
    document = license_signer.issue_license(signer_args)
    payload = document["payload"]
    output = (
        root / "licenses" / payload["issued_at"][:4]
        / f"{payload['issued_at']}_{payload['license_id']}.qflicense"
    )
    license_signer.write_license(document, output)
    _record(
        connection, root, document, output,
        replaces=replaces, note=getattr(args, "note", ""),
    )
    print(f"[OK] license_id: {payload['license_id']}")
    print(f"[OK] expires: {payload['expires_at'] or 'perpetual'}")
    print(f"[OK] file: {output}")
    return document


def _password_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--password-env", default="QUIZFORGE_SIGNING_PASSWORD",
        help="读取私钥密码的环境变量名（默认 QUIZFORGE_SIGNING_PASSWORD）",
    )
    parser.add_argument(
        "--no-password", action="store_true",
        help="仅限临时内测密钥；正式发行不要使用",
    )


def _duration_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--issued", default="")
    parser.add_argument("--not-before", default="")
    expiry = parser.add_mutually_exclusive_group()
    expiry.add_argument(
        "--valid-days", type=int, default=license_signer.DEFAULT_VALID_DAYS,
        help=f"有效自然日数（默认 {license_signer.DEFAULT_VALID_DAYS} 天，首日计入）",
    )
    expiry.add_argument("--expires", default="", help="固定到期日 YYYY-MM-DD")
    expiry.add_argument("--perpetual", action="store_true")
    parser.add_argument("--updates-until", default="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QuizForge 本地许可证台账")
    parser.add_argument(
        "--publisher-dir", default="",
        help="仓库外的发布者目录；默认 文档/QuizForgePublisher",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化发布密钥和 SQLite 台账")
    _password_arguments(init)

    adopt = sub.add_parser("adopt-key", help="把客户端已采用的密钥对接入仓库外台账")
    adopt.add_argument("--private-key-source", type=Path, required=True)
    adopt.add_argument("--public-key-source", type=Path, required=True)
    _password_arguments(adopt)

    issue = sub.add_parser("issue", help="签发新许可证（默认 7 天）")
    issue.add_argument("--licensee", required=True)
    issue.add_argument("--device-id", required=True)
    issue.add_argument("--edition", default="beta")
    issue.add_argument(
        "--feature", action="append", choices=sorted(license_manager.KNOWN_FEATURES)
    )
    issue.add_argument("--note", default="")
    _duration_arguments(issue)
    _password_arguments(issue)

    listing = sub.add_parser("list", help="列出台账记录")
    listing.add_argument(
        "--status", choices=["all", "active", "superseded", "revoked"], default="all"
    )
    listing.add_argument("--licensee", default="")

    show = sub.add_parser("show", help="查看单条记录（不输出许可证签名）")
    show.add_argument("license_id")

    verify = sub.add_parser("verify", help="验证一个已签发文件")
    verify.add_argument("license_file", type=Path)

    renew = sub.add_parser("renew", help="同设备续期并生成新许可证")
    renew.add_argument("license_id")
    renew.add_argument("--note", default="")
    _duration_arguments(renew)
    _password_arguments(renew)

    replace = sub.add_parser("replace", help="换机重签并将旧记录标记为已替代")
    replace.add_argument("license_id")
    replace.add_argument("--device-id", required=True)
    replace.add_argument("--note", default="")
    _duration_arguments(replace)
    _password_arguments(replace)

    revoke = sub.add_parser("revoke", help="仅在本地台账标记作废")
    revoke.add_argument("license_id")
    revoke.add_argument("--note", default="")
    return parser


def _run(args) -> int:
    root = _publisher_dir(args.publisher_dir)
    if args.command == "init":
        private_key, public_key = _key_paths(root)
        password = license_signer._password_from_args(args, confirm=True)
        license_signer.init_key(private_key, public_key, password)
        # sqlite3.Connection 的 with 只负责提交/回滚，不会关闭 Windows 文件句柄。
        # 发布工具是短进程，但测试和后续自动化会立即备份/移动台账，必须显式 close。
        with closing(_connect(root)):
            pass
        print(f"[OK] publisher directory: {root}")
        print(f"[OK] public key: {public_key}")
        print("[NEXT] 将公钥替换进 assets/license_public_key.pem 后重新构建客户端")
        if password is None:
            print("[WARN] 当前私钥未加密，仅适合临时内测")
        return 0
    if args.command == "adopt-key":
        _adopt_key(args, root)
        with closing(_connect(root)):
            pass
        print(f"[OK] existing key pair adopted: {root}")
        if args.no_password:
            print("[WARN] 当前私钥未加密，仅适合临时内测")
        return 0

    with closing(_connect(root)) as connection:
        if args.command == "issue":
            _issue(
                args, root, connection,
                licensee=args.licensee,
                device_id=args.device_id,
                edition=args.edition,
                features=args.feature or ["export"],
            )
            return 0

        if args.command == "list":
            clauses: list[str] = []
            values: list[str] = []
            if args.status != "all":
                clauses.append("status = ?")
                values.append(args.status)
            if args.licensee:
                clauses.append("licensee LIKE ?")
                values.append(f"%{args.licensee}%")
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = connection.execute(
                "SELECT license_id, licensee, status, expires_at, device_id "
                f"FROM licenses{where} ORDER BY created_at DESC",
                values,
            ).fetchall()
            print("license_id\tlicensee\tstatus\texpires_at\tdevice_id")
            for row in rows:
                print("\t".join(str(row[key] or "perpetual") for key in row.keys()))
            print(f"[OK] total: {len(rows)}")
            return 0

        if args.command == "show":
            row = _find(connection, args.license_id)
            value = dict(row)
            value["features"] = json.loads(value.pop("features_json"))
            print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        if args.command == "verify":
            raw = args.license_file.read_bytes()
            document = json.loads(raw.decode("utf-8"))
            payload = document.get("payload", {}) if isinstance(document, dict) else {}
            expected = str(payload.get("device_id", ""))
            state = license_manager.verify_bytes(
                raw, expected_device_id=expected, require_device=True
            )
            if not state.valid:
                raise ValueError(f"{state.summary}；{state.detail}")
            ledger = connection.execute(
                "SELECT status FROM licenses WHERE license_id = ?", (state.license_id,)
            ).fetchone()
            print(f"[OK] signature/date/device: valid")
            print(f"[OK] license_id: {state.license_id}")
            print(f"[INFO] ledger_status: {ledger['status'] if ledger else 'not_recorded'}")
            return 0

        if args.command in {"renew", "replace"}:
            old = _find(connection, args.license_id)
            if old["status"] != "active":
                raise ValueError("只有 active 记录可以续期或换机；请从当前有效记录操作")
            target_device = args.device_id if args.command == "replace" else old["device_id"]
            _issue(
                args, root, connection,
                licensee=old["licensee"],
                device_id=target_device,
                edition=old["edition"],
                features=json.loads(old["features_json"]),
                replaces=old["license_id"],
            )
            return 0

        if args.command == "revoke":
            _find(connection, args.license_id)
            connection.execute(
                "UPDATE licenses SET status = 'revoked', note = ? WHERE license_id = ?",
                (args.note.strip(), args.license_id),
            )
            connection.commit()
            print(f"[OK] ledger status: revoked ({args.license_id})")
            print("[WARN] 纯离线客户端无法立即收到作废状态，现有许可证仍会使用到到期日")
            return 0
    return 2


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return _run(args)
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
