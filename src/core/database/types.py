from collections.abc import Sequence

from sqlalchemy.orm.strategy_options import _AbstractLoad as AbstractLoad  # noqa SLF001

# Shared aliases for eager-loading options, so callers never name the private
# SQLAlchemy type.
EagerLoad = AbstractLoad
EagerLoadSequence = Sequence[EagerLoad]
