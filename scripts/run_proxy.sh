#!/usr/bin/env bash
set -euo pipefail

plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
project_python="${plugin_dir}/.venv/bin/python"
workspace_python="${plugin_dir}/../mcp-aauth/.venv/bin/python"

if [[ -x "${project_python}" ]]; then
  python_bin="${project_python}"
elif [[ -x "${workspace_python}" ]]; then
  python_bin="${workspace_python}"
else
  python_bin="${PYTHON:-python3}"
fi

export PYTHONPATH="${plugin_dir}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${python_bin}" -m mcp_edocs_agent
