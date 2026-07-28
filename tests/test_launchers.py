import json
import os
import shutil
import stat
import subprocess
from pathlib import Path


def test_claude_launcher_uses_generated_strict_mcp_config(tmp_path):
    plugin_root = Path(__file__).resolve().parents[1]
    capture = tmp_path / "args.json"
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "value = {'args': sys.argv[1:], 'cwd': os.getcwd(), "
        "'edocs_env': sorted(k for k in os.environ if k.startswith('EDOCS_'))}\n"
        "open(os.environ['CAPTURE'], 'w').write(json.dumps(value))\n"
    )
    fake_claude.chmod(
        fake_claude.stat().st_mode | stat.S_IXUSR
    )
    mcp_config = tmp_path / "claude-mcp.json"
    mcp_config.write_text('{"mcpServers": {}}')
    agent_env = tmp_path / "producer.env"
    agent_env.write_text(
        "EDOCS_DEMO_AGENT_ID=aauth:producer@demo.local\n"
        "EDOCS_DEMO_AGENT_ROLE=producer\n"
        f"EDOCS_CLAUDE_MCP_CONFIG={mcp_config}\n"
    )
    env = {
        **os.environ,
        "CLAUDE_BIN": str(fake_claude),
        "CAPTURE": str(capture),
        "TMPDIR": str(tmp_path),
    }

    completed = subprocess.run(
        [
            plugin_root / "scripts" / "run_coding_agent.sh",
            "claude",
            agent_env,
            "Producer",
            "--model",
            "test-model",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0
    captured = json.loads(capture.read_text())
    assert captured["args"] == [
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        "--no-chrome",
        "--disable-slash-commands",
        "--disallowedTools",
        "Bash,Read,Glob,Grep,WebFetch,WebSearch,Edit,Write,NotebookEdit,Task",
        "--model",
        "test-model",
    ]
    assert Path(captured["cwd"]).parent == tmp_path
    assert Path(captured["cwd"]).name.startswith("edocs-producer.")
    assert captured["edocs_env"] == []


def test_codex_launcher_disables_general_purpose_tools(tmp_path):
    plugin_root = Path(__file__).resolve().parents[1]
    capture = tmp_path / "codex.json"
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "value = {'args': sys.argv[1:], 'cwd': os.getcwd(), "
        "'edocs_env': sorted(k for k in os.environ if k.startswith('EDOCS_'))}\n"
        "open(os.environ['CAPTURE'], 'w').write(json.dumps(value))\n"
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
    agent_env = tmp_path / "producer.env"
    agent_env.write_text(
        "EDOCS_PROVIDER_FILE=/private/provider.json\n"
        "EDOCS_AGENT_KEY_FILE=/private/agent.jwk\n"
        "EDOCS_AGENT_TOKEN_FILE=/private/agent.token\n"
        "EDOCS_PERSON=alice\n"
        "EDOCS_DEMO_AGENT_ID=aauth:producer@demo.local\n"
        "EDOCS_DEMO_AGENT_ROLE=producer\n"
        "EDOCS_FUNCTION_REGISTRY_URL=http://127.0.0.1:8721/functions\n"
    )
    env = {
        **os.environ,
        "CODEX_BIN": str(fake_codex),
        "CAPTURE": str(capture),
        "TMPDIR": str(tmp_path),
    }

    completed = subprocess.run(
        [
            plugin_root / "scripts" / "run_coding_agent.sh",
            "codex",
            agent_env,
            "Producer",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0
    captured = json.loads(capture.read_text())
    disabled = {
        captured["args"][index + 1]
        for index, value in enumerate(captured["args"][:-1])
        if value == "--disable"
    }
    assert {
        "shell_tool",
        "unified_exec",
        "apps",
        "browser_use",
        "computer_use",
        "plugins",
        "skill_search",
    } <= disabled
    assert 'web_search="disabled"' in captured["args"]
    assert "agents.enabled=false" in captured["args"]
    assert "mcp_servers={}" in captured["args"]
    assert Path(captured["cwd"]).parent == tmp_path
    assert Path(captured["cwd"]).name.startswith("edocs-producer.")
    assert captured["edocs_env"] == []


def test_demo_launcher_keeps_background_output_out_of_tui(tmp_path):
    plugin_root = tmp_path / "plugin"
    scripts_dir = plugin_root / "scripts"
    python_bin = plugin_root / ".venv" / "bin" / "python"
    scripts_dir.mkdir(parents=True)
    python_bin.parent.mkdir(parents=True)
    source_root = Path(__file__).resolve().parents[1]
    launcher = scripts_dir / "run_demo.sh"
    shutil.copy(source_root / "scripts" / "run_demo.sh", launcher)
    python_bin.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ $1 == *setup_demo_db.py ]]; then\n"
        "  exit 0\n"
        "fi\n"
        "state_dir=$4\n"
        "mkdir -p \"${state_dir}\"\n"
        "touch \"${state_dir}/ready\"\n"
        "echo backend-ready\n"
        "echo backend-warning >&2\n"
        "trap 'exit 0' TERM\n"
        "while true; do sleep 1; done\n"
    )
    agent_launcher = scripts_dir / "run_coding_agent.sh"
    agent_launcher.write_text(
        "#!/usr/bin/env bash\n"
        "echo tui-started\n"
    )
    for path in (launcher, python_bin, agent_launcher):
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    completed = subprocess.run(
        [launcher, "--client", "claude"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "tui-started" in completed.stdout
    assert "backend-ready" not in completed.stdout
    assert "backend-warning" not in completed.stderr
    assert (plugin_root / ".demo-state" / "demo.log").read_text() == (
        "backend-ready\nbackend-warning\n"
    )
