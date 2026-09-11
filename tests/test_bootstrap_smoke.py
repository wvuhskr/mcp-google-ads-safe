def test_package_imports():
    import mcp_google_ads_safe  # noqa: F401


def test_app_module_imports():
    from mcp_google_ads_safe.app import mcp

    assert mcp is not None
