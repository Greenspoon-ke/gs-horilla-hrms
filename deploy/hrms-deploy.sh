#!/usr/bin/env bash
#
# Production deploy for the HRMS. Runs on the EC2 server.
#
# Installed as /home/ubuntu/bin/hrms-deploy and triggered by the GitHub
# Actions workflow "Test and deploy" (.github/workflows/deploy.yml) through an
# SSH key that may only run this script (authorized_keys command=...). The
# request arrives in SSH_ORIGINAL_COMMAND, for example:
#
#     deploy sha=<40-character commit> backup=yes migrate=none
#
# It can also be run by hand on the server with the same words as arguments.
#
# Steps: refuse unless the working tree is clean and origin/1.0 is exactly the
# tested commit and a fast-forward; back up the database (S3); pull; migrate
# only the named apps; check; restart gunicorn; check the log and the login
# page. If a step fails after the pull and no migration ran, the code goes
# back to the previous commit and gunicorn is restarted.
#
# Shares its lock with the daily accrual cron, so the two never overlap.

set -Eeuo pipefail
umask 077

APP_DIR=/home/ubuntu/apps/horilla-hrms
PYTHON="$APP_DIR/horillaenv/bin/python"
BRANCH=1.0
DATABASE=horilla_main
BACKUP_DIR=/home/ubuntu/db-backups
S3_PREFIX=s3://greenspoon-hrms-backups/pre-deploy/
LOCK=/home/ubuntu/hrms-local/accrue_leave.lock
LOG=/home/ubuntu/logs/deploy.log
SOCKET=/home/ubuntu/gunicorn.sock

mkdir -p "$(dirname "$LOG")" "$BACKUP_DIR"
exec > >(tee -a "$LOG") 2>&1
echo "=== deploy request $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# --- Parse and validate the request (never echo it back) -------------------
request="${SSH_ORIGINAL_COMMAND:-$*}"
read -r -a words <<< "$request"
if [[ "${words[0]:-}" != deploy ]]; then
  echo "REFUSED: unknown request"
  exit 2
fi
sha="" backup=yes migrate=none
for word in "${words[@]:1}"; do
  case "$word" in
    sha=*) sha="${word#sha=}" ;;
    backup=yes | backup=no) backup="${word#backup=}" ;;
    migrate=*) migrate="${word#migrate=}" ;;
    *) echo "REFUSED: unknown option"; exit 2 ;;
  esac
done
if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "REFUSED: sha must be a full commit hash"
  exit 2
fi
if [[ ! "$migrate" =~ ^(none|[a-z_]+(,[a-z_]+)*)$ ]]; then
  echo "REFUSED: migrate must be none or app labels, comma-separated"
  exit 2
fi

# --- One deploy at a time, never during the accrual run ---------------------
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "REFUSED: the accrual job or another deploy is running; try again later"
  exit 2
fi

cd "$APP_DIR"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "REFUSED: the server has local changes to tracked files:"
  git status --short --untracked-files=no
  exit 2
fi
previous="$(git rev-parse HEAD)"
git fetch --quiet origin "$BRANCH"
target="$(git rev-parse "origin/$BRANCH")"
if [[ "$target" != "$sha" ]]; then
  echo "REFUSED: origin/$BRANCH is $target, not the tested commit $sha"
  echo "         (something was merged after the tests ran; run the workflow again)"
  exit 2
fi
if ! git merge-base --is-ancestor "$previous" "$target"; then
  echo "REFUSED: $target is not a fast-forward from the deployed $previous"
  exit 2
fi
if [[ "$previous" == "$target" && "$migrate" == none ]]; then
  echo "Nothing to deploy: already at $target"
  exit 0
fi
echo "deploying $previous -> $target (backup=$backup, migrate=$migrate)"

# --- Backup -----------------------------------------------------------------
backup_file=""
if [[ "$backup" == yes ]]; then
  backup_file="$BACKUP_DIR/pre-deploy-$(date -u +%Y%m%d-%H%M%S).backup"
  sudo -u postgres pg_dump -Fc "$DATABASE" > "$backup_file"
  pg_restore -f /dev/null "$backup_file"
  aws s3 cp --only-show-errors --sse AES256 "$backup_file" "$S3_PREFIX"
  echo "backup: $backup_file ($(stat -c %s "$backup_file") bytes), copied to $S3_PREFIX"
fi

# --- From here a failure puts the previous code back -------------------------
migrated=no
rollback() {
  local status=$?
  trap - ERR
  if [[ "$migrated" == yes ]]; then
    echo "FAILED after migrations started: NOT rolled back automatically."
    echo "Previous commit: $previous. Backup: ${backup_file:-none}."
    exit "$status"
  fi
  echo "FAILED: putting back $previous and restarting gunicorn"
  git reset --hard --quiet "$previous"
  sudo systemctl restart gunicorn
  systemctl is-active gunicorn || true
  exit "$status"
}
trap rollback ERR

git merge --ff-only --quiet "$target"
git log --oneline -1

if [[ "$migrate" != none ]]; then
  IFS=, read -r -a apps <<< "$migrate"
  for app in "${apps[@]}"; do
    "$PYTHON" manage.py migrate "$app" --plan
  done
  migrated=yes
  for app in "${apps[@]}"; do
    "$PYTHON" manage.py migrate "$app" --noinput
  done
fi

"$PYTHON" manage.py check

sudo systemctl restart gunicorn
since="$(date '+%Y-%m-%d %H:%M:%S')"
sleep 20
systemctl is-active --quiet gunicorn

# Errors from the new processes only (the old ones log theirs while stopping).
errors="$(sudo journalctl -u gunicorn --since "$since" --no-pager \
  | grep -cE "Traceback|Error submitting job" || true)"
if [[ "$errors" != 0 ]]; then
  echo "gunicorn logged $errors error line(s) after the restart"
  false
fi

host="$(grep -m1 '^ALLOWED_HOSTS=' .env | cut -d= -f2- | tr -d '"' | cut -d, -f1)"
[[ -z "$host" || "$host" == "*" ]] && host=localhost
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
  --unix-socket "$SOCKET" -H "Host: $host" -H "X-Forwarded-Proto: https" \
  http://localhost/login/ || true)"
echo "login page: HTTP $code"
if [[ ! "$code" =~ ^[23] ]]; then
  false
fi

trap - ERR
echo "DEPLOYED $target (was $previous)"
