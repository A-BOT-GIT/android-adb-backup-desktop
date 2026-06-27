from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .adb import AdbClient, AdbError
from .models import AppInfo, BackupOptions


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, str], None]


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


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
            raise ValueError("No apps selected for backup.")

        options.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        zip_path = options.output_dir / f"android-app-backup-{timestamp}.zip"

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
                if progress:
                    progress(index - 1, total_steps, f"Backing up {app.package}")
                self._log(log, f"Backing up {app.package}")
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
                        "package": app.package,
                        "name": app.name,
                        "version_name": app.version_name,
                        "version_code": app.version_code,
                        "apk_files": apk_files,
                        "data_files": data_files,
                        "obb_files": obb_files,
                    }
                )
                if progress:
                    progress(index, total_steps, f"Finished {app.package}")

            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._zip_directory(staging, zip_path)

        self._log(log, f"Backup archive written: {zip_path}")
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

        with tempfile.TemporaryDirectory(prefix="android-app-restore-") as tmp_name:
            staging = Path(tmp_name)
            with zipfile.ZipFile(zip_path, "r") as archive:
                archive.extractall(staging)

            manifest_path = staging / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            apps = manifest.get("apps", [])
            if not isinstance(apps, list):
                raise ValueError("Invalid backup manifest.")

            for index, app_entry in enumerate(apps, start=1):
                package = str(app_entry.get("package", ""))
                if progress:
                    progress(index - 1, len(apps), f"Restoring {package}")
                self._log(log, f"Restoring {package}")

                app_dir = staging / "apps" / safe_name(package)
                apk_files = sorted((app_dir / "apk").glob("*.apk"))
                if apk_files:
                    self.adb.install(apk_files)
                    self._log(log, f"Installed APKs for {package}")

                obb_dir = app_dir / "obb"
                if obb_dir.exists():
                    remote_obb = f"/sdcard/Android/obb/{package}"
                    self.adb.shell("mkdir", "-p", remote_obb, timeout=30, check=False)
                    for child in obb_dir.iterdir():
                        self.adb.push(child, remote_obb, timeout=None)
                    self._log(log, f"Restored OBB files for {package}")

                if restore_data:
                    for ab_file in sorted((app_dir / "data").glob("*.ab")):
                        self._log(log, f"Starting adb restore for {ab_file.name}; confirm on the device if prompted.")
                        self.adb.adb_restore(ab_file)

                if progress:
                    progress(index, len(apps), f"Finished {package}")

    def _backup_apks(self, app: AppInfo, apk_dir: Path, log: LogCallback | None) -> list[str]:
        apk_paths = app.apk_paths or self.adb.apk_paths(app.package)
        if not apk_paths:
            raise AdbError(f"No APK path found for {app.package}")

        apk_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for remote_path in apk_paths:
            filename = Path(remote_path).name or "base.apk"
            local = apk_dir / filename
            self._log(log, f"Pulling APK: {remote_path}")
            self.adb.pull(remote_path, local, timeout=None)
            copied.append(str(local.relative_to(apk_dir.parent.parent.parent)))
        return copied

    def _backup_data(self, app: AppInfo, data_dir: Path, log: LogCallback | None) -> list[str]:
        data_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []

        run_as_tar = data_dir / "run-as-data.tar"
        self._log(log, f"Trying run-as data export for {app.package}")
        if self.adb.export_run_as_data(app.package, run_as_tar):
            self._log(log, f"run-as data export succeeded for {app.package}")
            copied.append(str(run_as_tar.relative_to(data_dir.parent.parent.parent)))
            return copied

        ab_file = data_dir / "adb-backup.ab"
        self._log(log, f"Trying adb backup fallback for {app.package}; confirm on the device if prompted.")
        try:
            self.adb.adb_backup_package(app.package, ab_file, include_apk=False)
        except AdbError as exc:
            self._log(log, f"Data backup skipped for {app.package}: {exc}")
            ab_file.unlink(missing_ok=True)
            return copied

        if ab_file.exists() and ab_file.stat().st_size > 1024:
            copied.append(str(ab_file.relative_to(data_dir.parent.parent.parent)))
        else:
            self._log(log, f"Data backup for {app.package} was empty or refused by Android.")
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
            self._log(log, f"Pulling OBB file: {remote_file}")
            self.adb.pull(remote_file, local, timeout=None)
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

    @staticmethod
    def _log(log: LogCallback | None, message: str) -> None:
        if log:
            log(message)
