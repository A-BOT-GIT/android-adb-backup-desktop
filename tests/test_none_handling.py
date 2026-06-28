from android_backup_desktop.models import AppInfo, format_size


def test_app_info_with_none_values() -> None:
    """验证AppInfo能正确处理None值。"""
    # 创建包含None值的AppInfo（虽然不应该发生，但需要防御）
    app = AppInfo(
        package="com.example",
        name="Example App",
        version_name=None,
        version_code=None,
    )

    # display_version应该优雅地处理None值
    assert app.display_version == ""

    # 字符串转换应该是安全的
    assert str(app.package or "") == "com.example"
    assert str(app.name or "") == "Example App"
    assert str(app.version_name or "") == ""
    assert str(app.version_code or "") == ""


def test_app_info_display_version_with_values() -> None:
    """验证AppInfo正确格式化有值的版本信息。"""
    app = AppInfo(
        package="com.example",
        name="Example",
        version_name="1.0.0",
        version_code="1",
    )

    assert app.display_version == "1.0.0 (1)"


def test_app_info_display_version_partial_values() -> None:
    """验证AppInfo在只有部分版本信息时的处理。"""
    app1 = AppInfo(
        package="com.example1",
        name="Example1",
        version_name="1.0.0",
        version_code="",
    )
    assert app1.display_version == "1.0.0"

    app2 = AppInfo(
        package="com.example2",
        name="Example2",
        version_name="",
        version_code="1",
    )
    assert app2.display_version == "1"


def test_app_info_display_name_prefers_localized_name() -> None:
    app = AppInfo(
        package="com.example",
        name="Example",
        localized_name="示例应用",
    )

    assert app.display_name == "示例应用"


def test_app_info_display_name_falls_back_to_existing_name() -> None:
    app = AppInfo(
        package="com.example",
        name="Example",
    )

    assert app.display_name == "Example"


def test_app_info_display_package_size() -> None:
    app = AppInfo(
        package="com.example",
        name="Example",
        package_size_bytes=1_572_864,
    )

    assert app.display_package_size == "1.5 MB"
    assert format_size(512) == "512 B"
