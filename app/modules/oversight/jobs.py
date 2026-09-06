"""The periodic sweep job's actual logic (plan ruling c: every 5 minutes, not
daily — `tz/09` promises the internal recording stays immediate while RN
itself is disconnected). `app/workers/jobs.py::oversight_sweep` is the thin
wrapper (open session, run this, commit) every other job in that file
already follows — this function takes `db` and does not commit, the same
split `payments.jobs.refund_sla_sweep`/`applications.jobs.sla_sweep` have
from their own thin wrappers."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit import service as audit
from app.modules.oversight import service


async def sweep(db: AsyncSession, *, correlation_id: str) -> dict[str, int]:
    counts = await service.sweep(db)
    await audit.log(
        db,
        action="oversight.sweep",
        user_id=None,
        correlation_id=correlation_id,
        extra=counts,
    )
    return counts
