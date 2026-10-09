#!/usr/bin/env bash
#
# Production deploy for the HRMS. Runs on the EC2 server.
#
# Installed as /home/ubuntu/bin/hrms-deploy and triggered by the GitHub
# Actions workflow "Test and deploy" (.github/workflows/deploy.yml) through an
# SSH key that may only run this script (authorized_keys command=...). The
# request arrives in SSH_ORIGINAL_COMMAND, for example:
#
#     deploy sha=<40-character commit> backup=yes migrate=none run=123 actor=name
#
# It can also be run by hand on the server with the same words as arguments.
#
# Steps: refuse unless the working tree is clean and origin/1.0 is exactly the
# tested commit and a fast-forward; back up the database (S3); pull; migrate
# only the named apps; check; restart gunicorn; check the log and the login
# page. If a step fails after the pull and no migration ran, the code goes
# back to the previous commit and gunicorn is restarted.
#
# Logs (server only; the GitHub log is public, so it gets status lines only):
#   ~/logs/deploy.log          every line of every run, timestamped
#   ~/logs/deploy-history.log  one line per run: when, who, what, result
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
LOG_DIR=/home/ubuntu/logs
LOG="$LOG_DIR/deploy.log"
HISTORY="$LOG_DIR/deploy-history.log"
SOCKET=/home/ubuntu/gunicorn.sock
KEEP_LOG_LINES=5000

mkdir -p "$LOG_DIR" "$BACKUP_DIR"
# Keep the full log to a sensible size (trimmed before this run writes to it).
if [[ -f "$LOG" ]]; then
  tail -n "$KEEP_LOG_LINES" "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
# Every line gets a UTC time and goes to the screen (or GitHub) and the log.
exec > >(while IFS= read -r line; do
  printf '%s %s\n' "$(date -u +%H:%M:%S)" "$line"
done | tee -a "$LOG") 2>&1

started="$(date +%s)"
result="FAILED"
detail=""
current_step="checking the request"
sha="" backup=yes migrate=none run="" actor=""
previous="" target="" backup_file=""

step() { echo "==> $*"; }

finish() {
  local status=$? seconds
  seconds=$(( $(date +%s) - started ))
  if [[ "$result" == FAILED && -z "$detail" ]]; then
    detail="failed during: $current_step; code not changed"
  fi
  echo "RESULT: $result${detail:+ - $detail} (${seconds}s)"
  printf '%s | %-12s | %s -> %s | backup=%s | migrate=%s | actor=%s | run=%s | %ss | %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$result" "${previous:0:8}" "${target:0:8}" \
    "${backup_file:+$(basename "$backup_file")}" "$migrate" "${actor:-manual}" \
    "${run:--}" "$seconds" "$detail" >> "$HISTORY"
  exit "$status"
}
trap finish EXIT

refuse() {
  result="REFUSED"
  detail="$*"
  echo "REFUSED: $*"
  exit 2
}

echo "=== deploy request $(date -u +%Y-%m-%d)"

# --- Parse and validate the request (never echo it back) -------------------
step "Checking the request"
request="${SSH_ORIGINAL_COMMAND:-$*}"
read -r -a words <<< "$request"
[[ "${words[0]:-}" == deploy ]] || refuse "unknown request"
for word in "${words[@]:1}"; do
  case "$word" in
    sha=*) sha="${word#sha=}" ;;
    backup=yes | backup=no) backup="${word#backup=}" ;;
    migrate=*) migrate="${word#migrate=}" ;;
    run=*) run="${word#run=}" ;;
    actor=*) actor="${word#actor=}" ;;
    *) refuse "unknown option" ;;
  esac
done
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || refuse "sha must be a full commit hash"
[[ "$migrate" =~ ^(none|[a-z_]+(,[a-z_]+)*)$ ]] \
  || { migrate="invalid"; refuse "migrate must be none or app labels, comma-separated"; }
[[ -z "$run" || "$run" =~ ^[0-9]{1,20}$ ]] || { run=""; refuse "bad run id"; }
[[ -z "$actor" || "$actor" =~ ^[A-Za-z0-9-]{1,39}$ ]] || { actor=""; refuse "bad actor"; }
echo "requested by ${actor:-manual} (GitHub run ${run:-none}): backup=$backup migrate=$migrate"

# --- One deploy at a time, never during the accrual run ---------------------
exec 9>"$LOCK"
flock -n 9 || refuse "the accrual job or another deploy is running; try again later"

current_step="checking the server's code"
step "Checking the server's code"
cd "$APP_DIR"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  git status --short --untracked-files=no
  refuse "the server has local changes to tracked files"
fi
previous="$(git rev-parse HEAD)"
git fetch --quiet origin "$BRANCH"
target="$(git rev-parse "origin/$BRANCH")"
echo "deployed now: $(git log --oneline -1 "$previous")"
echo "origin/$BRANCH: $(git log --oneline -1 "$target")"
[[ "$target" == "$sha" ]] \
  || refuse "origin/$BRANCH is ${target:0:8}, not the tested ${sha:0:8} (merged since the tests ran?)"
git merge-base --is-ancestor "$previous" "$target" \
  || refuse "${target:0:8} is not a fast-forward from ${previous:0:8}"
if [[ "$previous" == "$target" && "$migrate" == none ]]; then
  result="UP TO DATE"
  detail="already at ${target:0:8}"
  exit 0
fi
echo "commits to deploy:"
git log --oneline "$previous..$target" | sed 's/^/  /'

# --- Backup -----------------------------------------------------------------
if [[ "$backup" == yes ]]; then
  current_step="backup"
  step "Backing up the database"
  backup_file="$BACKUP_DIR/pre-deploy-$(date -u +%Y%m%d-%H%M%S).backup"
  sudo -u postgres pg_dump -Fc "$DATABASE" > "$backup_file"
  pg_restore -f /dev/null "$backup_file"
  aws s3 cp --only-show-errors --sse AES256 "$backup_file" "$S3_PREFIX"
  echo "backup $(basename "$backup_file"): $(stat -c %s "$backup_file") bytes, read back OK, copied to S3"
fi

# --- From here a failure puts the previous code back -------------------------
migrated=no
rollback() {
  local status=$?
  trap - ERR
  if [[ "$migrated" == yes ]]; then
    result="FAILED"
    detail="during: $current_step; migrations started, so NOT rolled back"
    echo "Previous commit: $previous. Backup: ${backup_file:-none}."
    exit "$status"
  fi
  result="ROLLED BACK"
  detail="failed during: $current_step; back on ${previous:0:8}"
  step "Rolling back"
  git reset --hard --quiet "$previous"
  sudo systemctl restart gunicorn
  echo "gunicorn: $(systemctl is-active gunicorn || true)"
  exit "$status"
}
trap rollback ERR

current_step="pull"
step "Updating the code"
git merge --ff-only --quiet "$target"
echo "now at: $(git log --oneline -1)"

if [[ "$migrate" != none ]]; then
  current_step="migrations"
  step "Migrating: $migrate"
  IFS=, read -r -a apps <<< "$migrate"
  for app in "${apps[@]}"; do
    "$PYTHON" manage.py migrate "$app" --plan
  done
  migrated=yes
  for app in "${apps[@]}"; do
    "$PYTHON" manage.py migrate "$app" --noinput
  done
fi

current_step="system check"
step "Running the system check"
"$PYTHON" manage.py check

current_step="restart"
step "Restarting gunicorn"
sudo systemctl restart gunicorn
since="$(date '+%Y-%m-%d %H:%M:%S')"
sleep 20
systemctl is-active --quiet gunicorn
echo "gunicorn: active"

current_step="health check"
step "Health check"
# Errors from the new processes only (the old ones log theirs while stopping).
# Counted, never printed: tracebacks can contain personal data, and the
# GitHub log is public. Details: sudo journalctl -u gunicorn --since "$since".
errors="$(sudo journalctl -u gunicorn --since "$since" --no-pager \
  | grep -cE "Traceback|Error submitting job" || true)"
echo "errors logged by gunicorn since the restart: $errors"
if [[ "$errors" != 0 ]]; then
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
result="DEPLOYED"
detail="${previous:0:8} -> ${target:0:8}"
