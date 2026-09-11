"""Plan-mandated AST guard: the google.ads.googleads import and every get_service() call
live ONLY in client.py. Any other module reaching for the Google API directly would break
the single-choke-point invariant the whole safety design rests on.

Scope is the PACKAGE only (mcp_google_ads_safe/); tests/ is not scanned, so the bidding
exhaustiveness test's `from google.ads...enums import ...` needs no allowlist here.
"""
import ast
import pathlib

_PKG = pathlib.Path(__file__).resolve().parent.parent / "mcp_google_ads_safe"


def _package_modules_except_client():
    return [p for p in sorted(_PKG.rglob("*.py")) if p.name != "client.py"]


def _offenders(check):
    hits = []
    for path in _package_modules_except_client():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if check(node):
                hits.append(path.name)
                break
    return hits


def test_no_google_ads_import_outside_client():
    def is_google_ads_import(node):
        if isinstance(node, ast.ImportFrom):
            return (node.module or "").startswith("google.ads.googleads")
        if isinstance(node, ast.Import):
            return any(a.name.startswith("google.ads.googleads") for a in node.names)
        return False

    offenders = _offenders(is_google_ads_import)
    assert not offenders, f"google.ads.googleads imported outside client.py: {offenders}"


def test_no_get_service_call_outside_client():
    def is_get_service_call(node):
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_service")

    offenders = _offenders(is_get_service_call)
    assert not offenders, f"get_service( called outside client.py: {offenders}"


def test_guard_actually_scans_something():
    # A guard that scans zero files would pass vacuously — pin that it saw the package.
    names = {p.name for p in _package_modules_except_client()}
    assert {"rails.py", "settings.py"} <= names
    assert "client.py" not in names


def test_no_transport_library_import_outside_client():
    def is_transport_import(node):
        if isinstance(node, ast.ImportFrom):
            return (node.module or '').startswith(('google.api_core', 'grpc'))
        if isinstance(node, ast.Import):
            return any(a.name.startswith(('google.api_core', 'grpc')) for a in node.names)
        return False

    assert not _offenders(is_transport_import)
