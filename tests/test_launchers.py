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
        "open(os.environ['CAPTURE'], 'w').write(json.dumps(sys.argv[1:]))\n"
    )
    fake_claude.chmod(
        fake_claude.stat().st_mode | stat.S_IXUSR
    )
    mcp_config = tmp_path / "claude-mcp.json"
    mcp_config.write_text('{"mcpServers": {}}')
    agent_env = tmp_path / "producer.env"
    agent_env.write_text(
        "EDOCS_DEMO_AGENT_ID=aauth:producer@demo.local\n"
        "EDOCS_DEMO_CONTROL_URL=http://127.0.0.1:8721/demo\n"
        f"EDOCS_CLAUDE_MCP_CONFIG={mcp_config}\n"
    )
    env = {
        **os.environ,
        "CLAUDE_BIN": str(fake_claude),
        "CAPTURE": str(capture),
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
    assert json.loads(capture.read_text()) == [
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        "--model",
        "test-model",
    ]


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
