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
from .backup import BackupService
from .models import AppInfo, BackupOptions, Device


class AppLoadWorker(QObject):
    finished = Signal(list)
    failed = Signal(str)
    log = Signal(str)

    def __init__(self, adb_path: str, serial: str, include_system: bool) -> None:
        super().__init__()
        self.adb_path = adb_path
        self.serial = serial
        self.include_system = include_system

    def run(self) -> None:
        try:
            adb = AdbClient(self.adb_path, self.serial)
            apps = adb.list_apps(include_system=self.include_system, progress=self.log.emit)
            self.finished.emit(apps)
        except Exception as exc:
            self.failed.emit(str(exc) or "加载应用失败。")


class BackupWorker(QObject):
    finished = Signal(str)
    failed = Signal(str)
    progress = Signal(int, int, str)
    log = Signal(str)

    def __init__(self, adb_path: str, serial: str, apps: list[AppInfo], options: BackupOptions) -> None:
        super().__init__()
        self.adb_path = adb_path
        self.serial = serial
        self.apps = apps
        self.options = options

    def run(self) -> None:
        try:
            service = BackupService(AdbClient(self.adb_path, self.serial))
            zip_path = service.backup_apps(
                self.apps,
                self.options,
                log=self.log.emit,
                progress=self.progress.emit,
            )
            self.finished.emit(str(zip_path))
        except Exception as exc:
            self.failed.emit(str(exc) or "备份失败。")


class RestoreWorker(QObject):
    finished = Signal()
    failed = Signal(str)
    progress = Signal(int, int, str)
    log = Signal(str)

    def __init__(self, adb_path: str, serial: str, zip_path: Path, restore_data: bool) -> None:
        super().__init__()
        self.adb_path = adb_path
        self.serial = serial
        self.zip_path = zip_path
        self.restore_data = restore_data

    def run(self) -> None:
        try:
            service = BackupService(AdbClient(self.adb_path, self.serial))
            service.restore_backup(
                self.zip_path,
                restore_data=self.restore_data,
                log=self.log.emit,
                progress=self.progress.emit,
            )
            self.finished.emit()
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

        self.adb_path = QLineEdit("adb")
        self.adb_path.setPlaceholderText("adb 或 adb.exe 的完整路径")
        self.browse_adb_button = QPushButton("浏览")
        self.refresh_devices_button = QPushButton("刷新设备")
        self.device_combo = QComboBox()

        self.include_system = QCheckBox("显示系统应用")
        self.include_data = QCheckBox("在允许时包含应用数据")
        self.include_obb = QCheckBox("包含 OBB 文件")
        self.include_obb.setChecked(True)
        self.restore_data = QCheckBox("存在 .ab 数据时恢复")
        self.restore_data.setChecked(True)

        self.output_dir = QLineEdit(str(Path.home() / "AndroidBackups"))
        self.browse_output_button = QPushButton("浏览")

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("按包名、应用名称或版本筛选")
        self.refresh_apps_button = QPushButton("加载应用")
        self.select_all_button = QPushButton("全选")
        self.clear_button = QPushButton("清除")
        self.backup_selected_button = QPushButton("备份选中")
        self.backup_all_button = QPushButton("备份全部")
        self.restore_button = QPushButton("恢复备份")

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["备份", "应用", "包名", "版本", "APK 数量"])
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
        options_layout.addWidget(self.include_data, 1, 1)
        options_layout.addWidget(self.include_obb, 1, 2)
        options_layout.addWidget(self.restore_data, 2, 1)
        layout.addWidget(options_group)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.search_box, 1)
        toolbar.addWidget(self.refresh_apps_button)
        toolbar.addWidget(self.select_all_button)
        toolbar.addWidget(self.clear_button)
        toolbar.addWidget(self.backup_selected_button)
        toolbar.addWidget(self.backup_all_button)
        toolbar.addWidget(self.restore_button)
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
        self.search_box.textChanged.connect(self.apply_filter)

    def browse_adb(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(self, "选择 adb.exe", str(Path.home()), "ADB (adb.exe adb);;所有文件 (*)")
        if file_name:
            self.adb_path.setText(file_name)

    def browse_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择备份输出文件夹", self.output_dir.text())
        if directory:
            self.output_dir.setText(directory)

    def refresh_devices(self) -> None:
        try:
            adb = AdbClient(self.adb_path.text().strip() or "adb")
            adb.ensure_available()
            self.devices = adb.devices()
        except AdbError as exc:
            self.show_error(str(exc))
            return

        self.device_combo.clear()
        for device in self.devices:
            self.device_combo.addItem(device.display_name, device.serial)
        self.log(f"找到 {len(self.devices)} 台设备。")

    def load_apps(self) -> None:
        serial = self.current_serial()
        if not serial:
            self.show_error("请先连接并选择一台 ADB 设备。")
            return
        self.set_busy(True, "正在加载应用...")
        worker = AppLoadWorker(self.adb_path.text().strip() or "adb", serial, self.include_system.isChecked())
        self.start_worker(worker, worker.run)
        worker.log.connect(self.log)
        worker.finished.connect(self.on_apps_loaded)
        worker.failed.connect(self.on_worker_failed)

    def on_apps_loaded(self, apps: list[AppInfo]) -> None:
        self.apps = apps
        self.populate_table()
        self.set_busy(False, f"已加载 {len(apps)} 个应用。")

    def populate_table(self) -> None:
        self.table.setRowCount(0)
        for app in self.apps:
            row = self.table.rowCount()
            self.table.insertRow(row)

            checkbox_item = QTableWidgetItem()
            checkbox_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            checkbox_item.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, checkbox_item)
            self.table.setItem(row, 1, QTableWidgetItem(app.name))
            self.table.setItem(row, 2, QTableWidgetItem(app.package))
            self.table.setItem(row, 3, QTableWidgetItem(app.display_version))
            self.table.setItem(row, 4, QTableWidgetItem(str(len(app.apk_paths))))
        self.apply_filter(self.search_box.text())

    def apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row, app in enumerate(self.apps):
            visible = not needle or needle in " ".join([app.name, app.package, app.display_version]).lower()
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
        )
        self.set_busy(True, "正在开始备份...")
        worker = BackupWorker(self.adb_path.text().strip() or "adb", serial, apps, options)
        self.start_worker(worker, worker.run)
        worker.log.connect(self.log)
        worker.progress.connect(self.on_progress)
        worker.finished.connect(self.on_backup_finished)
        worker.failed.connect(self.on_worker_failed)

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
        self.start_worker(worker, worker.run)
        worker.log.connect(self.log)
        worker.progress.connect(self.on_progress)
        worker.finished.connect(self.on_restore_finished)
        worker.failed.connect(self.on_worker_failed)

    def start_worker(self, worker: QObject, run_slot) -> None:
        if self.worker_thread and self.worker_thread.isRunning():
            self.show_error("已有另一个操作正在运行。")
            return
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(run_slot)
        thread.finished.connect(thread.deleteLater)
        self.worker_thread = thread

        def cleanup() -> None:
            thread.quit()
            worker.deleteLater()

        if hasattr(worker, "finished"):
            worker.finished.connect(lambda *_args: cleanup())
        if hasattr(worker, "failed"):
            worker.failed.connect(lambda *_args: cleanup())
        thread.start()

    def on_progress(self, current: int, total: int, message: str) -> None:
        percent = int((current / total) * 100) if total else 0
        self.progress.setValue(percent)
        self.status_label.setText(message)

    def on_backup_finished(self, zip_path: str) -> None:
        self.set_busy(False, f"备份完成：{zip_path}")
        QMessageBox.information(self, "备份完成", f"已创建备份归档：\n{zip_path}")

    def on_restore_finished(self) -> None:
        self.set_busy(False, "恢复完成。")
        QMessageBox.information(self, "恢复完成", "恢复操作已完成。")

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
