# 安卓 ADB 备份工具

这是一个 Windows 桌面程序，用于通过 ADB 备份 Android 设备上已安装的应用。

项目采用类似 Open Android Backup 的实用模型：优先直接导出 APK 和媒体文件，而不是依赖 `adb backup` 作为主要归档格式。应用数据备份只会在 Android 允许时尝试：

1. 对可调试应用使用 `run-as <package>` 导出 tar。
2. 回退到 `adb backup -noapk <package>`；在较新的 Android 版本上，或应用禁用备份时，这一步可能会被系统忽略。

## 功能

- 通过命令行 `adb` 工具连接 Android 设备。
- 列出已安装应用，包含包名、尽力获取的应用名称、版本和 APK 路径。
- 备份选中的应用或列表中的全部应用。
- 导出 base/split APK 文件。
- 在设备允许时可选导出 OBB 文件和应用数据。
- 生成包含 `manifest.json` 的可移植 `.zip` 备份归档。
- 从备份 ZIP 恢复 APK。若归档中包含 OBB 文件，也会恢复 OBB；`run-as-data.tar` 会通过 `run-as` 恢复，`.ab` 数据文件会通过 `adb restore` 恢复。

## 环境要求

- Windows 10/11。
- Python 3.10 或更新版本。
- 打包后的 Windows 版本已内置 `adb.exe`，无需额外配置 ADB 环境变量；源码运行时也可以在应用内手动选择 `adb.exe`。
- Android 设备已开启 USB 调试。

## 安装和运行

在 PowerShell 中执行：

```powershell
cd android-adb-backup-desktop
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[apk-labels,dev]"
android-backup-desktop
```

`apkutils2` 是可选依赖。未安装时程序仍可使用，但应用名称和版本可能会回退为从 `dumpsys package` 获取的包元数据。

## 打包 Windows EXE

```powershell
pip install pyinstaller
pyinstaller --noconfirm AndroidBackupDesktop.spec
```

打包配置会将 `tools\adb\` 中的 `adb.exe` 和必要 DLL 一并复制到 `dist\AndroidBackupDesktop\`。

## 备份归档结构

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

## 注意事项

- Android 会限制私有应用数据访问。缺失数据文件或数据文件很小，通常表示应用或系统拒绝了备份。
- `adb backup` 已废弃，在较新的 Android 版本上并不可靠。本程序保留它作为回退方案，因为部分设备和旧应用仍然支持。
- `run-as-data.tar` 恢复仍依赖 Android 允许目标包使用 `run-as`；如果目标安装包不可调试，设备会拒绝该数据恢复。
