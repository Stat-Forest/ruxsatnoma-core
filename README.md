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
- `EIMZO_MODE=mock` is the default and `app_env=prod` refuses to start with it (`app/config.py`'s mock-adapters guard, same check as `oneid_mode`/`sms_mode`/`email_mode`) — production needs the real E-IMZO adapter (stage 5.2), which itself needs an `e-imzo-server` instance (JRE 8 + `e-imzo-server.jar` + config + VPN key files, `design/04` §2.6) reachable only from inside Uzbekistan; nothing about it can be verified from CI or from outside the country.
- **WeasyPrint needs Pango, GLib and HarfBuzz as SYSTEM libraries** (stage 3.11a) — they are not Python packages and `uv sync` does not install them. Without them `import weasyprint` raises `OSError: cannot load library 'libgobject-2.0-0'` and **permits cannot be issued at all**; that is how a deploy discovers this, in production, on the first issuance.
  - Debian / Ubuntu (CI and the deploy image): `apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0`. `libharfbuzz-subset0` is nominally optional — WeasyPrint falls back to fontTools for subsetting — but the fallback embeds **different font bytes**, so omitting it makes one environment's PDFs disagree with another's for no visible reason.
  - macOS (development): `brew install pango` (pulls cairo, glib, harfbuzz, fontconfig, freetype; `gdk-pixbuf` is not needed by WeasyPrint ≥ 60). Homebrew's `/opt/homebrew/lib` is not on dyld's default search path, so `app/modules/permits/render.py` sets `DYLD_FALLBACK_LIBRARY_PATH` before importing weasyprint — `uv run pytest`, `uv run uvicorn` and `python -m app.workers` therefore need no magic command prefix. `setdefault`, so an operator's own value wins; no effect on Linux and none on the rendered bytes on any platform.
  - The permit is PDF/A-1b. Conformance is asserted structurally in tests (`%PDF`, an `/OutputIntent`, `pdfaid:part` in the XMP); a formal ISO check needs **veraPDF** (a Java tool) and belongs to the deploy checklist, not to CI.
- **The `/check` page must be served before the first production permit is issued** (stage 3.11a, ruling F-1). The QR printed on every permit encodes `{PUBLIC_BASE_URL}/check?qr=<token>` — the front-end page a citizen reads, not `/api/v1/public/permits/check`, which is the JSON that page calls (requisite 24 exists so a scan lands on something readable, not on a raw object). That page is stage 6's and does not exist yet, so the URL 404s today. **This costs nothing until the first production permit exists and is uncorrectable from that moment on**: a permit is printed once and the URL on it cannot be changed afterwards — `permits.doc_hash` is frozen over those bytes and all four ERI signatures are taken over them. So: serve `/check` (even as a stub that reads the JSON route), or do not issue.
- **`GET /api/v1/public/permits/check` is the first route the open internet reaches with no credentials** (stage 3.11a). `qr_check_log` deliberately records no IP address and no personal data, and `app/core/logging.py` drops that path's uvicorn access-log line for the same reason — that line would carry the visitor's IP **and** the printed QR token in plaintext, in a file no purge job covers. Two limits worth knowing: the filter reaches **uvicorn's logger and nothing further out**, so a deployment fronting the app with its own access logging (nginx, an ingress, a sidecar) must exclude the same path itself; and it is a prefix match on that one exact path, so a mixed-case or double-slashed variant is not filtered. The route's rate limit is its whole security control (CAPTCHA is the front end's, at stage 6) and it keys on `request.client.host` — behind a proxy that needs `--proxy-headers`/`--forwarded-allow-ips` **set to the proxy's own address**: unset, every citizen in the country shares one bucket; set to `*`, any client can spoof `X-Forwarded-For` for a fresh bucket per request and the control is gone entirely.
- Running two or more app servers needs Redis for E-IMZO's shared challenge store (`design/04` §2.6) — this stage deliberately ships without it (one instance today; decision #36 already rejected Redis for jobs). The day a second instance joins, the challenge store is the first thing to break, and it breaks silently: a user gets `-20 challenge expired` at random, which reads like a client bug, not a missing dependency.
