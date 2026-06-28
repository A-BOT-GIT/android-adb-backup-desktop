"""Entry point for PyInstaller - ensures module is properly discovered."""
from android_backup_desktop.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
