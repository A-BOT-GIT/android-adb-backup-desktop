import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from android_backup_desktop.adb import LONG_ADB_OPERATION_TIMEOUT, AdbClient
from android_backup_desktop.backup import BackupService, OperationCancelled
from android_backup_desktop.models import AppInfo, BackupOptions, Device


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

    def load_app_metadata(self, app: AppInfo) -> AppInfo:
        return AppInfo(
            package=app.package,
            name=app.name or app.package,
            localized_name=app.localized_name,
            version_name=app.version_name or "",
            version_code=app.version_code or "",
            apk_paths=app.apk_paths,
            package_size_bytes=app.package_size_bytes,
            is_system=app.is_system,
            metadata_loaded=True,
        )


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
        self.serial = None
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


def test_load_app_metadata_adds_package_size_from_apk_paths() -> None:
    class SizeAdbClient(AdbClient):
        def __init__(self) -> None:
            self.serial = None
            self.shell_calls: list[tuple[str, ...]] = []

        def shell(self, *args: str, timeout: int | None = 60, check: bool = True) -> str:
            self.shell_calls.append(args)
            if args[:2] == ("dumpsys", "package"):
                return "versionName=1.0 versionCode=2 minSdk=23"
            if args[:3] == ("stat", "-c", "%s"):
                if args[3].endswith("/base.apk"):
                    return "1024\n"
                if args[3].endswith("/split.apk"):
                    return "2048\n"
                return ""
            raise AssertionError(f"unexpected shell call: {args}")

        def _read_label_from_apk(self, package: str, apk_paths: list[str]) -> tuple[str, str, str, str]:
            return "", "", "", ""

    adb = SizeAdbClient()

    app = adb.load_app_metadata(
        AppInfo(
            package="com.example",
            name="com.example",
            apk_paths=["/data/app/com.example/base.apk", "/data/app/com.example/split.apk"],
        )
    )

    assert app.package_size_bytes == 3072
    assert app.display_package_size == "3.0 KB"
    assert app.display_name == "com.example"


def test_load_app_metadata_uses_localized_name_when_apk_reader_provides_it() -> None:
    class LocalizedNameAdbClient(AdbClient):
        def __init__(self) -> None:
            self.serial = None

        def shell(self, *args: str, timeout: int | None = 60, check: bool = True) -> str:
            if args[:2] == ("dumpsys", "package"):
                return ""
            if args[:3] == ("stat", "-c", "%s"):
                return ""
            if args[0] == "wc":
                return ""
            raise AssertionError(f"unexpected shell call: {args}")

        def _read_label_from_apk(self, package: str, apk_paths: list[str]) -> tuple[str, str, str, str]:
            return "Example", "示例应用", "1.0", "2"

    adb = LocalizedNameAdbClient()

    app = adb.load_app_metadata(
        AppInfo(
            package="com.example",
            name="com.example",
            apk_paths=["/data/app/com.example/base.apk"],
        )
    )

    assert app.name == "Example"
    assert app.localized_name == "示例应用"
    assert app.display_name == "示例应用"


def test_device_refresh_is_dispatched_to_background_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from android_backup_desktop.gui import DeviceLoadWorker, MainWindow

    app = QApplication.instance() or QApplication([])
    captured: dict[str, object] = {}

    def fake_start_worker(self: MainWindow, worker, run_slot) -> bool:
        captured["worker"] = worker
        captured["run_slot"] = run_slot
        return True

    monkeypatch.setattr(MainWindow, "start_worker", fake_start_worker)
    window = MainWindow()
    app.processEvents()

    assert isinstance(captured["worker"], DeviceLoadWorker)
    assert captured["run_slot"] == captured["worker"].run
    window.close()


def test_fast_device_worker_result_is_handled_after_worker_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    import android_backup_desktop.gui as gui_module
    from android_backup_desktop.gui import MainWindow

    class FakeAdbClient:
        def __init__(self, adb_path: str) -> None:
            self.adb_path = adb_path

        def ensure_available(self) -> None:
            pass

        def devices(self) -> list[Device]:
            return [Device(serial="serial-1", state="device", description="")]

    captured: dict[str, object] = {}

    def fake_start_worker(self: MainWindow, worker, run_slot) -> bool:
        captured["worker"] = worker
        captured["run_slot"] = run_slot
        return True

    def fake_begin_worker(self: MainWindow) -> None:
        captured["began"] = True
        captured["run_slot"]()

    monkeypatch.setattr(gui_module, "AdbClient", FakeAdbClient)
    monkeypatch.setattr(MainWindow, "start_worker", fake_start_worker)
    monkeypatch.setattr(MainWindow, "begin_worker", fake_begin_worker)

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    app.processEvents()

    assert captured["began"] is True
    assert window.device_combo.count() == 1
    assert window.current_serial() == "serial-1"
    assert "找到 1 台设备" in window.status_label.text()
    window.close()


def test_metadata_thread_reference_is_cleared_before_thread_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    from android_backup_desktop.gui import MainWindow

    monkeypatch.setattr(MainWindow, "start_worker", lambda *_args: False)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    thread = QThread(window)

    window.metadata_thread = thread
    window._clear_metadata_thread(thread)

    assert window.metadata_thread is None
    thread.deleteLater()
    window.close()


def test_app_table_displays_localized_name_and_package_size(monkeypatch: pytest.MonkeyPatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from android_backup_desktop.gui import MainWindow

    monkeypatch.setattr(MainWindow, "refresh_devices", lambda _self: None)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.apps = [
        AppInfo(
            package="com.example",
            name="Example",
            localized_name="示例应用",
            version_name="1.0",
            version_code="2",
            apk_paths=["/data/app/com.example/base.apk"],
            package_size_bytes=1_572_864,
            metadata_loaded=True,
        )
    ]

    window.populate_table()

    assert window.table.columnCount() == 6
    assert window.table.horizontalHeaderItem(5).text() == "大小"
    assert window.table.item(0, 1).text() == "示例应用"
    assert window.table.item(0, 5).text() == "1.5 MB"
    window.close()


def test_worker_thread_reference_is_cleared_when_thread_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication

    from android_backup_desktop.gui import MainWindow

    class FinishedWorker(QObject):
        finished = Signal()

        def run(self) -> None:
            pass

    monkeypatch.setattr(MainWindow, "refresh_devices", lambda _self: None)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    worker = FinishedWorker()

    assert window.start_worker(worker, worker.run) is True
    assert window.worker_thread is not None

    window.worker_thread.finished.emit()
    app.processEvents()

    assert window.worker_thread is None
    window.close()


def test_start_worker_recovers_from_deleted_thread_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication

    from android_backup_desktop.gui import MainWindow

    class DeletedThreadReference:
        def isRunning(self) -> bool:
            raise RuntimeError("Internal C++ object already deleted")

    class FinishedWorker(QObject):
        finished = Signal()

        def run(self) -> None:
            pass

    monkeypatch.setattr(MainWindow, "refresh_devices", lambda _self: None)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    stale_thread = DeletedThreadReference()
    worker = FinishedWorker()

    window.worker_thread = stale_thread  # type: ignore[assignment]

    assert window.start_worker(worker, worker.run) is True
    assert window.worker_thread is not None
    assert window.worker_thread is not stale_thread

    window.worker_thread.finished.emit()
    app.processEvents()

    assert window.worker_thread is None
    window.close()


def test_long_adb_operations_use_bounded_timeouts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: list[tuple[list[str], int | None]] = []

    def fake_run(command, **kwargs):
        captured.append((list(command), kwargs.get("timeout")))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    client = AdbClient.__new__(AdbClient)
    client.adb_path = "adb"
    client.serial = None
    monkeypatch.setattr(subprocess, "run", fake_run)
    (tmp_path / "backup.ab").write_bytes(b"data")

    client.pull("/sdcard/file.bin", tmp_path / "file.bin")
    client.push(tmp_path / "file.bin", "/sdcard/file.bin")
    client.install([tmp_path / "app.apk"])
    client.adb_restore(tmp_path / "backup.ab")
    client.restore_run_as_data("com.example.app", tmp_path / "backup.ab")

    assert [timeout for _, timeout in captured] == [LONG_ADB_OPERATION_TIMEOUT] * 5
