from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Device:
    serial: str
    state: str
    description: str = ""

    @property
    def display_name(self) -> str:
        if self.description:
            return f"{self.serial} ({self.description})"
        return f"{self.serial} [{self.state}]"


@dataclass(slots=True)
class AppInfo:
    package: str
    name: str
    localized_name: str = ""
    version_name: str = ""
    version_code: str = ""
    apk_paths: list[str] = field(default_factory=list)
    package_size_bytes: int | None = None
    is_system: bool = False
    metadata_loaded: bool = False

    @property
    def display_name(self) -> str:
        return self.localized_name or self.name or self.package or ""

    @property
    def display_version(self) -> str:
        if self.version_name and self.version_code:
            return f"{self.version_name} ({self.version_code})"
        return self.version_name or self.version_code or ""

    @property
    def display_package_size(self) -> str:
        if self.package_size_bytes is None:
            return ""
        return format_size(self.package_size_bytes)


def format_size(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(max(size_bytes, 0))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024


@dataclass(slots=True)
class BackupOptions:
    output_dir: Path
    include_data: bool = False
    include_obb: bool = True
    auto_confirm_adb_backup: bool = False
