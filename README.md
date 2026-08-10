# ruxsatnoma-core

Сервис `core` государственной системы «Ruxsatnoma» — выдача электронных разрешений на пользование землями лесного фонда Республики Узбекистан.

Модульный монолит: один процесс, одиннадцать доменных модулей и три общих. Каждый модуль владеет ровно одной схемой БД и общается с соседями только через публичный API.

**Сейчас это скелет.** Моделей, миграций и бизнес-логики в нём нет — их пишут владельцы модулей.

Документация проекта — в соседнем репозитории `../ruxsatnoma-docs`, начинать с `plans/00-roadmap.md`. Границы модулей — `architecture/modules.md`, инженерные стандарты — `architecture/engineering-standards.md`.

## Стек

Python 3.13, FastAPI, SQLAlchemy 2.0 + GeoAlchemy2, Alembic, Pydantic v2, PostgreSQL 18 + PostGIS 3.6, Redis, RabbitMQ, MinIO.

## Как поднять окружение

Нужны Docker и Python 3.13.

```bash
cp .env.example .env          # заполнить пароли
docker compose up -d          # PostGIS, Redis, RabbitMQ, MinIO
```

Порты в `docker-compose.yml` сдвинуты, чтобы не конфликтовать с уже запущенными на машине: PostgreSQL — 5442, Redis — 6389, RabbitMQ — 5682, MinIO — 9010.

## Установка и запуск

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install

uvicorn core.main:app --reload
curl -i localhost:8000/health        # в ответе будет заголовок Correlation-Id
```

## Миграции

Первую миграцию создаёт владелец моделей — после того, как подключит `target_metadata` в `alembic/env.py`.

```bash
alembic revision -m "add geo contour tables"   # имя только на английском
alembic upgrade head
alembic downgrade -1                            # каждая миграция обязана откатываться
```

Правила: одна миграция — одно изменение, структура и данные — разными миграциями, файл называется по дате и по-английски.

## Тесты

```bash
pytest                       # всё
pytest -m integration        # только те, что поднимают базу
```

Интеграционные тесты поднимают настоящий PostgreSQL с PostGIS через testcontainers и накатывают `alembic upgrade head`. **SQLite не используется нигде** — он не умеет ни PostGIS, ни exclusion constraints, ни RLS, то есть ровно то, на чём держатся инварианты системы.

## Проверки

Шаги 6 и 7 ходят в базу: перед запуском нужны поднятая инфраструктура (`make up`) и заполненный `.env`.

```bash
make check
```

Те же девять шагов, что и в CI (`.github/workflows/ci.yml`), в том же порядке. Ни один не пропускается. Полный список целей — `Makefile`.

## Куда класть код

| Пакет | Схема БД | Что внутри | Владелец |
|---|---|---|---|
| `core/modules/iam` | `iam` | пользователи, роли, организации, заявители | Технический лидер |
| `core/modules/geo` | `geo` | контуры, слои, занятость, тайлы | Backend 2 |
| `core/modules/rules` | `rules` | нормы, тарифы, расчёт | Backend 2 |
| `core/modules/application` | `app` | заявка, workflow, SLA | Backend 1 |
| `core/modules/permit` | `permit` | разрешение, шаблоны, QR | Backend 1 |
| `core/modules/signature` | `permit` | хранение подписей | Технический лидер |
| `core/modules/payment` | `pay` | инвойс, сверка, распределение, возврат | Backend 3 |
| `core/modules/inspection` | `insp` | акты, нарушения, медиа | Backend 1 |
| `core/modules/reporting` | `rep` | отчёты, dashboard | Backend 2 |
| `core/modules/archive` | `arch` | архив, лесной билет | Backend 1 |
| `core/modules/prosecutor` | — | витрина и детекторы риск-индикаторов | Технический лидер |
| `core/shared/{audit,outbox,classifiers}` | `nsi` и служебные | аудит, исходящие события, справочники | Технический лидер |
| `core/api` | — | контроллеры, middleware, коды ошибок | Технический лидер |

Внутри доменного пакета: `__init__.py` — публичный API, `models.py` — таблицы, `service.py` — бизнес-логика, `repository.py` — запросы, `state_machine.py` — переходы статусов, `schemas.py` — типы запросов и ответов, `router.py` — маршруты. Роутер подключается в `core/main.py`.

## Правила проекта

- **В коде только английский** — имена, комментарии, docstring-и, сообщения коммитов, имена миграций и ключи логов. Тексты для пользователя живут в `locales/`. Проверяется скриптом `scripts/check_english_only.py`.
- **Модуль работает только со своей схемой.** Нужны данные соседа — вызываешь его публичный API, а не его таблицу и не его `repository.py`. Проверяется `lint-imports` по контрактам из `setup.cfg`.
- **Деньги — только `Decimal` и `numeric(18,2)`.** Ни `float`, ни `int`: старая система теряла копейки на округлении.
- **Наружу из модуля не выставляются объекты SQLAlchemy** — иначе шов для выделения `geo` и `payment` в отдельные сервисы зарастает в первый же месяц.
- **Никаких синхронных походов во внешние системы** из цикла запроса — для этого есть сервис `integration` и очереди.
- **Имена сущностей берутся из единого языка**, а не изобретаются: `Application`, `Permit`, `Contour`, `Norm`.
