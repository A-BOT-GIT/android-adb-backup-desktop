import json
import stat
import zipfile
from pathlib import Path

import pytest

import android_backup_desktop.backup as backup_module
from android_backup_desktop.backup import BackupService


class FakeAdb:
    def __init__(self) -> None:
        self.shell_calls: list[tuple[str, ...]] = []
        self.push_calls: list[tuple[Path, str]] = []

    def install(self, apk_files: list[Path]) -> None:
        pass

    def shell(self, *args: str, timeout: int | None = 60, check: bool = True) -> str:
        self.shell_calls.append(args)
        return ""

    def push(self, local: Path, remote: str, timeout: int | None = None) -> None:
        self.push_calls.append((local, remote))

    def adb_restore(self, backup_file: Path) -> None:
        pass


def write_backup(zip_path: Path, manifest: dict, files: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, content in (files or {}).items():
            archive.writestr(name, content)


def test_restore_rejects_zip_path_traversal(tmp_path: Path) -> None:
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("../escape.txt", b"bad")
        archive.writestr("manifest.json", json.dumps({"apps": []}))

    with pytest.raises(ValueError, match="不安全路径"):
        BackupService(FakeAdb()).restore_backup(zip_path)


def test_restore_rejects_zip_symlink(tmp_path: Path) -> None:
    zip_path = tmp_path / "symlink.zip"
    symlink = zipfile.ZipInfo("apps/com.example/link")
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"apps": []}))
        archive.writestr(symlink, "target")

    with pytest.raises(ValueError, match="符号链接"):
        BackupService(FakeAdb()).restore_backup(zip_path)


def test_restore_rejects_zip_uncompressed_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_module, "MAX_ZIP_UNCOMPRESSED_SIZE", 3)
    zip_path = tmp_path / "large.zip"
    write_backup(zip_path, {"apps": []}, {"apps/com.example/file.bin": b"1234"})

    with pytest.raises(ValueError, match="解压后过大"):
        BackupService(FakeAdb()).restore_backup(zip_path)


def test_restore_rejects_invalid_package_name(tmp_path: Path) -> None:
    zip_path = tmp_path / "bad-package.zip"
    write_backup(zip_path, {"apps": [{"package": "../evil"}]})

    with pytest.raises(ValueError, match="包名无效"):
        BackupService(FakeAdb()).restore_backup(zip_path)


def test_restore_pushes_nested_obb_files_to_matching_remote_parents(tmp_path: Path) -> None:
    zip_path = tmp_path / "backup.zip"
    manifest = {"apps": [{"package": "com.example.game"}]}
    write_backup(
        zip_path,
        manifest,
        {
            "apps/com.example.game/obb/main.obb": b"main",
            "apps/com.example.game/obb/patches/level1/patch.obb": b"patch",
        },
    )
    adb = FakeAdb()

    BackupService(adb).restore_backup(zip_path, restore_data=False)

    assert ("mkdir", "-p", "/sdcard/Android/obb/com.example.game") in adb.shell_calls
    assert ("mkdir", "-p", "/sdcard/Android/obb/com.example.game/patches/level1") in adb.shell_calls
    assert {remote for _, remote in adb.push_calls} == {
        "/sdcard/Android/obb/com.example.game",
        "/sdcard/Android/obb/com.example.game/patches/level1",
    }
