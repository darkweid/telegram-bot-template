# The names use cases queue tasks by; tasks.py imports the use cases, so the
# names cannot live there without an import cycle.
RUN_BROADCAST = "broadcast.run"
RESUME_STALLED_BROADCASTS = "broadcast.resume_stalled"
