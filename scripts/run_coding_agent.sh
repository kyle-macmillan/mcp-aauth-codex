#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: $0 CLIENT AGENT_ENV DISPLAY_NAME [CLIENT_ARGS...]" >&2
  exit 2
fi

client=$1
agent_env=$2
display_name=$3
shift 3

plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
launcher="${plugin_dir}/scripts/run_proxy.sh"

set -a
source "${agent_env}"
set +a

echo "${display_name}: ${EDOCS_DEMO_AGENT_ID}"
echo "Provider controls: ${EDOCS_DEMO_CONTROL_URL}"

case "${client}" in
  codex)
    exec "${CODEX_BIN:-codex}" \
      -c 'approval_policy={granular={sandbox_approval=true,rules=true,mcp_elicitations=true,request_permissions=true,skill_approval=true}}' \
      -c "mcp_servers.edocs-aauth.command=\"${launcher}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_PROVIDER_FILE=\"${EDOCS_PROVIDER_FILE}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_AGENT_KEY_FILE=\"${EDOCS_AGENT_KEY_FILE}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_AGENT_TOKEN_FILE=\"${EDOCS_AGENT_TOKEN_FILE}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_PERSON=\"${EDOCS_PERSON}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_FUNCTION_REGISTRY_URL=\"${EDOCS_FUNCTION_REGISTRY_URL}\"" \
      "$@"
    ;;
  claude)
    # Built-in tools (Read, Bash, Edit, ...) are unrelated to the AAuth/eDocs
    # consent flow and default to unprompted access to the local checkout;
    # deny them so the session is limited to the edocs-aauth MCP tools.
    exec "${CLAUDE_BIN:-claude}" \
      --strict-mcp-config \
      --mcp-config "${EDOCS_CLAUDE_MCP_CONFIG}" \
      --disallowedTools "Read,Bash,Grep,Glob,Edit,Write,WebFetch,WebSearch,Task,NotebookEdit,TodoWrite,BashOutput,KillShell,ExitPlanMode" \
      "$@"
    ;;
  *)
    echo "Unsupported coding-agent client: ${client}" >&2
    exit 2
    ;;
esac
