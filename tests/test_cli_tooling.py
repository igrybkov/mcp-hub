"""Tests for the config-editing and skill CLI helpers.

Covers `mcp-hub add`, `validate`, `config path`, and the `skill` group, plus a
round-trip of a freshly-added server through `load_servers()`.
"""

from __future__ import annotations

import json

import pytest
import yaml
from click.testing import CliRunner
from mcp_hub.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolate_learned(monkeypatch, tmp_path):
    """Point the learned-auth store at an empty temp file for hermetic loads."""
    monkeypatch.setattr("mcp_hub.auth.LEARNED_AUTH_PATH", tmp_path / "learned-auth.json")


def _json_out(result) -> dict:
    return json.loads(result.output)


# --- config path ---


def test_config_path_lists_sources(runner, monkeypatch, tmp_path):
    cfg = tmp_path / "servers.yml"
    monkeypatch.setenv("CONFIG_FILE", str(cfg))
    result = runner.invoke(main, ["config", "path"])
    assert result.exit_code == 0, result.output
    payload = _json_out(result)
    assert [s["path"] for s in payload["sources"]] == [str(cfg.resolve())]
    assert payload["sources"][0]["exists"] is False
    assert payload["write_target"] == str(cfg.resolve())
    assert payload["write_target_format"] == "yaml"


# --- add ---


def test_add_stdio_writes_yaml_and_roundtrips(runner, monkeypatch, tmp_path, isolate_learned):
    cfg = tmp_path / "servers.yml"
    monkeypatch.setenv("CONFIG_FILE", str(cfg))
    result = runner.invoke(
        main,
        [
            "add",
            "github",
            "--command",
            "gh-mcp",
            "--arg",
            "--stdio",
            "--env",
            "GH_HOST=github.com",
            "--description",
            "GitHub issues",
            "--tag",
            "dev",
        ],
    )
    assert result.exit_code == 0, result.output + result.stderr

    # Written as YAML, not JSON.
    text = cfg.read_text()
    assert not text.lstrip().startswith("{")
    data = yaml.safe_load(text)
    assert data["github"]["command"] == "gh-mcp"
    assert data["github"]["args"] == ["--stdio"]
    assert data["github"]["env"] == {"GH_HOST": "github.com"}

    # Round-trips through the real loader.
    from mcp_hub.config import load_servers

    servers = load_servers()
    spec = servers["github"]
    assert spec.transport == "stdio"
    assert spec.command == "gh-mcp"
    assert spec.args == ["--stdio"]
    assert spec.env == {"GH_HOST": "github.com"}
    assert spec.tags == ["dev"]


def test_add_from_json_moves_secret_to_keychain_schema(
    runner, monkeypatch, tmp_path, isolate_learned
):
    cfg = tmp_path / "servers.yml"
    monkeypatch.setenv("CONFIG_FILE", str(cfg))
    snippet = json.dumps(
        {
            "mcpServers": {
                "MongoDB": {
                    "command": "npx",
                    "args": ["-y", "mongodb-mcp-server@latest"],
                    "env": {
                        "MDB_MCP_CONNECTION_STRING": "mongodb+srv://user:pass@host/db",
                        "MDB_LOG_PATH": "/tmp/log",
                    },
                }
            }
        }
    )
    result = runner.invoke(
        main,
        ["add", "mongodb", "--from-json", snippet, "--arg", "--readOnly"],
    )
    assert result.exit_code == 0, result.output + result.stderr
    assert "MDB_MCP_CONNECTION_STRING" in result.output  # reported as moved

    data = yaml.safe_load(cfg.read_text())
    entry = data["mongodb"]
    # Secret moved out of env, value dropped; non-secret env kept.
    assert "MDB_MCP_CONNECTION_STRING" not in entry.get("env", {})
    assert entry["env"] == {"MDB_LOG_PATH": "/tmp/log"}
    assert entry["auth"]["secrets"][0]["env_var"] == "MDB_MCP_CONNECTION_STRING"
    assert entry["args"] == ["-y", "mongodb-mcp-server@latest", "--readOnly"]

    from mcp_hub.config import load_servers

    spec = load_servers()["mongodb"]
    assert spec.auth is not None
    assert [s.env_var for s in spec.auth.secrets] == ["MDB_MCP_CONNECTION_STRING"]


def test_add_keep_env_secrets_leaves_value(runner, monkeypatch, tmp_path):
    cfg = tmp_path / "servers.yml"
    result = runner.invoke(
        main,
        [
            "add",
            "leaky",
            "--command",
            "foo",
            "--env",
            "FOO_TOKEN=raw-secret",
            "--keep-env-secrets",
            "--config",
            str(cfg),
        ],
    )
    assert result.exit_code == 0, result.output + result.stderr
    data = yaml.safe_load(cfg.read_text())
    assert data["leaky"]["env"] == {"FOO_TOKEN": "raw-secret"}
    assert "auth" not in data["leaky"]


def test_add_http_sse_with_header(runner, tmp_path):
    cfg = tmp_path / "servers.yml"
    result = runner.invoke(
        main,
        [
            "add",
            "metrics",
            "--url",
            "https://metrics.example.com/sse",
            "--transport",
            "sse",
            "--header",
            "X-Trace=on",
            "--config",
            str(cfg),
        ],
    )
    assert result.exit_code == 0, result.output + result.stderr
    entry = yaml.safe_load(cfg.read_text())["metrics"]
    assert entry["url"] == "https://metrics.example.com/sse"
    assert entry["transport"] == "sse"
    assert entry["headers"] == {"X-Trace": "on"}
    assert "command" not in entry


def test_add_secret_flag_with_label_and_url(runner, tmp_path):
    cfg = tmp_path / "servers.yml"
    result = runner.invoke(
        main,
        [
            "add",
            "linear",
            "--command",
            "linear-mcp",
            "--secret",
            "LINEAR_API_KEY:Linear API key:https://linear.app/settings/api",
            "--config",
            str(cfg),
        ],
    )
    assert result.exit_code == 0, result.output + result.stderr
    secret = yaml.safe_load(cfg.read_text())["linear"]["auth"]["secrets"][0]
    assert secret == {
        "env_var": "LINEAR_API_KEY",
        "label": "Linear API key",
        "create_url": "https://linear.app/settings/api",
    }


def test_add_refuses_duplicate_without_force(runner, tmp_path):
    cfg = tmp_path / "servers.yml"
    base = ["add", "dup", "--command", "foo", "--config", str(cfg)]
    assert runner.invoke(main, base).exit_code == 0
    dup = runner.invoke(main, base)
    assert dup.exit_code != 0
    assert "already exists" in dup.stderr
    forced = runner.invoke(main, [*base, "--force"])
    assert forced.exit_code == 0


def test_add_requires_command_or_url(runner, tmp_path):
    cfg = tmp_path / "servers.yml"
    result = runner.invoke(main, ["add", "empty", "--config", str(cfg)])
    assert result.exit_code != 0
    assert "command" in result.stderr


def test_add_dry_run_does_not_write(runner, tmp_path):
    cfg = tmp_path / "servers.yml"
    result = runner.invoke(
        main, ["add", "ghost", "--command", "foo", "--config", str(cfg), "--dry-run"]
    )
    assert result.exit_code == 0, result.output + result.stderr
    assert not cfg.exists()
    assert "ghost:" in result.output


def test_add_new_json_file_is_wrapped(runner, tmp_path):
    cfg = tmp_path / "servers.json"
    result = runner.invoke(main, ["add", "x", "--command", "foo", "--config", str(cfg)])
    assert result.exit_code == 0, result.output + result.stderr
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["x"]["command"] == "foo"


# --- validate ---


def test_validate_ok(runner, tmp_path):
    cfg = tmp_path / "servers.yml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "good": {
                    "command": "foo",
                    "env": {"FOO_BASE_URL": "https://api.example.com"},
                }
            }
        )
    )
    result = runner.invoke(main, ["validate", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    payload = _json_out(result)
    assert payload["ok"] is True
    assert payload["errors"] == []


def test_validate_flags_raw_secret_and_missing_command(runner, tmp_path):
    cfg = tmp_path / "servers.yml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "leaky": {
                    "command": "foo",
                    "env": {
                        "FOO_API_TOKEN": "sk-secret",
                        "FOO_BASE_URL": "https://api.example.com",
                    },
                },
                "broken": {},
            }
        )
    )
    result = runner.invoke(main, ["validate", "--config", str(cfg)])
    assert result.exit_code == 1
    payload = _json_out(result)
    assert payload["ok"] is False
    codes = {e["code"] for e in payload["errors"]}
    assert "raw_secret" in codes
    assert "missing_command_or_url" in codes
    fields = {e.get("field") for e in payload["errors"]}
    assert "env.FOO_API_TOKEN" in fields
    # Non-secret env var is not flagged.
    assert "env.FOO_BASE_URL" not in fields


def test_validate_placeholder_header_not_flagged(runner, tmp_path):
    cfg = tmp_path / "servers.yml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "svc": {
                    "url": "https://x.example.com/mcp",
                    "headers": {"Authorization": "Bearer ${TOKEN}"},
                }
            }
        )
    )
    result = runner.invoke(main, ["validate", "--config", str(cfg)])
    assert result.exit_code == 0, result.output


def test_validate_command_and_url_conflict(runner, tmp_path):
    cfg = tmp_path / "servers.yml"
    cfg.write_text(yaml.safe_dump({"both": {"command": "foo", "url": "https://x"}}))
    result = runner.invoke(main, ["validate", "--config", str(cfg)])
    assert result.exit_code == 1
    codes = {e["code"] for e in _json_out(result)["errors"]}
    assert "command_and_url" in codes


def test_validate_reports_parse_error_as_json(runner, tmp_path):
    cfg = tmp_path / "servers.json"
    cfg.write_text("{ not valid json")
    result = runner.invoke(main, ["validate", "--config", str(cfg)])
    assert result.exit_code == 1
    payload = _json_out(result)
    assert payload["ok"] is False
    assert any(e["code"] == "parse_error" for e in payload["errors"])


# --- skill ---


def test_skill_show_default(runner):
    result = runner.invoke(main, ["skill", "show"])
    assert result.exit_code == 0, result.stderr
    assert "name: mcp-hub" in result.output
    assert "Managing MCP servers with mcp-hub" in result.output


def test_skill_show_unknown(runner):
    result = runner.invoke(main, ["skill", "show", "does-not-exist"])
    assert result.exit_code != 0
    assert "unknown skill" in result.stderr


def test_skill_list_reads_bundled_frontmatter(runner):
    result = runner.invoke(main, ["skill", "list"])
    assert result.exit_code == 0, result.stderr
    payload = _json_out(result)
    names = {s["name"] for s in payload["skills"]}
    assert "mcp-hub" in names
    mcp = next(s for s in payload["skills"] if s["name"] == "mcp-hub")
    assert mcp["description"]


def test_skill_install_copies_files(runner, tmp_path):
    dest = tmp_path / "skills"
    result = runner.invoke(main, ["skill", "install", "--dir", str(dest)])
    assert result.exit_code == 0, result.stderr
    assert (dest / "mcp-hub" / "SKILL.md").is_file()
    assert (dest / "mcp-hub" / "reference.md").is_file()

    # Refuses to overwrite without --force, succeeds with it.
    again = runner.invoke(main, ["skill", "install", "--dir", str(dest)])
    assert again.exit_code != 0
    forced = runner.invoke(main, ["skill", "install", "--dir", str(dest), "--force"])
    assert forced.exit_code == 0
