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

source "${agent_env}"
agent_role="${EDOCS_DEMO_AGENT_ROLE:-agent}"
case "${agent_role}" in
  *[!a-zA-Z0-9_-]*)
    echo "Invalid demo agent role: ${agent_role}" >&2
    exit 2
    ;;
esac
agent_workspace="$(mktemp -d "${TMPDIR:-/tmp}/edocs-${agent_role}.XXXXXX")"
chmod 700 "${agent_workspace}"
cd -- "${agent_workspace}"

echo "${display_name}: ${EDOCS_DEMO_AGENT_ID}"

case "${client}" in
  codex)
    exec env \
      -u EDOCS_PROVIDER_FILE \
      -u EDOCS_AGENT_KEY_FILE \
      -u EDOCS_AGENT_TOKEN_FILE \
      -u EDOCS_PERSON \
      -u EDOCS_DEMO_AGENT_ID \
      -u EDOCS_DEMO_AGENT_ROLE \
      -u EDOCS_DEMO_CONTROL_URL \
      -u EDOCS_CONTROL_URL \
      -u EDOCS_SENTINEL_URL \
      -u EDOCS_AGENT_RESOURCE_URL \
      -u EDOCS_CLAUDE_MCP_CONFIG \
      -u EDOCS_FUNCTION_REGISTRY_URL \
      "${CODEX_BIN:-codex}" \
      -C "${agent_workspace}" \
      --disable shell_tool \
      --disable unified_exec \
      --disable apps \
      --disable browser_use \
      --disable browser_use_external \
      --disable in_app_browser \
      --disable computer_use \
      --disable image_generation \
      --disable plugins \
      --disable skill_search \
      -c 'web_search="disabled"' \
      -c 'agents.enabled=false' \
      -c 'approval_policy={granular={sandbox_approval=true,rules=true,mcp_elicitations=true,request_permissions=true,skill_approval=true}}' \
      -c 'mcp_servers={}' \
      -c "mcp_servers.edocs-aauth.command=\"${launcher}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_PROVIDER_FILE=\"${EDOCS_PROVIDER_FILE}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_AGENT_KEY_FILE=\"${EDOCS_AGENT_KEY_FILE}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_AGENT_TOKEN_FILE=\"${EDOCS_AGENT_TOKEN_FILE}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_PERSON=\"${EDOCS_PERSON}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_FUNCTION_REGISTRY_URL=\"${EDOCS_FUNCTION_REGISTRY_URL}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_SENTINEL_URL=\"${EDOCS_SENTINEL_URL}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_AGENT_RESOURCE_URL=\"${EDOCS_AGENT_RESOURCE_URL}\"" \
      -c "mcp_servers.edocs-aauth.env.EDOCS_DEMO_AGENT_ID=\"${EDOCS_DEMO_AGENT_ID}\"" \
      "$@"
    ;;
  claude)
    # Built-in tools (Read, Bash, Edit, ...) are unrelated to the AAuth/eDocs
    # consent flow and default to unprompted access to the local checkout;
    # deny them so the session is limited to the edocs-aauth MCP tools.
    exec env \
      -u EDOCS_PROVIDER_FILE \
      -u EDOCS_AGENT_KEY_FILE \
      -u EDOCS_AGENT_TOKEN_FILE \
      -u EDOCS_PERSON \
      -u EDOCS_DEMO_AGENT_ID \
      -u EDOCS_DEMO_AGENT_ROLE \
      -u EDOCS_DEMO_CONTROL_URL \
      -u EDOCS_CONTROL_URL \
      -u EDOCS_SENTINEL_URL \
      -u EDOCS_AGENT_RESOURCE_URL \
      -u EDOCS_CLAUDE_MCP_CONFIG \
      -u EDOCS_FUNCTION_REGISTRY_URL \
      "${CLAUDE_BIN:-claude}" \
      --strict-mcp-config \
      --mcp-config "${EDOCS_CLAUDE_MCP_CONFIG}" \
      --no-chrome \
      --disable-slash-commands \
      --disallowedTools "Bash,Read,Glob,Grep,WebFetch,WebSearch,Edit,Write,NotebookEdit,Task,TodoWrite,BashOutput,KillShell,ExitPlanMode" \
      "$@"
    ;;
  *)
    echo "Unsupported coding-agent client: ${client}" >&2
    exit 2
    ;;
esac
