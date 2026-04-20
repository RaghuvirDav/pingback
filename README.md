# Pingback

A minimal, self-hostable uptime monitor. FastAPI + SQLite + nginx on a single t2.micro.

## Quick start — Docker Compose

```bash
git clone https://github.com/RaghuvirDav/pingback.git
cd pingback
cp .env.example .env
# set ENCRYPTION_KEY (see the comment inside .env); leave RESEND_API_KEY blank for a no-email run
docker compose up -d
open http://localhost:8000
```

That's the whole local self-host path. The SQLite DB lives in the `pingback-data` Docker volume.

## Production deploy (AWS free tier)

See **[README-SELFHOST.md](README-SELFHOST.md)** for the end-to-end AWS walkthrough: launch EC2 → clone → `setup-ec2.sh` → DNS → `enable-https.sh` → healthy HTTPS deployment.

## Local development (no Docker)

```bash
cp .env.example .env
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn pingback.main:app --reload
```

## Repo layout

- `pingback/` — FastAPI app (routes, scheduler, config, templates).
- `deploy/` — EC2 bootstrap, nginx template, systemd unit, HTTPS provisioning, CloudWatch scripts.
- `docs/` — [`OPERATIONS.md`](docs/OPERATIONS.md), [`QA.md`](docs/QA.md), [`PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md).
- `tests/` — pytest suite (`pytest` from repo root).

## Tests

```bash
pip install -r requirements.txt
SENTRY_DSN="" pytest
```

## License

MIT — see [LICENSE](LICENSE).
