#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_code_mower="${script_dir}/code_mower"
pin_file="${CODE_MOWER_STANDALONE_PIN_FILE:-${script_dir}/code_mower_standalone_pin.env}"

standalone_env_configured() {
  [ -n "${CODE_MOWER_STANDALONE_COMMAND:-}" ] \
    || [ -n "${CODE_MOWER_STANDALONE_PATH:-}" ] \
    || [ -n "${CODE_MOWER_STANDALONE_REF:-}" ] \
    || [ -n "${CODE_MOWER_STANDALONE_SOURCE_DIR:-}" ]
}

standalone_pin_configured() {
  [ -f "${pin_file}" ] || return 1
  if grep -Eq 'https://github.com/OWNER/code-mower.git|<pin-a-reviewed-code-mower-commit-or-tag>' "${pin_file}"; then
    return 1
  fi
  grep -Eq '^CODE_MOWER_STANDALONE_(COMMAND|PATH|REF|SOURCE_DIR)=' "${pin_file}"
}

resolve_code_mower() {
  local installed
  if [ "${CODE_MOWER_USE_LOCAL:-}" = "1" ] || [ "${CODE_MOWER_USE_STANDALONE:-}" = "1" ] || standalone_env_configured || standalone_pin_configured; then
    printf '%s\n' "${repo_code_mower}"
    return
  fi
  installed="$(command -v code-mower 2>/dev/null || true)"
  if [ -n "${installed}" ]; then
    echo "notice: using installed code-mower from PATH; configure ${pin_file} or set CODE_MOWER_STANDALONE_REF to use standalone shadow mode." >&2
    printf '%s\n' "${installed}"
    return
  fi
  printf '%s\n' "${repo_code_mower}"
}

code_mower="$(resolve_code_mower)"

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
  exec env -u GITHUB_TOKEN -u GH_TOKEN "${code_mower}" devin-cli-audit --read-token-from-stdin "${filtered_args[@]}"
fi

env_token="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
if [ -n "${env_token}" ]; then
  printf '%s\n' "${env_token}" | env -u GITHUB_TOKEN -u GH_TOKEN "${code_mower}" devin-cli-audit --read-token-from-stdin "${filtered_args[@]}"
  exit $?
fi

exec env -u GITHUB_TOKEN -u GH_TOKEN "${code_mower}" devin-cli-audit "${filtered_args[@]}"
