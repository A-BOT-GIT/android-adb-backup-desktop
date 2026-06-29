from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import stat
import tarfile
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from .adb import AdbClient, AdbError
from .logging_utils import configure_file_logging
from .models import AppInfo, BackupOptions


configure_file_logging()
logger = logging.getLogger(__name__)

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, str], None]

MAX_ZIP_ENTRIES = 10000
MAX_ZIP_UNCOMPRESSED_SIZE = 20 * 1024 * 1024 * 1024
MAX_ZIP_ENTRY_SIZE = 4 * 1024 * 1024 * 1024
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def validate_package_name(package: str) -> bool:
    return bool(PACKAGE_RE.fullmatch(package))


class OperationCancelled(RuntimeError):
    def __init__(
        self,
        message: str = "操作已取消。",
        *,
        completed_apps: list[str] | None = None,
        archive_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.completed_apps = completed_apps or []
        self.archive_path = archive_path


class BackupService:
    def __init__(self, adb: AdbClient) -> None:
        self.adb = adb
        self.cancel_requested = False
        self.completed_apps: list[str] = []
        self._completed_lock = threading.Lock()

    def request_cancel(self) -> None:
        self.cancel_requested = True

    def _check_cancel(self) -> None:
        if self.cancel_requested:
            with self._completed_lock:
                completed = list(self.completed_apps)
            raise OperationCancelled("操作已取消。", completed_apps=completed)

    def backup_apps(
        self,
        apps: list[AppInfo],
        options: BackupOptions,
        *,
        log: LogCallback | None = None,
        progress: ProgressCallback | None = None,
    ) -> Path:
        if not apps:
            raise ValueError("没有选择要备份的应用。")

        self.completed_apps = []
        options.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        zip_path = options.output_dir / f"android-app-backup-{timestamp}.zip"
        operation_start = time.perf_counter()
        self._log(
            log,
            "开始备份："
            f"应用数={len(apps)} "
            f"include_data={options.include_data} "
            f"include_obb={options.include_obb} "
            f"auto_confirm_adb_backup={options.auto_confirm_adb_backup} "
            f"输出={zip_path}",
        )

        loaded_apps: list[AppInfo] = []
        for index, app in enumerate(apps, start=1):
            self._check_cancel()
            if app.metadata_loaded:
                loaded_apps.append(app)
            else:
                self._log(log, f"正在加载应用元数据 {index}/{len(apps)}：{app.package}")
                loaded_app = self.adb.load_app_metadata(app)
                # 验证元数据字段非空
                if not loaded_app.name:
                    loaded_app.name = loaded_app.package
                if not loaded_app.version_name:
                    loaded_app.version_name = ""
                if not loaded_app.version_code:
                    loaded_app.version_code = ""
                loaded_app.metadata_loaded = True
                loaded_apps.append(loaded_app)

        with tempfile.TemporaryDirectory(prefix="android-app-backup-") as tmp_name:
            staging = Path(tmp_name)
            apps_dir = staging / "apps"
            manifest: dict[str, object] = {
                "format": "android-backup-desktop",
                "format_version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "completed",
                "completed_apps": [],
                "apps": [],
            }

            total_steps = len(loaded_apps)
            try:
                for index, app in enumerate(loaded_apps, start=1):
                    self._check_cancel()
                    app_start = time.perf_counter()
                    if progress:
                        progress(index - 1, total_steps, f"正在备份 {app.package}")
                    self._log(log, f"开始备份应用 {index}/{total_steps}：{app.package}")
                    app_dir = apps_dir / safe_name(app.package)
                    tmp_app_dir = apps_dir / ".tmp" / safe_name(app.package)
                    if tmp_app_dir.exists():
                        shutil.rmtree(tmp_app_dir)
                    if app_dir.exists():
                        shutil.rmtree(app_dir)

                    try:
                        apk_dir = tmp_app_dir / "apk"
                        apk_files = self._backup_apks(app, apk_dir, log)

                        data_files: list[str] = []
                        self._check_cancel()
                        if options.include_data:
                            data_files = self._backup_data(app, tmp_app_dir / "data", options, log)

                        obb_files: list[str] = []
                        self._check_cancel()
                        if options.include_obb:
                            obb_files = self._backup_obb(app, tmp_app_dir / "obb", log)

                        self._check_cancel()
                        app_dir.parent.mkdir(parents=True, exist_ok=True)
                        tmp_app_dir.replace(app_dir)
                    except Exception:
                        shutil.rmtree(tmp_app_dir, ignore_errors=True)
                        raise

                    manifest["apps"].append(
                        {
                            "package": app.package or "",
                            "name": app.name or "",
                            "localized_name": app.localized_name or "",
                            "version_name": app.version_name or "",
                            "version_code": app.version_code or "",
                            "package_size_bytes": app.package_size_bytes,
                            "apk_files": apk_files,
                            "data_files": data_files,
                            "obb_files": obb_files,
                        }
                    )
                    with self._completed_lock:
                        self.completed_apps.append(app.package)
                        manifest["completed_apps"] = list(self.completed_apps)
                    app_size = self._directory_size(app_dir)
                    elapsed = time.perf_counter() - app_start
                    self._log(
                        log,
                        f"完成备份应用 {index}/{total_steps}：{app.package} 文件={len(apk_files) + len(data_files) + len(obb_files)} 大小={app_size}B 耗时={elapsed:.2f}s",
                    )
                    if progress:
                        progress(index, total_steps, f"已完成 {app.package}")
            except OperationCancelled as exc:
                manifest["status"] = "cancelled"
                with self._completed_lock:
                    completed = list(self.completed_apps)
                manifest["completed_apps"] = completed
                shutil.rmtree(apps_dir / ".tmp", ignore_errors=True)
                (staging / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self._zip_directory(staging, zip_path)
                if progress:
                    progress(len(completed), total_steps, "已中断。")
                self._log(log, f"备份已中断：已完成={len(completed)}/{total_steps} 归档={zip_path}")
                raise OperationCancelled(
                    "备份已中断。",
                    completed_apps=completed,
                    archive_path=zip_path,
                ) from exc

            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._zip_directory(staging, zip_path)

        elapsed = time.perf_counter() - operation_start
        size = zip_path.stat().st_size if zip_path.exists() else 0
        self._log(log, f"备份结束：归档={zip_path} 大小={size}B 耗时={elapsed:.2f}s")
        return zip_path

    def restore_backup(
        self,
        zip_path: Path,
        *,
        restore_data: bool = True,
        log: LogCallback | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        if not zip_path.exists():
            raise FileNotFoundError(zip_path)

        self.completed_apps = []
        operation_start = time.perf_counter()
        self._log(log, f"开始恢复：归档={zip_path} restore_data={restore_data}")
        with tempfile.TemporaryDirectory(prefix="android-app-restore-") as tmp_name:
            staging = Path(tmp_name)
            with zipfile.ZipFile(zip_path, "r") as archive:
                self._safe_extract_zip(archive, staging)

            manifest_path = staging / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            apps = manifest.get("apps", [])
            if not isinstance(apps, list):
                raise ValueError("备份清单无效。")

            for index, app_entry in enumerate(apps, start=1):
                self._check_cancel()
                app_start = time.perf_counter()
                if not isinstance(app_entry, dict):
                    raise ValueError("备份清单中的应用条目无效。")
                package = str(app_entry.get("package", ""))
                if not validate_package_name(package):
                    raise ValueError(f"备份清单中的包名无效：{package!r}")
                if progress:
                    progress(index - 1, len(apps), f"正在恢复 {package}")
                self._log(log, f"开始恢复应用 {index}/{len(apps)}：{package}")

                app_dir = staging / "apps" / safe_name(package)
                apk_files = sorted((app_dir / "apk").glob("*.apk"))
                if apk_files:
                    apk_size = sum(path.stat().st_size for path in apk_files)
                    self._log(log, f"正在安装 {package} 的 APK：文件={len(apk_files)} 大小={apk_size}B")
                    self.adb.install(apk_files)
                    self._log(log, f"已安装 {package} 的 APK")

                obb_dir = app_dir / "obb"
                if obb_dir.exists():
                    remote_obb = f"/sdcard/Android/obb/{package}"
                    obb_files = sorted(path for path in obb_dir.rglob("*") if path.is_file())
                    for child in obb_files:
                        self._check_cancel()
                        relative_parent = child.relative_to(obb_dir).parent.as_posix()
                        remote_parent = remote_obb if relative_parent == "." else f"{remote_obb}/{relative_parent}"
                        self.adb.shell("mkdir", "-p", shlex.quote(remote_parent), timeout=30, check=False)
                        self.adb.push(child, remote_parent, timeout=None)
                    obb_size = sum(path.stat().st_size for path in obb_files)
                    self._log(log, f"已恢复 {package} 的 OBB 文件：文件={len(obb_files)} 大小={obb_size}B")

                if restore_data:
                    run_as_tar = app_dir / "data" / "run-as-data.tar"
                    if run_as_tar.exists():
                        self._check_cancel()
                        self._validate_run_as_tar(run_as_tar)
                        size = run_as_tar.stat().st_size
                        self._log(log, f"正在通过 run-as 恢复 {package} 的数据 大小={size}B")
                        self.adb.restore_run_as_data(package, run_as_tar)

                    for ab_file in sorted((app_dir / "data").glob("*.ab")):
                        self._check_cancel()
                        size = ab_file.stat().st_size
                        self._log(log, f"正在通过 adb 恢复 {ab_file.name} 大小={size}B；如设备提示，请在设备上确认。")
                        self.adb.adb_restore(ab_file)

                with self._completed_lock:
                    self.completed_apps.append(package)
                elapsed = time.perf_counter() - app_start
                self._log(log, f"完成恢复应用 {index}/{len(apps)}：{package} 耗时={elapsed:.2f}s")
                if progress:
                    progress(index, len(apps), f"已完成 {package}")
        elapsed = time.perf_counter() - operation_start
        self._log(log, f"恢复结束：归档={zip_path} 耗时={elapsed:.2f}s")

    def _backup_apks(self, app: AppInfo, apk_dir: Path, log: LogCallback | None) -> list[str]:
        apk_paths = app.apk_paths or self.adb.apk_paths(app.package)
        if not apk_paths:
            raise AdbError(f"未找到 {app.package} 的 APK 路径")

        apk_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for remote_path in apk_paths:
            self._check_cancel()
            filename = Path(remote_path).name or "base.apk"
            local = apk_dir / filename
            start = time.perf_counter()
            self._log(log, f"开始拉取 APK：{remote_path}")
            self.adb.pull(remote_path, local, timeout=None)
            size = local.stat().st_size if local.exists() else 0
            self._log(log, f"完成拉取 APK：{remote_path} -> {local} 大小={size}B 耗时={time.perf_counter() - start:.2f}s")
            copied.append(self._backup_relative_path(local, apk_dir))
        return copied

    def _backup_data(
        self,
        app: AppInfo,
        data_dir: Path,
        options: BackupOptions,
        log: LogCallback | None,
    ) -> list[str]:
        self._check_cancel()
        data_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []

        run_as_tar = data_dir / "run-as-data.tar"
        self._log(log, f"正在尝试通过 run-as 导出 {app.package} 的数据")
        if self.adb.export_run_as_data(app.package, run_as_tar):
            size = run_as_tar.stat().st_size if run_as_tar.exists() else 0
            self._log(log, f"{app.package} 的 run-as 数据导出成功 大小={size}B")
            copied.append(self._backup_relative_path(run_as_tar, data_dir))
            return copied

        ab_file = data_dir / "adb-backup.ab"
        self._log(log, f"正在尝试通过 adb backup 备份 {app.package}；如设备提示，请在设备上确认。")
        try:
            self.adb.adb_backup_package(
                app.package,
                ab_file,
                include_apk=False,
                auto_confirm=options.auto_confirm_adb_backup,
                log=log,
            )
        except AdbError as exc:
            self._log(log, f"已跳过 {app.package} 的数据备份：{exc}")
            ab_file.unlink(missing_ok=True)
            return copied

        if ab_file.exists() and ab_file.stat().st_size > 1024:
            self._log(log, f"{app.package} 的 adb backup 数据导出成功 大小={ab_file.stat().st_size}B")
            copied.append(self._backup_relative_path(ab_file, data_dir))
        else:
            self._log(log, f"{app.package} 的数据备份为空，或已被 Android 拒绝。")
            ab_file.unlink(missing_ok=True)
        return copied

    def _backup_obb(self, app: AppInfo, obb_dir: Path, log: LogCallback | None) -> list[str]:
        remote_obb = f"/sdcard/Android/obb/{app.package}"
        if not self.adb.path_exists(remote_obb):
            return []

        obb_dir.mkdir(parents=True, exist_ok=True)
        remote_files = [
            line.strip()
            for line in self.adb.shell("find", remote_obb, "-type", "f", timeout=60, check=False).splitlines()
            if line.strip().startswith(remote_obb)
        ]
        copied: list[str] = []
        for remote_file in remote_files:
            self._check_cancel()
            relative_name = remote_file.removeprefix(remote_obb).lstrip("/")
            local = obb_dir / relative_name
            start = time.perf_counter()
            self._log(log, f"开始拉取 OBB 文件：{remote_file}")
            self.adb.pull(remote_file, local, timeout=None)
            size = local.stat().st_size if local.exists() else 0
            self._log(log, f"完成拉取 OBB 文件：{remote_file} -> {local} 大小={size}B 耗时={time.perf_counter() - start:.2f}s")
            copied.append(self._backup_relative_path(local, obb_dir))
        return copied

    @staticmethod
    def _backup_relative_path(path: Path, content_dir: Path) -> str:
        app_dir = content_dir.parent
        if app_dir.parent.name == ".tmp":
            staging = app_dir.parent.parent.parent
            final_path = staging / "apps" / app_dir.name / content_dir.name / path.relative_to(content_dir)
            return final_path.relative_to(staging).as_posix()
        staging = app_dir.parent.parent
        return path.relative_to(staging).as_posix()

    @staticmethod
    def _zip_directory(source: Path, target: Path) -> None:
        if target.exists():
            target.unlink()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in source.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(source))

    def _safe_extract_zip(self, archive: zipfile.ZipFile, target: Path) -> None:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ValueError(f"备份归档文件数量过多：{len(infos)} > {MAX_ZIP_ENTRIES}")

        total_size = 0
        target_root = target.resolve()
        for info in infos:
            self._validate_zip_member(info)
            if not info.is_dir():
                if info.file_size > MAX_ZIP_ENTRY_SIZE:
                    raise ValueError(f"备份归档单个文件过大：{info.filename}")
                total_size += info.file_size
                if total_size > MAX_ZIP_UNCOMPRESSED_SIZE:
                    raise ValueError(
                        f"备份归档解压后过大：{total_size}B > {MAX_ZIP_UNCOMPRESSED_SIZE}B"
                    )

            destination = (target / info.filename).resolve()
            if not destination.is_relative_to(target_root):
                raise ValueError(f"备份归档包含不安全路径：{info.filename}")

            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)

        self._log(None, f"归档安全校验通过：条目={len(infos)} 解压大小={total_size}B")

    @staticmethod
    def _validate_zip_member(info: zipfile.ZipInfo) -> None:
        name = info.filename
        parts = PurePosixPath(name).parts
        if not name or name.startswith("/") or "\\" in name or ":" in name:
            raise ValueError(f"备份归档包含不安全路径：{name}")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"备份归档包含不安全路径：{name}")

        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise ValueError(f"备份归档包含不支持的符号链接：{name}")

    @staticmethod
    def _validate_run_as_tar(path: Path) -> None:
        try:
            with tarfile.open(path, "r:*") as archive:
                for member in archive.getmembers():
                    name = member.name
                    parts = PurePosixPath(name).parts
                    if not name or name.startswith("/") or "\\" in name or ":" in name:
                        raise ValueError(f"run-as 数据归档包含不安全路径：{name}")
                    if any(part in {"", ".."} for part in parts):
                        raise ValueError(f"run-as 数据归档包含不安全路径：{name}")
                    if member.issym() or member.islnk():
                        raise ValueError(f"run-as 数据归档包含不支持的链接：{name}")
                    if not (member.isfile() or member.isdir()):
                        raise ValueError(f"run-as 数据归档包含不支持的条目：{name}")
        except tarfile.TarError as exc:
            raise ValueError(f"run-as 数据归档无效：{path.name}") from exc

    @staticmethod
    def _directory_size(path: Path) -> int:
        return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())

    @staticmethod
    def _log(log: LogCallback | None, message: str) -> None:
        logger.info(message)
        if log:
            log(message)
