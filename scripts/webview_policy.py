"""Temporarily configure a fixed application's elevated WebView2 debug policy."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Protocol


POLICY_KEY = r"SOFTWARE\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments"
POLICY_NAMES = ("WuwaTappo.exe", "com.kuro.wuwatappo.cn")
REG_SZ = 1
MAX_ARGUMENT_LENGTH = 32_768
MAX_BACKUP_BYTES = 512_000


class PolicyError(Exception):
    """The app-specific policy could not be installed or restored safely."""


@dataclass(frozen=True)
class RegistryValue:
    data: Any
    kind: int


class RegistryBackend(Protocol):
    def read(self, name: str) -> RegistryValue | None: ...
    def write(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class WindowsRegistry:
    """Access only two fixed named values in the machine's 64-bit policy view."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise PolicyError("WebView2 策略仅支持 Windows。")
        import winreg
        self.registry = winreg

    @staticmethod
    def _check_name(name: str) -> None:
        if name not in POLICY_NAMES:
            raise PolicyError("拒绝访问其他应用的策略。")

    def read(self, name: str) -> RegistryValue | None:
        self._check_name(name)
        registry = self.registry
        try:
            with registry.OpenKey(
                registry.HKEY_LOCAL_MACHINE, POLICY_KEY, 0,
                registry.KEY_QUERY_VALUE | registry.KEY_WOW64_64KEY,
            ) as key:
                data, kind = registry.QueryValueEx(key, name)
                return RegistryValue(data, kind)
        except FileNotFoundError:
            return None

    def write(self, name: str, value: str) -> None:
        self._check_name(name)
        registry = self.registry
        with registry.CreateKeyEx(
            registry.HKEY_LOCAL_MACHINE, POLICY_KEY, 0,
            registry.KEY_SET_VALUE | registry.KEY_WOW64_64KEY,
        ) as key:
            registry.SetValueEx(key, name, 0, registry.REG_SZ, value)

    def delete(self, name: str) -> None:
        self._check_name(name)
        registry = self.registry
        try:
            with registry.OpenKey(
                registry.HKEY_LOCAL_MACHINE, POLICY_KEY, 0,
                registry.KEY_SET_VALUE | registry.KEY_WOW64_64KEY,
            ) as key:
                registry.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def _reject_links(path: Path) -> None:
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise PolicyError("策略备份路径不能包含符号链接或重解析点。")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError("策略备份包含重复字段。")
        result[key] = value
    return result


def _checked_port(port: object) -> int:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise PolicyError("调试端口必须介于 1024 和 65535 之间。")
    return port


def _arguments(original: RegistryValue | None, port: int) -> str:
    if original is not None and (
        original.kind != REG_SZ or not isinstance(original.data, str)
        or len(original.data) > MAX_ARGUMENT_LENGTH
    ):
        raise PolicyError("已有桌宠策略不是受支持的 REG_SZ 字符串，未修改。")
    value = original.data if original is not None else ""
    if "remote-debugging" in value.casefold():
        raise PolicyError("已有桌宠策略包含其他调试参数，未覆盖。")
    flags = f"--remote-debugging-address=127.0.0.1 --remote-debugging-port={port}"
    temporary = f"{value} {flags}" if value else flags
    if len(temporary) > MAX_ARGUMENT_LENGTH:
        raise PolicyError("桌宠策略参数过长，未修改。")
    return temporary


def _original_record(value: RegistryValue | None) -> dict[str, Any]:
    return {
        "exists": value is not None,
        "data": value.data if value is not None else None,
        "type": value.kind if value is not None else None,
    }


def _decode_original(value: Any) -> RegistryValue | None:
    if not isinstance(value, dict) or set(value) != {"exists", "data", "type"}:
        raise PolicyError("策略备份的原始值格式不受支持。")
    if type(value["exists"]) is not bool:
        raise PolicyError("策略备份的原始值格式不受支持。")
    if not value["exists"]:
        if value["data"] is not None or value["type"] is not None:
            raise PolicyError("策略备份的缺失值格式不受支持。")
        return None
    if (
        type(value["type"]) is not int or value["type"] != REG_SZ
        or not isinstance(value["data"], str) or len(value["data"]) > MAX_ARGUMENT_LENGTH
    ):
        raise PolicyError("策略备份包含不受支持的原始类型。")
    return RegistryValue(value["data"], value["type"])


class TemporaryDebugPolicy:
    """Back up, install and compare-and-restore two app-specific policy values.

    The backup stays on disk with state ``pending`` until restoration completes.
    A fresh process can call ``recover_pending`` after an interrupted launch.
    No other registry value or registry key is deleted or overwritten.
    """

    def __init__(
        self, port: int, backup_path: Path, *, backend: RegistryBackend | None = None,
    ) -> None:
        self.port = _checked_port(port)
        self.backup_path = Path(backup_path)
        self._backend = backend

    @property
    def backend(self) -> RegistryBackend:
        if self._backend is None:
            self._backend = WindowsRegistry()
        return self._backend

    def _write_backup(self, record: dict[str, Any]) -> None:
        _reject_links(self.backup_path)
        self.backup_path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_BACKUP_BYTES:
            raise PolicyError("策略备份超过大小限制。")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{self.backup_path.name}.", suffix=".tmp",
                dir=self.backup_path.parent, delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            _reject_links(self.backup_path)
            os.replace(temporary, self.backup_path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _read_backup(self) -> dict[str, Any] | None:
        _reject_links(self.backup_path)
        try:
            info = self.backup_path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BACKUP_BYTES:
            raise PolicyError("策略备份的文件类型或大小异常。")
        try:
            with self.backup_path.open("rb") as source:
                raw = source.read(MAX_BACKUP_BYTES + 1)
            if len(raw) > MAX_BACKUP_BYTES:
                raise PolicyError("策略备份超过大小限制。")
            record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeError, ValueError) as error:
            raise PolicyError("策略备份无法识别，未进行恢复。") from error
        if (
            not isinstance(record, dict)
            or set(record) != {"version", "policy_key", "state", "port", "entries"}
            or type(record["version"]) is not int or record["version"] != 1
            or record["policy_key"] != POLICY_KEY
            or record["state"] not in ("pending", "restored")
            or not isinstance(record["entries"], dict)
            or set(record["entries"]) != set(POLICY_NAMES)
        ):
            raise PolicyError("策略备份清单不受支持，未进行恢复。")
        port = _checked_port(record["port"])
        for entry in record["entries"].values():
            if not isinstance(entry, dict) or set(entry) != {"original", "temporary"}:
                raise PolicyError("策略备份条目格式不受支持。")
            original = _decode_original(entry["original"])
            if entry["temporary"] != _arguments(original, port):
                raise PolicyError("策略备份中的临时参数校验失败。")
        return record

    def install(self) -> None:
        existing = self._read_backup()
        if existing is not None and existing["state"] == "pending":
            raise PolicyError("存在未恢复的桌宠策略，请先调用 recover_pending()。")
        try:
            entries: dict[str, Any] = {}
            for name in POLICY_NAMES:
                original = self.backend.read(name)
                entries[name] = {
                    "original": _original_record(original),
                    "temporary": _arguments(original, self.port),
                }
            record = {
                "version": 1, "policy_key": POLICY_KEY, "state": "pending",
                "port": self.port, "entries": entries,
            }
            self._write_backup(record)
        except PermissionError as error:
            raise PolicyError("读取或备份桌宠策略失败，请右键启动器选择“以管理员身份运行”。") from error
        try:
            for name in POLICY_NAMES:
                entry = entries[name]
                if self.backend.read(name) != _decode_original(entry["original"]):
                    raise PolicyError("桌宠策略在备份后被其他程序修改，已停止安装。")
                self.backend.write(name, entry["temporary"])
        except BaseException as error:
            try:
                self.restore()
            except (OSError, PolicyError) as rollback:
                raise PolicyError(
                    "桌宠调试策略安装失败，且未能完整恢复；"
                    f"请保留备份并以管理员身份重新运行：{self.backup_path}"
                ) from rollback
            if isinstance(error, PermissionError):
                raise PolicyError("写入桌宠策略需要管理员权限，请右键启动器选择“以管理员身份运行”。") from error
            if isinstance(error, (KeyboardInterrupt, SystemExit, PolicyError)):
                raise
            raise PolicyError("安装桌宠调试策略失败，已恢复原值。") from error

    def restore(self) -> bool:
        record = self._read_backup()
        if record is None or record["state"] == "restored":
            return False
        conflicts: list[str] = []
        errors: list[str] = []
        for name in POLICY_NAMES:
            entry = record["entries"][name]
            original = _decode_original(entry["original"])
            try:
                current = self.backend.read(name)
                if current == original:
                    continue
                if current != RegistryValue(entry["temporary"], REG_SZ):
                    conflicts.append(name)
                    continue
                if original is None:
                    self.backend.delete(name)
                else:
                    self.backend.write(name, original.data)
                if self.backend.read(name) != original:
                    errors.append(name)
            except (OSError, PolicyError):
                errors.append(name)
        if conflicts or errors:
            details = []
            if conflicts:
                details.append("外部改动未覆盖：" + ", ".join(conflicts))
            if errors:
                details.append("恢复失败：" + ", ".join(errors))
            raise PolicyError(
                "；".join(details) + f"。请保留策略备份并以管理员身份处理：{self.backup_path}"
            )
        record["state"] = "restored"
        self._write_backup(record)
        return True

    def recover_pending(self) -> bool:
        """Restore a previous pending transaction, including a partial installation."""
        return self.restore()

    def adopt_pending(self) -> bool:
        """Read-only adoption of a complete pending transaction for this port.

        This lets a replacement worker retain the original restoration record
        while its already-running WebView2 instance is still being connected.
        Partial installations and externally changed values cannot be adopted.
        """
        record = self._read_backup()
        if record is None or record["state"] == "restored":
            return False
        if record["port"] != self.port:
            raise PolicyError("待接管的桌宠调试策略端口与当前端口不一致，未修改。")
        try:
            for name in POLICY_NAMES:
                expected = RegistryValue(record["entries"][name]["temporary"], REG_SZ)
                if self.backend.read(name) != expected:
                    raise PolicyError(
                        "待接管的桌宠调试策略未完整安装或已被其他程序修改，"
                        f"未接管也未修改；请保留备份：{self.backup_path}"
                    )
        except PermissionError as error:
            raise PolicyError("读取待接管的桌宠策略失败，请以管理员身份运行。") from error
        return True
