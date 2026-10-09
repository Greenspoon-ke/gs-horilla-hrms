# Deploying with GitHub Actions

The **Test and deploy** workflow (`.github/workflows/deploy.yml`) is started by hand: **Actions → Test and deploy → Run workflow**.

- **Use workflow from:** pick a branch.
  - Any branch: the leave tests run.
  - `1.0`, with **deploy** ticked: the tested commit is also deployed to production.
- **backup:** take a database backup to S3 first. The default is yes.
- **migrate_apps:** the apps to migrate, comma-separated, for example `leave`. Leave it empty when no migration is needed.
  - Migrations are git-ignored. Any migration being deployed must be committed with `git add -f`.
  - Never run `makemigrations` on the server.

The server side is `deploy/hrms-deploy.sh`, installed as `/home/ubuntu/bin/hrms-deploy`. GitHub's SSH key may only run that script.

## What the server script does
1. **Refuses to start** in any of these cases:
   - the server has local changes;
   - `origin/1.0` is not the exact commit that was tested;
   - the update is not a fast-forward;
   - the daily accrual job is running (the two share a lock).
2. **Backs up** the database, checks the backup reads back, and copies it to `s3://greenspoon-hrms-backups/pre-deploy/`.
3. **Pulls** the code, **migrates** only the named apps, and runs `manage.py check`.
4. **Restarts** gunicorn, then checks two things: no new errors in its log, and the login page answers.
5. **On failure after the pull,** if no migration has run, it puts the previous commit back and restarts. After a migration it stops and reports instead, so a person decides.

## Where to find the logs

**On GitHub (public, because the repo is public):**
- **Run page summary:**
  - **Tests:** branch, commit, who started the run, and the result, with any failing tests listed.
  - **Deploy:** the commit, backup, migrations and the server's result.
- **Step log:**
  - **Tests:** every test by name, with its result.
  - **Deploy:** one collapsible section per server step, each with timings, and the `RESULT:` line underneath.
- **Error banner:** a deploy that was refused, rolled back or failed shows a red banner at the top of the run.
- **Never in the GitHub log:** error tracebacks from the server. They can contain employee data, so the GitHub log only counts them.

**On the server (private):**

| Log | Contents | Command |
|---|---|---|
| `~/logs/deploy-history.log` | One line per deploy: time, result, from → to commit, backup file, migrations, who, GitHub run number, duration, reason | `cat ~/logs/deploy-history.log` |
| `~/logs/deploy.log` | Every line of every deploy, timestamped (UTC); the last 5000 lines are kept | `tail -n 80 ~/logs/deploy.log` |
| gunicorn | Application errors, including those counted by the health check | `sudo journalctl -u gunicorn --since "1 hour ago"` |
| `~/logs/accrue_leave.log` | The daily 06:00 (Nairobi) Annual Leave accrual | `tail -n 20 ~/logs/accrue_leave.log` |

**Possible results:**

| Result | Meaning |
|---|---|
| `DEPLOYED` | The new code is live and the health check passed. |
| `UP TO DATE` | Nothing new to deploy. |
| `REFUSED` | Nothing was changed. The reason is given: local changes, a commit merged since the tests ran, or the accrual job running. |
| `ROLLED BACK` | A step failed after the pull. The previous code is back and gunicorn has restarted. |
| `FAILED` | Failed before the pull (nothing changed), or during a migration. After a migration nothing is undone automatically: use the backup named in the log and decide by hand.

## One-time setup
1. **Create a deploy key on your Mac.** Never use the EC2 `.pem` key for this.

   ```
   ssh-keygen -t ed25519 -N "" -C github-actions-deploy -f ~/hrms-local/github_deploy_key
   ```

2. **Record the server's host key**, so GitHub can detect an impostor server. Compare its fingerprint with what the server prints for `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`.

   ```
   ssh-keyscan -t ed25519 <server-ip> > ~/hrms-local/deploy_known_hosts
   ```

3. **On the server,** install `deploy/hrms-deploy.sh` as `~/bin/hrms-deploy` with mode 700.
   - Add the public key to `~/.ssh/authorized_keys`, restricted so it can only run the script:

     ```
     command="/home/ubuntu/bin/hrms-deploy",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA... github-actions-deploy
     ```

   - After the script changes in the repo, copy it to the server again. That is deliberate: a change to what runs on production is a separate step.

4. **AWS:** GitHub's runners connect from changing addresses, so the EC2 security group must allow SSH (port 22) from them.

5. **GitHub, Settings → Environments → New environment `production`:**
   - **Deployment branches:** allow `1.0` only.
   - Optional **required reviewers:** a deploy then waits for a click.
   - **Environment secrets:**
     - `DEPLOY_SSH_KEY`: the private key file contents;
     - `DEPLOY_KNOWN_HOSTS`: the line from step 2;
     - `DEPLOY_HOST`: the server's address.

## Running the tests locally the same way

```
DJANGO_SETTINGS_MODULE=horilla.settings_test python manage.py migrate --run-syncdb
DJANGO_SETTINGS_MODULE=horilla.settings_test python manage.py test leave --keepdb
```

`horilla/settings_test.py` builds the tables from the models instead of migrations, and switches Sentry off.
