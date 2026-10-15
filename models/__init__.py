"""Every model module, imported in one place.

`migrations/env.py` runs `import models`, which fills `Base.metadata` for Alembic
autogenerate and `alembic check`. A model module missing here produces an empty
migration with no error; tests/unit/test_models_registration.py guards against that.
"""

from src.broadcast.models import Broadcast as Broadcast
from src.core.llm.models import LLMUsageRecord as LLMUsageRecord
from src.core.outbox.models import OutboxMessage as OutboxMessage
from src.note.models import Note as Note
from src.user.models import User as User
