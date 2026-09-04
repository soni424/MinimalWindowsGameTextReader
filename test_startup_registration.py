"""Tests for current-user Windows sign-in registration."""

from __future__ import annotations

import unittest

from startup_registration import (
    RUN_KEY,
    STARTUP_ARGUMENT,
    VALUE_NAME,
    StartupRegistration,
    StartupRegistrationError,
    build_startup_command,
)


class _FakeKey:
    def __init__(self, registry: "_FakeRegistry") -> None:
        self.registry = registry

    def __enter__(self) -> "_FakeKey":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self) -> None:
        self.values: dict[str, tuple[str, int]] = {}
        self.fail_reads = False
        self.fail_writes = False

    def OpenKey(self, _root: object, path: str, _reserved: int, access: int) -> _FakeKey:
        if path != RUN_KEY:
            raise FileNotFoundError(path)
        if access == self.KEY_READ and self.fail_reads:
            raise PermissionError("read blocked")
        if access == self.KEY_SET_VALUE and self.fail_writes:
            raise PermissionError("write blocked")
        return _FakeKey(self)

    def CreateKeyEx(self, _root: object, path: str, _reserved: int, _access: int) -> _FakeKey:
        if path != RUN_KEY:
            raise FileNotFoundError(path)
        if self.fail_writes:
            raise PermissionError("write blocked")
        return _FakeKey(self)

    def QueryValueEx(self, _key: _FakeKey, name: str) -> tuple[str, int]:
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(
        self, _key: _FakeKey, name: str, _reserved: int, value_type: int, value: str
    ) -> None:
        if self.fail_writes:
            raise PermissionError("write blocked")
        self.values[name] = (value, value_type)

    def DeleteValue(self, _key: _FakeKey, name: str) -> None:
        if self.fail_writes:
            raise PermissionError("write blocked")
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class StartupRegistrationTests(unittest.TestCase):
    def test_packaged_command_quotes_paths_and_adds_startup_argument(self) -> None:
        command = build_startup_command(
            r"C:\Program Files\Game Text Reader\GameTextReader.exe",
            frozen=True,
        )
        self.assertEqual(
            command,
            f'"C:\\Program Files\\Game Text Reader\\GameTextReader.exe" {STARTUP_ARGUMENT}',
        )

    def test_source_command_uses_pythonw_and_absolute_script(self) -> None:
        command = build_startup_command(
            r"C:\Python\python.exe",
            r"C:\Game Reader Source\main.py",
            frozen=False,
            gui_executable=r"C:\Python\pythonw.exe",
        )
        self.assertEqual(
            command,
            'C:\\Python\\pythonw.exe "C:\\Game Reader Source\\main.py" --startup',
        )

    def test_enable_disable_and_path_repair_preserve_unrelated_values(self) -> None:
        registry = _FakeRegistry()
        registry.values["AnotherApp"] = ("another.exe", registry.REG_SZ)
        registry.values[VALUE_NAME] = ("old-location.exe --startup", registry.REG_SZ)
        registration = StartupRegistration("new-location.exe --startup", registry)

        self.assertFalse(registration.is_enabled())
        registration.set_enabled(True)
        self.assertTrue(registration.is_enabled())
        self.assertEqual(
            registry.values[VALUE_NAME],
            ("new-location.exe --startup", registry.REG_SZ),
        )
        registration.set_enabled(False)

        self.assertNotIn(VALUE_NAME, registry.values)
        self.assertEqual(registry.values["AnotherApp"], ("another.exe", registry.REG_SZ))

    def test_registry_access_errors_are_actionable(self) -> None:
        registry = _FakeRegistry()
        registration = StartupRegistration("reader.exe --startup", registry)
        registry.fail_reads = True
        with self.assertRaisesRegex(StartupRegistrationError, "could not read"):
            registration.is_enabled()
        registry.fail_reads = False
        registry.fail_writes = True
        with self.assertRaisesRegex(StartupRegistrationError, "could not enable"):
            registration.set_enabled(True)


if __name__ == "__main__":
    unittest.main()
