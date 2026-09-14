from src.integrations import coros_mcp
from src.runtime.tenant import TenantContext, tenant_scope


def test_new_tenants_receive_distinct_mcp_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_DATA_ROOT", str(tmp_path / "tenants"))

    with tenant_scope(TenantContext("tenant-a")):
        first_dir = coros_mcp.mcp_config_dir()
        first_port = coros_mcp.mcp_callback_port()

    with tenant_scope(TenantContext("tenant-b")):
        second_dir = coros_mcp.mcp_config_dir()
        second_port = coros_mcp.mcp_callback_port()

    assert first_dir == tmp_path / "tenants" / "tenant-a" / "mcp-auth"
    assert second_dir == tmp_path / "tenants" / "tenant-b" / "mcp-auth"
    assert first_dir != second_dir
    assert first_port != second_port


def test_default_tenant_preserves_legacy_mcp_profile(monkeypatch):
    monkeypatch.delenv("MCP_REMOTE_CONFIG_DIR", raising=False)
    with tenant_scope(TenantContext("default")):
        assert coros_mcp.mcp_config_dir() is None
        assert coros_mcp.mcp_callback_port() == 20450


def test_stdio_parameters_include_tenant_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_DATA_ROOT", str(tmp_path / "tenants"))

    with tenant_scope(TenantContext("tenant-a")):
        parameters = coros_mcp.coros_server_parameters()

    assert parameters.args[-1] == str(coros_mcp.mcp_callback_port("tenant-a"))
    assert parameters.env["MCP_REMOTE_CONFIG_DIR"].endswith("tenant-a/mcp-auth")
