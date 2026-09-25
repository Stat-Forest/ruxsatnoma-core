"""`GET /signatures/export.xlsx` (stage 13, ruling #204): one object's
signature list on paper. `signature_rows()` calls the list's own service
function — `service.list_signatures_page` (ruling R2) — with the cap as the
page size, then resolves every user id the sheet shows in ONE batch query
(`auth.service.user_names`). The route gets no Excel button (out of its
track's screen list) — `PermitSignaturesPanel.tsx` reads the list, but shows
a small single-object list rather than a register. The certificates export
beside it was removed on 2026-09-25 together with the cabinet tab."""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store, xlsx
from app.core.schemas import PageParams
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.signatures import service
from app.modules.signatures.models import Signature

SIGNATURES_TITLE = {"uz_latn": "Imzolar", "ru": "Подписи"}
KIND_LABELS: dict[str, dict[str, str]] = {
    "eri": {"uz_latn": "ERI", "ru": "ЭЦП"},
    "simple": {"uz_latn": "Oddiy", "ru": "Простая"},
}
VERIFICATION_STATUS_LABELS: dict[str, dict[str, str]] = {
    "valid": {"uz_latn": "Haqiqiy", "ru": "Действительна"},
    "invalid": {"uz_latn": "Haqiqiy emas", "ru": "Недействительна"},
}


def _label(table: dict[str, dict[str, str]], code: str, lang: xlsx.Lang) -> str:
    return table.get(code, {}).get(lang, code)


# --- signatures ----------------------------------------------------------


class SignatureRow:
    def __init__(self, sig: Signature, *, signer: str) -> None:
        self.sig = sig
        self.id = sig.id
        self.signer = signer


def signature_columns(lang: xlsx.Lang) -> list[xlsx.Column[SignatureRow]]:
    s = lambda f: lambda r: getattr(r.sig, f)  # noqa: E731 - column accessors read alike
    return [
        xlsx.Column(
            "object_type", {"uz_latn": "Obyekt turi", "ru": "Тип объекта"}, s("object_type"), 16
        ),
        xlsx.Column(
            "object_id",
            {"uz_latn": "Obyekt ID", "ru": "ID объекта"},
            lambda r: str(r.sig.object_id),
            38,
        ),
        xlsx.Column("purpose", {"uz_latn": "Maqsad", "ru": "Назначение"}, s("purpose"), 20),
        xlsx.Column(
            "kind",
            {"uz_latn": "Turi", "ru": "Вид"},
            lambda r: _label(KIND_LABELS, r.sig.kind, lang),
            12,
        ),
        xlsx.Column("signer", {"uz_latn": "Imzolagan", "ru": "Подписал"}, lambda r: r.signer, 26),
        xlsx.Column("signed_at", {"uz_latn": "Imzolangan", "ru": "Подписано"}, s("signed_at"), 18),
        xlsx.Column(
            "verification_status",
            {"uz_latn": "Tekshiruv holati", "ru": "Статус проверки"},
            lambda r: _label(VERIFICATION_STATUS_LABELS, r.sig.verification_status, lang),
            18,
        ),
        xlsx.id_column(),
    ]


async def signature_rows(
    db: AsyncSession,
    *,
    actor: User,
    lang: xlsx.Lang,
    object_type: str,
    object_id: uuid.UUID,
    kind: str | None,
) -> tuple[list[SignatureRow], int, int]:
    cap = await settings_store.get_int(db, xlsx.CAP_SETTING)
    sigs, total = await service.list_signatures_page(
        db,
        object_type=object_type,
        object_id=object_id,
        user=actor,
        params=PageParams.model_construct(page=1, page_size=cap),
        kind=kind,
    )
    signers = await auth_service.user_names(
        db, {s.signer_user_id for s in sigs if s.signer_user_id}
    )
    return (
        [
            SignatureRow(s, signer=signers.get(s.signer_user_id, "") if s.signer_user_id else "")
            for s in sigs
        ],
        total,
        cap,
    )


def render_signatures(items: Sequence[SignatureRow], *, lang: xlsx.Lang) -> bytes:
    return xlsx.render(items, signature_columns(lang), lang=lang, title=SIGNATURES_TITLE[lang])
