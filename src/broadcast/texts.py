from aiogram.utils.i18n import gettext as _, ngettext

from src.broadcast.enums import BroadcastStatus
from src.broadcast.schemas import BroadcastProgress
from src.core.i18n import N_

TITLES: dict[BroadcastStatus, str] = {
    BroadcastStatus.PENDING: N_("Broadcast in progress: {processed} of {total}"),
    BroadcastStatus.RUNNING: N_("Broadcast in progress: {processed} of {total}"),
    BroadcastStatus.COMPLETED: N_("Broadcast finished: {processed} of {total}"),
    BroadcastStatus.CANCELLED: N_("Broadcast stopped: {processed} of {total}"),
    BroadcastStatus.FAILED: N_("Broadcast failed: {processed} of {total}"),
}


def progress_text(progress: BroadcastProgress) -> str:
    """The progress message while the broadcast runs, and its final report."""
    title = _(TITLES[progress.status]).format(
        processed=progress.processed, total=progress.total
    )
    counters = _("Sent: {sent}\nUnreachable: {unreachable}\nFailed: {failed}").format(
        sent=progress.sent, unreachable=progress.unreachable, failed=progress.failed
    )
    return f"{title}\n{counters}"


def preview_text(count: int) -> str:
    return ngettext(
        "This message will go to {count} user. Send it?",
        "This message will go to {count} users. Send it?",
        count,
    ).format(count=count)
