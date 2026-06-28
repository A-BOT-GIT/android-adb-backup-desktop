import io
import json
import shlex
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

import android_backup_desktop.backup as backup_module
from android_backup_desktop.backup import BackupService


class FakeAdb:
    def __init__(self) -> None:
        self.shell_calls: list[tuple[str, ...]] = []
        self.push_calls: list[tuple[Path, str]] = []
        self.adb_restore_calls: list[Path] = []
        self.restore_run_as_calls: list[tuple[str, Path]] = []
        self.restore_run_as_payloads: list[bytes] = []

    def install(self, apk_files: list[Path]) -> None:
        pass

    def shell(self, *args: str, timeout: int | None = 60, check: bool = True) -> str:
        self.shell_calls.append(args)
        return ""

    def push(self, local: Path, remote: str, timeout: int | None = None) -> None:
        self.push_calls.append((local, remote))

    def adb_restore(self, backup_file: Path) -> None:
        self.adb_restore_calls.append(backup_file)

    def restore_run_as_data(self, package: str, input_tar: Path) -> None:
        self.restore_run_as_calls.append((package, input_tar))
        self.restore_run_as_payloads.append(input_tar.read_bytes())


def write_backup(zip_path: Path, manifest: dict, files: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, content in (files or {}).items():
            archive.writestr(name, content)


def make_tar(path: Path, files: dict[str, bytes]) -> bytes:
    with tarfile.open(path, "w") as archive:
        for name, content in files.items():
            tar_info = tarfile.TarInfo(name)
            tar_info.size = len(content)
            archive.addfile(tar_info, fileobj=io.BytesIO(content))
    return path.read_bytes()


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


def test_restore_restores_run_as_data_tar(tmp_path: Path) -> None:
    zip_path = tmp_path / "backup.zip"
    tar_bytes = make_tar(tmp_path / "run-as-data.tar", {"files/settings.json": b"{}"})
    manifest = {
        "apps": [
            {
                "package": "com.example.app",
                "data_files": ["apps/com.example.app/data/run-as-data.tar"],
            }
        ]
    }
    write_backup(zip_path, manifest, {"apps/com.example.app/data/run-as-data.tar": tar_bytes})
    adb = FakeAdb()

    BackupService(adb).restore_backup(zip_path)

    assert len(adb.restore_run_as_calls) == 1
    package, input_tar = adb.restore_run_as_calls[0]
    assert package == "com.example.app"
    assert input_tar.name == "run-as-data.tar"
    assert adb.restore_run_as_payloads == [tar_bytes]
    assert adb.adb_restore_calls == []


def test_restore_legacy_ab_data_still_uses_adb_restore(tmp_path: Path) -> None:
    zip_path = tmp_path / "backup.zip"
    manifest = {
        "apps": [
            {
                "package": "com.example.app",
                "data_files": ["apps/com.example.app/data/adb-backup.ab"],
            }
        ]
    }
    write_backup(zip_path, manifest, {"apps/com.example.app/data/adb-backup.ab": b"ANDROID BACKUP"})
    adb = FakeAdb()

    BackupService(adb).restore_backup(zip_path)

    assert [path.name for path in adb.adb_restore_calls] == ["adb-backup.ab"]
    assert adb.restore_run_as_calls == []


def test_restore_rejects_run_as_tar_path_traversal(tmp_path: Path) -> None:
    zip_path = tmp_path / "backup.zip"
    tar_bytes = make_tar(tmp_path / "run-as-data.tar", {"../escape.txt": b"bad"})
    manifest = {"apps": [{"package": "com.example.app"}]}
    write_backup(zip_path, manifest, {"apps/com.example.app/data/run-as-data.tar": tar_bytes})

    with pytest.raises(ValueError, match="不安全路径"):
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


def test_restore_quotes_archive_derived_obb_parent_for_shell_mkdir(tmp_path: Path) -> None:
    zip_path = tmp_path / "backup.zip"
    manifest = {"apps": [{"package": "com.example.game"}]}
    write_backup(
        zip_path,
        manifest,
        {
            "apps/com.example.game/obb/levels;echo injected/patch.obb": b"patch",
        },
    )
    adb = FakeAdb()

    BackupService(adb).restore_backup(zip_path, restore_data=False)

    remote_parent = "/sdcard/Android/obb/com.example.game/levels;echo injected"
    assert ("mkdir", "-p", shlex.quote(remote_parent)) in adb.shell_calls
    assert ("mkdir", "-p", remote_parent) not in adb.shell_calls
    assert [remote for _, remote in adb.push_calls] == [remote_parent]
