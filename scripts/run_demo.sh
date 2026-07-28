#!/usr/bin/env bash
set -euo pipefail

plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
state_dir="${plugin_dir}/.demo-state"
demo_log="${state_dir}/demo.log"
agent_launcher="${plugin_dir}/scripts/run_coding_agent.sh"
workspace_python="${plugin_dir}/.venv/bin/python"
python_bin="${PYTHON:-${workspace_python}}"
client="codex"

if [[ ${1:-} == "--client" ]]; then
  if [[ $# -lt 2 ]]; then
    echo "--client requires codex or claude" >&2
    exit 2
  fi
  client=$2
  shift 2
fi
if [[ ${1:-} == "--" ]]; then
  shift
fi
if [[ "${client}" != "codex" && "${client}" != "claude" ]]; then
  echo "Unsupported coding-agent client: ${client}" >&2
  exit 2
fi

if [[ ! -x "${python_bin}" ]]; then
  echo "Python environment not found: ${python_bin}; run 'uv sync --frozen'." >&2
  exit 1
fi

export PYTHONPATH="${plugin_dir}/src${PYTHONPATH:+:${PYTHONPATH}}"
"${python_bin}" "${plugin_dir}/scripts/setup_demo_db.py" --state-dir "${state_dir}"
mkdir -p -m 700 "${state_dir}"
: > "${demo_log}"
"${python_bin}" -m mcp_edocs_agent.demo --state-dir "${state_dir}" \
  > "${demo_log}" 2>&1 &
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
    tail -n 20 "${demo_log}" >&2
    exit 1
  }
  sleep 0.1
done

if [[ ! -f "${state_dir}/ready" ]]; then
  echo "Timed out waiting for demo services." >&2
  tail -n 20 "${demo_log}" >&2
  exit 1
fi

echo "Demo ready for ${client}. Ask it to list providers, then inspect Alice, Bob, or Carol."
"${agent_launcher}" \
  "${client}" \
  "${state_dir}/agents/producer.env" \
  "Producer" \
  "$@"
