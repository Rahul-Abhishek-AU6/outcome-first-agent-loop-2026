# Crash-recovery evidence

The controller's SQLite persistence check was run on 2026-11-06.

1. Opened a fresh store and inserted event ID `42`, events cursor `7`, and pending proposal `p1`.
2. Committed and closed the process.
3. Reopened the same store.
4. Verified that event ID `42` was rejected as a duplicate, cursor `7` was retained, and proposal `p1` was still pending.

Result: `crash-recovery persistence check passed`.
