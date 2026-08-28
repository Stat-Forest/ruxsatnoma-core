# Ruxsatnoma-urmon — backend

FastAPI-монолит системы электронных разрешений на пользование лесным фондом.
Архитектура и решения — в репозитории документации (`../docs`): `design/01..03`, `decisions.md`.

## Запуск с нуля

Требуется: Docker, [uv](https://docs.astral.sh/uv/), Python 3.14 (поставит uv).

```bash
uv sync                        # зависимости
cp .env.example .env           # конфиг (дефолты рабочие для локалки)
docker compose up -d           # PostgreSQL 16 + PostGIS, MinIO
uv run alembic upgrade head    # миграции
uv run python -m app.bootstrap --login admin --full-name "Admin"   # первый sys_admin (печатает одноразовый пароль и TOTP URI)
uv run uvicorn app.main:create_app --factory --reload   # API на :8000
```

Проверка: `curl localhost:8000/health/ready` → `{"status":"ok","postgis":"3.4..."}`.

## Applicant login (dev)

Mock adapters are the default (`ONEID_MODE`/`EIMZO_MODE`/`SMS_MODE=mock`). OneID's `code` and E-IMZO's `signed_challenge` are just base64url-JSON payloads — build one with `encode_mock_code`/`encode_mock_signed_challenge` from `app/modules/auth/adapters/{oneid,eimzo}.py` and pass it to `GET /auth/oneid/callback?code=` / `POST /auth/eimzo/login`. `POST /auth/otp/request` doesn't send anything either — the mock sender logs the code (`otp.mock_send`), so check the app log/console for it during local testing.

## Files API (dev)

MinIO's bucket is created automatically at startup (`ensure_bucket()` in the app lifespan) — nothing to set up by hand, even on a fresh `docker compose up -d` volume.

Smoke test `POST /api/v1/files` (multipart) against a session cookie jar `cj` (obtained via one of the login flows above):

```bash
curl -c cj -b cj -F "file=@/path/to/doc.pdf;type=application/pdf" localhost:8000/api/v1/files
```

`GET /api/v1/files/{id}` (same cookie jar) returns the bytes. Allowed types: PDF, PNG, JPEG, WEBP — the declared content type must match the file's magic bytes; size is capped by the `max_upload_mb` setting (default 20 MB).

## Тесты и качество

```bash
docker compose up -d   # тестам нужна БД (ruxsatnoma_test создаётся сама)
uv run pytest -v
uv run ruff check . && uv run ruff format --check .
uv run pre-commit install   # один раз, хуки на коммит
```

## Миграции

```bash
uv run alembic revision --autogenerate -m "…"
uv run alembic upgrade head
```

⚠️ `docker/initdb/01-test-db.sql` (создание тестовой БД `ruxsatnoma_test`) отрабатывает
только при первой инициализации пустого `pg-data` — Postgres запускает `initdb.d`-скрипты
один раз, при создании тома. Если `pg-data` существовал ещё до появления этого скрипта,
тестовая БД сама не появится — пересоздайте том:

```bash
docker compose down -v && rm -rf pg-data
docker compose up -d
```

## Структура

`app/core` — обвязка (конфиг, БД, ошибки ERR-*, логи, healthcheck), без бизнес-логики.
`app/modules/<имя>` — доменные модули (этап 3), слои router → service → repo → models.
Полная карта — `../docs/design/01-struktura-monolita.md`.

## Deployment notes

- Run uvicorn behind the reverse proxy with `--proxy-headers` so the client IP reaches the audit trail instead of the proxy's address. Without it behind a TLS-terminating proxy, uvicorn sees the proxy's own plain-HTTP connection, so `request.base_url` renders `http://…` while the browser's `Origin` header says `https://…` — the mismatch makes `_origin_allowed`'s same-origin fallback reject every same-origin mutation (`ERR-AUTH-006` on every `POST`/`PATCH`/`PUT`/`DELETE`), not just the audit IP.
- The adminka is served from its own origin (decision: stage 3.3a ruling 3). Set `CORS_ORIGINS` to the exact frontend origins (JSON list) — with cookie credentials a wildcard is not allowed. In `prod` this also switches the session and CSRF cookies to `SameSite=None; Secure`, so the API must be served over https.
- Reference data: `uv run python -m app.seed districts <file.json>` and `... organizations <file.json>` (idempotent upsert by `code`).
