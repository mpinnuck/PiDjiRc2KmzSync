#!/usr/bin/env bash
set -euo pipefail

# deploy.sh
# ---------
# Deploys this code/ folder to the Raspberry Pi over rsync.
#
# Usage:
#   ./deploy.sh              Normal deploy (skips pip install and service
#                             restart by default -- use this for UI-only
#                             changes, since Flask serves static files
#                             straight off disk with no restart needed)
#   ./deploy.sh --dry-run    Show what would change, without changing anything
#   ./deploy.sh --pip        Also reinstall requirements.txt on the Pi
#                            (use when requirements.txt changed)
#   ./deploy.sh --restart    Also restart the pidjirc2kmzsync service
#                            (use when .py code changed)

REMOTE_USER="mark"
REMOTE_HOST="rc2kmzupdater.lan"
REMOTE_PATH="~/PiDjiRc2KmzSync"
REMOTE_VENV="~/PiDjiRc2KmzSync/.venv"

log() {
  printf '[deploy] %s\n' "$*"
}

DRY_RUN=false
DO_PIP=false
DO_RESTART=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --pip) DO_PIP=true ;;
    --restart) DO_RESTART=true ;;
    *)
      log "ERROR: unknown argument: $arg"
      exit 1
      ;;
  esac
done

script_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$script_dir"

# --- Pre-deploy guard: catch syntax errors before they land on the Pi -----
log "Checking Python syntax..."
if ! find . -name "*.py" -not -path "./.venv/*" -print0 | xargs -0 python3 -m py_compile; then
  log "ERROR: one or more .py files failed to compile. Aborting deploy."
  exit 1
fi
log "Syntax check passed."

# --- Pre-deploy guard: confirm the Pi is reachable before rsyncing --------
log "Checking $REMOTE_HOST is reachable..."
if ! ping -c 1 -t 2 "$REMOTE_HOST" >/dev/null 2>&1; then
  log "ERROR: $REMOTE_HOST did not respond to ping. Aborting deploy."
  exit 1
fi
log "$REMOTE_HOST is reachable."

# --- Rsync -----------------------------------------------------------------
# Preserve destination-only files; deploys are additive updates rather than
# exact mirror operations.
RSYNC_FLAGS=(-avz)
if $DRY_RUN; then
  RSYNC_FLAGS+=(--dry-run)
  log "DRY RUN — no files will actually be changed."
fi

# Excluded: local dev artifacts, dev-only tooling that has no purpose on
# the Pi (Makefile, this script itself), and runtime state the Pi
# generates itself. The Pi's own config/copy-map must never be
# overwritten by a deploy from the Mac -- kmz_copy_map_m.json is this
# Mac's own (see CopyMapService's Darwin-specific filename),
# kmz_copy_map.json is the Pi's equivalent.
EXCLUDES=(
  --exclude ".venv/"
  --exclude "__pycache__/"
  --exclude "*.pyc"
  --exclude ".DS_Store"
  --exclude "kmz_sync_config.json"
  --exclude "kmz_copy_map.json"
  --exclude "kmz_copy_map_m.json"
  --exclude "Makefile"
  --exclude "*.sh"
)

log "Syncing to $REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH ..."
rsync "${RSYNC_FLAGS[@]}" "${EXCLUDES[@]}" ./ "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH/"

if $DRY_RUN; then
  log "Dry run complete. Nothing was deployed."
  exit 0
fi

# --- Post-deploy: reinstall requirements only if --pip was passed --------
if $DO_PIP; then
  log "Reinstalling requirements on the Pi..."
  ssh "$REMOTE_USER@$REMOTE_HOST" "source $REMOTE_VENV/bin/activate && pip install -q -r $REMOTE_PATH/requirements.txt"
  log "Requirements up to date."
else
  log "Skipping pip install (pass --pip if requirements.txt changed)."
fi

# --- Post-deploy: restart the running service only if --restart was passed
# User-level systemd unit (see pidjirc2kmzsync.service) -- no sudo needed.
if $DO_RESTART; then
  log "Restarting pidjirc2kmzsync service on the Pi..."
  ssh "$REMOTE_USER@$REMOTE_HOST" "systemctl --user restart pidjirc2kmzsync"
else
  log "Skipping service restart (pass --restart if .py code changed)."
fi
log "Deploy complete."
