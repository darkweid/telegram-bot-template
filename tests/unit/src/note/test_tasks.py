from src.note.tasks import note_created
from tests.helpers.logs import captured_logs

NOTE_ID = "01900000-0000-7000-8000-000000000001"


async def test_the_task_logs_one_line_with_ids_only() -> None:
    """Ids only: a note's text must never reach a log line."""
    with captured_logs("src.note.tasks") as records:
        await note_created(note_id=NOTE_ID, user_id=7)
    [record] = records
    assert record.getMessage() == f"Note {NOTE_ID} created by user 7"
