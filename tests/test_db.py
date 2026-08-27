from sqlalchemy import text


async def test_db_roundtrip(db):
    result = await db.execute(text("SELECT 1"))
    assert result.scalar_one() == 1
