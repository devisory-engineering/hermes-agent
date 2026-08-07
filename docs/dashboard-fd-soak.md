# Dashboard SQLite descriptor soak (24h operational procedure)

Operator-facing companion to
`tests/test_dashboard_session_db_fd_lifecycle.py`. CI runs a deterministic
90-session churn as the regression proxy; this is how to reproduce the original
long-horizon report against a real backend.

## Scope

This procedure measures **file-descriptor lifetime only**. It is not a
corruption test and produces no evidence about database integrity. The deleted
WAL/SHM descriptors seen on gateway masters during the related incident were
established to be inert orphan inodes held by no live writer (one live writer
population per DB), so a clean run here does not clear — and a dirty run does
not implicate — any wider corruption question.

## Procedure

1. Start the backend under the profile you want to soak:

   ```
   hermes -p <profile> dashboard --isolated --host 127.0.0.1 --port 9119
   ```

2. Record the backend PID. The launcher may re-exec, so resolve the process
   that actually owns the listening socket rather than the launcher shell:

   ```
   BACKEND_PID=$(pgrep -f 'hermes (dashboard|serve) --isolated' | tail -1)
   ```

3. Sample descriptors every 5 minutes for 24 hours. Both totals matter: the
   overall count catches sockets/pipes, the `state.db` slice catches this
   defect specifically, and the `(deleted)` slice catches handles pinned to
   unlinked inodes:

   ```
   while :; do
     total=$(ls /proc/$BACKEND_PID/fd | wc -l)
     statedb=$(ls -l /proc/$BACKEND_PID/fd | grep -c 'state\.db')
     deleted=$(ls -l /proc/$BACKEND_PID/fd | grep 'state\.db' | grep -c '(deleted)')
     printf '%s total=%s state_db=%s deleted=%s\n' "$(date -Is)" "$total" "$statedb" "$deleted"
     sleep 300
   done | tee dashboard-fd-soak.log
   ```

4. Drive realistic load for the duration. The defect is per-session, not
   per-request, so the load must churn sessions rather than just poll:
   open the desktop app, switch between chats in several profiles, branch a
   session, and close sessions. Pure `/api/status` polling will not move the
   number in either direction.

5. Evaluate. Fit a trend across the samples rather than comparing endpoints —
   a single spike during an active session is expected and not a leak:

   * `state_db` returns to its idle floor after sessions close: PASS.
   * `state_db` rises monotonically with the number of sessions opened, and
     never returns to the floor: FAIL — the ownership seam has regressed.
   * `deleted` above zero and growing: FAIL, and capture
     `ls -l /proc/$BACKEND_PID/fd` before restarting the process.

## Expected numbers

Before the ownership fix, each closed profile-scoped session left exactly one
`state.db` descriptor open (measured 1.0 leaked fd per closed session, 75 fds
after 75 sessions). After it, the count returns to baseline on every teardown
and the 24h trend is flat.
