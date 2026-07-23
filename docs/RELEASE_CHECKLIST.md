# Discord Traders — Release & User Acceptance Checklist

Reusable, version-agnostic release procedure. This document is the
**template and procedure** — the one-time results, defect log, and sign-off
for a specific milestone/release are recorded separately in that
milestone's handoff document (e.g.
`docs/HANDOFFS/2D.7_release_and_user_acceptance_testing.txt` for the
`v0.1.0` release), never in this file.

This checklist assumes the reader may be a **non-technical Product
Owner** — every step is written to be followed without prior Windows
Sandbox, Python, or Git experience.

---

## 0. Before you start

- You will need: Windows 10/11 Pro/Enterprise/Education with the
  **Windows Sandbox** optional feature enabled, and the exact
  release-candidate commit hash for this release (recorded in the
  milestone's handoff document).
- Windows Sandbox is a temporary, disposable, isolated Windows
  environment. Nothing you do inside it can affect your real computer,
  and everything inside it disappears the moment you close the Sandbox
  window.
- **Never** run any of these tests against your real, everyday
  installation of Discord Traders or your real database. Every step in
  this checklist is designed to run only inside Windows Sandbox, against
  a disposable copy.
- `sandbox\prepare_release_candidate.ps1`'s default commit hash is a
  **placeholder** and must be replaced with the exact approved
  release-candidate commit for the release being tested before this
  checklist is executed — either by passing `-CommitHash <hash>` when
  running the script, or by editing the script's default value.

## 1. Windows Sandbox setup (developer/engineer step, one-time per release)

These setup steps are performed once by whoever is preparing the release
candidate (not the Product Owner) before handing the Sandbox off for
testing:

1. Enable Windows Sandbox if not already enabled: **Settings → Apps →
   Optional Features → More Windows Features → Windows Sandbox** (or
   `Turn Windows features on or off` in Control Panel), then restart if
   prompted.
2. On the development machine (not inside Sandbox), run
   `sandbox\prepare_release_candidate.ps1` from a PowerShell prompt,
   passing the exact approved release-candidate commit hash if it differs
   from the script's default. This creates a separate, detached `git
   worktree` checkout of that exact commit at
   `C:\DiscordTradersReleaseCandidate` — **the live development working
   directory is never read from or modified**, and no tracked files
   change.
3. Double-click `sandbox\discord_traders_uat.wsb`. This launches a fresh
   Windows Sandbox session that:
   - Maps `C:\DiscordTradersReleaseCandidate` in **read-only**, so nothing
     running inside Sandbox can modify the release-candidate checkout or
     reach outside it.
   - Automatically copies that read-only content into a **writable**
     folder inside the Sandbox at
     `C:\Users\WDAGUtilityAccount\Desktop\Discord_Traders_UAT`, excluding
     `.git`, `.venv`, and `__pycache__`.
   - Shows on-screen instructions for what to do next.
4. **Always run `start.bat` from the writable copy**
   (`C:\Users\WDAGUtilityAccount\Desktop\Discord_Traders_UAT\start.bat`)
   — never from the read-only mapped folder, which cannot create
   `.venv` or any other runtime file.

### Two required Sandbox sessions

Because closing Windows Sandbox discards everything inside it, two
**separate** fresh Sandbox launches are needed to cover every check below
without cross-contamination:

- **Session A — Python-missing test**: launch a fresh Sandbox via the
  `.wsb` file. When the setup script finishes, do **not** install
  Python. Go straight to Section 2 ("Python prerequisite test")
  below, then close this Sandbox session entirely once that check is
  done.
- **Session B — full happy-path test**: launch a **new**, separate fresh
  Sandbox via the same `.wsb` file. Install Python first (Section 3),
  then continue through **every** remaining section **without closing
  this Sandbox window** until Section 13 and all remaining UAT work are
  complete — closing Sandbox at any earlier point would discard the
  `.venv`, database, and log state those later steps depend on.

---

## 2. Python prerequisite test (Session A)

1. In a fresh Session A (Python **not** installed), double-click
   `start.bat` in the writable UAT folder.
2. **Expected**: a window appears showing exactly:
   > ERROR: Python was not found on this computer.
   > Install Python 3 from https://www.python.org/downloads/ and try again.

   and the window **pauses** (waits for a key press) rather than closing
   immediately.
3. **Pass** if that exact message appears and the window stays open until
   dismissed. **Fail** if the window closes immediately, shows a
   different/generic error, or Python is somehow found anyway.
4. Close this Sandbox session. It is not needed again.

## 3. First-run installation test (Session B)

1. In a fresh Session B, download Python 3 **only from the official
   website**, https://www.python.org/downloads/ — do not use any other
   download source.
2. Run the installer. On the first install screen, **explicitly check
   the "Add Python to PATH" checkbox** before clicking Install — this is
   easy to miss and, if skipped, will cause `start.bat` to report
   "Python was not found" even though Python is installed.
3. Wait for the installer to finish completely (it will show a
   "Setup was successful" screen) before continuing.
4. Verify the installation before running `start.bat`: open Command
   Prompt (see the tip below) and run:
   ```
   python --version
   ```
   If that doesn't print a version number, try instead:
   ```
   py -3 --version
   ```
   Proceed to the next step only once **one** of these two commands
   prints a Python version number. If neither does, Python was not
   installed correctly — repeat steps 1–3 before continuing.

   > **Tip — opening Command Prompt in a specific folder**: in File
   > Explorer, navigate to the folder you need (e.g. the writable UAT
   > folder), hold **Shift** and right-click an empty area inside it,
   > then choose **"Open PowerShell window here"** or **"Open command
   > window here."**

5. Double-click `start.bat` in the writable UAT folder.
6. **Expected**: a console window shows `Creating virtual environment...`,
   then `Installing dependencies...` with `pip` output, then
   `Starting Discord Traders...`.
7. **Pass** if `.venv` is created (visible in the writable UAT folder) and
   installation completes without an `ERROR:` message. **Fail** on any
   `ERROR:` message or if the window closes without reaching
   "Starting Discord Traders...".

## 4. Browser launch and localhost verification (Session B, continued)

1. **Expected**: a web browser opens automatically to the app.
2. Confirm the address bar shows `http://localhost:8501` and the
   Discord Traders UI is visible and responsive.
3. **Pass** if the browser opens automatically and the app loads at that
   exact address. **Fail** if the browser doesn't open, or the app
   doesn't load there.

## 5. Second-run venv-reuse test (Session B, continued — do not close Sandbox)

1. Close the browser tab/window and the `start.bat` console window (do
   **not** close the Sandbox itself).
2. Double-click `start.bat` again from the same writable UAT folder.
3. **Expected**: no `Creating virtual environment...` or
   `Installing dependencies...` messages this time — it goes straight to
   `Starting Discord Traders...` and is noticeably faster.
4. **Pass** if no reinstall occurs. **Fail** if dependencies are
   reinstalled or a new `.venv` is created.

## 6. Database creation and persistence across restart (Session B, continued)

1. With the app running (from Section 5), go to **Manual Message Entry**
   and submit a known sample message (see Section 7 for the exact text).
2. Confirm the success message and that the signal appears under
   **Review Signals**.
3. Close the browser and the console window entirely (do not close
   Sandbox).
4. Double-click `start.bat` again.
5. **Expected**: the same signal from step 2 is still visible under
   **Review Signals** after this fresh app restart.
6. **Pass** if the data survives the restart exactly as submitted.
   **Fail** if the signal is missing, altered, or the database appears to
   have been recreated empty.

## 7. Valid and malformed message tests

1. **Valid message**: paste
   `BTO SPY 450C 7/19/2025 @3.25 10 contracts`, click **Parse Message**,
   confirm the structured preview shows symbol `SPY`, action `BTO`,
   option type `call`, price `3.25`, expiration `2025-07-19`, position
   size `10 contracts`. Fill in a trader name and external trader ID,
   click **Submit to Database**, confirm the exact success message and
   that the row appears under Review Signals with the same values.
2. **Malformed message**: paste `just some random text, nothing
   parseable here`, click **Parse Message**, confirm the exact message
   `No trade signals found in this message.` appears and no new row is
   created under Review Signals.
3. **Pass** if both behaviors match exactly. **Fail** on any mismatch,
   missing message, or unexpected database write from the malformed case.

## 8. Duplicate advisory test

1. Submit the exact same message and trader/external-ID combination used
   in Section 7's valid-message test a second time.
2. **Expected**: a non-blocking duplicate warning appears, but the
   message still reports success and the signal is still saved.
3. Confirm under Review Signals that **both** the original and the new
   signal are present as separate rows.
4. **Pass** if the warning appears and both rows persist. **Fail** if the
   submission is blocked, or if only one row exists.

## 9. Review filters test

1. Submit at least one additional signal for a **different** trader and
   a **different** symbol than used above (e.g.
   `STC AAPL 190P 12/15/2025 @1.10` for a second trader).
2. Under Review Signals, filter by each trader name individually and
   confirm only that trader's signal(s) appear.
3. Filter by each symbol individually and confirm the same.
4. If a date filter is available, confirm it narrows results to the
   expected day.
5. **Pass** if every filter shows exactly the expected subset. **Fail** on
   any missing or extra row.

## 10. Correction and audit-history test

1. Select one of the signals submitted above and click **Correct
   Signal**.
2. Change one editable field (e.g. price), confirm the correction, and
   click **Save Correction**.
3. **Expected**: the exact success message
   `Trade signal correction saved.`, the field's new value reflected in
   the review list/detail view, and exactly **one** new entry under
   **Correction History** showing the previous (pre-correction) value.
4. Attempt a second correction using stale/outdated expected values if
   practical (e.g. reload the page in a second browser tab first, then
   submit a correction from the first, now-stale tab).
5. **Expected**: the exact conflict message
   `This trade signal changed or is no longer available. Reload it before correcting.`
   and **no** new audit-history row is created for the rejected attempt.
6. **Pass** if both behaviors match exactly. **Fail** on any incorrect
   message, missing/extra audit row, or incorrect persisted value.

## 11. Backup test

1. Click **Create Backup**.
2. **Expected**: the exact success message
   `Database backup created successfully.`
3. Confirm (via File Explorer inside the Sandbox, at
   `%LOCALAPPDATA%\DiscordTraders\backups`) that a new timestamped backup
   file was created.
4. **Pass** if the message and file both appear as expected. **Fail** on
   any error message or missing file.

## 12. Disposable restore/recovery test

**This test must only ever run against disposable Sandbox data — never
against a real production database, and never on the development
machine.**

> **Tip — opening Command Prompt in the writable UAT folder**: in File
> Explorer, navigate to
> `C:\Users\WDAGUtilityAccount\Desktop\Discord_Traders_UAT`, hold
> **Shift** and right-click an empty area inside it, then choose **"Open
> PowerShell window here"** or **"Open command window here."**

1. Still inside Session B, note the current database's location
   (`%LOCALAPPDATA%\DiscordTraders\discord_traders.db`) and the backup
   file created in Section 11.
2. Close `start.bat`'s console window (the app must be fully stopped
   before running restore, per `database/backup.py`'s documented usage).
3. From the writable UAT folder, open a command prompt (see the tip
   below).

   > **⚠ RUN INSIDE WINDOWS SANDBOX ONLY — DISPOSABLE DATA ONLY.**
   > Never run this command anywhere except inside this Windows Sandbox
   > session, and never against anything other than the disposable
   > Sandbox-local database created in this session.
   ```
   .venv\Scripts\python.exe -m database.backup restore "<path to the Section 11 backup file>"
   ```
4. **Expected**: the exact message `Restore completed successfully.`
5. Relaunch `start.bat` and confirm under Review Signals that the
   database reflects the state at the time of that backup (i.e., **not**
   including anything submitted after the backup was taken, if
   applicable).
6. **Rejected-candidate check**: attempt a restore against a clearly
   invalid file (e.g. a renamed `.txt` file).

   > **⚠ RUN INSIDE WINDOWS SANDBOX ONLY — DISPOSABLE DATA ONLY.**
   > Same warning as above — this is another live invocation of the
   > restore command and must only ever run inside this Sandbox session.

   Confirm the exact failure message (`Restore failed. See logs for
   details.`) with the production database left completely untouched.
7. **Pass** if both the successful and rejected restore behave exactly as
   documented in `database/backup.py`. **Fail** on any data loss,
   corruption, or a rejected candidate that still modifies the
   production file.

## 13. Logging and error-sanitization test

1. Trigger at least one deliberate failure — for example, submit a
   correction with an invalid price format, or attempt to parse while the
   database is temporarily inaccessible.
2. Confirm the user-facing message is one of the fixed, generic messages
   documented for that workflow (never raw exception text).
3. Locate the log file at
   `%LOCALAPPDATA%\DiscordTraders\logs\discord_traders.log` and confirm
   the corresponding entry contains
   **only**: a fixed operation label, the exception's class name, and a
   sanitized traceback (file basenames, line numbers, function names,
   and static source lines) — **never** raw message text, trader/source
   names, field values, full file paths, or the exception's message
   text. Confirm no `CRITICAL`-level entry exists anywhere in the log.
4. **Pass** if the log matches this sanitization contract exactly.
   **Fail** on any raw/sensitive value appearing in the log.

---

## 14. Defect table

Record every deviation found during any of the above steps here (or in
the milestone-specific handoff document, if this checklist is being
followed as part of a specific milestone's UAT execution):

| Defect ID | Step | Severity (blocking/non-blocking) | Expected | Actual | Status | Retest Result |
|---|---|---|---|---|---|---|
| | | | | | | |

## 15. Pass/fail criteria (summary)

**Pass** = the exact documented fixed message/behavior appears, and the
correct persisted/audit state is directly verified (not merely "it didn't
crash"). **Fail** = any deviation, silent failure, incorrect persisted
value, crash, or unhandled exception.

## 16. Release-blocking criteria

Any of the following blocks release until fixed and retested:
- Data loss or corruption of any kind.
- A crash or unhandled exception anywhere in the tested workflows.
- An incorrect persisted value (trade signal, audit history, or backup
  content).
- A backup or restore failure, or a restore that succeeds but leaves
  incorrect data.
- Any raw/sensitive value (message text, trader/source names, field
  values, paths, exception text) appearing in a log or user-facing
  message.

Purely cosmetic issues (wording, minor UI polish) do not block release —
they are logged as known issues and may be deferred to a later release.

Only narrowly scoped, clearly release-blocking fixes may be made during
UAT execution, and each such fix requires its own separate planning,
review, approval, implementation, testing, and commit — never bundled
silently into the UAT results. Any defect that is structural, or whose
scope is not immediately obvious, stops UAT entirely and is split into a
separate, separately-approved sub-milestone.

## 17. Product Owner sign-off

UAT sign-off is not self-certified by the implementation engineer. The
Product Owner must personally witness or execute this checklist and
record:

```
Release candidate commit:   ______________________________
Checklist completed by:     ______________________________
Date:                       ______________________________
All steps passed:           [ ] Yes   [ ] No (see defect table)
All release-blocking
  defects closed/retested:  [ ] Yes   [ ] N/A
Product Owner approval to
  proceed to tagging:       [ ] Approved   [ ] Not approved
Signature / name:           ______________________________
```

## 18. `v0.1.0` tagging gates

The release tag is created and pushed **only after, in this exact
order**:

1. Every item in this checklist has passed (or has an explicitly
   approved, documented exception).
2. Every release-blocking defect opened during UAT is closed and
   retested.
3. ChatGPT review of the full UAT results and documentation is complete.
4. The Product Owner gives **explicit** release approval (Section 17),
   distinct from any individual fix approval given along the way.

Tagging is a separate, explicitly gated final action — it does not happen
automatically as part of completing the milestone's documentation.

## 19. Cleanup after UAT

Once UAT is fully complete (whether or not tagging has happened yet),
the engineer who ran `sandbox\prepare_release_candidate.ps1` should
remove the release-candidate worktree from the development machine. From
within the repository, on the development machine (never inside
Sandbox):

```
git worktree remove C:\DiscordTradersReleaseCandidate
git worktree prune
```

This removes both the checkout directory and its `git worktree`
administrative metadata entry. It does not affect the live development
working directory's branch, tracked files, or history in any way.
