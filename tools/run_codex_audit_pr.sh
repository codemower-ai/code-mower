#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(CDPATH= cd -- "${script_dir}/.." && pwd -P)"
unset CODE_MOWER_USE_LOCAL
unset CODE_MOWER_USE_STANDALONE

token_from_stdin=""
filtered_args=()
for arg in "$@"; do
  case "${arg}" in
    --token-from-stdin|--read-token-from-stdin)
      token_from_stdin=1
      ;;
    *)
      filtered_args+=("${arg}")
      ;;
  esac
done

if [ -n "${token_from_stdin}" ]; then
  exec env -u GITHUB_TOKEN -u GH_TOKEN "${repo_root}/scripts/dev-python" -m code_mower.cli codex-audit --read-token-from-stdin "${filtered_args[@]}"
fi

env_token="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
if [ -n "${env_token}" ]; then
  printf '%s\n' "${env_token}" | env -u GITHUB_TOKEN -u GH_TOKEN "${repo_root}/scripts/dev-python" -m code_mower.cli codex-audit --read-token-from-stdin "${filtered_args[@]}"
  exit $?
fi

exec env -u GITHUB_TOKEN -u GH_TOKEN "${repo_root}/scripts/dev-python" -m code_mower.cli codex-audit "${filtered_args[@]}"
