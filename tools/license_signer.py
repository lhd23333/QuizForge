"""QuizForge 发布者离线许可证签发工具；本文件不会进入桌面发行包。"""

from __future__ import annotations

import argparse
from base64 import b64encode
from datetime import date, timedelta
from getpass import getpass
import json
import os
from pathlib import Path
import sys
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import device_identity
import license_manager


DEFAULT_VALID_DAYS = 7


def _password_from_args(args, *, confirm: bool = False) -> bytes | None:
    if getattr(args, "no_password", False):
        return None
    env_name = str(getattr(args, "password_env", "") or "").strip()
    if env_name:
        value = os.environ.get(env_name)
        if value is None:
            raise ValueError(f"环境变量 {env_name} 不存在")
        if not value:
            raise ValueError("私钥密码不能为空")
        return value.encode("utf-8")
    first = getpass("私钥密码：")
    if not first:
        raise ValueError("私钥密码不能为空；内测临时密钥可显式使用 --no-password")
    if confirm and first != getpass("再次输入私钥密码："):
        raise ValueError("两次输入的私钥密码不一致")
    return first.encode("utf-8")


def init_key(private_path: Path, public_path: Path, password: bytes | None) -> None:
    if private_path.exists() or public_path.exists():
        raise FileExistsError("私钥或公钥文件已经存在；为防止旧许可证全部失效，拒绝覆盖")
    key = Ed25519PrivateKey.generate()
    encryption = (
        serialization.BestAvailableEncryption(password)
        if password else serialization.NoEncryption()
    )
    private_raw = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        encryption,
    )
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(private_raw)
    public_path.write_bytes(public_raw)


def _load_private(path: Path, password: bytes | None) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=password)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("私钥不是 Ed25519 类型")
    return key


def _parse_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD")
    return parsed


def _license_dates(args) -> tuple[str, str, str | None, str | None]:
    issued_day = _parse_date(args.issued, "--issued") if args.issued else date.today()
    effective_day = (
        _parse_date(args.not_before, "--not-before")
        if args.not_before else issued_day
    )
    if effective_day < issued_day:
        raise ValueError("--not-before 不能早于 --issued")

    if getattr(args, "perpetual", False):
        expires_day = None
    elif args.expires:
        expires_day = _parse_date(args.expires, "--expires")
    else:
        valid_days = int(args.valid_days)
        if valid_days < 1:
            raise ValueError("--valid-days 必须至少为 1")
        # 按自然日计算且首日计入：8 月 12 日签发 7 天证，到期日为 8 月 18 日。
        expires_day = effective_day + timedelta(days=valid_days - 1)
    if expires_day is not None and expires_day < effective_day:
        raise ValueError("到期日不能早于生效日")

    if args.updates_until:
        updates_day = _parse_date(args.updates_until, "--updates-until")
        if updates_day < issued_day:
            raise ValueError("更新有效期不能早于签发日")
    else:
        updates_day = expires_day
    return (
        issued_day.isoformat(),
        effective_day.isoformat(),
        expires_day.isoformat() if expires_day else None,
        updates_day.isoformat() if updates_day else None,
    )


def issue_license(args) -> dict:
    """签发一份绑定指定设备请求码的 schema 2 许可证。"""
    password = _password_from_args(args)
    key = _load_private(args.private_key, password)
    issued, not_before, expires, updates_until = _license_dates(args)
    bound_device = device_identity.normalize_device_id(args.device_id)
    payload = {
        "product": license_manager.PRODUCT_ID,
        "license_id": args.license_id or str(uuid.uuid4()),
        "licensee": args.licensee.strip(),
        "edition": args.edition.strip(),
        "device_id": bound_device,
        "issued_at": issued,
        "not_before": not_before,
        "expires_at": expires,
        "updates_until": updates_until,
        "features": sorted(set(args.feature or ["export"])),
    }
    if not payload["licensee"] or not payload["edition"]:
        raise ValueError("licensee 和 edition 不能为空")
    signature = key.sign(license_manager.canonical_payload(payload))
    document = {
        "schema": license_manager.LICENSE_SCHEMA,
        "payload": payload,
        "signature": b64encode(signature).decode("ascii"),
    }
    # 发布者电脑不必与客户设备相同，显式传入目标设备码完成签发后自检。
    state = license_manager.verify_document(
        document, expected_device_id=bound_device, require_device=True
    )
    if not state.valid:
        raise ValueError(f"生成的许可证未通过自检：{state.summary}；{state.detail}")
    return document


def write_license(document: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def add_issue_arguments(issue: argparse.ArgumentParser) -> None:
    issue.add_argument("--private-key", type=Path, required=True)
    issue.add_argument("--output", type=Path, required=True)
    issue.add_argument("--licensee", required=True)
    issue.add_argument("--device-id", required=True, help="客户设置页显示的设备请求码")
    issue.add_argument("--license-id", default="")
    issue.add_argument("--edition", default="beta")
    issue.add_argument("--issued", default="")
    issue.add_argument("--not-before", default="")
    expiry = issue.add_mutually_exclusive_group()
    expiry.add_argument(
        "--valid-days", type=int, default=DEFAULT_VALID_DAYS,
        help=f"有效自然日数，首日计入（默认 {DEFAULT_VALID_DAYS} 天）",
    )
    expiry.add_argument("--expires", default="", help="固定到期日 YYYY-MM-DD")
    expiry.add_argument("--perpetual", action="store_true", help="永久有效")
    issue.add_argument("--updates-until", default="")
    issue.add_argument(
        "--feature", action="append", choices=sorted(license_manager.KNOWN_FEATURES)
    )
    issue.add_argument("--password-env", default="")
    issue.add_argument("--no-password", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QuizForge 离线许可证签发工具")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-key", help="生成一次性的 Ed25519 签名密钥对")
    init.add_argument("--private-key", type=Path, required=True)
    init.add_argument("--public-key", type=Path, required=True)
    init.add_argument("--password-env", default="")
    init.add_argument("--no-password", action="store_true")

    issue = sub.add_parser("issue", help="签发一份绑定设备的 .qflicense")
    add_issue_arguments(issue)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "init-key":
            password = _password_from_args(args, confirm=True)
            init_key(args.private_key.resolve(), args.public_key.resolve(), password)
            print(f"[OK] private key: {args.private_key.resolve()}")
            print(f"[OK] public key: {args.public_key.resolve()}")
            if password is None:
                print("[WARN] beta key is not password-protected; do not use it for paid releases")
            return 0
        document = issue_license(args)
        write_license(document, args.output)
        print(f"[OK] license: {args.output.resolve()}")
        print(f"[OK] expires: {document['payload']['expires_at'] or 'perpetual'}")
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
