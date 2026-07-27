#!/usr/bin/env bash
set -euo pipefail

plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
workspace_python="${plugin_dir}/../mcp-aauth/.venv/bin/python"

if [[ -x "${workspace_python}" ]]; then
  python_bin="${workspace_python}"
else
  python_bin="${PYTHON:-python3}"
fi

export PYTHONPATH="${plugin_dir}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${python_bin}" -m mcp_aauth_codex
