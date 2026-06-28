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
    version_name: str = ""
    version_code: str = ""
    apk_paths: list[str] = field(default_factory=list)
    is_system: bool = False

    @property
    def display_version(self) -> str:
        if self.version_name and self.version_code:
            return f"{self.version_name} ({self.version_code})"
        return self.version_name or self.version_code or ""


@dataclass(slots=True)
class BackupOptions:
    output_dir: Path
    include_data: bool = False
    include_obb: bool = True
