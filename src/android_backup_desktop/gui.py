from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .adb import AdbClient, AdbError
from .backup import BackupService, OperationCancelled
from .models import AppInfo, BackupOptions, Device


class DeviceLoadWorker(QObject):
    finished = Signal(list)
    failed = Signal(str)
    log = Signal(str)

    def __init__(self, adb_path: str) -> None:
        super().__init__()
        self.adb_path = adb_path

    def run(self) -> None:
        try:
            adb = AdbClient(self.adb_path)
            adb.ensure_available()
            self.finished.emit(adb.devices())
        except Exception as exc:
            self.failed.emit(str(exc) or "刷新设备失败。")


class AppLoadWorker(QObject):
    finished = Signal(list)
    failed = Signal(str)
    cancelled = Signal(list)
    log = Signal(str)

    def __init__(
        self,
        adb_path: str,
        serial: str,
        include_system: bool,
        preload_metadata: bool = False,
    ) -> None:
        super().__init__()
        self.adb_path = adb_path
        self.serial = serial
        self.include_system = include_system
        self.preload_metadata = preload_metadata
        self.cancel_requested = False

    def request_cancel(self) -> None:
        self.cancel_requested = True

    def run(self) -> None:
        try:
            adb = AdbClient(self.adb_path, self.serial)
            apps = adb.list_apps(
                include_system=self.include_system,
                progress=self.log.emit,
                should_cancel=lambda: self.cancel_requested,
            )
            if self.cancel_requested:
                self.cancelled.emit(apps)
                return
            if self.preload_metadata:
                loaded_apps: list[AppInfo] = []
                total = len(apps)
                for index, app in enumerate(apps, start=1):
                    if self.cancel_requested:
                        self.cancelled.emit(loaded_apps)
                        return
                    self.log.emit(f"正在解析应用名称 {index}/{total}：{app.package}")
                    loaded_apps.append(adb.load_app_metadata(app))
                apps = loaded_apps
            self.finished.emit(apps)
        except Exception as exc:
            self.failed.emit(str(exc) or "加载应用失败。")


class AppMetadataWorker(QObject):
    finished = Signal(int, object)
    failed = Signal(str)

    def __init__(self, adb_path: str, serial: str, row: int, app: AppInfo) -> None:
        super().__init__()
        self.adb_path = adb_path
        self.serial = serial
        self.row = row
        self.app = app

    def run(self) -> None:
        try:
            adb = AdbClient(self.adb_path, self.serial)
            self.finished.emit(self.row, adb.load_app_metadata(self.app))
        except Exception as exc:
            self.failed.emit(str(exc) or "读取应用元数据失败。")


class BackupWorker(QObject):
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal(list, str)
    progress = Signal(int, int, str)
    log = Signal(str)

    def __init__(self, adb_path: str, serial: str, apps: list[AppInfo], options: BackupOptions) -> None:
        super().__init__()
        self.adb_path = adb_path
        self.serial = serial
        self.apps = apps
        self.options = options
        self.service: BackupService | None = None
        self.cancel_requested = False

    def request_cancel(self) -> None:
        self.cancel_requested = True
        if self.service:
            self.service.request_cancel()

    def run(self) -> None:
        try:
            self.service = BackupService(AdbClient(self.adb_path, self.serial))
            if self.cancel_requested:
                self.service.request_cancel()
            zip_path = self.service.backup_apps(
                self.apps,
                self.options,
                log=self.log.emit,
                progress=self.progress.emit,
            )
            self.finished.emit(str(zip_path))
        except OperationCancelled as exc:
            self.cancelled.emit(exc.completed_apps, str(exc.archive_path or ""))
        except Exception as exc:
            self.failed.emit(str(exc) or "备份失败。")


class RestoreWorker(QObject):
    finished = Signal()
    failed = Signal(str)
    cancelled = Signal(list)
    progress = Signal(int, int, str)
    log = Signal(str)

    def __init__(self, adb_path: str, serial: str, zip_path: Path, restore_data: bool) -> None:
        super().__init__()
        self.adb_path = adb_path
        self.serial = serial
        self.zip_path = zip_path
        self.restore_data = restore_data
        self.service: BackupService | None = None
        self.cancel_requested = False

    def request_cancel(self) -> None:
        self.cancel_requested = True
        if self.service:
            self.service.request_cancel()

    def run(self) -> None:
        try:
            self.service = BackupService(AdbClient(self.adb_path, self.serial))
            if self.cancel_requested:
                self.service.request_cancel()
            self.service.restore_backup(
                self.zip_path,
                restore_data=self.restore_data,
                log=self.log.emit,
                progress=self.progress.emit,
            )
            self.finished.emit()
        except OperationCancelled as exc:
            self.cancelled.emit(exc.completed_apps)
        except Exception as exc:
            self.failed.emit(str(exc) or "恢复失败。")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("安卓 ADB 备份工具")
        self.resize(1080, 720)

        self.devices: list[Device] = []
        self.apps: list[AppInfo] = []
        self.worker_thread: QThread | None = None
        self.metadata_thread: QThread | None = None
        self.active_worker: QObject | None = None

        self.adb_path = QLineEdit("adb")
        self.adb_path.setPlaceholderText("adb 或 adb.exe 的完整路径")
        self.browse_adb_button = QPushButton("浏览")
        self.refresh_devices_button = QPushButton("刷新设备")
        self.device_combo = QComboBox()

        self.include_system = QCheckBox("显示系统应用")
        self.preload_app_labels = QCheckBox("加载时解析所有应用名称")
        self.include_data = QCheckBox("在允许时包含应用数据")
        self.include_obb = QCheckBox("包含 OBB 文件")
        self.include_obb.setChecked(True)
        self.auto_confirm_adb_backup = QCheckBox("ADB backup 时自动确认设备弹窗（实验性）")
        self.restore_data = QCheckBox("存在 .ab 数据时恢复")
        self.restore_data.setChecked(True)

        self.output_dir = QLineEdit(str(Path.home() / "AndroidBackups"))
        self.browse_output_button = QPushButton("浏览")

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("按包名、应用名称、版本或大小筛选")
        self.refresh_apps_button = QPushButton("加载应用")
        self.select_all_button = QPushButton("全选")
        self.clear_button = QPushButton("清除")
        self.backup_selected_button = QPushButton("备份选中")
        self.backup_all_button = QPushButton("备份全部")
        self.restore_button = QPushButton("恢复备份")
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setVisible(False)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["备份", "应用", "包名", "版本", "APK 数量", "大小"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.status_label = QLabel("就绪")
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)

        self._build_layout()
        self._connect_signals()
        self.refresh_devices()

    def _build_layout(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        adb_group = QGroupBox("连接")
        adb_layout = QGridLayout(adb_group)
        adb_layout.addWidget(QLabel("ADB"), 0, 0)
        adb_layout.addWidget(self.adb_path, 0, 1)
        adb_layout.addWidget(self.browse_adb_button, 0, 2)
        adb_layout.addWidget(QLabel("设备"), 1, 0)
        adb_layout.addWidget(self.device_combo, 1, 1)
        adb_layout.addWidget(self.refresh_devices_button, 1, 2)
        layout.addWidget(adb_group)

        options_group = QGroupBox("备份选项")
        options_layout = QGridLayout(options_group)
        options_layout.addWidget(QLabel("输出目录"), 0, 0)
        options_layout.addWidget(self.output_dir, 0, 1)
        options_layout.addWidget(self.browse_output_button, 0, 2)
        options_layout.addWidget(self.include_system, 1, 0)
        options_layout.addWidget(self.preload_app_labels, 1, 1)
        options_layout.addWidget(self.include_data, 1, 2)
        options_layout.addWidget(self.include_obb, 2, 0)
        options_layout.addWidget(self.auto_confirm_adb_backup, 2, 1, 1, 2)
        options_layout.addWidget(self.restore_data, 3, 1)
        layout.addWidget(options_group)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.search_box, 1)
        toolbar.addWidget(self.refresh_apps_button)
        toolbar.addWidget(self.select_all_button)
        toolbar.addWidget(self.clear_button)
        toolbar.addWidget(self.backup_selected_button)
        toolbar.addWidget(self.backup_all_button)
        toolbar.addWidget(self.restore_button)
        toolbar.addWidget(self.cancel_button)
        layout.addLayout(toolbar)

        layout.addWidget(self.table, 1)
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label)
        layout.addWidget(self.log_view, 1)

        self.setCentralWidget(root)

    def _connect_signals(self) -> None:
        self.browse_adb_button.clicked.connect(self.browse_adb)
        self.refresh_devices_button.clicked.connect(self.refresh_devices)
        self.browse_output_button.clicked.connect(self.browse_output)
        self.refresh_apps_button.clicked.connect(self.load_apps)
        self.select_all_button.clicked.connect(lambda: self.set_all_checked(True))
        self.clear_button.clicked.connect(lambda: self.set_all_checked(False))
        self.backup_selected_button.clicked.connect(lambda: self.start_backup(False))
        self.backup_all_button.clicked.connect(lambda: self.start_backup(True))
        self.restore_button.clicked.connect(self.start_restore)
        self.cancel_button.clicked.connect(self.cancel_current_operation)
        self.search_box.textChanged.connect(self.apply_filter)
        self.table.itemSelectionChanged.connect(self.load_selected_metadata)

    def browse_adb(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(self, "选择 adb.exe", str(Path.home()), "ADB (adb.exe adb);;所有文件 (*)")
        if file_name:
            self.adb_path.setText(file_name)

    def browse_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择备份输出文件夹", self.output_dir.text())
        if directory:
            self.output_dir.setText(directory)

    def refresh_devices(self) -> None:
        self.set_busy(True, "正在刷新设备...")
        worker = DeviceLoadWorker(self.adb_path.text().strip() or "adb")
        if not self.start_worker(worker, worker.run):
            return
        worker.log.connect(self.log)
        worker.finished.connect(self.on_devices_loaded)
        worker.failed.connect(self.on_worker_failed)
        self.begin_worker()

    def on_devices_loaded(self, devices: list[Device]) -> None:
        self.devices = devices
        self.device_combo.clear()
        for device in self.devices:
            self.device_combo.addItem(device.display_name, device.serial)
        self.set_busy(False, f"找到 {len(self.devices)} 台设备。")

    def load_apps(self) -> None:
        serial = self.current_serial()
        if not serial:
            self.show_error("请先连接并选择一台 ADB 设备。")
            return
        self.set_busy(True, "正在加载应用...")
        worker = AppLoadWorker(
            self.adb_path.text().strip() or "adb",
            serial,
            self.include_system.isChecked(),
            self.preload_app_labels.isChecked(),
        )
        if not self.start_worker(worker, worker.run):
            return
        worker.log.connect(self.log)
        worker.finished.connect(self.on_apps_loaded)
        worker.cancelled.connect(self.on_apps_load_cancelled)
        worker.failed.connect(self.on_worker_failed)
        self.begin_worker()

    def on_apps_loaded(self, apps: list[AppInfo]) -> None:
        self.apps = apps
        self.populate_table()
        self.set_busy(False, f"已加载 {len(apps)} 个应用。")

    def on_apps_load_cancelled(self, apps: list[AppInfo]) -> None:
        self.apps = apps
        self.populate_table()
        self.set_busy(False, f"应用加载已中断，已加载 {len(apps)} 个应用。")

    def populate_table(self) -> None:
        self.table.setRowCount(0)
        for app in self.apps:
            row = self.table.rowCount()
            self.table.insertRow(row)

            checkbox_item = QTableWidgetItem()
            checkbox_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            checkbox_item.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, checkbox_item)
            self.table.setItem(row, 1, QTableWidgetItem(app.display_name))
            self.table.setItem(row, 2, QTableWidgetItem(app.package or ""))
            self.table.setItem(row, 3, QTableWidgetItem(app.display_version or ""))
            self.table.setItem(row, 4, QTableWidgetItem(str(len(app.apk_paths or []))))
            self.table.setItem(row, 5, QTableWidgetItem(app.display_package_size))
        self.apply_filter(self.search_box.text())

    def load_selected_metadata(self) -> None:
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            return
        row = selected_rows[0].row()
        if row < 0 or row >= len(self.apps):
            return
        app = self.apps[row]
        serial = self.current_serial()
        if not serial or app.metadata_loaded:
            return
        if self.metadata_thread and self.metadata_thread.isRunning():
            return

        self.status_label.setText(f"正在读取 {app.package} 的应用信息...")
        worker = AppMetadataWorker(self.adb_path.text().strip() or "adb", serial, row, app)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self.on_metadata_loaded)
        worker.failed.connect(self.log)
        worker.finished.connect(lambda *_args: thread.quit())
        worker.failed.connect(lambda *_args: thread.quit())
        thread.finished.connect(lambda: self._clear_metadata_thread(thread))
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.metadata_thread = thread
        thread.start()

    def _clear_metadata_thread(self, thread: QThread) -> None:
        if self.metadata_thread is thread:
            self.metadata_thread = None

    def on_metadata_loaded(self, row: int, app: AppInfo) -> None:
        if row < 0 or row >= len(self.apps) or self.apps[row].package != app.package:
            return
        self.apps[row] = app
        self.table.item(row, 1).setText(app.display_name)
        self.table.item(row, 3).setText(app.display_version or "")
        self.table.item(row, 4).setText(str(len(app.apk_paths or [])))
        self.table.item(row, 5).setText(app.display_package_size)
        self.status_label.setText(f"已读取 {app.package} 的应用信息。")
        self.apply_filter(self.search_box.text())

    def apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row, app in enumerate(self.apps):
            visible = not needle or needle in " ".join(
                [
                    app.display_name,
                    app.name or "",
                    app.localized_name or "",
                    app.package or "",
                    app.display_version or "",
                    app.display_package_size,
                ]
            ).lower()
            self.table.setRowHidden(row, not visible)

    def set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                self.table.item(row, 0).setCheckState(state)

    def selected_apps(self) -> list[AppInfo]:
        selected: list[AppInfo] = []
        for row, app in enumerate(self.apps):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected.append(app)
        return selected

    def start_backup(self, all_apps: bool) -> None:
        serial = self.current_serial()
        if not serial:
            self.show_error("请先连接并选择一台 ADB 设备。")
            return
        apps = list(self.apps) if all_apps else self.selected_apps()
        if not apps:
            self.show_error("请至少选择一个要备份的应用。")
            return

        options = BackupOptions(
            output_dir=Path(self.output_dir.text()).expanduser(),
            include_data=self.include_data.isChecked(),
            include_obb=self.include_obb.isChecked(),
            auto_confirm_adb_backup=self.auto_confirm_adb_backup.isChecked(),
        )
        self.set_busy(True, "正在开始备份...")
        worker = BackupWorker(self.adb_path.text().strip() or "adb", serial, apps, options)
        if not self.start_worker(worker, worker.run):
            return
        worker.log.connect(self.log)
        worker.progress.connect(self.on_progress)
        worker.finished.connect(self.on_backup_finished)
        worker.cancelled.connect(self.on_backup_cancelled)
        worker.failed.connect(self.on_worker_failed)
        self.begin_worker()

    def start_restore(self) -> None:
        serial = self.current_serial()
        if not serial:
            self.show_error("请先连接并选择一台 ADB 设备。")
            return
        file_name, _ = QFileDialog.getOpenFileName(self, "选择备份压缩包", self.output_dir.text(), "压缩包 (*.zip)")
        if not file_name:
            return
        self.set_busy(True, "正在开始恢复...")
        worker = RestoreWorker(
            self.adb_path.text().strip() or "adb",
            serial,
            Path(file_name),
            self.restore_data.isChecked(),
        )
        if not self.start_worker(worker, worker.run):
            return
        worker.log.connect(self.log)
        worker.progress.connect(self.on_progress)
        worker.finished.connect(self.on_restore_finished)
        worker.cancelled.connect(self.on_restore_cancelled)
        worker.failed.connect(self.on_worker_failed)
        self.begin_worker()

    def start_worker(self, worker: QObject, run_slot) -> bool:
        try:
            if self.worker_thread and self.worker_thread.isRunning():
                self.show_error("已有另一个操作正在运行。")
                return False
        except RuntimeError:
            self.worker_thread = None

        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(run_slot)
        thread.finished.connect(thread.deleteLater)
        self.worker_thread = thread
        self.worker_thread.finished.connect(lambda: setattr(self, "worker_thread", None))
        self.active_worker = worker
        self.cancel_button.setEnabled(hasattr(worker, "request_cancel"))

        def cleanup() -> None:
            try:
                thread.quit()
                worker.deleteLater()
            except RuntimeError:
                pass
            finally:
                self.active_worker = None
                self.worker_thread = None

        if hasattr(worker, "finished"):
            worker.finished.connect(lambda *_args: cleanup())
        if hasattr(worker, "failed"):
            worker.failed.connect(lambda *_args: cleanup())
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(lambda *_args: cleanup())
        return True

    def begin_worker(self) -> None:
        try:
            if self.worker_thread:
                self.worker_thread.start()
        except RuntimeError:
            self.worker_thread = None

    def cancel_current_operation(self) -> None:
        worker = self.active_worker
        if worker and hasattr(worker, "request_cancel"):
            worker.request_cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("正在中断...")
            self.log("正在中断...")

    def on_progress(self, current: int, total: int, message: str) -> None:
        percent = int((current / total) * 100) if total else 0
        self.progress.setValue(percent)
        self.status_label.setText(message)

    def on_backup_finished(self, zip_path: str) -> None:
        self.set_busy(False, f"备份完成：{zip_path}")
        QMessageBox.information(self, "备份完成", f"已创建备份归档：\n{zip_path}")

    def on_backup_cancelled(self, completed_apps: list[str], archive_path: str) -> None:
        completed_text = "\n".join(completed_apps) if completed_apps else "无"
        archive_text = f"\n\n已保存部分归档：\n{archive_path}" if archive_path else ""
        self.set_busy(False, f"备份已中断，已完成 {len(completed_apps)} 个应用。")
        self.log(f"已完成应用：{', '.join(completed_apps) if completed_apps else '无'}")
        QMessageBox.information(self, "备份已中断", f"已完成应用：\n{completed_text}{archive_text}")

    def on_restore_finished(self) -> None:
        self.set_busy(False, "恢复完成。")
        QMessageBox.information(self, "恢复完成", "恢复操作已完成。")

    def on_restore_cancelled(self, completed_apps: list[str]) -> None:
        completed_text = "\n".join(completed_apps) if completed_apps else "无"
        self.set_busy(False, f"恢复已中断，已完成 {len(completed_apps)} 个应用。")
        QMessageBox.information(self, "恢复已中断", f"已完成应用：\n{completed_text}")

    def on_worker_failed(self, message: str) -> None:
        self.set_busy(False, "操作失败。")
        self.show_error(message)

    def set_busy(self, busy: bool, status: str) -> None:
        for widget in [
            self.refresh_devices_button,
            self.refresh_apps_button,
            self.backup_selected_button,
            self.backup_all_button,
            self.restore_button,
            self.browse_adb_button,
            self.browse_output_button,
        ]:
            widget.setEnabled(not busy)
        if busy:
            self.progress.setValue(0)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy and self.active_worker is not None and hasattr(self.active_worker, "request_cancel"))
        self.status_label.setText(status)
        self.log(status)

    def current_serial(self) -> str:
        return str(self.device_combo.currentData() or "")

    def log(self, message: str) -> None:
        self.log_view.append(message)

    def show_error(self, message: str) -> None:
        self.log(message)
        QMessageBox.critical(self, "安卓 ADB 备份工具", message)


def run_app() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
