# Broadcasts

How an admin sends one message to every user, what the worker does while it
goes out, and the limits that keep it inside Telegram's rules. Open it before
the first broadcast on a real bot, when a broadcast looks stuck, or when
changing its pace. The code is in `src/broadcast`; the task machinery it runs
on is in [background jobs](background-jobs.md).

## Sending a broadcast

`/broadcast` is an admin command (see [roles](bot-layer.md#roles)) and works in
the admin's private chat only.

1. Send `/broadcast`. The bot refuses while another broadcast is pending or
   running, and otherwise asks for the message.
2. Send the message. Any single message that is not a command becomes the
   draft: text, a photo or a video with a caption, a document. The
   bot copies it back as a preview, followed by the number of recipients and
   two buttons, Send and Cancel. `/cancel` drops the draft at any step.
3. Press Send. The confirmation turns into the progress message ("Starting the
   broadcast…"), the broadcast is stored, and its `broadcast.run` task is
   enqueued in the same transaction.

Albums are refused. Telegram delivers every part of an album as a message of
its own, and the bot copies one message, so a broadcast would carry only the
first part. The draft remembers the album's `media_group_id` in the FSM data,
so an album gets one refusal however many parts it has; send its parts one by
one or as a single message instead.

The progress message carries a Stop button and the counters: sent,
unreachable, failed, and how many recipients were processed out of the total.
When the broadcast ends, the progress message turns
into the final report ("finished", "stopped" or "failed"), and the bot sends
the report again as a new message, which notifies the admin.

Only one broadcast exists at a time. `StartBroadcastUseCase` takes an advisory
lock and checks for an active broadcast in the same transaction, so two admins
pressing Send at once start one broadcast; the other is told one is in
progress.

**Recipients** are users who are reachable (the bot can still write to them),
not banned, and were stored before Send was pressed: someone who starts the bot
later does not get this broadcast. They are walked in order of their Telegram
id through the partial index `ix_users_recipients`. A change mid-run counts only
for users the walk has not reached yet: one who unblocks the bot is reached
too, one who blocks it or is banned is skipped. The total shown is the count at
Send; the progress never reads past it, and the report of a completed broadcast
counts the users who were actually walked.

Do not delete the original message until the broadcast has finished. Every copy
is made from it; once it is gone, Telegram refuses the copy and the broadcast
stops as failed.

## How a run works

`RunBroadcastUseCase` (`src/broadcast/usecases/run_broadcast.py`) runs in the
worker. Every read and write opens a short session of its own, so no
connection is held while messages go out.

- **Lease.** A run owns its broadcast through a lease: `claim_run` takes the
  row only when it is pending or its lease was last renewed more than
  `STALE_AFTER` (5 minutes, `src/broadcast/services.py`) ago, and writes the
  run's own `run_id`. A run that finds another `run_id` at a checkpoint exits
  without writing anything.
- **Checkpoints.** Recipients are read in batches of
  `BROADCAST_CHECKPOINT_EVERY` (50). The run saves its position and counters
  after every batch, at least once a minute, and right after a recipient who
  turned out to be unreachable: that user is already marked unreachable, and a
  resumed run walks reachable users only, so the outcome would otherwise be
  lost. A checkpoint renews the lease and reads the Stop request, so Stop takes
  effect at the next one, within about a minute.
- **Outcomes.** A copy Telegram accepts counts as sent. A Forbidden (the user
  blocked the bot or deleted the account) counts as unreachable; the session
  middleware has already marked the user so. A bad request for one chat (a
  chat Telegram cannot find) counts as failed. A network or server error is
  retried after 1, 2, 4 and 8 seconds; one more failure fails the broadcast,
  and so does a missing source message or any other error.
- **Failure.** A failed broadcast is marked `FAILED`, its report is sent, and
  the error leaves the task, so the developers get a
  [task failure alert](background-jobs.md#failure-alerts). `broadcast.run` is
  never retried: a retry would be a second run, and a run that was cut off
  comes back through the resume task instead.
- **Resume.** `broadcast.resume_stalled` runs every minute and queues a run for
  every broadcast whose lease is older than `STALE_AFTER`. If the worker stops
  mid-broadcast, the broadcast resumes within about five minutes from its last
  checkpoint, so up to `BROADCAST_CHECKPOINT_EVERY` users may get the message
  twice.
- **Report.** The final report edits the progress message and is recorded in
  `reported_at` once Telegram has taken the edit or refused it for good (a
  deleted progress message, a blocked bot). A network, server or flood error
  leaves it unrecorded, and the resume task queues a run that only resends the
  report from the stored counts, claimed through the same lease
  (`claim_report`). The notification message goes out after the record, at
  most once: a worker that stops in between leaves the edited progress message
  as the only report.

The progress message is edited at most every 5 seconds. Every Bot API call of a
run is capped at 5 seconds, so a hanging request cannot outlast the lease.

## Limits

Telegram accepts about 30 messages a second from one bot to different users.
The run paces itself with a `Pacer` (`src/core/limiter/pacer.py`), a local
pace with no Redis round trip, since a broadcast runs in one worker at a time.

| Setting | Default | Range | Effect |
| --- | --- | --- | --- |
| `BROADCAST_RATE_PER_SECOND` | 25 | above 0, at most 30 | copies a second |
| `BROADCAST_CHECKPOINT_EVERY` | 50 | 1 to 1000 | recipients per batch and checkpoint |

A larger `BROADCAST_CHECKPOINT_EVERY` means fewer writes, but more duplicate
copies after a worker restart and a slower Stop.

Flood control still happens: other traffic of the bot shares the same limit.
The session middleware retries a `RetryAfter` within its budget (see
[the middleware chain](bot-layer.md#middleware-chain)); a longer wait is sat
out by the run itself, which keeps checkpointing during the wait, so the lease
stays alive and a Stop still lands. With 25 copies a second, 10,000 users take
about seven minutes.
