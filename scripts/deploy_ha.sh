#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

DRY_RUN=0
RESTART_APPDAEMON=1
APPDAEMON_APP_NAME="dynamisch_handelen"

usage() {
  cat <<'EOF'
Gebruik:
  scripts/deploy_ha.sh [--dry-run] [--no-restart]

Environment variables:
  HA_SSH_HOST                 Verplicht. Hostnaam of IP-adres van Home Assistant.
  HA_SSH_USER                 Optioneel. SSH-gebruiker. Default: root
  HA_SSH_PORT                 Optioneel. SSH-poort. Default: 22
  HA_CONFIG_DIR               Optioneel. Home Assistant config-map. Default: /config
  HA_APPDAEMON_ADDON_SLUG     Optioneel. AppDaemon app slug. Default: a0d7b954_appdaemon

Het script leest eerst .env uit de repo-root wanneer dat bestand bestaat.

Opties:
  --dry-run       Toon welke bestanden rsync zou kopieren. Voer geen HA check, HA reload, of AppDaemon restart uit.
  --no-restart    Kopieer bestanden maar herstart AppDaemon niet.
  -h, --help      Toon deze hulptekst.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      RESTART_APPDAEMON=0
      shift
      ;;
    --no-restart)
      RESTART_APPDAEMON=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Onbekende optie: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  set +a
fi

HA_SSH_HOST="${HA_SSH_HOST:-}"
HA_SSH_USER="${HA_SSH_USER:-root}"
HA_SSH_PORT="${HA_SSH_PORT:-22}"
HA_CONFIG_DIR="${HA_CONFIG_DIR:-/config}"
HA_APPDAEMON_ADDON_SLUG="${HA_APPDAEMON_ADDON_SLUG:-a0d7b954_appdaemon}"

if [[ -z "${HA_SSH_HOST}" ]]; then
  echo "HA_SSH_HOST is verplicht. Zet HA_SSH_HOST in .env of exporteer HA_SSH_HOST voor je dit script start." >&2
  exit 1
fi

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "${name} is nodig, maar ${name} staat niet in PATH." >&2
    exit 1
  fi
}

remote_quote() {
  printf '%q' "$1"
}

remote_run() {
  ssh -p "${HA_SSH_PORT}" "${HA_SSH_USER}@${HA_SSH_HOST}" "$@"
}

sync_file() {
  local source_rel="$1"
  local target_rel="$2"
  local source_path="${REPO_ROOT}/${source_rel}"
  local target_path="${HA_CONFIG_DIR}/${target_rel}"
  local target_dir
  target_dir="$(dirname -- "${target_path}")"

  if [[ ! -f "${source_path}" ]]; then
    echo "Bronbestand bestaat niet: ${source_rel}" >&2
    exit 1
  fi

  remote_run "mkdir -p $(remote_quote "${target_dir}")"

  local rsync_args=(-avz)
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    rsync_args+=(--dry-run)
  fi

  rsync "${rsync_args[@]}" -e "ssh -p ${HA_SSH_PORT}" "${source_path}" "${HA_SSH_USER}@${HA_SSH_HOST}:$(remote_quote "${target_path}")"
}

sync_apps_yaml_section() {
  local target_path="${HA_CONFIG_DIR}/appdaemon/apps/apps.yaml"
  local target_dir
  target_dir="$(dirname -- "${target_path}")"
  local temp_dir
  temp_dir="$(mktemp -d)"
  local remote_apps="${temp_dir}/remote-apps.yaml"
  local merged_apps="${temp_dir}/merged-apps.yaml"

  remote_run "mkdir -p $(remote_quote "${target_dir}")"
  remote_run "[ -f $(remote_quote "${target_path}") ] && cat $(remote_quote "${target_path}") || true" >"${remote_apps}"

  python3 "${REPO_ROOT}/scripts/merge_apps_yaml.py" \
    --section "${APPDAEMON_APP_NAME}" \
    --source "${REPO_ROOT}/appdaemon/apps/apps.yaml" \
    --remote "${remote_apps}" \
    --output "${merged_apps}"

  local rsync_args=(-avz)
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    rsync_args+=(--dry-run)
  fi

  rsync "${rsync_args[@]}" -e "ssh -p ${HA_SSH_PORT}" "${merged_apps}" "${HA_SSH_USER}@${HA_SSH_HOST}:$(remote_quote "${target_path}")"
  rm -rf "${temp_dir}"
}

check_and_reload_home_assistant() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "Home Assistant config check en reload overgeslagen door --dry-run."
    return
  fi

  echo "Controleer Home Assistant YAML-configuratie."
  remote_run "ha core check"

  echo "Reload Home Assistant configuratie."
  remote_run "ha core reload"
}

require_command ssh
require_command rsync
require_command python3

echo "Deploy naar ${HA_SSH_USER}@${HA_SSH_HOST}:${HA_CONFIG_DIR}"

sync_apps_yaml_section
sync_file "appdaemon/apps/dynamisch_handelen.py" "appdaemon/apps/dynamisch_handelen.py"
sync_file "appdaemon/apps/strategie_dp.py" "appdaemon/apps/strategie_dp.py"
sync_file "Dutch (NL) Integration/packages/zendure_gielz1986_nl.yaml" "packages/zendure_gielz1986_nl.yaml"
sync_file "Dutch (NL) Integration/packages/zendure_local_nl.yaml" "packages/zendure_local_nl.yaml"

check_and_reload_home_assistant

if [[ "${RESTART_APPDAEMON}" -eq 1 ]]; then
  echo "Herstart AppDaemon app: ${HA_APPDAEMON_ADDON_SLUG}"
  remote_run "ha apps restart $(remote_quote "${HA_APPDAEMON_ADDON_SLUG}")"
else
  echo "AppDaemon restart overgeslagen."
fi
