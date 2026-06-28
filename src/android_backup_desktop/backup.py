from __future__ import annotations

import json
import logging
import re
import shutil
import stat
import tempfile
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


class BackupService:
    def __init__(self, adb: AdbClient) -> None:
        self.adb = adb

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

        options.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        zip_path = options.output_dir / f"android-app-backup-{timestamp}.zip"
        operation_start = time.perf_counter()
        self._log(
            log,
            f"开始备份：应用数={len(apps)} include_data={options.include_data} include_obb={options.include_obb} 输出={zip_path}",
        )

        with tempfile.TemporaryDirectory(prefix="android-app-backup-") as tmp_name:
            staging = Path(tmp_name)
            apps_dir = staging / "apps"
            manifest: dict[str, object] = {
                "format": "android-backup-desktop",
                "format_version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "apps": [],
            }

            total_steps = len(apps)
            for index, app in enumerate(apps, start=1):
                app_start = time.perf_counter()
                if progress:
                    progress(index - 1, total_steps, f"正在备份 {app.package}")
                self._log(log, f"开始备份应用 {index}/{total_steps}：{app.package}")
                app_dir = apps_dir / safe_name(app.package)
                apk_dir = app_dir / "apk"
                apk_files = self._backup_apks(app, apk_dir, log)

                data_files: list[str] = []
                if options.include_data:
                    data_files = self._backup_data(app, app_dir / "data", log)

                obb_files: list[str] = []
                if options.include_obb:
                    obb_files = self._backup_obb(app, app_dir / "obb", log)

                manifest["apps"].append(
                    {
                        "package": app.package or "",
                        "name": app.name or "",
                        "version_name": app.version_name or "",
                        "version_code": app.version_code or "",
                        "apk_files": apk_files,
                        "data_files": data_files,
                        "obb_files": obb_files,
                    }
                )
                app_size = self._directory_size(app_dir)
                elapsed = time.perf_counter() - app_start
                self._log(
                    log,
                    f"完成备份应用 {index}/{total_steps}：{app.package} 文件={len(apk_files) + len(data_files) + len(obb_files)} 大小={app_size}B 耗时={elapsed:.2f}s",
                )
                if progress:
                    progress(index, total_steps, f"已完成 {app.package}")

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
                        relative_parent = child.relative_to(obb_dir).parent.as_posix()
                        remote_parent = remote_obb if relative_parent == "." else f"{remote_obb}/{relative_parent}"
                        self.adb.shell("mkdir", "-p", remote_parent, timeout=30, check=False)
                        self.adb.push(child, remote_parent, timeout=None)
                    obb_size = sum(path.stat().st_size for path in obb_files)
                    self._log(log, f"已恢复 {package} 的 OBB 文件：文件={len(obb_files)} 大小={obb_size}B")

                if restore_data:
                    for ab_file in sorted((app_dir / "data").glob("*.ab")):
                        size = ab_file.stat().st_size
                        self._log(log, f"正在通过 adb 恢复 {ab_file.name} 大小={size}B；如设备提示，请在设备上确认。")
                        self.adb.adb_restore(ab_file)

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
            filename = Path(remote_path).name or "base.apk"
            local = apk_dir / filename
            start = time.perf_counter()
            self._log(log, f"开始拉取 APK：{remote_path}")
            self.adb.pull(remote_path, local, timeout=None)
            size = local.stat().st_size if local.exists() else 0
            self._log(log, f"完成拉取 APK：{remote_path} -> {local} 大小={size}B 耗时={time.perf_counter() - start:.2f}s")
            copied.append(str(local.relative_to(apk_dir.parent.parent.parent)))
        return copied

    def _backup_data(self, app: AppInfo, data_dir: Path, log: LogCallback | None) -> list[str]:
        data_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []

        run_as_tar = data_dir / "run-as-data.tar"
        self._log(log, f"正在尝试通过 run-as 导出 {app.package} 的数据")
        if self.adb.export_run_as_data(app.package, run_as_tar):
            size = run_as_tar.stat().st_size if run_as_tar.exists() else 0
            self._log(log, f"{app.package} 的 run-as 数据导出成功 大小={size}B")
            copied.append(str(run_as_tar.relative_to(data_dir.parent.parent.parent)))
            return copied

        ab_file = data_dir / "adb-backup.ab"
        self._log(log, f"正在尝试通过 adb backup 备份 {app.package}；如设备提示，请在设备上确认。")
        try:
            self.adb.adb_backup_package(app.package, ab_file, include_apk=False)
        except AdbError as exc:
            self._log(log, f"已跳过 {app.package} 的数据备份：{exc}")
            ab_file.unlink(missing_ok=True)
            return copied

        if ab_file.exists() and ab_file.stat().st_size > 1024:
            self._log(log, f"{app.package} 的 adb backup 数据导出成功 大小={ab_file.stat().st_size}B")
            copied.append(str(ab_file.relative_to(data_dir.parent.parent.parent)))
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
            relative_name = remote_file.removeprefix(remote_obb).lstrip("/")
            local = obb_dir / relative_name
            start = time.perf_counter()
            self._log(log, f"开始拉取 OBB 文件：{remote_file}")
            self.adb.pull(remote_file, local, timeout=None)
            size = local.stat().st_size if local.exists() else 0
            self._log(log, f"完成拉取 OBB 文件：{remote_file} -> {local} 大小={size}B 耗时={time.perf_counter() - start:.2f}s")
            copied.append(str(local.relative_to(obb_dir.parent.parent.parent)))
        return copied

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
    def _directory_size(path: Path) -> int:
        return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())

    @staticmethod
    def _log(log: LogCallback | None, message: str) -> None:
        logger.info(message)
        if log:
            log(message)
