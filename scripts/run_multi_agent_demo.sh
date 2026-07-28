#!/usr/bin/env bash
set -euo pipefail

plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
state_dir="${plugin_dir}/.demo-state"
workspace_python="${plugin_dir}/.venv/bin/python"
python_bin="${PYTHON:-${workspace_python}}"
agent_launcher="${plugin_dir}/scripts/run_agent.sh"
session_name="edocs-demo"

if [[ ! -x "${python_bin}" ]]; then
  echo "Python environment not found: ${python_bin}; run 'uv sync --frozen'." >&2
  exit 1
fi
if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is required for the multi-agent launcher." >&2
  exit 1
fi
if tmux has-session -t "${session_name}" 2>/dev/null; then
  echo "tmux session ${session_name} already exists." >&2
  echo "Attach with: tmux attach -t ${session_name}" >&2
  exit 1
fi

export PYTHONPATH="${plugin_dir}/src${PYTHONPATH:+:${PYTHONPATH}}"
"${python_bin}" "${plugin_dir}/scripts/setup_demo_db.py" \
  --state-dir "${state_dir}"
"${python_bin}" -m mcp_aauth_codex.demo --state-dir "${state_dir}" &
demo_pid=$!

cleanup() {
  kill "${demo_pid}" 2>/dev/null || true
  wait "${demo_pid}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 100); do
  [[ -f "${state_dir}/ready" ]] && break
  kill -0 "${demo_pid}" 2>/dev/null || {
    echo "Demo services exited before becoming ready." >&2
    exit 1
  }
  sleep 0.1
done
if [[ ! -f "${state_dir}/ready" ]]; then
  echo "Timed out waiting for demo services." >&2
  exit 1
fi

tmux new-session -d -s "${session_name}" -n agents \
  "\"${agent_launcher}\" \"${state_dir}/agents/producer.env\" Producer"
tmux split-window -h -t "${session_name}:agents" \
  "\"${agent_launcher}\" \"${state_dir}/agents/carol.env\" Carol"
tmux split-window -v -t "${session_name}:agents.1" \
  "\"${agent_launcher}\" \"${state_dir}/agents/bob.env\" Bob"
tmux select-layout -t "${session_name}:agents" tiled
tmux select-pane -t "${session_name}:agents.0"

echo "Multi-agent demo ready."
echo "Producer: aauth:codex@demo.local"
echo "Carol:    aauth:carol@demo.local"
echo "Bob:      aauth:bob@demo.local"
echo "Control panel: http://127.0.0.1:8721/demo"
tmux attach-session -t "${session_name}"
