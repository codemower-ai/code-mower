#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(CDPATH= cd -- "${script_dir}/.." && pwd -P)"
pin_file="${CODE_MOWER_STANDALONE_PIN_FILE:-${script_dir}/code_mower_standalone_pin.env}"
override_repo_url="${CODE_MOWER_STANDALONE_REPO_URL:-}"
override_ref="${CODE_MOWER_STANDALONE_REF:-}"
override_source_dir="${CODE_MOWER_STANDALONE_SOURCE_DIR:-}"
pin_file_missing=0

if [ -f "${pin_file}" ]; then
  # shellcheck disable=SC1090
  . "${pin_file}"
else
  pin_file_missing=1
fi
if [ -n "${override_repo_url}" ]; then
  CODE_MOWER_STANDALONE_REPO_URL="${override_repo_url}"
fi
if [ -n "${override_ref}" ]; then
  CODE_MOWER_STANDALONE_REF="${override_ref}"
fi
if [ -n "${override_source_dir}" ]; then
  CODE_MOWER_STANDALONE_SOURCE_DIR="${override_source_dir}"
fi

repo_url="${CODE_MOWER_STANDALONE_REPO_URL:-https://github.com/OWNER/code-mower.git}"
source_dir="${CODE_MOWER_STANDALONE_SOURCE_DIR:-${repo_root}/.code-mower/standalone/code-mower}"
checkout_lock_acquired=0
checkout_lock_dir=""

release_checkout_lock() {
  if [ "${checkout_lock_acquired}" = "1" ] && [ -n "${checkout_lock_dir}" ]; then
    rm -rf "${checkout_lock_dir}"
  fi
  checkout_lock_acquired=0
  checkout_lock_dir=""
}
trap release_checkout_lock EXIT

acquire_checkout_lock() {
  local lock_age lock_mtime lock_pid now parent_dir stale_seconds started timeout_seconds
  checkout_lock_dir="${source_dir}.checkout.lock"
  parent_dir="$(dirname -- "${checkout_lock_dir}")"
  stale_seconds="${CODE_MOWER_STANDALONE_CHECKOUT_LOCK_STALE_SECONDS:-30}"
  timeout_seconds="${CODE_MOWER_STANDALONE_CHECKOUT_LOCK_TIMEOUT_SECONDS:-120}"
  started="$(date +%s)"
  mkdir -p "${parent_dir}"
  while ! mkdir "${checkout_lock_dir}" 2>/dev/null; do
    now="$(date +%s)"
    lock_pid=""
    if [ -f "${checkout_lock_dir}/pid" ]; then
      lock_pid="$(cat "${checkout_lock_dir}/pid" 2>/dev/null || true)"
    fi
    lock_mtime="$(stat -c %Y "${checkout_lock_dir}" 2>/dev/null || stat -f %m "${checkout_lock_dir}" 2>/dev/null || echo "${now}")"
    case "${lock_mtime}" in
      ''|*[!0-9]*) lock_mtime="${now}" ;;
    esac
    lock_age=$((now - lock_mtime))
    if [ -z "${lock_pid}" ] && [ "${lock_age}" -ge "${stale_seconds}" ]; then
      rm -rf "${checkout_lock_dir}" 2>/dev/null || true
      continue
    fi
    if [ -n "${lock_pid}" ] && ! kill -0 "${lock_pid}" 2>/dev/null; then
      rm -rf "${checkout_lock_dir}" 2>/dev/null || true
      continue
    fi
    if [ $((now - started)) -ge "${timeout_seconds}" ]; then
      echo "error: timed out waiting for standalone checkout lock: ${checkout_lock_dir}" >&2
      exit 1
    fi
    sleep 0.2
  done
  checkout_lock_acquired=1
  printf '%s\n' "$$" > "${checkout_lock_dir}/pid"
}

if [ -n "${CODE_MOWER_STANDALONE_COMMAND:-}" ]; then
  exec "${script_dir}/code_mower" "$@"
fi

if [ -z "${CODE_MOWER_STANDALONE_PATH:-}" ]; then
  if [ -z "${CODE_MOWER_STANDALONE_REF:-}" ]; then
    if [ "${pin_file_missing}" = "1" ]; then
      echo "error: missing Code Mower standalone pin file: ${pin_file}" >&2
      echo "Restore tools/code_mower_standalone_pin.env or set CODE_MOWER_STANDALONE_REF explicitly." >&2
    else
      echo "error: CODE_MOWER_STANDALONE_REF is required for standalone shadow mode." >&2
      echo "Add CODE_MOWER_STANDALONE_REF to ${pin_file} or set it explicitly." >&2
    fi
    exit 1
  fi
  ref="${CODE_MOWER_STANDALONE_REF}"
  if [ "${repo_url}" = "https://github.com/OWNER/code-mower.git" ] || [ "${ref}" = "<pin-a-reviewed-code-mower-commit-or-tag>" ]; then
    echo "error: replace the placeholder Code Mower standalone repository URL and ref before using standalone shadow mode." >&2
    echo "Update ${pin_file}, or set CODE_MOWER_STANDALONE_REPO_URL and CODE_MOWER_STANDALONE_REF explicitly." >&2
    exit 1
  fi
  acquire_checkout_lock
  if [ ! -d "${source_dir}/.git" ]; then
    mkdir -p "$(dirname -- "${source_dir}")"
    git clone --quiet "${repo_url}" "${source_dir}"
  fi
  repo_url_changed=""
  existing_origin="$(git -C "${source_dir}" remote get-url origin 2>/dev/null || true)"
  if [ "${existing_origin}" != "${repo_url}" ]; then
    git -C "${source_dir}" remote set-url origin "${repo_url}"
    repo_url_changed=1
  fi
  previous_head="$(git -C "${source_dir}" rev-parse --verify HEAD 2>/dev/null || true)"
  if [ -n "${repo_url_changed}" ] || ! git -C "${source_dir}" cat-file -e "${ref}^{commit}" 2>/dev/null; then
    git -C "${source_dir}" fetch --quiet --tags origin "${ref}" || git -C "${source_dir}" fetch --quiet --tags origin
  fi
  git -C "${source_dir}" checkout --quiet --detach "${ref}"
  checked_out_head="$(git -C "${source_dir}" rev-parse --verify HEAD)"
  if [ -n "${previous_head}" ] && [ "${previous_head}" != "${checked_out_head}" ] && [ -z "${CODE_MOWER_STANDALONE_REINSTALL:-}" ]; then
    export CODE_MOWER_STANDALONE_REINSTALL=1
  fi
  export CODE_MOWER_STANDALONE_PATH="${source_dir}"
fi

release_checkout_lock
set +e
"${script_dir}/code_mower" "$@"
rc=$?
set -e
exit "${rc}"
