#!/usr/bin/env bash
# Example: ship the working tree to a server over rsync and rebuild the container there.
#
# Never touches the server's state: data/ (DB, cookie sessions, exports), .env and profiles/
# are excluded from both copying and --delete. Cookie sessions go to the server only through
# `python3 cli.py push-sessions <platform>`; the profile goes once by hand together with its
# templates: rsync -a profiles/ user@server:/opt/serm/profiles/
#
# Order: the code goes to <dir>.staging, an image is built from it and `cli.py doctor --offline`
# runs in it against the server's .env, data/ and profiles/. Only after that the code replaces
# <dir> and the container is recreated; a failed check leaves both as they were.
#
# Usage:
#   examples/deploy/rsync-deploy.sh --host user@server --key ~/.ssh/id_ed25519 --dir /opt/serm [--yes] [--skip-doctor]
#
# Without --yes the script shows the rsync dry run (including deletions) and asks to confirm.
set -euo pipefail

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

HOST=""
SSH_KEY=""
REMOTE_DIR=""
ASSUME_YES=0
RUN_DOCTOR=1

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="${2:-}"; shift 2 ;;
    --key) SSH_KEY="${2:-}"; shift 2 ;;
    --dir) REMOTE_DIR="${2:-}"; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    --skip-doctor) RUN_DOCTOR=0; shift ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 2 ;;
  esac
done

if [ -z "$HOST" ] || [ -z "$SSH_KEY" ] || [ -z "$REMOTE_DIR" ]; then
  echo "Error: --host, --key and --dir are required." >&2
  usage 2
fi
if [ ! -f "$SSH_KEY" ]; then
  echo "Error: SSH key not found: $SSH_KEY" >&2
  exit 2
fi
case "$SSH_KEY" in
  *"'"*) echo "Error: SSH key path must not contain a single quote: $SSH_KEY" >&2; exit 2 ;;
esac

# rsync ships the WORKING TREE as-is: run from the repo root, refuse uncommitted changes
cd "$(git rev-parse --show-toplevel)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Deploy refused: uncommitted changes in tracked files. Commit them first." >&2
  git status --short --untracked-files=no >&2
  exit 1
fi

REMOTE_DIR="${REMOTE_DIR%/}"
STAGING_DIR="$REMOTE_DIR.staging"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$SSH_KEY")

# rsync -e takes one string and splits it itself: quote every part, so a key path with spaces survives
RSH=""
for part in "${SSH[@]}"; do
  RSH+="'$part' "
done

# Anchored to the repo root: excluded paths are neither copied nor deleted on the server
EXCLUDES=(
  --exclude '/.git/'
  --exclude '/.venv/'
  --exclude '__pycache__/'
  --exclude '*.egg-info/'
  --exclude '/.pytest_cache/'
  --exclude '/data/'
  --exclude '/.env'
  --exclude '/.env.*'
  --exclude '/profiles/'
  --exclude '/.local/'
  --exclude '/.claude/'
  --exclude '/.agents/'
  --exclude '/horizon/'
)
RSYNC_ARGS=(-az --delete "${EXCLUDES[@]}" -e "$RSH")

# Runs the script from stdin on the server: remote_script <args...> <<'EOF' ... EOF
remote_script() {
  "${SSH[@]}" "$HOST" "bash -s -- $(printf '%q ' "$@")"
}

echo "Deploying to $HOST:$REMOTE_DIR"

# The server must already have .env and a profile: a missing ./.env would be bind-mounted
# by docker as an empty directory, and without a profile the process stops with ProfileError.
# An own (non-example) profile also needs its templates: without them the daemon refuses to start.
remote_script "$REMOTE_DIR" <<'REMOTE'
set -u
dir="$1"
missing=""
[ -f "$dir/.env" ] || missing="$missing $dir/.env"
ls "$dir"/profiles/*.yaml >/dev/null 2>&1 || missing="$missing $dir/profiles/*.yaml"
for profile in "$dir"/profiles/*.yaml; do
  [ -f "$profile" ] || continue
  name="$(basename "$profile" .yaml)"
  case "$name" in example-*) continue ;; esac
  # an explicit templates_dir is checked by doctor inside the container
  explicit="$(sed -n 's/^templates_dir:[[:space:]]*//p' "$profile" | sed 's/[[:space:]]*#.*$//')"
  case "$explicit" in ""|null|"~") ;; *) continue ;; esac
  # an empty dir is not enough: at least one template file (*.j2 / *.yaml), as core.profile.missing_own_templates
  found="$(find "$dir/profiles/$name/templates" -type f \( -name '*.j2' -o -name '*.yaml' \) -print -quit 2>/dev/null)"
  [ -n "$found" ] || missing="$missing $dir/profiles/$name/templates/"
done
if [ -n "$missing" ]; then
  echo "Deploy refused: missing on the server:$missing" >&2
  echo "Copy them once by hand (rsync -a profiles/ <host>:$dir/profiles/), then run the deploy again." >&2
  exit 1
fi
REMOTE

echo "Dry run (lines starting with 'deleting' will be removed on the server):"
rsync "${RSYNC_ARGS[@]}" --dry-run --itemize-changes ./ "$HOST:$REMOTE_DIR/"

if [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "Apply these changes? [y/N] " answer
  case "$answer" in
    y|Y|yes) ;;
    *) echo "Cancelled."; exit 1 ;;
  esac
fi

rsync "${RSYNC_ARGS[@]}" ./ "$HOST:$STAGING_DIR/"

# Build from staging and check it in a throwaway container with the server's state mounted as compose
# does (the .env file is read by the app itself: `docker run --env-file` would keep quotes in values);
# the working directory and the running container are replaced only if the check passes
remote_script "$REMOTE_DIR" "$STAGING_DIR" "$RUN_DOCTOR" "${EXCLUDES[@]}" <<'REMOTE'
set -euo pipefail
dir="$1"; staging="$2"; run_doctor="$3"
shift 3
if [ "$run_doctor" = 1 ]; then
  docker build -t serm-engine:staging "$staging"
  if ! docker run --rm -e ADMIN_HOST=0.0.0.0 -e ADMIN_PORT=8180 \
      -v "$dir/data:/app/data" -v "$dir/.env:/app/.env" -v "$dir/profiles:/app/profiles:ro" \
      serm-engine:staging python3 cli.py doctor --offline; then
    echo "Deploy stopped: doctor failed on the new code." >&2
    echo "$dir and the running container are unchanged; the new code is in $staging." >&2
    exit 3
  fi
fi
rsync -a --delete "$@" "$staging/" "$dir/"
cd "$dir"
docker compose build
docker compose up -d --remove-orphans
docker compose ps
REMOTE

echo "Deployment completed."
