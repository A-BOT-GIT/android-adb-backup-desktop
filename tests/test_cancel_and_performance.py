import json
import os
import zipfile
from pathlib import Path

import pytest

from android_backup_desktop.adb import AdbClient
from android_backup_desktop.backup import BackupService, OperationCancelled
from android_backup_desktop.models import AppInfo, BackupOptions


class FakeBackupAdb:
    def __init__(self) -> None:
        self.service: BackupService | None = None

    def pull(self, remote: str, local: Path, timeout: int | None = None) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(f"apk:{remote}".encode())
        if "com.two" in remote and self.service:
            self.service.request_cancel()

    def path_exists(self, remote_path: str) -> bool:
        return False


def test_cancelled_backup_keeps_completed_apps_and_removes_tmp(tmp_path: Path) -> None:
    adb = FakeBackupAdb()
    service = BackupService(adb)  # type: ignore[arg-type]
    adb.service = service
    apps = [
        AppInfo(package="com.one", name="One", apk_paths=["/data/app/com.one/base.apk"]),
        AppInfo(package="com.two", name="Two", apk_paths=["/data/app/com.two/base.apk"]),
    ]

    with pytest.raises(OperationCancelled) as exc_info:
        service.backup_apps(
            apps,
            BackupOptions(output_dir=tmp_path, include_data=False, include_obb=False),
        )

    assert exc_info.value.completed_apps == ["com.one"]
    assert exc_info.value.archive_path is not None
    with zipfile.ZipFile(exc_info.value.archive_path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))

    assert manifest["status"] == "cancelled"
    assert manifest["completed_apps"] == ["com.one"]
    assert [app["package"] for app in manifest["apps"]] == ["com.one"]
    assert "apps/com.one/apk/base.apk" in names
    assert not any(name.startswith("apps/.tmp/") for name in names)
    assert not any(name.startswith("apps/com.two/") for name in names)


class CountingAdbClient(AdbClient):
    def __init__(self, package_count: int) -> None:
        self.package_count = package_count
        self.run_calls: list[list[str]] = []
        self.shell_calls: list[tuple[str, ...]] = []

    def _run(self, args: list[str], **kwargs):  # type: ignore[override]
        self.run_calls.append(args)
        stdout = "\n".join(
            f"package:/data/app/com.example{i}/base.apk=com.example{i}"
            for i in range(self.package_count)
        )

        class Result:
            pass

        result = Result()
        result.stdout = stdout
        return result

    def shell(self, *args: str, timeout: int | None = 60, check: bool = True) -> str:
        self.shell_calls.append(args)
        if args[:4] == ("pm", "list", "packages", "-3"):
            return "\n".join(f"package:com.example{i}" for i in range(0, self.package_count, 2))
        raise AssertionError(f"unexpected per-app shell call: {args}")


def test_list_apps_uses_bulk_package_queries_for_100_plus_apps() -> None:
    adb = CountingAdbClient(125)

    apps = adb.list_apps(include_system=True)

    assert len(apps) == 125
    assert adb.run_calls == [["shell", "pm", "list", "packages", "-f"]]
    assert adb.shell_calls == [("pm", "list", "packages", "-3")]
    assert apps[0].name == "com.example0"
    assert apps[0].metadata_loaded is False
    assert apps[0].is_system is False
    assert apps[1].is_system is True


def test_device_refresh_is_dispatched_to_background_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from android_backup_desktop.gui import DeviceLoadWorker, MainWindow

    app = QApplication.instance() or QApplication([])
    captured: dict[str, object] = {}

    def fake_start_worker(self: MainWindow, worker, run_slot) -> None:
        captured["worker"] = worker
        captured["run_slot"] = run_slot

    monkeypatch.setattr(MainWindow, "start_worker", fake_start_worker)
    window = MainWindow()
    app.processEvents()

    assert isinstance(captured["worker"], DeviceLoadWorker)
    assert captured["run_slot"] == captured["worker"].run
    window.close()
