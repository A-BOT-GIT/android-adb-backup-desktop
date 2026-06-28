from __future__ import annotations

import os
import inspect
import logging
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from .logging_utils import configure_file_logging
from .models import AppInfo, Device


configure_file_logging()
logger = logging.getLogger(__name__)


class AdbError(RuntimeError):
    pass


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEFAULT_ADB_NAMES = {"adb", "adb.exe"}
LONG_ADB_OPERATION_TIMEOUT = 30 * 60


def _is_default_adb_path(adb_path: str) -> bool:
    return adb_path.strip().lower() in DEFAULT_ADB_NAMES


def _resource_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        base_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)).resolve()
        logger.debug(
            "Resolving ADB resource base for frozen app: frozen=%s, _MEIPASS=%r, executable=%s, base=%s",
            getattr(sys, "frozen", False),
            getattr(sys, "_MEIPASS", None),
            sys.executable,
            base_dir,
        )
        return base_dir

    base_dir = Path(__file__).resolve().parents[2]
    logger.debug("Resolving ADB resource base for source run: file=%s, base=%s", __file__, base_dir)
    return base_dir


def _bundled_adb_candidates() -> list[Path]:
    base_dir = _resource_base_dir()
    candidates: list[Path] = [
        base_dir / "tools" / "adb" / "adb.exe",
        Path.cwd() / "tools" / "adb" / "adb.exe",
    ]
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                executable_dir / "tools" / "adb" / "adb.exe",
                executable_dir / "_internal" / "tools" / "adb" / "adb.exe",
            ]
        )

    unique_candidates = list(dict.fromkeys(candidate.resolve() for candidate in candidates))
    logger.debug("Bundled ADB candidate paths: %s", [str(candidate) for candidate in unique_candidates])
    return unique_candidates


def resolve_adb_path(adb_path: str = "adb") -> str:
    requested_path = adb_path or "adb"
    logger.debug("Resolving ADB path: requested=%r, os.name=%s", requested_path, os.name)
    if _is_default_adb_path(requested_path):
        for candidate in _bundled_adb_candidates():
            logger.debug("Checking bundled ADB candidate: path=%s, exists=%s", candidate, candidate.exists())
            if candidate.exists():
                logger.debug("Resolved ADB path to bundled executable: %s", candidate)
                return str(candidate)
    else:
        logger.debug("Skipping bundled ADB lookup because requested path is explicit: %r", requested_path)
    logger.debug("Falling back to requested ADB path: %r", requested_path)
    return requested_path


def _looks_like_file_path(adb_path: str) -> bool:
    path = Path(adb_path)
    return path.is_absolute() or any(separator in adb_path for separator in (os.sep, os.altsep) if separator)


def parse_devices(output: str) -> list[Device]:
    devices: list[Device] = []
    for line in output.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        description = " ".join(parts[2:])
        devices.append(Device(serial=serial, state=state, description=description))
    return devices


def parse_package_lines(output: str) -> dict[str, list[str]]:
    packages: dict[str, list[str]] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("package:"):
            continue
        payload = line.removeprefix("package:")
        if "=" in payload:
            path, package = payload.rsplit("=", 1)
        else:
            path, package = "", payload
        if package:
            packages.setdefault(package, [])
            if path:
                packages[package].append(path)
    return packages


def parse_pm_path_lines(output: str) -> list[str]:
    paths: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("package:"):
            path = line.removeprefix("package:")
            if path:
                paths.append(path)
    return paths


def parse_dumpsys_package(output: str) -> tuple[str, str]:
    version_name = ""
    version_code = ""

    version_name_match = re.search(r"\bversionName=([^\s]+)", output)
    if version_name_match:
        version_name = version_name_match.group(1)

    version_code_match = re.search(r"\bversionCode=(\d+)", output)
    if version_code_match:
        version_code = version_code_match.group(1)
    else:
        legacy_match = re.search(r"\bversionCode=(\d+)\s+", output)
        if legacy_match:
            version_code = legacy_match.group(1)

    return version_name, version_code


def _localized_apk_label(apk, fallback_label: str) -> str:
    get_app_name = getattr(apk, "get_app_name", None)
    if not callable(get_app_name):
        return ""

    locale_codes = ("zh-CN", "zh_CN", "zh")
    for locale_code in locale_codes:
        for kwargs in ({"locale": locale_code}, {"language": locale_code}, {"lang": locale_code}):
            try:
                label = get_app_name(**kwargs)
            except TypeError:
                continue
            except Exception:
                continue
            if label and str(label) != fallback_label:
                return str(label)

        try:
            signature = inspect.signature(get_app_name)
        except (TypeError, ValueError):
            signature = None
        if signature is None or any(
            parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
            for parameter in signature.parameters.values()
        ):
            try:
                label = get_app_name(locale_code)
            except TypeError:
                continue
            except Exception:
                continue
            if label and str(label) != fallback_label:
                return str(label)

    return ""


def _apk_label_and_version(apk_path: Path) -> tuple[str, str, str, str]:
    try:
        from apkutils2 import APK  # type: ignore
    except Exception:
        return "", "", "", ""

    try:
        apk = APK(str(apk_path))
        manifest = apk.get_manifest()
        label = apk.get_app_name() or ""
        localized_label = _localized_apk_label(apk, str(label))
        version_name = manifest.get("@android:versionName", "") or ""
        version_code = manifest.get("@android:versionCode", "") or ""
        return str(label), localized_label, str(version_name), str(version_code)
    except Exception:
        return "", "", "", ""


class AdbClient:
    def __init__(self, adb_path: str = "adb", serial: str | None = None) -> None:
        self.adb_path = resolve_adb_path(adb_path)
        self.serial = serial

    def _base_args(self) -> list[str]:
        args = [self.adb_path]
        if self.serial:
            args.extend(["-s", self.serial])
        return args

    def _run(
        self,
        args: list[str],
        *,
        timeout: int | None = 60,
        text: bool = True,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        command = self._base_args() + args
        command_text = " ".join(str(part) for part in command)
        start = time.perf_counter()
        logger.info("ADB begin: %s timeout=%s serial=%s", command_text, timeout, self.serial)
        try:
            logger.debug("Calling subprocess.run with command: %s", command)
            completed = subprocess.run(
                command,
                capture_output=True,
                text=text,
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
            logger.debug("subprocess.run returned successfully")
        except FileNotFoundError as exc:
            logger.exception("ADB failed: %s", command_text)
            raise AdbError(f"未找到 ADB：{self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - start
            logger.exception("ADB timeout after %.2fs: %s", elapsed, command_text)
            raise AdbError(f"ADB 命令超时：{' '.join(command)}") from exc

        elapsed = time.perf_counter() - start
        stdout = completed.stdout if isinstance(completed.stdout, str) else completed.stdout.decode(errors="replace")
        stderr = completed.stderr if isinstance(completed.stderr, str) else completed.stderr.decode(errors="replace")
        stdout_len = len(stdout) if stdout else 0
        stderr_len = len(stderr) if stderr else 0
        logger.info(
            "ADB end: %s returncode=%s elapsed=%.2fs stdout_len=%d stderr_len=%d",
            command_text,
            completed.returncode,
            elapsed,
            stdout_len,
            stderr_len,
        )
        if stdout_len > 100000:
            logger.warning("Large stdout output detected: %d bytes for command: %s", stdout_len, command_text)
        if check and completed.returncode != 0:
            message = (stderr or stdout or "未知 ADB 错误").strip()
            raise AdbError(message)
        return completed

    def ensure_available(self) -> None:
        if _looks_like_file_path(self.adb_path) and not Path(self.adb_path).exists():
            tried_paths = [str(c) for c in _bundled_adb_candidates()]
            raise AdbError(f"ADB 路径不存在：{self.adb_path}\n尝试过的bundled路径：{', '.join(tried_paths)}")
        if not _looks_like_file_path(self.adb_path) and shutil.which(self.adb_path) is None:
            raise AdbError("在 PATH 中未找到 adb。请从 Android platform-tools 中选择 adb.exe。")
        self._run(["version"], timeout=10)

    def devices(self) -> list[Device]:
        result = self._run(["devices", "-l"], timeout=15)
        return parse_devices(result.stdout)

    def shell(self, *args: str, timeout: int | None = 60, check: bool = True) -> str:
        result = self._run(["shell", *args], timeout=timeout, check=check)
        return result.stdout

    def list_apps(
        self,
        *,
        include_system: bool = False,
        progress: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[AppInfo]:
        logger.info("list_apps starting: include_system=%s serial=%s", include_system, self.serial)
        if progress:
            progress("正在获取应用列表...")

        if include_system:
            logger.debug("list_apps: include_system=True, fetching all packages first")
            start = time.perf_counter()
            if progress:
                progress("正在获取所有应用...")
            logger.info("list_apps: calling pm list packages -f (all packages)")
            package_map = parse_package_lines(self._run(["shell", "pm", "list", "packages", "-f"], timeout=120).stdout)
            elapsed = time.perf_counter() - start
            logger.info("list_apps: retrieved all packages: count=%d elapsed=%.2fs", len(package_map), elapsed)

            logger.debug("list_apps: now fetching third-party packages only")
            start = time.perf_counter()
            if progress:
                progress("正在获取第三方应用集合...")
            logger.info("list_apps: calling pm list packages -3 (third-party packages)")
            third_party_output = self.shell("pm", "list", "packages", "-3", timeout=120, check=False)
            third_party_packages = set(parse_package_lines(third_party_output))
            elapsed = time.perf_counter() - start
            logger.info("list_apps: retrieved third-party packages: count=%d elapsed=%.2fs", len(third_party_packages), elapsed)
        else:
            logger.debug("list_apps: include_system=False, fetching third-party packages with paths")
            start = time.perf_counter()
            logger.info("list_apps: calling pm list packages -3 -f (third-party packages only)")
            package_map = parse_package_lines(self._run(["shell", "pm", "list", "packages", "-3", "-f"], timeout=120).stdout)
            elapsed = time.perf_counter() - start
            logger.info("list_apps: retrieved third-party packages only: count=%d elapsed=%.2fs", len(package_map), elapsed)
            third_party_packages = set(package_map)

        logger.info("list_apps: building AppInfo objects from %d packages", len(package_map))
        apps: list[AppInfo] = []
        for index, (package, apk_paths) in enumerate(sorted(package_map.items()), start=1):
            if should_cancel and should_cancel():
                logger.info("list_apps: cancelled at index %d/%d", index, len(package_map))
                break
            if progress:
                progress(f"正在读取应用列表 {index}/{len(package_map)}：{package}")
            if index % 10 == 0 or index == 1:
                logger.debug("list_apps: processing index %d/%d package=%s", index, len(package_map), package)

            apps.append(
                AppInfo(
                    package=package,
                    name=package,
                    version_name="",
                    version_code="",
                    apk_paths=apk_paths,
                    is_system=package not in third_party_packages,
                    metadata_loaded=False,
                )
            )
        logger.info("list_apps completed: total_apps=%d include_system=%s", len(apps), include_system)
        return apps

    def third_party_packages(self) -> set[str]:
        start = time.perf_counter()
        output = self.shell("pm", "list", "packages", "-3", timeout=120, check=False)
        packages = set(parse_package_lines(output))
        elapsed = time.perf_counter() - start
        logger.info("Retrieved third-party packages: count=%d elapsed=%.2fs", len(packages), elapsed)
        return packages

    def load_app_metadata(self, app: AppInfo) -> AppInfo:
        apk_paths = app.apk_paths or self.apk_paths(app.package)
        version_name, version_code = self.package_version(app.package)
        name = app.name or app.package
        localized_name = app.localized_name or ""
        package_size_bytes = self.package_size(apk_paths)

        label, apk_localized_label, apk_version_name, apk_version_code = self._read_label_from_apk(app.package, apk_paths)
        if label:
            name = label
        if apk_localized_label:
            localized_name = apk_localized_label
        if apk_version_name:
            version_name = apk_version_name
        if apk_version_code:
            version_code = apk_version_code

        return AppInfo(
            package=app.package,
            name=name,
            localized_name=localized_name,
            version_name=version_name,
            version_code=version_code,
            apk_paths=apk_paths,
            package_size_bytes=package_size_bytes,
            is_system=app.is_system,
            metadata_loaded=True,
        )

    def package_version(self, package: str) -> tuple[str, str]:
        output = self.shell("dumpsys", "package", package, timeout=30, check=False)
        return parse_dumpsys_package(output)

    def apk_paths(self, package: str) -> list[str]:
        return parse_pm_path_lines(self.shell("pm", "path", package, timeout=20, check=False))

    def package_size(self, apk_paths: list[str]) -> int | None:
        sizes = [size for path in apk_paths if (size := self.remote_file_size(path)) is not None]
        if not sizes:
            return None
        return sum(sizes)

    def remote_file_size(self, remote_path: str) -> int | None:
        output = self.shell("stat", "-c", "%s", shlex.quote(remote_path), timeout=15, check=False)
        match = re.search(r"\b(\d+)\b", output)
        if match:
            return int(match.group(1))

        output = self.shell("wc", "-c", shlex.quote(remote_path), timeout=15, check=False)
        match = re.search(r"\b(\d+)\b", output)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _long_timeout(timeout: int | None) -> int:
        # Cancellation is cooperative at the BackupService layer, so an in-flight
        # adb subprocess cannot be interrupted without a larger process-manager
        # redesign. Keep these high-risk operations bounded instead of allowing
        # indefinite hangs when cancellation is requested during the subprocess.
        return LONG_ADB_OPERATION_TIMEOUT if timeout is None else timeout

    def pull(self, remote: str, local: Path, timeout: int | None = None) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self._run(["pull", remote, str(local)], timeout=self._long_timeout(timeout))

    def push(self, local: Path, remote: str, timeout: int | None = None) -> None:
        self._run(["push", str(local), remote], timeout=self._long_timeout(timeout))

    def install(self, apk_files: list[Path]) -> None:
        if not apk_files:
            raise AdbError("没有可安装的 APK 文件。")
        if len(apk_files) == 1:
            self._run(["install", "-r", str(apk_files[0])], timeout=LONG_ADB_OPERATION_TIMEOUT)
            return
        self._run(["install-multiple", "-r", *[str(path) for path in apk_files]], timeout=LONG_ADB_OPERATION_TIMEOUT)

    def adb_backup_package(self, package: str, output_file: Path, *, include_apk: bool = False) -> None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        apk_flag = "-apk" if include_apk else "-noapk"
        self._run(["backup", "-f", str(output_file), apk_flag, package], timeout=180)

    def adb_restore(self, backup_file: Path) -> None:
        self._run(["restore", str(backup_file)], timeout=LONG_ADB_OPERATION_TIMEOUT)

    def restore_run_as_data(self, package: str, input_tar: Path) -> None:
        command = self._base_args() + [
            "exec-in",
            "run-as",
            package,
            "tar",
            "-xf",
            "-",
            "-C",
            f"/data/data/{package}",
        ]
        command_text = " ".join(str(part) for part in command)
        start = time.perf_counter()
        logger.info("ADB begin: %s timeout=%s stdin=%s", command_text, LONG_ADB_OPERATION_TIMEOUT, input_tar)
        try:
            with input_tar.open("rb") as fh:
                completed = subprocess.run(
                    command,
                    stdin=fh,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=LONG_ADB_OPERATION_TIMEOUT,
                    creationflags=CREATE_NO_WINDOW,
                )
        except FileNotFoundError as exc:
            logger.exception("ADB failed: %s", command_text)
            raise AdbError(f"未找到 ADB：{self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - start
            logger.exception("ADB timeout after %.2fs: %s", elapsed, command_text)
            raise AdbError(f"ADB 命令超时：{' '.join(command)}") from exc

        elapsed = time.perf_counter() - start
        stdout = completed.stdout.decode(errors="replace") if isinstance(completed.stdout, bytes) else str(completed.stdout)
        stderr = completed.stderr.decode(errors="replace") if isinstance(completed.stderr, bytes) else str(completed.stderr)
        logger.info(
            "ADB end: %s returncode=%s elapsed=%.2fs stdout_len=%d stderr_len=%d",
            command_text,
            completed.returncode,
            elapsed,
            len(stdout),
            len(stderr),
        )
        if completed.returncode != 0:
            message = (stderr or stdout or "未知 ADB 错误").strip()
            raise AdbError(message)

    def export_run_as_data(self, package: str, output_tar: Path) -> bool:
        output_tar.parent.mkdir(parents=True, exist_ok=True)
        command = self._base_args() + [
            "exec-out",
            "run-as",
            package,
            "tar",
            "-cf",
            "-",
            "-C",
            f"/data/data/{package}",
            ".",
        ]
        command_text = " ".join(str(part) for part in command)
        start = time.perf_counter()
        logger.info("ADB begin: %s timeout=%s stdout=%s", command_text, 120, output_tar)
        try:
            with output_tar.open("wb") as fh:
                completed = subprocess.run(
                    command,
                    stdout=fh,
                    stderr=subprocess.PIPE,
                    timeout=120,
                    creationflags=CREATE_NO_WINDOW,
                )
        except Exception:
            logger.exception("ADB failed: %s", command_text)
            output_tar.unlink(missing_ok=True)
            return False
        elapsed = time.perf_counter() - start
        stderr = completed.stderr.decode(errors="replace") if isinstance(completed.stderr, bytes) else str(completed.stderr)
        size = output_tar.stat().st_size if output_tar.exists() else 0
        logger.info(
            "ADB end: %s returncode=%s elapsed=%.2fs stdout_file=%s stdout_size=%s stderr=%r",
            command_text,
            completed.returncode,
            elapsed,
            output_tar,
            size,
            stderr,
        )
        if completed.returncode != 0 or size < 1024:
            output_tar.unlink(missing_ok=True)
            return False
        return True

    def path_exists(self, remote_path: str) -> bool:
        result = self.shell("test", "-e", remote_path, "&&", "echo", "yes", timeout=15, check=False)
        return "yes" in result

    def _read_label_from_apk(self, package: str, apk_paths: list[str]) -> tuple[str, str, str, str]:
        if not apk_paths:
            return "", "", "", ""
        with tempfile.TemporaryDirectory(prefix="adb-apk-meta-") as tmp:
            local_apk = Path(tmp) / f"{package}.apk"
            try:
                self.pull(apk_paths[0], local_apk, timeout=90)
            except AdbError:
                return "", "", "", ""
            return _apk_label_and_version(local_apk)

    def _is_third_party(self, package: str) -> bool:
        output = self.shell("pm", "list", "packages", "-3", package, timeout=15, check=False)
        return package in output
