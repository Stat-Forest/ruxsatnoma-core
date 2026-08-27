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

- Run uvicorn behind the reverse proxy with `--proxy-headers` so the client IP reaches the audit trail instead of the proxy's address.
- The adminka is served from its own origin (decision: stage 3.3a ruling 3). Set `CORS_ORIGINS` to the exact frontend origins (JSON list) — with cookie credentials a wildcard is not allowed. In `prod` this also switches the session and CSRF cookies to `SameSite=None; Secure`, so the API must be served over https.
- Reference data: `uv run python -m app.seed districts <file.json>` and `... organizations <file.json>` (idempotent upsert by `code`).
