"""Inspect, back up, edit, and restore Wuwa Tappo's local count files."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any, Sequence
import uuid


ENVIRONMENT = "cn-cn-prod"
MAX_COUNT = (1 << 53) - 1
MAX_FILE_SIZE = 1_048_576
PROJECT_ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent.parent)


class SaveError(Exception):
    """A save file or requested operation cannot be handled safely."""


@dataclass(frozen=True)
class SaveSpec:
    filename: str
    xor_key: bytes
    count_field: str
    label: str


PENDING = SaveSpec(
    "key_press_count_cn-cn-prod.dat",
    b"kuro-mc-keypress-store-v1",
    "t",
    "待上报增量(t)",
)
TOTAL = SaveSpec(
    "cumulative_click_cn-cn-prod.dat",
    b"kuro-mc-cumulative-click-v1",
    "count",
    "本地累计快照(count)",
)
SPECS = {item.filename: item for item in (PENDING, TOTAL)}


def default_data_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SaveError("未找到 APPDATA；请使用 --data-dir 指定存档目录。")
    return Path(appdata) / "com.kuro.wuwatappo.cn"


def checked_count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise SaveError(f"计数必须是 0 到 {MAX_COUNT} 之间的整数。")
    return value


def count_argument(value: str) -> int:
    try:
        return checked_count(int(value))
    except (ValueError, SaveError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SaveError("JSON 包含重复字段。")
        result[key] = value
    return result


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise SaveError("文件格式无法识别，未进行修改。") from exc


def xor_bytes(raw: bytes, key: bytes) -> bytes:
    if not key:
        raise SaveError("XOR 密钥不能为空。")
    return bytes(value ^ key[index % len(key)] for index, value in enumerate(raw))


def decode_save(raw: bytes, spec: SaveSpec) -> dict[str, Any]:
    if not 0 < len(raw) <= MAX_FILE_SIZE:
        raise SaveError(f"{spec.filename} 的文件大小异常。")
    record = _parse_json(xor_bytes(raw, spec.xor_key))
    if not isinstance(record, dict) or record.get("environment") != ENVIRONMENT:
        raise SaveError(f"{spec.filename} 不是受支持的 {ENVIRONMENT} 存档。")
    checked_count(record.get(spec.count_field))
    if spec == TOTAL and (
        not isinstance(record.get("cuid"), str) or not record["cuid"]
    ):
        raise SaveError(f"{spec.filename} 缺少必需的用户关联字段。")
    return record


def encode_save(record: dict[str, Any], spec: SaveSpec) -> bytes:
    try:
        plain = json.dumps(
            record, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SaveError("存档包含无法编码的字段。") from exc
    encoded = xor_bytes(plain, spec.xor_key)
    decode_save(encoded, spec)
    return encoded


def reject_links(path: Path) -> None:
    """Reject symlinks and Windows reparse points in every existing ancestor."""
    current = path.absolute()
    for candidate in (current, *current.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise SaveError(f"拒绝通过符号链接或重解析点访问文件：{candidate}")


def read_regular_file(path: Path) -> bytes:
    reject_links(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_SIZE:
        raise SaveError(f"文件类型或大小不受支持：{path.name}")
    with path.open("rb") as source:
        raw = source.read(MAX_FILE_SIZE + 1)
    if len(raw) > MAX_FILE_SIZE:
        raise SaveError(f"文件过大：{path.name}")
    return raw


def app_running() -> bool:
    if os.name != "nt":
        raise SaveError("写入操作仅支持 Windows。")
    tasklist = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tasklist.exe"
    result = subprocess.run(
        [str(tasklist), "/FI", "IMAGENAME eq WuwaTappo.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise SaveError("无法检查应用进程；为避免存档被覆盖，已停止写入。")
    return any(
        row and row[0].casefold() == "wuwatappo.exe"
        for row in csv.reader(io.StringIO(result.stdout))
    )


def require_app_closed() -> None:
    if app_running():
        raise SaveError("请先从托盘完全退出 WuwaTappo，再执行 --apply。")


def atomic_write(path: Path, raw: bytes) -> None:
    reject_links(path)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        reject_links(path)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def create_backup(files: dict[str, bytes], backup_root: Path) -> Path:
    reject_links(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = backup_root / f"{stamp}-{uuid.uuid4().hex[:8]}"
    destination.mkdir(mode=0o700)
    manifest: dict[str, Any] = {
        "version": 1,
        "environment": ENVIRONMENT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {},
    }
    for filename, raw in files.items():
        if filename not in SPECS:
            raise SaveError("备份包含未知文件名。")
        decode_save(raw, SPECS[filename])
        atomic_write(destination / filename, raw)
        manifest["files"][filename] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }
    atomic_write(
        destination / "manifest.json",
        json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    return destination


def load_backup(backup_dir: Path) -> dict[str, bytes]:
    manifest = _parse_json(read_regular_file(backup_dir / "manifest.json"))
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("version")) is not int
        or manifest["version"] != 1
        or manifest.get("environment") != ENVIRONMENT
        or not isinstance(manifest.get("files"), dict)
        or not manifest["files"]
    ):
        raise SaveError("备份清单格式不受支持。")
    files: dict[str, bytes] = {}
    for filename, entry in manifest["files"].items():
        if filename not in SPECS or not isinstance(entry, dict):
            raise SaveError("备份清单含未知文件名或路径。")
        raw = read_regular_file(backup_dir / filename)
        if (
            type(entry.get("size")) is not int
            or entry["size"] != len(raw)
            or entry.get("sha256") != hashlib.sha256(raw).hexdigest()
        ):
            raise SaveError(f"备份校验失败：{filename}")
        decode_save(raw, SPECS[filename])
        files[filename] = raw
    return files


def apply_files(
    data_dir: Path, replacements: dict[str, bytes], backup_root: Path
) -> Path:
    """Back up and replace supported files, rolling back a partial failure."""
    if not replacements or not set(replacements).issubset(SPECS):
        raise SaveError("没有受支持的待写入文件。")
    require_app_closed()
    originals: dict[str, bytes] = {}
    for filename, raw in replacements.items():
        decode_save(raw, SPECS[filename])
        originals[filename] = read_regular_file(data_dir / filename)
        decode_save(originals[filename], SPECS[filename])
    backup = create_backup(originals, backup_root)
    for filename, original in originals.items():
        if read_regular_file(data_dir / filename) != original:
            raise SaveError("存档在备份期间发生变化；已停止写入，请确认应用完全退出。")
    require_app_closed()
    changed: list[str] = []
    try:
        for filename, raw in replacements.items():
            atomic_write(data_dir / filename, raw)
            changed.append(filename)
            if read_regular_file(data_dir / filename) != raw:
                raise SaveError(f"写入后校验失败：{filename}")
    except BaseException as exc:
        failures: list[str] = []
        for filename in reversed(changed):
            try:
                atomic_write(data_dir / filename, originals[filename])
            except Exception:
                failures.append(filename)
        if failures:
            raise SaveError(
                f"写入失败且部分文件无法回滚：{', '.join(failures)}；原始备份：{backup}"
            ) from exc
        raise SaveError(f"写入失败，已回滚已修改文件；原始备份：{backup}") from exc
    return backup


def prepare_set(
    data_dir: Path, pending: int | None, total: int | None
) -> dict[str, bytes]:
    replacements: dict[str, bytes] = {}
    for spec, value in ((PENDING, pending), (TOTAL, total)):
        if value is None:
            continue
        checked_count(value)
        record = decode_save(read_regular_file(data_dir / spec.filename), spec)
        record[spec.count_field] = value
        replacements[spec.filename] = encode_save(record, spec)
    if not replacements:
        raise SaveError("请至少指定 --pending 或 --total。")
    return replacements


def print_preview(data_dir: Path, replacements: dict[str, bytes]) -> None:
    for filename, raw in replacements.items():
        spec = SPECS[filename]
        before = decode_save(read_regular_file(data_dir / filename), spec)
        after = decode_save(raw, spec)
        print(f"{spec.label}：{before[spec.count_field]} -> {after[spec.count_field]}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="指定本地存档目录")
    parser.add_argument(
        "--backup-root", type=Path, default=PROJECT_ROOT / "backups", help="备份根目录"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="只读查看本地计数，不显示用户标识")
    edit = commands.add_parser("set", help="设置本地计数；默认仅预览")
    edit.add_argument("--pending", type=count_argument, help="设置尚未上报的增量 t")
    edit.add_argument("--total", type=count_argument, help="设置账号累计快照 count")
    edit.add_argument("--apply", action="store_true", help="备份后实际写入")
    restore = commands.add_parser("restore", help="从备份目录恢复；默认仅预览")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--apply", action="store_true", help="备份当前值后实际恢复")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        data_dir = args.data_dir if args.data_dir is not None else default_data_dir()
        if args.command == "status":
            print(f"环境：{ENVIRONMENT}")
            for spec in (PENDING, TOTAL):
                record = decode_save(read_regular_file(data_dir / spec.filename), spec)
                print(f"{spec.label}：{record[spec.count_field]}")
            return 0
        if args.command == "set":
            if args.apply:
                require_app_closed()
            replacements = prepare_set(data_dir, args.pending, args.total)
        else:
            replacements = load_backup(args.backup)
        print_preview(data_dir, replacements)
        if not args.apply:
            print("仅预览，未修改。确认后添加 --apply；实际写入前请完全退出应用。")
            return 0
        print(
            "仅修改离线本地计数。开箱会向服务器提交累计值，服务器可返回"
            " 70003（点击数异常）或 70006（冷却中）；不保证奖励兑换或服务器持久生效。"
        )
        backup = apply_files(data_dir, replacements, args.backup_root)
        print(f"写入完成。修改前备份：{backup}")
        print("重新打开应用后读取本地计数；本工具不修改宝箱冷却或服务器状态。")
        return 0
    except (SaveError, OSError, subprocess.SubprocessError) as exc:
        print(f"操作停止：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
