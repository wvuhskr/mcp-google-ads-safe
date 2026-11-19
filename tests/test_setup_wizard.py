"""Guided `setup` and `reauth`: plain questions in, one auth_helper call out. The sign-in
and every file-safety rule stay in auth_helper (tested in test_auth_helper.py); here it is
replaced by a recorder so no browser or network is involved."""
import json
import os
import stat

import pytest

from mcp_google_ads_safe import server, setup_wizard

SECRET = "sentinel-client-secret"
TOKEN = "sentinel-refresh-token"


@pytest.fixture
def wiz(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    client_json = tmp_path / "client.json"
    client_json.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": SECRET}}))
    state = {"home": home, "client_json": client_json, "calls": [], "code": 0, "answers": []}

    def fake_auth(argv):
        state["calls"].append(list(argv))
        return state["code"]

    def ask(prompt):
        if not state["answers"]:
            raise EOFError
        return state["answers"].pop(0)

    monkeypatch.setattr(setup_wizard.auth_helper, "main", fake_auth)
    state["run"] = lambda *answers, reauth=False: (
        state["answers"].extend(answers) or setup_wizard.main(reauth=reauth, ask=ask))
    return state


def test_setup_collects_three_answers_and_runs_the_existing_sign_in(wiz, capsys):
    target = wiz["home"] / "ads" / "google-ads.yaml"
    assert wiz["run"](str(wiz["client_json"]), "123-456-7890", str(target)) == 0
    assert wiz["calls"] == [["--client-secrets", str(wiz["client_json"]), "--profile",
                             str(target), "--login-customer-id", "1234567890"]]
    out = capsys.readouterr().out
    block = json.loads(out[out.index('{\n  "mcpServers"'):out.index("\n}\n") + 2])
    entry = block["mcpServers"]["google-ads-safe"]
    assert entry["env"] == {"GOOGLE_ADS_YAML": str(target), "GOOGLE_ADS_READ_SCOPE": "manager"}
    assert entry["command"] == "uvx" and "mcp-google-ads-safe" in entry["args"]
    assert "GOOGLE_ADS_ENABLE_WRITES" not in out.split("To allow changes later")[0]
    assert "claude mcp add" in out and "find_accounts" in out


def test_blank_profile_answer_uses_the_default_in_a_private_folder(wiz):
    assert wiz["run"](str(wiz["client_json"]), "1234567890", "") == 0
    folder = wiz["home"] / ".mcp-google-ads-safe"
    assert wiz["calls"][0][3] == str(folder / "google-ads.yaml")
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_dragged_in_paths_with_quotes_and_tilde_are_understood(wiz):
    (wiz["home"] / "c.json").write_text("{}")
    assert wiz["run"]("'~/c.json' ", "1234567890", '"~/p/google-ads.yaml"') == 0
    assert wiz["calls"][0][1] == str(wiz["home"] / "c.json")
    assert wiz["calls"][0][3] == str(wiz["home"] / "p" / "google-ads.yaml")


def test_setup_never_overwrites_an_existing_profile(wiz, capsys):
    target = wiz["home"] / "google-ads.yaml"
    target.write_text("keep me")
    assert wiz["run"](str(wiz["client_json"]), "1234567890", str(target)) == 2
    assert wiz["calls"] == [] and target.read_text() == "keep me"
    assert "reauth" in capsys.readouterr().out


def test_a_wrong_answer_is_asked_again_then_given_up_on(wiz, capsys):
    target = str(wiz["home"] / "google-ads.yaml")
    assert wiz["run"]("/no/such/file.json", str(wiz["client_json"]),
                      "12345", "abc", "123 456 7890", target) == 0
    assert wiz["calls"][0][5] == "1234567890"
    wiz["calls"].clear()
    assert wiz["run"]("/nope1", "/nope2", "/nope3") == 2
    assert wiz["calls"] == []


@pytest.mark.parametrize("bad", ["０１２３４５６７８９", "12345678901", "0000000000x", ""])
def test_manager_id_must_be_ten_plain_digits(wiz, bad):
    assert wiz["run"](str(wiz["client_json"]), bad, bad, bad) == 2
    assert wiz["calls"] == []


def test_a_failed_sign_in_prints_no_config(wiz, capsys):
    wiz["code"] = 3
    assert wiz["run"](str(wiz["client_json"]), "1234567890", "") == 3
    assert "mcpServers" not in capsys.readouterr().out


def test_no_terminal_to_ask_in_stops_cleanly(wiz, capsys):
    assert wiz["run"]() == 2
    assert wiz["calls"] == [] and "Terminal" in capsys.readouterr().out


def test_reauth_replaces_the_same_profile_and_keeps_its_manager(wiz, capsys):
    target = wiz["home"] / ".mcp-google-ads-safe" / "google-ads.yaml"
    target.parent.mkdir()
    target.write_text(json.dumps({"client_id": "id", "client_secret": SECRET,
                                  "refresh_token": TOKEN, "login_customer_id": "1234567890"}))
    assert wiz["run"]("", str(wiz["client_json"]), reauth=True) == 0
    assert wiz["calls"] == [["--client-secrets", str(wiz["client_json"]), "--profile",
                             str(target), "--login-customer-id", "1234567890", "--replace"]]
    out = capsys.readouterr().out
    assert SECRET not in out and TOKEN not in out


def test_reauth_without_a_profile_points_to_setup(wiz, capsys):
    assert wiz["run"]("", reauth=True) == 2
    assert wiz["calls"] == [] and "setup" in capsys.readouterr().out


def test_reauth_refuses_a_profile_without_a_usable_manager_id(wiz):
    target = wiz["home"] / "p.yaml"
    target.write_text(json.dumps({"login_customer_id": "12-34"}))
    assert wiz["run"](str(target), str(wiz["client_json"]), reauth=True) == 2
    target.write_text("- not\n- a mapping\n")
    assert wiz["run"](str(target), str(wiz["client_json"]), reauth=True) == 2
    assert wiz["calls"] == []


@pytest.mark.parametrize("word,reauth", [("setup", False), ("reauth", True)])
def test_the_server_command_routes_setup_and_reauth_without_starting(monkeypatch, word, reauth):
    seen = []
    monkeypatch.setattr(server.sys, "argv", ["mcp-google-ads-safe", word])
    monkeypatch.setattr(setup_wizard, "main", lambda reauth=False: seen.append(reauth) or 0)
    for name in ("load",):
        monkeypatch.setattr(server.settings, name, lambda: seen.append("served"))
    monkeypatch.setattr(server.client, "preflight", lambda: seen.append("served"))
    monkeypatch.setattr(server.mcp, "run", lambda: seen.append("served"))
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 0 and seen == [reauth]


def test_the_bare_command_still_serves(monkeypatch):
    seen = []
    monkeypatch.setattr(server.sys, "argv", ["mcp-google-ads-safe"])
    monkeypatch.setattr(server.settings, "load", lambda: seen.append("load"))
    monkeypatch.setattr(server.client, "preflight", lambda: seen.append("preflight"))
    monkeypatch.setattr(server.mcp, "run", lambda: seen.append("run"))
    server.main()
    assert seen == ["load", "preflight", "run"]
    assert os.environ.get("HOME")


def test_readme_names_every_tool_and_the_current_version():
    import asyncio
    import pathlib
    import tomllib

    from mcp_google_ads_safe import app, tools  # noqa: F401
    root = pathlib.Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text()
    for tool in asyncio.run(app.mcp.list_tools()):
        assert f"`{tool.name}`" in readme, f"README does not mention {tool.name}"
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert f"Version {version}." in readme
    assert f"## {version} " in (root / "CHANGELOG.md").read_text()
