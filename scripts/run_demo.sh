#!/usr/bin/env bash
set -euo pipefail

plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
state_dir="${plugin_dir}/.demo-state"
agent_launcher="${plugin_dir}/scripts/run_agent.sh"
workspace_python="${plugin_dir}/.venv/bin/python"
python_bin="${PYTHON:-${workspace_python}}"

if [[ ! -x "${python_bin}" ]]; then
  echo "Python environment not found: ${python_bin}; run 'uv sync --frozen'." >&2
  exit 1
fi

export PYTHONPATH="${plugin_dir}/src${PYTHONPATH:+:${PYTHONPATH}}"
"${python_bin}" "${plugin_dir}/scripts/setup_demo_db.py" --state-dir "${state_dir}"
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

echo "Demo ready. Ask Codex to list providers, then inspect Alice, Bob, or Carol."
"${agent_launcher}" \
  "${state_dir}/agents/producer.env" \
  "Producer" \
  "$@"
