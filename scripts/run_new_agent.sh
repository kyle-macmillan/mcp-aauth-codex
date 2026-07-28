#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 CLIENT ROLE [--agent-id ID] [--person PERSON] [--display-name NAME] [-- CLIENT_ARGS...]" >&2
}

if [[ $# -lt 2 ]]; then
  usage
  exit 2
fi

client=$1
role=$2
shift 2
agent_id=""
person=""
display_name="${role}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent-id)
      agent_id=$2
      shift 2
      ;;
    --person)
      person=$2
      shift 2
      ;;
    --display-name)
      display_name=$2
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ "${client}" != "codex" && "${client}" != "claude" ]]; then
  echo "Unsupported coding-agent client: ${client}" >&2
  exit 2
fi

plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
state_dir="${plugin_dir}/.demo-state"
workspace_python="${plugin_dir}/.venv/bin/python"
python_bin="${PYTHON:-${workspace_python}}"
agent_launcher="${plugin_dir}/scripts/run_coding_agent.sh"
agent_log="${state_dir}/agents/${role}.log"

if [[ ! -x "${python_bin}" ]]; then
  echo "Python environment not found: ${python_bin}; run 'uv sync --frozen'." >&2
  exit 1
fi
if [[ ! -f "${state_dir}/ready" ]]; then
  echo "Infra isn't running: ${state_dir}/ready not found." >&2
  echo "Start it first with scripts/run_infra.sh." >&2
  exit 1
fi

mkdir -p -m 700 "${state_dir}/agents"
: > "${agent_log}"

export PYTHONPATH="${plugin_dir}/src${PYTHONPATH:+:${PYTHONPATH}}"
new_agent_args=(--state-dir "${state_dir}" --role "${role}")
[[ -n "${agent_id}" ]] && new_agent_args+=(--agent-id "${agent_id}")
[[ -n "${person}" ]] && new_agent_args+=(--person "${person}")

"${python_bin}" -m mcp_edocs_agent.new_agent "${new_agent_args[@]}" \
  > "${agent_log}" 2>&1 &
agent_pid=$!

cleanup() {
  kill "${agent_pid}" 2>/dev/null || true
  wait "${agent_pid}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

ready_path="${state_dir}/agents/${role}.ready"
for _ in $(seq 1 100); do
  [[ -f "${ready_path}" ]] && break
  kill -0 "${agent_pid}" 2>/dev/null || {
    echo "Agent ${role} exited before becoming ready." >&2
    tail -n 20 "${agent_log}" >&2
    exit 1
  }
  sleep 0.1
done
if [[ ! -f "${ready_path}" ]]; then
  echo "Timed out waiting for agent ${role} to become ready." >&2
  tail -n 20 "${agent_log}" >&2
  exit 1
fi

"${agent_launcher}" \
  "${client}" \
  "${state_dir}/agents/${role}.env" \
  "${display_name}" \
  "$@"
