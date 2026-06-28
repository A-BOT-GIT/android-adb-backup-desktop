from android_backup_desktop.adb import (
    parse_devices,
    parse_dumpsys_package,
    parse_package_lines,
    parse_pm_path_lines,
    resolve_adb_path,
)
import android_backup_desktop.adb as adb_module


def test_parse_devices() -> None:
    output = """List of devices attached
emulator-5554 device product:sdk_gphone64 model:sdk_gphone64 device:emu64 transport_id:1
ABC123 unauthorized

"""
    devices = parse_devices(output)

    assert len(devices) == 2
    assert devices[0].serial == "emulator-5554"
    assert devices[0].state == "device"
    assert "model:sdk_gphone64" in devices[0].description
    assert devices[1].state == "unauthorized"


def test_parse_package_lines_with_paths() -> None:
    output = """package:/data/app/~~hash/com.example/base.apk=com.example
package:/data/app/~~hash/com.example/split_config.arm64_v8a.apk=com.example
package:com.no.path
"""
    packages = parse_package_lines(output)

    assert packages["com.example"] == [
        "/data/app/~~hash/com.example/base.apk",
        "/data/app/~~hash/com.example/split_config.arm64_v8a.apk",
    ]
    assert packages["com.no.path"] == []


def test_parse_pm_path_lines() -> None:
    output = """package:/data/app/~~hash/com.example/base.apk
package:/data/app/~~hash/com.example/split_config.en.apk
"""

    assert parse_pm_path_lines(output) == [
        "/data/app/~~hash/com.example/base.apk",
        "/data/app/~~hash/com.example/split_config.en.apk",
    ]


def test_parse_dumpsys_package_version() -> None:
    output = """
    Packages:
      Package [com.example] (abc):
        versionCode=42 minSdk=23 targetSdk=35
        versionName=1.2.3
    """

    assert parse_dumpsys_package(output) == ("1.2.3", "42")


def test_resolve_adb_path_prefers_bundled_windows_adb(tmp_path, monkeypatch) -> None:
    bundled_adb = tmp_path / "tools" / "adb" / "adb.exe"
    bundled_adb.parent.mkdir(parents=True)
    bundled_adb.write_bytes(b"")
    monkeypatch.setattr(adb_module.os, "name", "nt")
    monkeypatch.setattr(adb_module, "_bundled_adb_candidates", lambda: [bundled_adb])

    assert resolve_adb_path("adb") == str(bundled_adb)


def test_resolve_adb_path_falls_back_to_path_when_bundled_adb_is_missing(tmp_path, monkeypatch) -> None:
    missing_adb = tmp_path / "tools" / "adb" / "adb.exe"
    monkeypatch.setattr(adb_module.os, "name", "nt")
    monkeypatch.setattr(adb_module, "_bundled_adb_candidates", lambda: [missing_adb])

    assert resolve_adb_path("adb") == "adb"


def test_resolve_adb_path_preserves_explicit_path(tmp_path, monkeypatch) -> None:
    bundled_adb = tmp_path / "tools" / "adb" / "adb.exe"
    explicit_adb = tmp_path / "custom" / "adb.exe"
    bundled_adb.parent.mkdir(parents=True)
    bundled_adb.write_bytes(b"")
    monkeypatch.setattr(adb_module.os, "name", "nt")
    monkeypatch.setattr(adb_module, "_bundled_adb_candidates", lambda: [bundled_adb])

    assert resolve_adb_path(str(explicit_adb)) == str(explicit_adb)
