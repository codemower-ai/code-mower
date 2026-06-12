#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(CDPATH= cd -- "${script_dir}/.." && pwd -P)"
pin_file="${CODE_MOWER_STANDALONE_PIN_FILE:-${script_dir}/code_mower_standalone_pin.env}"
env_repo_url="${CODE_MOWER_STANDALONE_REPO_URL:-}"
env_ref="${CODE_MOWER_STANDALONE_REF:-}"

if [ -f "${pin_file}" ]; then
  # shellcheck disable=SC1090
  . "${pin_file}"
fi
if [ -n "${env_repo_url}" ]; then
  CODE_MOWER_STANDALONE_REPO_URL="${env_repo_url}"
fi
if [ -n "${env_ref}" ]; then
  CODE_MOWER_STANDALONE_REF="${env_ref}"
fi

repo_url="${CODE_MOWER_STANDALONE_REPO_URL:-https://github.com/OWNER/code-mower.git}"
ref="${CODE_MOWER_STANDALONE_REF:-}"
source_dir="${CODE_MOWER_STANDALONE_SOURCE_DIR:-${repo_root}/.code-mower/standalone/code-mower}"

if [ -z "${ref}" ]; then
  echo "error: CODE_MOWER_STANDALONE_REF is required for standalone checkout mode." >&2
  echo "Set it in ${pin_file} or export CODE_MOWER_STANDALONE_REF." >&2
  exit 1
fi
if [ "${repo_url}" = "https://github.com/OWNER/code-mower.git" ] || [ "${ref}" = "<pin-a-reviewed-code-mower-commit-or-tag>" ]; then
  echo "error: replace the placeholder Code Mower standalone repository URL and ref before using standalone checkout mode." >&2
  echo "Update ${pin_file}, or export CODE_MOWER_STANDALONE_REPO_URL and CODE_MOWER_STANDALONE_REF." >&2
  exit 1
fi

if [ ! -d "${source_dir}/.git" ]; then
  mkdir -p "$(dirname -- "${source_dir}")"
  git clone --quiet "${repo_url}" "${source_dir}"
fi

existing_origin="$(git -C "${source_dir}" remote get-url origin 2>/dev/null || true)"
if [ "${existing_origin}" != "${repo_url}" ]; then
  git -C "${source_dir}" remote set-url origin "${repo_url}"
fi

if ! git -C "${source_dir}" cat-file -e "${ref}^{commit}" 2>/dev/null; then
  git -C "${source_dir}" fetch --quiet --tags origin "${ref}" || git -C "${source_dir}" fetch --quiet --tags origin
fi
git -C "${source_dir}" checkout --quiet --detach "${ref}"

export CODE_MOWER_STANDALONE_PATH="${source_dir}"
exec "${script_dir}/code_mower" "$@"
