#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 AGENT_ENV DISPLAY_NAME [CODEX_ARGS...]" >&2
  exit 2
fi

agent_env=$1
display_name=$2
shift 2

plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
launcher="${plugin_dir}/scripts/run_proxy.sh"

set -a
source "${agent_env}"
set +a

echo "${display_name}: ${EDOCS_DEMO_AGENT_ID}"
echo "Provider controls: ${EDOCS_DEMO_CONTROL_URL}"
exec "${CODEX_BIN:-codex}" \
  -c 'approval_policy={granular={sandbox_approval=true,rules=true,mcp_elicitations=true,request_permissions=true,skill_approval=true}}' \
  -c "mcp_servers.edocs-aauth.command=\"${launcher}\"" \
  -c "mcp_servers.edocs-aauth.env.EDOCS_PROVIDER_FILE=\"${EDOCS_PROVIDER_FILE}\"" \
  -c "mcp_servers.edocs-aauth.env.EDOCS_AGENT_KEY_FILE=\"${EDOCS_AGENT_KEY_FILE}\"" \
  -c "mcp_servers.edocs-aauth.env.EDOCS_AGENT_TOKEN_FILE=\"${EDOCS_AGENT_TOKEN_FILE}\"" \
  -c "mcp_servers.edocs-aauth.env.EDOCS_PERSON=\"${EDOCS_PERSON}\"" \
  -c "mcp_servers.edocs-aauth.env.EDOCS_FUNCTION_REGISTRY_URL=\"${EDOCS_FUNCTION_REGISTRY_URL}\"" \
  "$@"
