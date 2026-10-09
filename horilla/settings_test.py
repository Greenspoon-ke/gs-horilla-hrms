"""
Settings for running the test suite on CI (GitHub Actions) or any fresh
checkout: DJANGO_SETTINGS_MODULE=horilla.settings_test.

Configuration still comes from environment variables, as in settings.py.
"""

import sentry_sdk

from horilla.settings import *  # noqa: F401,F403

# settings.py starts Sentry with a hard-coded DSN; tests must never report to
# the production project. Re-initialising without a DSN turns it off.
sentry_sdk.init(dsn=None)


class _DisableMigrations:
    def __contains__(self, app_label):
        return True

    def __getitem__(self, app_label):
        return None


# Migrations are git-ignored in this repository, so a fresh checkout cannot
# build the test database from them. Create the tables from the models instead.
MIGRATION_MODULES = _DisableMigrations()
