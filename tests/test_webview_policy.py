from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.webview_policy import (
    POLICY_KEY, POLICY_NAMES, REG_SZ, PolicyError, RegistryValue, TemporaryDebugPolicy,
)


class FakeRegistry:
    def __init__(self, values: dict[str, RegistryValue] | None = None) -> None:
        self.values = dict(values or {})
        self.operations: list[tuple[str, str, str | None]] = []
        self.fail_temporary_name: str | None = None

    def read(self, name: str) -> RegistryValue | None:
        if name not in POLICY_NAMES:
            raise AssertionError("unexpected policy name")
        return self.values.get(name)

    def write(self, name: str, value: str) -> None:
        self.operations.append(("write", name, value))
        if self.fail_temporary_name == name and "--remote-debugging-port=" in value:
            raise PermissionError("denied")
        self.values[name] = RegistryValue(value, REG_SZ)

    def delete(self, name: str) -> None:
        self.operations.append(("delete", name, None))
        self.values.pop(name, None)


class TemporaryDebugPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.backup = Path(self.directory.name) / "pending.json"
        self.registry = FakeRegistry()
        self.policy = TemporaryDebugPolicy(19227, self.backup, backend=self.registry)

    def test_install_and_restore_only_fixed_missing_values(self) -> None:
        self.registry.values["OtherApp.exe"] = RegistryValue("untouched", REG_SZ)
        self.policy.install()
        for name in POLICY_NAMES:
            self.assertEqual(
                self.registry.values[name], RegistryValue(
                    "--remote-debugging-address=127.0.0.1 --remote-debugging-port=19227", REG_SZ,
                ),
            )
        self.assertTrue(self.policy.restore())
        self.assertEqual(self.registry.values, {"OtherApp.exe": RegistryValue("untouched", REG_SZ)})
        self.assertFalse(self.policy.restore())
        self.assertEqual(json.loads(self.backup.read_text("utf-8"))["state"], "restored")

    def test_existing_arguments_and_whitespace_restored_exactly(self) -> None:
        originals = {
            POLICY_NAMES[0]: RegistryValue("  --disable-gpu --lang=zh-CN  ", REG_SZ),
            POLICY_NAMES[1]: RegistryValue("", REG_SZ),
        }
        self.registry.values.update(originals)
        self.policy.install()
        self.assertTrue(self.registry.values[POLICY_NAMES[0]].data.startswith(originals[POLICY_NAMES[0]].data))
        self.policy.restore()
        self.assertEqual(self.registry.values, originals)

    def test_reject_existing_type_or_debug_conflict_before_any_write(self) -> None:
        for original in (RegistryValue(1, 4), RegistryValue("--REMOTE-DEBUGGING-PORT=9999", REG_SZ)):
            with self.subTest(original=original):
                self.registry.values[POLICY_NAMES[0]] = original
                with self.assertRaises(PolicyError):
                    self.policy.install()
                self.assertEqual(self.registry.operations, [])
                self.assertFalse(self.backup.exists())

    def test_partial_install_failure_rolls_back_existing_and_missing_values(self) -> None:
        initial = {POLICY_NAMES[0]: RegistryValue("--lang=zh-CN", REG_SZ)}
        self.registry.values.update(initial)
        self.registry.fail_temporary_name = POLICY_NAMES[1]
        with self.assertRaisesRegex(PolicyError, "管理员"):
            self.policy.install()
        self.assertEqual(self.registry.values, initial)
        self.assertEqual(json.loads(self.backup.read_text("utf-8"))["state"], "restored")

    def test_first_install_write_failure_leaves_originals_unchanged(self) -> None:
        self.registry.fail_temporary_name = POLICY_NAMES[0]
        with self.assertRaisesRegex(PolicyError, "管理员"):
            self.policy.install()
        self.assertEqual(self.registry.values, {})
        self.assertFalse(self.policy.recover_pending())

    def test_new_process_recovers_pending_transaction_and_original_port(self) -> None:
        self.policy.install()
        recovered = TemporaryDebugPolicy(19228, self.backup, backend=self.registry)
        self.assertTrue(recovered.recover_pending())
        self.assertEqual(self.registry.values, {})
        self.assertFalse(recovered.recover_pending())

    def test_pending_transaction_is_not_overwritten_by_second_install(self) -> None:
        self.policy.install()
        previous = self.backup.read_bytes()
        with self.assertRaisesRegex(PolicyError, "recover_pending"):
            self.policy.install()
        self.assertEqual(self.backup.read_bytes(), previous)

    def test_external_change_is_preserved_and_other_value_is_restored(self) -> None:
        self.policy.install()
        external = RegistryValue("--lang=external", REG_SZ)
        self.registry.values[POLICY_NAMES[0]] = external
        with self.assertRaisesRegex(PolicyError, "外部改动未覆盖"):
            self.policy.restore()
        self.assertEqual(self.registry.values, {POLICY_NAMES[0]: external})
        self.assertEqual(json.loads(self.backup.read_text("utf-8"))["state"], "pending")

    def test_backup_write_failure_prevents_registry_mutation(self) -> None:
        with patch.object(self.policy, "_write_backup", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.policy.install()
        self.assertEqual(self.registry.operations, [])

    def test_backup_cannot_redirect_key_name_or_temporary_arguments(self) -> None:
        self.policy.install()
        original = json.loads(self.backup.read_text("utf-8"))
        for change in ("key", "name", "arguments", "port"):
            with self.subTest(change=change):
                record = json.loads(json.dumps(original))
                if change == "key":
                    record["policy_key"] = POLICY_KEY + "\\Other"
                elif change == "name":
                    record["entries"]["*"] = record["entries"].pop(POLICY_NAMES[0])
                elif change == "arguments":
                    record["entries"][POLICY_NAMES[0]]["temporary"] = "--remote-debugging-address=0.0.0.0"
                else:
                    record["port"] = True
                self.backup.write_text(json.dumps(record), encoding="utf-8")
                before = dict(self.registry.values)
                with self.assertRaises(PolicyError):
                    self.policy.recover_pending()
                self.assertEqual(self.registry.values, before)

    def test_missing_or_completed_backup_does_not_read_registry(self) -> None:
        with patch.object(self.registry, "read", side_effect=AssertionError("unexpected read")):
            self.assertFalse(self.policy.recover_pending())
        self.policy.install()
        self.policy.restore()
        with patch.object(self.registry, "read", side_effect=AssertionError("unexpected read")):
            self.assertFalse(self.policy.recover_pending())

    def test_partial_restore_can_be_retried_after_permission_returns(self) -> None:
        original = RegistryValue("--lang=zh-CN", REG_SZ)
        self.registry.values[POLICY_NAMES[0]] = original
        self.policy.install()
        normal = self.registry.write
        def fail_restore(name: str, value: str) -> None:
            if value == original.data:
                raise PermissionError("denied")
            normal(name, value)
        with patch.object(self.registry, "write", side_effect=fail_restore):
            with self.assertRaisesRegex(PolicyError, "恢复失败"):
                self.policy.restore()
        self.assertNotIn(POLICY_NAMES[1], self.registry.values)
        self.assertTrue(self.policy.recover_pending())
        self.assertEqual(self.registry.values, {POLICY_NAMES[0]: original})

    def test_adopt_complete_pending_transaction_is_read_only(self) -> None:
        self.policy.install()
        values = dict(self.registry.values)
        operations = list(self.registry.operations)
        backup = self.backup.read_bytes()
        replacement = TemporaryDebugPolicy(19227, self.backup, backend=self.registry)
        self.assertTrue(replacement.adopt_pending())
        self.assertEqual(self.registry.values, values)
        self.assertEqual(self.registry.operations, operations)
        self.assertEqual(self.backup.read_bytes(), backup)
        self.assertTrue(replacement.restore())
        self.assertEqual(self.registry.values, {})

    def test_adopt_missing_or_restored_transaction_does_not_read_registry(self) -> None:
        with patch.object(self.registry, "read", side_effect=AssertionError("unexpected read")):
            self.assertFalse(self.policy.adopt_pending())
        self.policy.install()
        self.policy.restore()
        backup = self.backup.read_bytes()
        with patch.object(self.registry, "read", side_effect=AssertionError("unexpected read")):
            self.assertFalse(self.policy.adopt_pending())
        self.assertEqual(self.backup.read_bytes(), backup)

    def test_adopt_rejects_port_mismatch_before_reading_registry(self) -> None:
        self.policy.install()
        backup = self.backup.read_bytes()
        replacement = TemporaryDebugPolicy(19228, self.backup, backend=self.registry)
        with patch.object(self.registry, "read", side_effect=AssertionError("unexpected read")):
            with self.assertRaisesRegex(PolicyError, "端口不一致"):
                replacement.adopt_pending()
        self.assertEqual(self.backup.read_bytes(), backup)

    def test_adopt_rejects_partial_or_external_change_without_mutation(self) -> None:
        self.policy.install()
        installed = dict(self.registry.values)
        operations = list(self.registry.operations)
        backup = self.backup.read_bytes()
        for replacement in (None, RegistryValue("--lang=external", REG_SZ)):
            with self.subTest(replacement=replacement):
                self.registry.values = dict(installed)
                if replacement is None:
                    self.registry.values.pop(POLICY_NAMES[1])
                else:
                    self.registry.values[POLICY_NAMES[1]] = replacement
                expected = dict(self.registry.values)
                with self.assertRaisesRegex(PolicyError, "未完整安装或已被其他程序修改"):
                    self.policy.adopt_pending()
                self.assertEqual(self.registry.values, expected)
                self.assertEqual(self.registry.operations, operations)
                self.assertEqual(self.backup.read_bytes(), backup)

    def test_adopt_permission_failure_keeps_backup(self) -> None:
        self.policy.install()
        backup = self.backup.read_bytes()
        with patch.object(self.registry, "read", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(PolicyError, "管理员"):
                self.policy.adopt_pending()
        self.assertEqual(self.backup.read_bytes(), backup)


if __name__ == "__main__":
    unittest.main()
