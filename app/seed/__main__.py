"""CLI: `uv run python -m app.seed <districts|organizations> <file.json>`.

Reference data lives in files, not migrations (ruling 5); this is how it reaches a
database. Idempotent: run it again after editing the file.
"""

import argparse
import asyncio
from pathlib import Path

from app.config import get_settings
from app.db import make_engine, make_session_factory
from app.seed import ENTITIES, load_rows, run


async def _main(entity: str, path: Path) -> None:
    rows = load_rows(path)
    engine = make_engine(get_settings().database_url)
    factory = make_session_factory(engine)
    try:
        async with factory() as db:
            summary = await run(entity, rows, db)
            await db.commit()
        print(summary)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.seed")
    parser.add_argument("entity", choices=ENTITIES)
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    asyncio.run(_main(args.entity, args.file))


if __name__ == "__main__":
    main()
