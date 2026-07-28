#!/usr/bin/env bash
set -euo pipefail

plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
state_dir="${plugin_dir}/.demo-state"
infra_log="${state_dir}/infra.log"
infra_launcher="${plugin_dir}/scripts/run_infra.sh"
agent_launcher="${plugin_dir}/scripts/run_new_agent.sh"
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

mkdir -p -m 700 "${state_dir}"
: > "${infra_log}"
"${infra_launcher}" > "${infra_log}" 2>&1 &
infra_pid=$!

cleanup() {
  kill "${infra_pid}" 2>/dev/null || true
  wait "${infra_pid}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 100); do
  [[ -f "${state_dir}/ready" ]] && break
  kill -0 "${infra_pid}" 2>/dev/null || {
    echo "Demo services exited before becoming ready." >&2
    tail -n 20 "${infra_log}" >&2
    exit 1
  }
  sleep 0.1
done

if [[ ! -f "${state_dir}/ready" ]]; then
  echo "Timed out waiting for demo services." >&2
  tail -n 20 "${infra_log}" >&2
  exit 1
fi

echo "Demo ready for ${client}. Ask it to list providers, then inspect Alice, Bob, or Carol."
echo "Human-only control panel: http://127.0.0.1:8721/demo"
"${agent_launcher}" \
  "${client}" \
  producer \
  --agent-id aauth:producer@demo.local \
  --person alice \
  --display-name Producer \
  -- \
  "$@"
