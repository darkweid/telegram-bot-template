import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.system.repositories import SystemRepository

# The integration marker comes from tests/integration/conftest.py; the loop
# scope must match db_session's.
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_ping_answers_on_a_live_database(db_session: AsyncSession) -> None:
    await SystemRepository().ping(db_session)
