"""permits_lifecycle

Plan `03.11b-permits-lifecycle`, rulings 5, 18 and 19. Four things and no more:

1. The classifier `permit_status_reasons` and its seven items, PS-01…PS-07 —
   `permit_status_history.reason_item_id` has FK'd the whole of
   `classifier_items` since migration 0019, with nothing under it to point
   at. `app.modules.permits.grounds.CLASSIFIER_CODE`/`.EXPLANATION_REQUIRED`
   repeat the code and PS-07 literally: a migration is a frozen historical
   statement and must not change meaning if the constant is ever renamed
   (the same reasoning `0024_benefit_proof_doc_type.py` gives for its own
   literal). Each item's `props` is exactly `{"kinds": [...]}` — the acts
   (`suspend`/`resume`/`revoke`) it may justify — read defensively by
   `grounds._kinds`, never trusted as shaped.
2. Six new `notification_templates` rows (ruling 18): `permit.suspended`,
   `permit.resumed`, `permit.revoked`, `permit.duplicate_issued`,
   `forest_ticket.issued`, `permit.unsigned_stalled` — `inapp` and `sms`,
   version 1, active. None of the six existed before this stage; without a
   template `notify()` writes a raw fallback string in-app and sends
   nothing at all by SMS, silently (ruling 17's failure mode, inherited).
3. `uq_forest_tickets_active` (ruling 19's `ERR-PERM-003`, raised by this
   stage's own service against a second active ticket on one permit) — a
   partial unique index on `forest_tickets(permit_id) WHERE status =
   'active'`, mirrored in `ForestTicket.__table_args__` in the same commit
   so `test_autogenerate_diff_empty` sees no drift.

`down_revision = "0022"`: `dev` forked at `0016` and rejoined at
`merge_0018_0020`, and this stage's own migration `0024` (3.9a-flow) already
took the next number off that merge point — `0023` is reserved for this
stage alone (`plans/03.9-3.11-parallel-run.md`), confirmed free by
`git fetch` before this file was created.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-05 09:00:00.000000
"""

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `app.modules.permits.grounds.CLASSIFIER_CODE`, repeated as a literal on purpose
# (see the module docstring above).
CLASSIFIER_ID = "0198f100-0023-7000-8000-000000000001"
CLASSIFIER_CODE = "permit_status_reasons"
CLASSIFIER_NAME_CYR = "Рухсатнома ҳолати сабаблари"
CLASSIFIER_NAME_RU = "Основания изменения статуса разрешения"

# (code, id, uz_cyrl name, ru name, kinds, sort_order) — ruling 5's table.
# `PS-04` never carries `"resume"`: it is the fire-danger restriction (ВМҚ
# 506), which lifts on its own when the ban period ends, never on a
# decision. `PS-07`, `grounds.EXPLANATION_REQUIRED`, is the one item every
# act may cite — the same shape as `rejection_reasons`' own RJ-15.
GROUNDS: list[tuple[str, str, str, str, list[str], int]] = [
    (
        "PS-01",
        "0198f100-0023-7000-8000-000000000002",
        "Инспекция натижаси бўйича",
        "По результатам инспекции",
        ["suspend", "revoke"],
        10,
    ),
    (
        "PS-02",
        "0198f100-0023-7000-8000-000000000003",
        "Рухсатнома эгасининг мурожаатига кўра",
        "По обращению владельца разрешения",
        ["suspend", "revoke"],
        20,
    ),
    (
        "PS-03",
        "0198f100-0023-7000-8000-000000000004",
        "Ваколатли орган ёки суд қарорига кўра",
        "По решению уполномоченного органа или суда",
        ["suspend", "revoke"],
        30,
    ),
    (
        "PS-04",
        "0198f100-0023-7000-8000-000000000005",
        "Ёнғин хавфи даври чеклови (ВМҚ 506)",
        "Ограничение в период пожарной опасности (ПКМ №506)",
        ["suspend"],
        40,
    ),
    (
        "PS-05",
        "0198f100-0023-7000-8000-000000000006",
        "Норма ёки лимитдан ошиб кетиш",
        "Превышение нормы или лимита",
        ["suspend", "revoke"],
        50,
    ),
    (
        "PS-06",
        "0198f100-0023-7000-8000-000000000007",
        "Сабаб бартараф этилди",
        "Причина устранена",
        ["resume"],
        60,
    ),
    (
        "PS-07",
        "0198f100-0023-7000-8000-000000000008",
        "Бошқа (изоҳ мажбурий)",
        "Другое (комментарий обязателен)",
        ["suspend", "resume", "revoke"],
        70,
    ),
]

# event_code -> body, shared by inapp and sms unless overridden below (0019's own
# idiom: a Cyrillic SMS bills at 70 characters per part, so a longer inapp text
# and a trimmed sms text are two different rows, not one reused verbatim).
_BODIES: dict[str, dict[str, str]] = {
    "permit.suspended": {
        "uz_cyrl": "Рухсатнома {permit_number} вақтинча тўхтатилди.",
        "ru": "Действие разрешения {permit_number} приостановлено.",
    },
    "permit.resumed": {
        "uz_cyrl": "Рухсатнома {permit_number} қайта тикланди.",
        "ru": "Действие разрешения {permit_number} возобновлено.",
    },
    "permit.revoked": {
        "uz_cyrl": "Рухсатнома {permit_number} бекор қилинди."
        " Тўлов бўйича қайтарим учун бухгалтерияга мурожаат қилишингиз мумкин.",
        "ru": "Разрешение {permit_number} отозвано."
        " За возвратом оплаты вы можете обратиться в бухгалтерию.",
    },
    "permit.duplicate_issued": {
        "uz_cyrl": "Рухсатнома {permit_number} нусхаси берилди.",
        "ru": "Выдан дубликат разрешения {permit_number}.",
    },
    "forest_ticket.issued": {
        "uz_cyrl": "{permit_number} рухсатномаси бўйича {ticket_number} рақамли"
        " ўрмон чиптаси расмийлаштирилди.",
        "ru": "По разрешению {permit_number} оформлен лесной билет {ticket_number}.",
    },
    "permit.unsigned_stalled": {
        "uz_cyrl": "{permit_number} рухсатномаси муддати тугади, аммо ҳамон"
        " имзоланмаган. Аризани якунланг.",
        "ru": "Срок разрешения {permit_number} истёк, но оно так и не подписано."
        " Завершите работу по заявке.",
    },
}

_SMS_BODIES: dict[str, dict[str, str]] = {
    "permit.revoked": {
        "uz_cyrl": "Рухсатнома {permit_number} бекор қилинди.",
        "ru": "Разрешение {permit_number} отозвано.",
    },
    "forest_ticket.issued": {
        "uz_cyrl": "{ticket_number} рақамли ўрмон чиптаси расмийлаштирилди.",
        "ru": "Оформлен лесной билет {ticket_number}.",
    },
    "permit.unsigned_stalled": {
        "uz_cyrl": "{permit_number} рухсатномаси муддати тугади, лекин имзоланмаган.",
        "ru": "Срок разрешения {permit_number} истёк, но оно не подписано.",
    },
}

SEED_TEMPLATES: list[tuple[str, str, dict[str, str]]] = [
    (event_code, channel, _SMS_BODIES.get(event_code, body) if channel == "sms" else body)
    for event_code, body in _BODIES.items()
    for channel in ("inapp", "sms")
]


def upgrade() -> None:
    """Insert reference rows. asyncpg needs explicit uuid casts for text binds
    (0005's idiom); `props` is built in Python and cast, matching 0010's own
    `CAST(:body AS jsonb)` — a plain jsonb-typed bind param sends the
    `BindParameter` construct itself to asyncpg's codec instead of serialized
    text, which asyncpg rejects."""
    op.execute(
        sa.text(
            "INSERT INTO classifiers (id, code, name) VALUES "
            "(CAST(:id AS uuid), :code, jsonb_build_object('uz_cyrl', :cyr, 'ru', :ru))"
        ).bindparams(
            id=CLASSIFIER_ID, code=CLASSIFIER_CODE, cyr=CLASSIFIER_NAME_CYR, ru=CLASSIFIER_NAME_RU
        )
    )

    for code, item_id, name_cyr, name_ru, kinds, sort_order in GROUNDS:
        op.execute(
            sa.text(
                "INSERT INTO classifier_items "
                "(id, classifier_id, code, name, props, valid_from, sort_order, status) VALUES "
                "(CAST(:id AS uuid), CAST(:classifier_id AS uuid), :code, "
                "jsonb_build_object('uz_cyrl', :cyr, 'ru', :ru), "
                "CAST(:props AS jsonb), DATE '2026-01-01', :sort, 'active')"
            ).bindparams(
                id=item_id,
                classifier_id=CLASSIFIER_ID,
                code=code,
                cyr=name_cyr,
                ru=name_ru,
                props=json.dumps({"kinds": kinds}),
                sort=sort_order,
            )
        )

    templates = sa.table(
        "notification_templates",
        sa.column("id", sa.Uuid()),
        sa.column("event_code", sa.Text()),
        sa.column("channel", sa.Text()),
        sa.column("body", postgresql.JSONB(astext_type=sa.Text())),
        sa.column("version", sa.Integer()),
        sa.column("status", sa.Text()),
    )
    op.bulk_insert(
        templates,
        [
            {
                # uuid4(), not app.db.uuid7 — a migration must not depend on app
                # code that could move or be renamed later (0009/0019/0020 made
                # the same call).
                "id": uuid.uuid4(),
                "event_code": event_code,
                "channel": channel,
                "body": body,
                "version": 1,
                "status": "active",
            }
            for event_code, channel, body in SEED_TEMPLATES
        ],
    )

    op.create_index(
        "uq_forest_tickets_active",
        "forest_tickets",
        ["permit_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_forest_tickets_active",
        table_name="forest_tickets",
        postgresql_where=sa.text("status = 'active'"),
    )

    # `notifications` FIRST — once anything actually sends one of these six events,
    # `notifications.template_id` points at a row the plain delete below would
    # orphan against `fk_notifications_template_id_notification_templates` (the
    # 0010 trap; a downgrade must delete whatever its upgrade made possible).
    event_codes = sorted(_BODIES)
    op.execute(
        sa.text("DELETE FROM notifications WHERE event_code = ANY(:codes)").bindparams(
            codes=event_codes
        )
    )
    op.execute(
        sa.text("DELETE FROM notification_templates WHERE event_code = ANY(:codes)").bindparams(
            codes=event_codes
        )
    )

    # Items before the classifier, or the FK from classifier_items blocks it
    # (0005's downgrade shape).
    op.execute(
        sa.text("DELETE FROM classifier_items WHERE classifier_id = CAST(:id AS uuid)").bindparams(
            id=CLASSIFIER_ID
        )
    )
    op.execute(
        sa.text("DELETE FROM classifiers WHERE id = CAST(:id AS uuid)").bindparams(id=CLASSIFIER_ID)
    )
