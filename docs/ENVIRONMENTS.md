# Environments and release workflow

Two deployments of the same app, each with its own database:

| | **Production** | **Dev** |
| --- | --- | --- |
| Git branch | `master` | `dev` |
| URL | <https://nbo-platform-production.up.railway.app> | <https://nbo-platform-dev.up.railway.app> |
| Railway | project `fulfilling-cat`, environment `production` | same project, environment `dev` |
| Database | its own Postgres service | its own Postgres service (separate data and password) |
| Marker | none | orange **DEV** banner on every page, and `noindex` |
| Deploys when | a commit lands on `master` | a commit lands on `dev` |

Nothing is shared between them. Test accounts, questions and issues created in
dev never appear in production, and the other way round.

## How a change reaches production

```
feature branch  ──►  dev  ──(tests + manual check on the dev site)──►  master
                      │                                                  │
                 Railway dev                                      Railway production
```

1. **Work on a branch** (or commit straight to `dev` for small changes).
2. **Push to `dev`.** GitHub runs the tests, and Railway builds and deploys the
   dev environment. Database migrations run automatically on start-up.
3. **Check it on the dev site** on a phone and a desktop. Log in, try the
   change, and try the things next to it.
4. **Promote to production** only when it works: merge `dev` into `master`
   (a pull request is best, so there is a record). Railway deploys production.
5. If production misbehaves, revert the merge on `master`, or redeploy the
   previous deployment from the Railway dashboard.

Rules of thumb:

- `master` only ever receives merges from `dev`. Never push new work straight to it.
- Keep `dev` ahead of `master`, never behind. After a hotfix on `master`, merge
  `master` back into `dev` straight away.
- A pushed database migration cannot be un-run on production. Look at the
  migration on dev first, and check that there is only one migration head
  (`alembic heads`).

## Automatic tests

`.github/workflows/tests.yml` runs `pytest` on every push and pull request to
`dev` and `master`. A red check means do not promote.

## Health check

`/healthz` answers `ok` only when the database responds to a query; otherwise
it returns `503 database unavailable`. Railway calls it while a new deployment
starts, and keeps the previous deployment running if the new one fails it. It
is checked when a deployment starts, not continuously, so use Railway's
notifications to catch a database that stops later.

## Configuration per environment

Both environments use the same variables. These differ:

| Variable | Production | Dev |
| --- | --- | --- |
| `NBO_SECRET_KEY` | its own random value | a different random value |
| `NBO_ENV_LABEL` | not set | `dev` (shows the banner and sets `noindex`) |
| `NBO_DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | `${{Postgres.DATABASE_URL}}` (resolves to the dev database) |

The first admin of an empty database can be created at start-up with
`NBO_BOOTSTRAP_ADMIN_EMAIL`, `NBO_BOOTSTRAP_ADMIN_PASSWORD` and optionally
`NBO_BOOTSTRAP_ADMIN_USERNAME`, `NBO_BOOTSTRAP_ADMIN_FIRST_NAME`,
`NBO_BOOTSTRAP_ADMIN_LAST_NAME`. Remove the password variable once the account
exists; it is only read when that email does not exist yet.

## Resetting the dev database

Dev data is disposable. To start clean, delete the dev `Postgres` service's
volume data (or the service) in the Railway dashboard under environment `dev`
and redeploy: migrations rebuild the schema and the bootstrap admin is created
again. Never do this in `production`.

## One-time GitHub setting (recommended)

Protect `master` (Settings → Branches → Add rule) so that it needs a pull
request and passing tests. This makes the flow above enforced instead of
a habit.
