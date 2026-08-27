# Ruxsatnoma-urmon — backend

FastAPI-монолит системы электронных разрешений на пользование лесным фондом.
Архитектура и решения — в репозитории документации (`../docs`): `design/01..03`, `decisions.md`.

## Запуск с нуля

Требуется: Docker, [uv](https://docs.astral.sh/uv/), Python 3.12 (поставит uv).

```bash
uv sync                        # зависимости
cp .env.example .env           # конфиг (дефолты рабочие для локалки)
docker compose up -d           # PostgreSQL 16 + PostGIS, MinIO
uv run alembic upgrade head    # миграции
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

## Структура

`app/core` — обвязка (конфиг, БД, ошибки ERR-*, логи, healthcheck), без бизнес-логики.
`app/modules/<имя>` — доменные модули (этап 3), слои router → service → repo → models.
Полная карта — `../docs/design/01-struktura-monolita.md`.
