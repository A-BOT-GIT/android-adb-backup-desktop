"""Entry point for PyInstaller to launch the application as a module."""
import runpy

runpy.run_module("android_backup_desktop", run_name="__main__", alter_sys=True)
