# Android Backup Desktop

Windows desktop program for backing up installed Android applications through ADB.

It follows the practical model used by tools such as Open Android Backup: APK and media files are exported directly instead of relying on `adb backup` as the primary archive format. Optional app-data backup is attempted only when Android allows it:

1. `run-as <package>` tar export for debuggable apps.
2. `adb backup -noapk <package>` fallback, which may be ignored by modern Android versions or by apps that disable backup.

## Features

- Connects to Android devices through the command-line `adb` tool.
- Lists installed apps with package name, best-effort app name, version, and APK path.
- Backs up selected apps or all listed apps.
- Exports base/split APK files.
- Optionally exports OBB files and app data where the device permits it.
- Writes one portable `.zip` archive with a `manifest.json`.
- Restores APKs from a backup zip. OBB restore is included when OBB files are present. `.ab` data files can be restored through `adb restore`.

## Requirements

- Windows 10/11.
- Python 3.10 or newer.
- Android platform-tools (`adb.exe`) available in `PATH`, or select `adb.exe` in the app.
- USB debugging enabled on the Android device.

## Install And Run

```powershell
cd android-adb-backup-desktop
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[apk-labels,dev]"
android-backup-desktop
```

`apkutils2` is optional. Without it, the app still works, but app labels and versions may fall back to package metadata from `dumpsys package`.

## Build A Windows EXE

```powershell
pip install pyinstaller
pyinstaller --noconfirm --windowed --name AndroidBackupDesktop `
  --collect-all PySide6 `
  src\android_backup_desktop\__main__.py
```

The executable will be in `dist\AndroidBackupDesktop\`.

## Archive Layout

```text
backup.zip
  manifest.json
  apps/
    com.example.app/
      apk/
        base.apk
        split_config.arm64_v8a.apk
      data/
        run-as-data.tar
        adb-backup.ab
      obb/
        ...
```

## Notes

- Private app data is intentionally restricted by Android. A missing or tiny data file usually means the app or OS refused backup.
- `adb backup` is deprecated and unreliable on recent Android versions. The app keeps it as a fallback because some devices and legacy apps still support it.
- Restoring `run-as-data.tar` is not automatic because Android only allows it for debuggable packages and ownership/SELinux state can vary. APK and `.ab` restore paths are implemented.
