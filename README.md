# pragent

A pull request review agent built with Django. Users connect GitHub once through the pragent GitHub App,
choose repositories, and pragent reviews new pull requests on them.

## How a review happens

1. GitHub sends a `pull_request` webhook to `/github/webhook/`. pragent checks the signature, stores the delivery
   (its unique ID deduplicates redeliveries) and queues `process_pr_event` once the transaction commits. It answers 202.
2. A Celery worker creates a `ReviewRun` for the commit, fetches the changed files, applies the size limits and
   skip patterns, checks the PR's head hasn't moved, and posts a review on the PR.
3. Every step is recorded as an append-only `AgentEvent`, which you can see on the run in Django admin.

## Local development

You need Python 3.14, PostgreSQL with the [pgvector](https://github.com/pgvector/pgvector) extension, and Redis.

```bash
# Python environment
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# PostgreSQL + pgvector (Ubuntu, PostgreSQL 18)
sudo apt install postgresql-18-pgvector
createdb pragent                       # as a Postgres role named after your OS user
psql -d pragent -c 'CREATE EXTENSION IF NOT EXISTS vector;'

# Redis (queue and token cache)
sudo apt install redis-server          # or: docker run -d -p 6379:6379 redis:7

python manage.py migrate
```

Settings come from environment variables, or a gitignored `.env` file in the project root:

| Variable | Default | Purpose |
|---|---|---|
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | `pragent`, local socket | PostgreSQL connection |
| `REDIS_URL` | `redis://localhost:6379/0` | Token cache, and the Celery broker unless `CELERY_BROKER_URL` is set |
| `CELERY_BROKER_URL` | `REDIS_URL` | Celery broker |
| `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_WEBHOOK_SECRET` | | The pragent GitHub App |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | | "Sign in with GitHub" (can be the same App) |
| `REVIEW_MAX_FILES`, `REVIEW_MAX_CHANGED_LINES` | `50`, `2000` | Size limits per review |
| `EMBEDDING_DIMENSIONS` | `1536` | Size of code embeddings (changing it needs a migration) |
| `PUBLIC_BASE_URL` | | Production address, used by `manage.py github_webhook --set` |

Run these in separate terminals:

```bash
python manage.py runserver
celery -A pragent worker -l info
npx smee-client --url https://smee.io/<channel> --target http://127.0.0.1:8000/github/webhook/
```

GitHub can't reach `127.0.0.1`, so locally its webhooks go through a [smee.io](https://smee.io) relay.
In production, point the App's webhook straight at the deployment instead:

```bash
PUBLIC_BASE_URL=https://pragent.example.com python manage.py github_webhook --set
```

## Tests

```bash
python manage.py test
```

Tests need PostgreSQL (with pgvector) but not Redis or network access: Celery runs tasks eagerly, the cache is
in memory, and every GitHub call is mocked.
