#!/usr/bin/env bash
set -euo pipefail

plugin_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
state_dir="${plugin_dir}/.demo-state"
workspace_python="${plugin_dir}/.venv/bin/python"
python_bin="${PYTHON:-${workspace_python}}"

if [[ ! -x "${python_bin}" ]]; then
  echo "Python environment not found: ${python_bin}; run 'uv sync --frozen'." >&2
  exit 1
fi

export PYTHONPATH="${plugin_dir}/src${PYTHONPATH:+:${PYTHONPATH}}"
"${python_bin}" "${plugin_dir}/scripts/setup_demo_db.py" --state-dir "${state_dir}"

echo "Starting infra: Alice/Bob/Carol resource + access servers, Sentinel, control panel."
echo "Control panel: http://127.0.0.1:8721/demo"
echo "Ctrl-C to stop."
exec "${python_bin}" -m mcp_edocs_agent.demo --state-dir "${state_dir}"
