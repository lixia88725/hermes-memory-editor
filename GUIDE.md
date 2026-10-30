# Hermes Memory Editor

A small, local safety editor for one Hermes profile's `MEMORY.md`, `USER.md`, and `SOUL.md`, plus the root `AGENTS.md` of its configured workspace.
Obsidian remains the main multi-profile document browser; this tool is the profile-aware capacity and recovery fallback.

## Start

On an installation that already includes the macOS app, double-click:

```text
Hermes Memory Editor.app
```

The app launcher opens an existing instance or starts a local instance. The app binary is not included in this source-only repository; use `launcher.py` below from a source checkout. Standalone local instances exit after five minutes without authenticated activity. An optional installed background instance described below stays running.

Default profile:

```bash
python3 launcher.py
```

A named profile:

```bash
HERMES_HOME="$HOME/.hermes/profiles/<profile>" python3 launcher.py
```

The page shows the resolved profile, `HERMES_HOME`, configured workspace, Memory/User enablement, and effective Hermes capacity. Per-file paths stay hidden unless a file is unavailable or linked elsewhere.

## Agent selection

The folder-style Agent tabs below the toolbar read the installed Hermes Profile discovery helpers. Use the adjacent refresh icon to refresh the list. Arrow keys/Home/End move tab focus; Enter/Space activates the focused tab. Selection affects only this editor, not Hermes's default Profile, running agents, or sessions.

All read/save/restore requests carry an explicit Profile. Browser tabs can select different Profiles independently. Switching with unsaved changes offers Save and switch, Discard and switch, or Cancel; saves, restores and reads temporarily lock the Agent tabs. If saving fails or newer edits remain, switching is cancelled. Profile loading fetches metadata and the four documents concurrently, then reveals settled results together; a failed document remains unavailable rather than becoming an editable empty file.

AGENTS.md is resolved only from an explicit absolute `terminal.cwd`. If missing, its panel is unavailable; no fallback to Default's workspace. Profiles sharing the same resolved AGENTS.md receive a shared-workspace warning. Editing this shared file affects all consumers of that workspace.

Saving updates files on disk, not an existing conversation's frozen prompt. Start a new Agent session to load updated context.

## Dream — nightly memory review

**Dream** opens one two-pane view: choose an Agent in the table, edit its enablement and nightly time directly as 24-hour `HH:MM`, and select saved action modules in the detail pane. Switches and module choices save automatically; a complete valid time saves after 500 ms, or on blur/Enter. There is no Apply button or save confirmation. Incomplete/invalid time stays local with a warning; changing the switch can still save using the last saved time. Empty module selection is invalid and is never submitted. A disabled Agent with missing context files may save time/module preferences without creating a job or missing documents, but cannot be enabled until its prerequisites are valid.

Per-Agent status shows 保存中 / 已保存 / 未保存 · 失败. Saves are serialized and verified by reading back the exact Agent before advancing its revision. Editing and Agent switching stay available; newer edits are retained and coalesced. Failed/conflicting saves retain drafts and stop automatic retries; use the row's error-only retry action for a temporary failure. Conflicts or uncertain readback may require reopening to inspect actual saved state; retry never bypasses the backend revision check. Closing flushes pending valid time changes and waits for save/readback to settle (click Close again afterward). Invalid/failed unsaved drafts require an explicit discard warning; browser unload also warns while changes are pending.

Open **最终提示词** to generate the preview (or read the unchanged existing prompt); loading and errors appear inside this single disclosure. Module selection changes clear stale previews. Saving changed module selections automatically regenerates the prompt; schedule-only or enablement-only saves preserve the existing prompt. Returning to the saved module selection before it is submitted preserves it too; once a changed selection has already saved, returning to an earlier selection composes anew. New jobs compose their prompt from the selected actions. Deduplication and concision are the initial modules. The shared module list is read-only in this frontend; its stored definitions and backend library API remain intact.

This reuses the installed official Hermes Web UI's context-local cron-store helper and native job mutations/scheduler registration, **not the React frontend**. There is no new scheduler or dashboard token. The row lightning action runs one saved-enabled managed task; the top lightning action confirms the explicit list of all saved-enabled Agents. Both are unavailable while any settings save/readback or unsaved invalid/failed draft remains, so neither submits an older configuration.

Run-now uses the official canonical gateway `cron.submit` admission contract with the exact managed ID and Profile, attaching only to a discovered ready owner (`connect_gateway(endpoint=...)`); it never starts a gateway or invokes deprecated dashboard execution helpers. The separate read-only trigger adapter rechecks the job revision and enabled state under the existing cron lock, performs no cron writes, and does not move the nightly time/next scheduled occurrence. Opening, previewing and denied requests never edit memory/configuration. An explicitly confirmed task **can modify that Agent's USER.md / MEMORY.md** once the gateway executes it. Results distinguish accepted, failed and unknown; acceptance is read back once with the exact `cron.status` receipt (no background polling) and is not completion. Transport uncertainty is not automatically retried. The UI suppresses further submissions for accepted/unknown Agents until page reload; inspect Hermes before manually retrying. A missing/incompatible ready canonical gateway is a clear refusal, with no API-server or ephemeral-process fallback. This path is independent of the Chronos NAS-only JWT fire webhook, and avoids the API-server `/run` operation that changes `next_run_at` and can resume paused jobs.

Enabled state and ticker heartbeat are shown separately; resuming an overdue job follows Hermes' catch-up policy. Recent report bodies are not currently exposed.

Only the built-in scheduler is supported. Both USER and MEMORY missing, an existing unreadable memory file, disabled memory, changed Profile/workspace bindings, stale cron reads and missing managed jobs block unsafe application. One readable USER or MEMORY is enough (an empty existing file counts); generated prompts skip the absent file and do not create it. SOUL/AGENTS are optional references. Existing prompts are not silently migrated; old prompts may require updating via action choices. Batch results are per Agent, not transactional across Agents. Cron memory writes use Hermes' native memory tool; editor version history does not capture all scheduled writes and is not an unattended-backup guarantee.

Before serving an installation that already has a Default Dream job, set `HERMES_EDITOR_DREAM_DEFAULT_JOB_ID` to its explicitly approved exact ID in the **server process environment**. The ID is not shipped in source. Without that binding, Default is unavailable rather than creating a possible duplicate. Other Profiles have no Dream job until explicitly enabled/applied; similarly named jobs are never adopted. The editor-private `dream-state.json` stores module definitions, selected modules, Profile binding hashes and owned IDs only after explicit saves. It is ignored by Git and never served as a static asset. Keep it with local runtime data; do not copy it into another installation. Rebinding failures need operator review, not edits to `jobs.json`.

Dream compatibility depends on the installed internal Hermes modules under `~/.hermes/hermes-agent`; failure is surfaced rather than replaced with a separate scheduler. The short-lived venv adapter denies writes during reads/previews (including config backup side effects of imports), and permits apply-time filesystem writes only inside the selected Profile's cron directory.

## Safety behavior

- The write API accepts only the four resolved context files.
- Missing or unreadable files are never created or overwritten implicitly.
- Every save/restore carries both the content hash and target identity from the last successful read. Workspace, Profile-home or link rebinding causes a conflict, even if file contents match.
- Every changed save creates a verified backup before replacing the original and records local Git versions. Unchanged saves do not rewrite the document or create redundant versions.
- Writes share a canonical-target editor lock across aliases; Profile context files also take MemoryStore-compatible alias/target `*.md.lock` locks. AGENTS locks stay inside the tool. Writes use a pinned parent directory, temporary file, `fsync`, atomic replacement, and full byte-for-byte verification. External applications that ignore these locks are not fully serialized.
- Saving through a symbolic link resolves and replaces the real target, preserving the link.
- The page tracks edits made while a save is in flight and keeps them marked unsaved.
- Reload/Restore freeze their edit area until completion; Profile transitions freeze all panels. Buttons and keyboard shortcuts obey the same busy gate.
- Closing with unsaved changes triggers a browser warning.
- API calls require a random per-process token. Writes also validate Host, Origin, content type, and request size.

## Capacity

For `MEMORY.md` and `USER.md`, the editor reads these values from the active profile's `config.yaml`:

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 2200
  user_char_limit: 1375
```

Usage follows Hermes's entry model: trim non-empty `§`-delimited entries, remove exact duplicates in first-seen order, then count the normalized joined content. The original text is not silently normalized or rewritten.

`SOUL.md` and `AGENTS.md` have no Memory budget. `AGENTS.md` is resolved from `terminal.cwd` and labeled as workspace context; both files still consume prompt context.

## Local versions and recovery

Each panel's **History** button opens that bound document's version list with dates, action labels and notes. The comparison has stable, labeled panes: **当前已保存版本** on the left (disk, never an unsaved draft), **所选历史版本** and its timestamp on the right. Desktop shows them side by side; phones stack current above historical. **完整内容** is selected by default; the visible **只看差异** segment hides unchanged lines. Neutral lines are unchanged, light green lines carry **当前新增**, and light red lines carry **历史中有，当前已移除**. Changes describe the direction from historical to current, not the inverse restore operation. Documents and notes render as text, never HTML, and raw Git metadata stays out of the UI. Empty histories and identical versions have explicit states.

The current baseline is captured when opening history and checked against each preview's current disk hash. Switching versions changes the selected historical content, not the baseline. If disk changes during comparison, further selection reports that the baseline is stale and disables restore until history is reopened. Opening history and previewing versions are read-only: they do not initialize Git, create backups or save context files. An empty history is normal until the first changed save.

**恢复此版本…** requires explicit confirmation naming the target path and selected timestamp and explaining that current saved content will be retained. A file with unresolved unsaved drafts cannot be restored: the draft is preserved and the user must first save or explicitly handle it outside history. Restore uses the same last-loaded content hash and `target_id` conflict checks as Save; preview never adopts a newer write base. A newer disk file or rebound target causes a conflict, not an overwrite. A restore appends a new version, including when restoring identical bytes; it never resets or rewrites Git history. The current disk content is recorded before either a save or restore, so externally edited current text remains recoverable.

There is one dedicated local bare repository, created lazily on the first changed save:

```text
~/.hermes/memory-editor/history.git/
  # Git tree paths: snapshots/<target_id>/<memory|user|soul|agents>.md
```

No Profile or workspace directory is Git-initialized. Only the four explicit context documents are eligible; configuration, credentials, sessions and other files are never staged. Git uses plumbing commands, no working tree/index, no hooks or inherited global/system Git configuration, and no remote/network operations. The repository root is private (`0700`); local context snapshots are not encrypted separately from the disk. Target identities preserve Profile-home, workspace generation, key and link bindings: another target cannot list, preview or restore these versions even when names or contents match. Shared workspace files retain their existing canonical lock and shared-file warning; version lists are bound to the selected Profile's identity.

Version capture is **not a watcher**. Intermediate edits made outside this editor are not recorded automatically; only the bytes present just before an editor overwrite and the successful result are captured. Reading or reloading alone does not capture a version. Pre-overwrite entries may remain after an aborted save. Git history is append-only with no automatic retention/pruning in this first release; legacy backup retention is separate. The version list currently reads the local commit log without pagination.

If pre-overwrite Git recording fails (including missing Git), the editor fails closed and does not replace the document. If the document was already atomically saved but recording the new Git version then fails, the API reports the real successful save with an explicit `history_warning`; the UI shows that warning rather than pretending the file was not saved. The pre-overwrite version and verified backup remain available. Fix local Git/disk access before further edits; no background retry is claimed.

API additions (all retain the existing token, Host, explicit Profile and write-Origin checks):

- `GET /api/history/<key>?profile=...&target_id=...` → versions newest first, plus read-only `current_content` and `current_hash` for the baseline (also available for empty histories).
- `GET /api/preview/<key>?profile=...&target_id=...&revision=...` → selected `content`, legacy `diff`, `current_content`, `current_hash`, and semantic `comparison` line opcodes, without writing. Both text panes and opcodes use the exact same disk read; opcodes are `[tag, current_start, current_end, historical_start, historical_end]` over `splitlines(keepends=True)`, with `equal`, `replace`, `delete`, or `insert` tags.
- `POST /api/restore-version` with `profile`, `key`, `target_id`, `base_hash`, `revision`, and `confirmed: true`.
- `POST /api/save` optionally accepts a `note` string (at most 200 characters). Notes are displayed safely as text. The compact first-release UI does not prompt for save notes.

### Existing backups

Backups are stored under:

```text
~/.hermes/memory-editor/backups/<profile>/<memory|user|soul|agents>/<target_id>/
```

Up to 20 backups per bound target are retained after successful commits. Restore reads the source before creating the rollback backup and protects both during retention. The previous `POST /api/restore` backup API remains compatible; the panel now offers Git History rather than Restore latest. Existing backups are neither deleted nor imported into Git automatically.

Legacy backups directly under the file category remain on disk, but are not offered for automatic restore because they have no verified target binding. They are not deleted or silently migrated.

Configuration YAML is parsed by PyYAML from the existing Hermes venv; no package was installed. Parser/type failures make documents unavailable rather than guessing settings.

## Markdown editing

The four-panel workspace uses **Vditor 3.11.2 in-place (IR) editing**, with an accessible per-document icon toggle: code brackets switch to Markdown Source, and the pencil switches back to rendered IR editing. Reload and History also use labeled icon controls with tooltips. Agent tabs, refresh, per-document Reload/History, Git comparison/restore and all write APIs use the same document controller; no trial localStorage repository or browser-history store is used.

- The adopted compact document typography is body 13px, H1 19px, H2 16px; decorative heading badges are hidden. The outer UI keeps quiet surfaces, compact icon controls and responsive light/dark layouts.
- Mounting, unedited Source/IR switching, no-op Save and IR undo back to the loaded baseline preserve the exact loaded string, including CRLF and missing terminal newline. No-op API submissions also preserve an existing UTF-8 BOM instead of stripping it. No-change saves produce no history or backup.
- **Actual IR editing can normalize Markdown across the document**: CRLF → LF, final newline, blank lines, list indentation and table padding may change. Only after actual IR changes does the unsaved status disclose `IR may normalize Markdown`. Storage remains Markdown; no claim of arbitrary lossless round trips is made. Source input itself follows the browser's newline normalization once edited.
- Toggling Source/IR preserves draft/dirty state but reloads the IR undo baseline; undo is verified within each mode, not as one continuous cross-mode undo stack.
- Save synchronously flushes current editor content, refuses active/settling text composition and freezes all edit areas for the request. Existing epoch/version/content checks remain as a second defense against adopting stale responses. Read/restore/Profile gates and disabled/unavailable behavior also apply to both modes.
- Vditor and required runtime resources are pinned and vendored locally. Typography auto-space/term rewriting, automatic links, uploads, file-paste/drop ingestion, remote image transfer, media, math/diagram preview and code-highlight downloads are disabled. CSP allows only same-origin API connections and blocks both remote and same-origin document image loading (`img-src data:`); image Markdown is retained, not downloaded or proxied.
- Vditor is MIT, bundled diff-match-patch is Apache-2.0, bundled Lute 1.7.6 is Mulan PSL v2. Full licenses and provenance are retained under `vendor/` and `notices/`; `vendor/SHA256SUMS.json` covers those shipped files (excluding the manifest itself). Runtime files are byte-identical to the approved trial's hash manifest. This is a bounded dependency review, not a full sanitizer/CVE audit.

## Files

| File | Purpose |
|---|---|
| `server.py` | Authenticated local server, safe writes, capacity and backups |
| `local_history.py` | Local-only Git snapshot storage and read-only previews |
| `editor.html` | Four-panel browser editor |
| `test_history.py` | Isolated real-backend and Chromium integration regression suite |
| `test_loading.py` | Config projection/cache, concurrent loading and stale-response regressions |
| `launcher.py` | Starts the server and surfaces startup failures on macOS |
| `vditor-adapter.js` | Exact-source / IR adapter, composition and freeze gates |
| `vditor-editor.css` | Approved compact document typography |
| `vendor/`, `notices/` | Pinned Vditor runtime, dependency hashes and full upstream licenses |
| `AGENTS.md`, `.gitignore` | Project maintenance rules and audited source-only allowlist |

## Verify

```bash
python3 -m py_compile server.py local_history.py launcher.py test_history.py test_loading.py
node --check vditor-adapter.js
python3 -c 'import re; print(re.findall(r"<script>(.*?)</script>", open("editor.html").read(), re.S)[0])' | node --check
/usr/bin/python3 test_history.py
/usr/bin/python3 test_loading.py
```

The durable suite starts its own ephemeral loopback server, synthetic Profile homes/workspaces, backups and bare Git repository entirely under `~/.hermes/cache/scratch/memory-editor-history-test-*`. It reads the shipped assets and installed YAML parser, uses installed Git and Playwright/Chromium (no package installs), and never calls the production save/restore endpoints. It covers all four documents, Profile/target isolation, external content preservation, stable two-pane current-disk/historical comparisons, selected full/changes modes, semantic labels, empty and identical versions, CRLF/Unicode line boundaries, escaped HTML-like documents/notes, dirty-draft restore blocking, and a full-file fingerprint proving previews do not write. It also covers explicit/cancelled restore, stale-hash conflicts, concurrent saves, symlink/mode preservation, API security/budgets, legacy restore compatibility, Git failure reporting and responsive light/dark browser interactions (including long changed text and reachable phone controls). It leaves synthetic fixture/screenshots/geometry in scratch for inspection; normal scratch retention applies. No production restart, launchd or network configuration changes are part of this test command.

The Vditor scenario additionally exercises exact BOM/CRLF/no-final-newline mount/no-op/API behavior, rendered headings/code/links/tables/Unicode/§, Source and IR edit/undo/selection/paste, synthetic composition and settled-input save gates, a delayed real-backend save with disabled editing and `Saving…` feedback, pre-save input flushing, recoverable Git failure, effective entry budgets and over-budget Save-and-switch rejection, Profile cancel/discard/save transitions, missing-file read-only recovery, heartbeat reload, external conflicts, exact-byte Git restore back into IR, image CSP and no extra math/diagram downloads. Screenshots cover desktop and phone light/dark. Physical macOS/iOS Chinese IME candidate windows, real touch devices, Safari and screen-reader use remain manual acceptance items; synthetic composition and narrow Chromium viewports do not certify them.

The loading suite additionally checks bounded, content-keyed config projections (never cached document bodies), concurrent parse sharing, immediate rebinding despite equal size/mtime, uncached failures, concurrent document reads, one-paint reveal with partial failure and stale-epoch rejection. Tests require the existing Hermes installation at `~/.hermes/hermes-agent`, its `venv/bin/python` with PyYAML, installed Git and Playwright/Chromium available to the test interpreter. Profile discovery uses that installation's internal helpers; a source checkout is not a standalone Hermes distribution.

## Optional private dashboard access

A private dashboard can have a top-right gear linking from `http://<TAILSCALE_IPV4>:<DASHBOARD_PORT>/` to `http://<TAILSCALE_IPV4>:8877/` in a new tab. The existing dashboard integration uses the unmodified Heroicons `cog-6-tooth` SVG (MIT); keep its license with that dashboard's assets. Dashboard files are separate from this source repository.

An operator-managed `~/Library/LaunchAgents/<YOUR_LAUNCHAGENT_LABEL>.plist` can run `server.py` with `HERMES_EDITOR_DAEMON=1`, `HERMES_EDITOR_PORT=8876`, and `HERMES_EDITOR_REMOTE_ORIGIN=http://<TAILSCALE_IPV4>:8877`. Use an actual Tailscale IPv4 address, not the placeholder. One editor process listens on loopback 8876 and the configured Tailscale IPv4 address 8877; it must never bind the LAN address or all interfaces. Incoming remote requests require the exact configured Host and a Tailscale-range source address. The private page bootstraps a per-process token; writes still require that token and the exact Origin. No CORS access is granted to the dashboard or library pages. The LaunchAgent and its local configuration are not shipped here.

This mode trusts devices admitted to the operator's tailnet under its network access policy; it is not a separate per-user login system. HTTP travels inside Tailscale's encrypted network, but the browser does not show an HTTPS lock. The fixed IPv4 entry avoids dependence on MagicDNS/system proxy or Serve HTTPS configuration. Preserve unrelated Tailscale Serve routes; this mode needs no editor Serve/Funnel route.

LaunchAgent RunAtLoad/KeepAlive starts the editor at user login and restarts it after an exit. This is not a pre-login system daemon. Changes to the Tailscale IP require updating the configured origin and homepage link. Do not expose this editor via a public tunnel, LAN-wide bind, or router port forwarding.

For deployment verification, use browser save/restore/conflict checks only against isolated scratch files. Keep authenticated checks against the installed production instance read-only; never use actual context documents as test fixtures.

## Source-only repository

The root `.gitignore` denies files by default and explicitly allows the audited source, tests, documentation, vendored runtime and notices. Audit new files before extending it. Never stage `history.git/` (it holds real context snapshots), backups, locks, runtime token JSON, logs, env/config, caches, actual context documents or macOS app binaries. Obsolete OverType/LDT assets and the unused icon binary are not part of the source export. Do not copy a home/workspace `AGENTS.md` into the repository; the project file is independently authored maintenance guidance. Keep local deployment files outside source control, even for a private remote. Third-party licenses do not assign a license to the project's original code.
