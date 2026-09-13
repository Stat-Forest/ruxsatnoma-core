"""Runtime policy parameters: code defaults, DB overrides, short process cache.

Level 0 by design (ruling 8): `auth` (level 1) reads session and lockout policy from
here; `admin` (also level 1) writes it through its own service. Putting the reader in
core avoids an auth → admin dependency, which would close a cycle (design/01 rule 3).

The DB stores only OVERRIDES. A missing row means "use the default below", so a fresh
database is fully functional and `system_settings` never needs seeding.

The cache is per-process and expires after 60 seconds: with several uvicorn workers an
admin's change reaches every worker within that window, and the hot path (every
authenticated request reads `session_idle_minutes`) does not hit the DB each time.
"""

import time
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.models import SystemSetting

CACHE_TTL_SECONDS = 60


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type: type
    default: Any
    description: str
    # A `str` spec's blank value is illegal by default (`coerce`'s ERR-VAL-001
    # guard against an admin accidentally clearing a required field). A few
    # keys are legitimately optional — an address or a social link the Agency
    # may not have yet — and for those a blank IS the valid "not set" value,
    # so it must round-trip through the admin API rather than being the one
    # value that API can never write back (finding 4, stage 8 fix wave).
    allow_blank: bool = False


SETTING_SPECS: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec("session_absolute_hours", int, 12, "Session lifetime in hours"),
        SettingSpec("session_idle_minutes", int, 300, "Sign-out after this much inactivity"),
        SettingSpec("login_max_attempts", int, 5, "Failed logins before the account locks"),
        SettingSpec("login_lockout_minutes", int, 15, "How long a locked account stays locked"),
        SettingSpec("mfa_token_ttl_minutes", int, 5, "Lifetime of the interim MFA token"),
        SettingSpec("mfa_max_attempts", int, 5, "Wrong TOTP codes before the MFA token burns"),
        SettingSpec(
            "mfa_enabled",
            bool,
            True,
            "Require the TOTP step at staff login; OFF is a break-glass measure",
        ),
        SettingSpec("otp_ttl_minutes", int, 5, "Lifetime of a phone/email OTP code"),
        SettingSpec("otp_max_attempts", int, 5, "Wrong OTP entries before the code burns"),
        SettingSpec("otp_hourly_limit", int, 5, "OTP requests per target per hour"),
        SettingSpec("privacy_policy_version", str, "1.0", "Current privacy policy version"),
        SettingSpec("offer_version", str, "1.0", "Current public offer version"),
        SettingSpec("max_upload_mb", int, 20, "Maximum accepted upload size, MB"),
        SettingSpec(
            "outbox_max_attempts", int, 8, "Delivery attempts before an outbox row goes dead"
        ),
        SettingSpec(
            "outbox_backoff_base_minutes", int, 1, "First retry delay; doubles each attempt"
        ),
        SettingSpec(
            "purge_otp_after_days", int, 7, "Delete expired/used otp_codes after this many days"
        ),
        SettingSpec(
            "purge_sessions_after_days",
            int,
            30,
            "Delete expired/revoked sessions after this many days",
        ),
        SettingSpec(
            "purge_outbox_delivered_after_days",
            int,
            7,
            "Delete delivered outbox rows after this many days",
        ),
        SettingSpec(
            "purge_idempotency_after_hours",
            int,
            24,
            "Delete idempotency keys after this many hours",
        ),
        SettingSpec("ratelimit_login_per_minute", int, 10, "Per-IP limit for POST /auth/login"),
        SettingSpec("ratelimit_otp_per_minute", int, 5, "Per-IP limit for POST /auth/otp/request"),
        SettingSpec(
            "ratelimit_challenge_per_minute", int, 10, "Per-IP limit for POST /auth/eimzo/challenge"
        ),
        # Ruling T78-1: this route used to share `eimzo_challenge`'s own bucket
        # (the brief's "rate-limited the way /auth/eimzo/challenge is" meant the
        # MECHANISM, not the bucket). One anonymous login-challenge burst behind
        # an office NAT would otherwise throttle unrelated, in-progress document
        # SIGNING for everyone at that address. 20/min, double the challenge
        # route's own limit: a permit can carry up to four signatures, each
        # timestamped separately, plus the occasional retry.
        SettingSpec(
            "ratelimit_eimzo_timestamp_per_minute",
            int,
            20,
            "Per-IP limit for POST /api/v1/eimzo/timestamp",
        ),
        SettingSpec("notifications_sms_enabled", bool, True, "Ops kill switch for the SMS channel"),
        # Decision #152: the nightly SMS quiet window, Asia/Tashkent whole hours.
        # `sms` only — `sms_otp` is a code somebody is waiting for on the login
        # screen, and holding it until morning is not politeness but an outage.
        # Setting the two equal turns the window off.
        SettingSpec("sms_quiet_hours_start", int, 21, "Hour SMS delivery pauses (Asia/Tashkent)"),
        SettingSpec("sms_quiet_hours_end", int, 8, "Hour SMS delivery resumes (Asia/Tashkent)"),
        SettingSpec(
            "outbox_breaker_failures",
            int,
            5,
            "Consecutive failures before a destination is skipped",
        ),
        SettingSpec(
            "outbox_breaker_cooldown_seconds",
            int,
            60,
            "How long a tripped destination stays skipped",
        ),
        SettingSpec(
            "ratelimit_webhook_per_minute",
            int,
            1200,
            # Deliberately high: providers post from a small, fixed IP set, so a bulk
            # notification run's delivery reports all arrive from ONE bucket — and a
            # 429'd report is simply gone (nothing retries it into the DLQ). The
            # endpoint only writes a small status update and is guarded by a secret.
            "Per-IP limit for provider webhooks (a provider's whole IP set shares one "
            "bucket, and a throttled delivery report is lost, not retried)",
        ),
        # The anonymous QR page has TWO budgets, not one (3.11a t5, review I1).
        # Its two lookup paths are not equally dangerous and must not share a
        # bucket: `qr_token` is `secrets.token_urlsafe(32)` and unguessable,
        # while `permit_counters` hands out `last_number + 1`, so series+number
        # is a GAPLESS space anyone can walk. One shared bucket meant the
        # guessable path could not be tightened without also throttling the
        # citizen scanning a printed code — and, worse, a scanner behind the
        # same NAT starved honest scans on that egress address.
        SettingSpec(
            "ratelimit_public_check_qr_per_minute",
            int,
            60,
            # The scanned path. One scan is one request; the headroom is for an
            # inspector working through a folder of permits. Nothing is walkable
            # here — 60/min against 2^256 tokens is not an attack.
            "Per-IP limit for the anonymous permit check by QR token",
        ),
        SettingSpec(
            "ratelimit_public_check_manual_per_minute",
            int,
            10,
            # The typed path, deliberately six times tighter and matching
            # `ratelimit_login_per_minute` — its closest analogue, a human typing
            # an identifier into a form. A person reading «серия А № 000123» off
            # paper needs seconds per attempt, so 10/min is generous for them and
            # cuts a walk of the gapless number space from 86 400 cards a day to
            # 14 400. It is a floor, not a wall: CAPTCHA is the front end's, at
            # stage 6. Both keys are per-IP via `request.client.host`, so a
            # carrier-grade NAT puts a whole region in one bucket — raise the
            # row, never the code, if that shows up in the field.
            "Per-IP limit for the anonymous permit check by series and number",
        ),
        # The public price estimate (decision #63) is the third anonymous surface
        # after the login-adjacent routes and the QR check — same reasoning as
        # both: an anonymous compute endpoint is an obvious abuse target, and the
        # two narrow catalog reads it needs (activity/livestock types) are cheap
        # enough to share one, higher-headroom bucket rather than one each.
        SettingSpec(
            "ratelimit_public_calc_estimate_per_minute",
            int,
            20,
            "Per-IP limit for the anonymous POST /public/calculations/estimate",
        ),
        SettingSpec(
            "ratelimit_public_refs_per_minute",
            int,
            60,
            "Per-IP limit for the anonymous GET /public/refs/* catalog reads",
        ),
        SettingSpec(
            "gis_area_mismatch_pct",
            int,
            10,
            "Import warns when declared and computed area differ by more than this %",
        ),
        SettingSpec(
            "gis_overlap_tolerance_m2",
            int,
            100,
            "Intersections below this area are a shared border, not an overlap",
        ),
        SettingSpec("gis_import_max_mb", int, 100, "Upload cap for a geodata import file, MB"),
        SettingSpec(
            "norms_publish_scope",
            str,
            "central",
            "Who may put a norm in force: 'central' (the office, per VMQ 689) or 'leshoz'",
        ),
        SettingSpec(
            "permit_required_signatures",
            str,
            # Ruling #210: the recipient does not sign the permit — the ERI
            # signature over the application at filing is the citizen's only
            # one. `permit_recipient` stays a known purpose (`permits.signers`)
            # an operator may add back here; by default it is never awaited.
            "permit_head,permit_chief_forester,permit_accountant",
            "Purposes that must all be signed before a permit may become ACTIVE (C11)",
        ),
        # `signatures._REQUIREMENT_SETTINGS["permit_decision"]` — the same data
        # change that spec's own comment invites, for a second `object_type`. Its
        # literal, "permit_decision", is `signers.DECISION_PURPOSE` repeated by
        # hand: this module may not import a domain module to reach it
        # (`app/core/` never imports domain modules).
        SettingSpec(
            "permit_decision_required_signatures",
            str,
            "permit_decision",
            "Purpose that must be signed to change a permit's status (suspend/resume/revoke)",
        ),
        SettingSpec(
            "payme_cashbox_key_hash",
            str,
            "",
            "SHA-256 hex digest of the rotated Payme cashbox key (empty means "
            "PAYME_CASHBOX_KEY is still authoritative)",
        ),
        SettingSpec("bank_statement_max_mb", int, 10, "Upload cap for a bank statement file"),
        SettingSpec(
            "provider_settlement_payer_fragment",
            str,
            "payme",
            "Payment provider that settles in bulk: matched case-insensitively as a "
            "fragment of a bank line's payer name, and used as the provider code "
            "whose turnover that payout is reconciled against",
        ),
        SettingSpec(
            "refund_control_working_days",
            int,
            20,
            "Working days from a refund request to its control deadline (tz/08; RI-07 past it)",
        ),
        # 4.4 `dashboard`, plan ruling d: a territory-slice cell naming fewer
        # than this many distinct applicants is suppressed rather than shown,
        # at every drill-down level and for every viewer including leadership
        # — a dashboard is a trends tool, not the case-lookup screen. 5 sits
        # inside the range several public statistics agencies use for small-
        # cell suppression; admin-tunable without a deploy.
        SettingSpec(
            "dashboard_k_anonymity_threshold",
            int,
            5,
            "Minimum distinct applicants before a dashboard slice cell is shown, not suppressed",
        ),
        # 4.6 `public`: three more anonymous surfaces, same reasoning as the QR
        # check and the norms estimate above — one bucket per abuse shape. The
        # submit path is the tightest (it writes a row); the status check next
        # (still walkable by number, R3); open data is a cheap, cacheable read.
        SettingSpec(
            "ratelimit_public_appeal_submit_per_minute",
            int,
            5,
            "Per-IP limit for the anonymous POST /public/appeals",
        ),
        SettingSpec(
            "ratelimit_public_appeal_status_per_minute",
            int,
            20,
            "Per-IP limit for the anonymous GET /public/appeals/check",
        ),
        SettingSpec(
            "ratelimit_public_open_data_per_minute",
            int,
            60,
            "Per-IP limit for the anonymous open-data reads (layers, features, stats)",
        ),
        SettingSpec(
            "ratelimit_public_announcements_per_minute",
            int,
            60,
            "Per-IP limit for the anonymous landing announcements reads (`0037`)",
        ),
        SettingSpec(
            "ratelimit_public_legal_documents_per_minute",
            int,
            60,
            "Per-IP limit for the anonymous legal-documents reads (`0043`)",
        ),
        # 4.8 `help`: the one anonymous route this module has.
        SettingSpec(
            "ratelimit_public_help_faq_per_minute",
            int,
            60,
            "Per-IP limit for the anonymous GET /help/faq",
        ),
        # С22 (`tz/12` #42, decision #98, ruling #20): the prosecutor's (and every
        # other `search.use` holder's) watermarked export is capped at this many
        # rows per file — "≤10 000, configurable" verbatim. The export never
        # errors past the cap; it truncates and reports `total_matched` alongside
        # `row_count` so a caller can SEE it was cut, never guess.
        SettingSpec(
            "search_export_max_rows",
            int,
            10000,
            "Maximum rows in one search/oversight export (С22, ruling #20)",
        ),
        # Stage 13 (ruling #204): every register's own `.xlsx` export is capped
        # here — a key of its own, because the prosecutor's figure above is a
        # legal one and this one is operational; the Agency may tune them apart.
        # Past the cap the file holds the first N rows and the response says so
        # (`X-Export-Truncated`); it never refuses.
        SettingSpec(
            "register_export_max_rows",
            int,
            10000,
            "Maximum rows in one register .xlsx export (stage 13)",
        ),
        # Ruling #104: RI-14 «long active with no inspection» — the strictest
        # of three options Oybek was offered, and his own text names the
        # consequence: at 30 days it fires often, and an indicator that always
        # fires stops being read. A setting, not a constant, is what lets that
        # be tuned without a deploy once there is real inspection volume to
        # judge it by.
        SettingSpec(
            "oversight_ri14_no_inspection_days",
            int,
            30,
            "Days an active permit may run with no inspection act before RI-14 fires",
        ),
        # ── public site (#174): the Agency edits these itself on the H7 screen ──
        SettingSpec("site_contact_phone", str, "+998 71 207 88 77", "Public site: hotline number"),
        SettingSpec(
            "site_contact_email", str, "urmoninfo@gmail.com", "Public site: contact e-mail"
        ),
        SettingSpec(
            "site_contact_address_uz",
            str,
            "",
            "Public site: address, Latin Uzbek",
            allow_blank=True,
        ),
        SettingSpec(
            "site_contact_address_ru", str, "", "Public site: address, Russian", allow_blank=True
        ),
        SettingSpec(
            "site_contact_hours_uz",
            str,
            "Dushanba – juma, 9:00 – 18:00",
            "Public site: working hours, Latin Uzbek",
        ),
        SettingSpec(
            "site_contact_hours_ru",
            str,
            "Понедельник – пятница, 9:00 – 18:00",
            "Public site: working hours, Russian",
        ),
        SettingSpec(
            "site_social_telegram",
            str,
            "",
            "Public site: Telegram channel URL",
            allow_blank=True,
        ),
        SettingSpec(
            "site_social_youtube", str, "", "Public site: YouTube channel URL", allow_blank=True
        ),
        # Ruling #184: "I have read the rules" is a mandatory acceptance before
        # any signature (`applications.service.submit`, `ApplicationSubmitIn.
        # rules_accepted`). The text the checkbox links to is a setting, not a
        # page this system renders itself — VMQ 278 on lex.uz by default, the
        # Agency's own act, editable from the settings screen like every other
        # `site_*` key. Admin-editable through `GET`/`PATCH /admin/settings`
        # (generic over every `SETTING_SPECS` entry) AND on the public
        # `site_settings` whitelist as `rules_url` (`public.service.
        # site_settings`), which is where the wizard's checkbox reads it.
        SettingSpec(
            "site_rules_url",
            str,
            "https://lex.uz/docs/-2770948",
            "Filing: the rules the applicant accepts",
        ),
        # `site_season_windows` (six hard-coded per-activity month lists, ruling
        # R3) lived here until the stage 8 fix wave: `origin/dev` had by then
        # merged stage 9's ruling #177, which put the REAL windows in
        # `norms.models.ActivitySeason`, admin-editable per leshoz. Keeping
        # both would have let the landing print "grazing: April-November" while
        # `norms.checks._season_check` refused the citizen's dates against
        # their leshoz's actual window — deleted rather than kept as a second,
        # disagreeing source. The public read is now `GET
        # /public/activity-seasons` (`public.service.public_activity_seasons`),
        # resolved through `norms.service.resolve_effective_windows`, the same
        # function the blocking check calls. Supersedes the R3 half of
        # decision #175.
        # Ruling R2: personal geodata — stays OFF until the Agency confirms in writing.
        SettingSpec(
            "public_permit_contour_enabled",
            bool,
            False,
            "Publish the permit contour on the anonymous check page (#174)",
        ),
    )
}

_cache: dict[str, tuple[float, Any]] = {}


def invalidate(key: str | None = None) -> None:
    """Drop one key (after an admin update) or the whole cache (tests, bootstrap)."""
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


def coerce(spec: SettingSpec, raw: Any) -> Any:
    """Validate an incoming value against the spec; raises ERR-VAL-001 on bad input.

    JSON bodies may carry "45" where 45 is meant, so numeric strings are accepted.
    Policy values are positive integers — 0 would mean "lock out forever" or
    "session already expired", which no admin means to type.
    """
    if spec.type is int:
        if isinstance(raw, bool):  # bool is an int subclass — not a policy value
            raise err("ERR-VAL-001", details={"setting": spec.key, "reason": "expected integer"})
        try:
            value = int(raw)
        # Deliberately parenthesized, not the PEP 758 bare form: for years
        # `except Foo, bar:` meant Python 2's except-as binding, so the bare
        # shape reads like that trap. ruff format's py314 target rewrites it
        # away otherwise, hence the fmt:skip.
        except (TypeError, ValueError):  # fmt: skip
            raise err(
                "ERR-VAL-001", details={"setting": spec.key, "reason": "expected integer"}
            ) from None
        if value <= 0:
            raise err("ERR-VAL-001", details={"setting": spec.key, "reason": "must be positive"})
        return value
    if spec.type is bool:
        if not isinstance(raw, bool):
            raise err("ERR-VAL-001", details={"setting": spec.key, "reason": "expected boolean"})
        return raw
    # No `dict`-typed spec remains (the last one, `site_season_windows`, was
    # deleted in the stage 8 fix wave) — removed along with it rather than
    # left as an unvalidated `isinstance(raw, dict)` branch waiting for the
    # next key to fall into (finding 3): a dict spec added later must bring
    # its OWN shape validation here, not inherit this one's "any object goes".
    if not isinstance(raw, str):
        raise err("ERR-VAL-001", details={"setting": spec.key, "reason": "expected text"})
    if not spec.allow_blank and not raw.strip():
        raise err("ERR-VAL-001", details={"setting": spec.key, "reason": "expected text"})
    return raw


async def get_setting(db: AsyncSession, key: str) -> Any:
    """Effective value: DB override if present and well-formed, else the code default."""
    spec = SETTING_SPECS.get(key)
    if spec is None:
        raise KeyError(f"unknown setting: {key!r}")  # programming error, not user input
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    row = await db.get(SystemSetting, key)
    value = spec.default
    if row is not None:
        try:
            value = coerce(spec, row.value)
        except Exception:
            # A malformed row (hand-edited SQL) must not break every request.
            structlog.get_logger().error(
                "system_setting_invalid", key=key, value=row.value, using_default=spec.default
            )
    _cache[key] = (now + CACHE_TTL_SECONDS, value)
    return value


async def get_int(db: AsyncSession, key: str) -> int:
    value = await get_setting(db, key)
    assert isinstance(value, int)  # SETTING_SPECS guarantees the type
    return value


async def get_str(db: AsyncSession, key: str) -> str:
    value = await get_setting(db, key)
    assert isinstance(value, str)  # SETTING_SPECS guarantees the type
    return value


async def get_bool(db: AsyncSession, key: str) -> bool:
    value = await get_setting(db, key)
    assert isinstance(value, bool)  # SETTING_SPECS guarantees the type
    return value


async def set_setting(db: AsyncSession, key: str, raw_value: Any) -> None:
    """Write an override with NO ACTOR — the level-0 counterpart to
    `admin.service.update_setting` for a caller that has no `User` at all
    (first caller: `payments.payme.ChangePassword`, a Payme RPC call).
    `admin.service` is deliberately left untouched rather than refactored to
    share this: core imports no domain modules, so audit logging stays the
    acting module's own job — this function only coerces (the same
    `coerce()` the admin surface uses) and writes the row; it does NOT
    invalidate the cache or audit, both of which the caller does itself,
    explicitly, in its own acting module."""
    spec = SETTING_SPECS.get(key)
    if spec is None:
        raise KeyError(f"unknown setting: {key!r}")  # programming error, not user input
    value = coerce(spec, raw_value)  # raises ERR-VAL-001 on bad input
    row = await db.get(SystemSetting, key)
    if row is None:
        row = SystemSetting(key=key, value=value, description=spec.description)
        db.add(row)
    else:
        row.value = value
    row.updated_by = None
    await db.flush()
