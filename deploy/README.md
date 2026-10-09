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

Each run is logged to `/home/ubuntu/logs/deploy.log` on the server.

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
