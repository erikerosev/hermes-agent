# Hermes tool audit — 2026-06-22

Trigger: Apollo claimed `transaction-service` was "not in the Peak codebase" when it was.
Root cause: `search_files` (find-fallback) capped at 50 and reported `total_count=50` with NO
`truncated` flag → a truncated listing looked complete → the model concluded "absent".
Bug CLASS audited across all tools: **tool results that mislead the model into a confident-wrong
conclusion** (silent truncation, misleading counts, swallowed errors, exit-code ignored,
empty==absent, send/write reported done when it failed, security fail-open).

7 parallel audit agents read ~all of `tools/`. Findings below, by severity. ✅ = fixed 2026-06-22.

## FIXED (2026-06-22)
- ✅ **search_files file-search truncation** (`file_operations.py` `_search_files`): now reports true
  `total_count`, sets `truncated`, `next_offset`, and an actionable `hint`. (`_search_files_rg` already had it.)
- ✅ **content-grep truncation** (`_search_with_rg` / `_search_with_grep`, all output modes): `fetch_limit`
  was `limit+offset` so `truncated = total > offset+limit` could NEVER fire; `files_only`/`count` never set
  it at all. Now `fetch_limit = limit+offset+1(+200 ctx)`, all 3 modes set `truncated`/`next_offset`. This
  is the EXACT Peak bug on the most-used "where is X used / defined" path. Verified live (gateway has no rg
  → grep-fallback is the live path; `import` search on peak/Payment now → truncated:True + hint).
- ✅ `SearchResult.to_dict` emits `truncated`, `next_offset`, and a hint: "INCOMPLETE result … do not
  conclude something is absent … call again with offset=N or narrow path/pattern."

## FIXED — batch 2 (2026-06-22, 837/837 targeted tests green, gateway restarted)
- ✅ **terminal background lies** (`terminal_tool.py`): no longer reports `exit_code:0/error:None`. Local
  spawns get a 0.25s grace then report REAL state — `status:"exited"`+real code+error msg on instant death,
  else `status:"started", exit_code:null` (+warning if no PID). Success is no longer implied.
- ✅ **remote execute_code stderr** (`code_execution_tool.py`): script run with `2>&1` → tracebacks now
  reach the model (were discarded).
- ✅ **process_registry env-poller** (`process_registry.py`): inconclusive PID check (unreadable pid file)
  no longer fabricates `exit_code:-1` for healthy jobs — keeps polling, bounded retry (~10s) then exits with
  `exit_code:None` (unknown, not -1).
- ✅ **delegate max_iterations** (`delegate_tool.py`): a child that produced output but hit the iteration
  cap is now `status:"incomplete"` (+warning) instead of `"completed"` — parent no longer treats partial as done.
- ✅ **multi-chunk send partial** (`send_message_tool.py`): a later-chunk failure after earlier chunks
  delivered now returns `partial:true, chunks_delivered, chunks_total` (all 5 platform loops) → no blind-retry dup.
- ✅ **cron create verify-after-write** (`cronjob_tools.py`): re-reads the store (`get_job`) before claiming
  success; a non-persisted job now errors instead of reporting "scheduled".

DEFERRED for Erik's call (security/UX decisions): tirith_security fail-open default; Camofox browser
blank-page guards. Remaining HIGH below + the MED list are still open.

## HIGH — not yet fixed
- **file_operations.py:1699-1701 / 1801-1803** — search errors swallowed: `_exec` merges stderr into stdout
  AND `ExecuteResult` has no `stderr` field, so `exit_code==2 and not stdout` never fires on a real error;
  an invalid regex returns `total_count=0` (empty==absent). Care: grep exit 2 can co-occur with partial
  matches, so don't blindly error on exit 2 — capture stderr separately and surface it.
- **terminal_tool.py:1949-1955** — `background=true` ALWAYS returns `exit_code:0, error:None,
  "Background process started"` even when the command instantly dies. Model thinks a dead server is up.
- **process_registry.py:744** — env-poller treats "PID file unreadable" as "exited", fabricates
  `exit_code:-1` for HEALTHY background jobs (and collides with real -1 exits).
- **code_execution_tool.py:936-1027** — remote `execute_code`: returncode read from the shell WRAPPER not
  the script → a crashed script can report `status:success`; AND stderr/traceback discarded entirely
  (remote path has no `2>&1`). The "errored script looks successful" failure Erik flagged.
- **delegate_tool.py:1620-1628** — a subagent that hit `exit_reason=max_iterations` is stamped
  `status="completed"` whenever its last message is non-empty → parent treats partial/failed work as done.
- **delegate_tool.py:618 / skills_hub.py:2020** — skill downloads silently drop fetch-failed files (some
  blobs 403) with no `incomplete` flag → a skill missing `references/foo.md` looks identical to one that
  never had it (absent vs truncated).
- **send_message_tool.py:707-757** — multi-chunk send: a later chunk failing returns a bare error AFTER
  earlier chunks already reached the user → model believes nothing sent → blind retry = duplicate delivery.
- **cronjob_tools.py:357-389** — create/update report success from the in-memory return value without
  re-reading the store; a silent persistence failure → "scheduled" but never fires. (kanban_create already
  does the verify-after-write re-read — copy that pattern.)
- **browser_camofox.py:343/379/409/598** — Camofox backend re-incarnates the blank-page bug: `navigate`
  fabricates `{ok:true}` for new tabs + swallows snapshot exception; `snapshot` returns `""` as success;
  `vision` ships a blank/placeholder screenshot to the vision model with no size guard.
- **browser_tool.py:2402-2405** — LLM-summarized snapshots returned with no "lossy summary" flag → a
  task-focused summary that drops the asked-about content → model reports present content as ABSENT.
- **tirith_security.py:733-756** — security scanner FAILS OPEN by default on timeout/spawn-fail/odd-exit
  (`tirith_fail_open=True`) → an unscanned or hung-scan command is reported to approval as a clean `allow`
  with no `degraded` flag. Also rc==0 with an error payload in stdout still → allow.
- **image_generation_tool.py:773-784** — `num_images>1` requested but only the FIRST image returned, with
  `success:true` and no count → model says "here are your 4 images" showing one.

## MEDIUM (selected)
- session_search_tool.py:533 — `sessions_searched` reports the capped count (≤limit) as the total distinct
  sessions matched. web_tools.py:1255 — web_crawl hard-caps at 20 pages, no "more" flag.
- web_tools.py:1093 — web_extract: backend failure (auth/rate-limit) collapses to "inaccessible or not
  found" (empty==absent). web_tools.py:399-417 — summarization failure silently returns first 5000 chars
  as if it were the content.
- vision_tools.py:854-866 — empty vision response → `success:true` with an error string as the "analysis".
- transcription_tools.py — empty transcript → `success:true` for 4/5 providers (only xai errors).
- tts_tool.py:1382-1614 — ffmpeg-absent path renames WAV bytes to `.ogg`/`.mp3` and reports
  `voice_compatible:true` (misnamed container). tts truncation has no `truncated` flag.
- checkpoint_manager.py:761-816 — `git checkout <commit> -- <path>` exits 0 even if the path didn't exist
  in that commit → "restored" reported with zero files changed.
- patch_parser.py:557-561 — addition-only hunk whose context_hint isn't found silently appends at EOF and
  reports success (wrong location). fuzzy_match.py:480 — 50%-similar-lines threshold can replace an
  unintended block on a short old_string.
- discord_tool.py:409-466 — pin/delete/role mutations report `success:true` from "no exception" without
  checking HTTP status. registry.py:405 — non-string handler returns not caught.
- mcp_oauth.py:439-450 — `_wait_for_callback` raises instead of polling the existing server on bind
  conflict → a completing auth misreported as timeout.

## LOW: read_file `wc -l` off-by-one on no-trailing-newline files (can hide last line with truncated=False);
lazy_deps version-parse failure treated as "satisfied"; misc dead branches. See agent outputs for full list.

## Cross-cutting fix pattern
Most of these reduce to two disciplines: (1) **never present a capped/partial/failed result as complete
or successful** — carry an explicit `truncated`/`partial`/`degraded`/`incomplete` flag + actionable next
step; (2) **verify-after-write** (re-read the store / check status) before claiming a send/create/restore
succeeded. The `SearchResult` hint + kanban_create's re-read are the reference implementations.
