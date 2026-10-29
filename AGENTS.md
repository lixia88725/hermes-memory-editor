# Hermes Memory Editor

Local, profile-aware Markdown context editor with capacity checks and recoverable writes. Read [README.md](README.md) for an overview and [GUIDE.md](GUIDE.md) for operation, interfaces, dependencies and verification; this is not a general file manager or a runtime configuration editor.

## Modules and ownership

- `server.py`: explicit Profile discovery/resolution, four-document read/write interface, capacity, authentication, target binding, locks and verified backups. `launcher.py` starts it and reports macOS startup failures.
- `local_history.py`: `LocalHistory` stores target-bound snapshots in a separate local bare `history.git`; listing/preview are read-only. Source control for this project must never include that repository.
- `editor.html`: document controller, draft/epoch state, Agent tabs, loading, Save and history comparison/restore. `vditor-adapter.js` exposes `createDocumentEditor` (value, disabled and composition interface); `vditor-editor.css` owns document typography. Rendering must not take ownership of persistence.
- `vendor/` and `notices/`: pinned upstream runtime, hashes, provenance and licenses. Preserve upstream bytes/notices; audit additions before allowlisting them.

For module/interface design or restructuring, load the `codebase-design` skill if available. Keep interfaces small and test through existing seams; do not invent deployment splits merely to divide work.

## Safety and validation

- Develop with synthetic Profile homes/workspaces and isolated editor storage in scratch. Run `/usr/bin/python3 test_history.py` and `/usr/bin/python3 test_loading.py` (prerequisites and coverage in README). Do not exercise saves/restores against production data. Production inspection is read-only; service restarts, deployment, network changes and real document edits require separate authorization.
- Preserve explicit Profile selection and the four resolved file keys, missing-file refusal, capacity enforcement, last-read hash plus `target_id` conflicts, symlink/parent rebinding defenses, canonical/runtime locks, verified pre-overwrite backup/history, atomic replacement and honest post-write warnings. History preview must not write; restore requires confirmation and appends history rather than resetting it.
- Preserve token/Host/Origin checks, static-file allowlisting, CSP and local/private-only networking. Never widen exposure or enable remote editor resources implicitly.
- Preserve loaded Markdown exactly on mount, unedited mode switches and no-op saves (including BOM/CRLF/no final newline). Actual IR edits may normalize Markdown; retain the warning and Source escape hatch. Do not claim arbitrary lossless IR round trips.
- Frontend refinements preserve the adopted quiet four-panel workspace, folder-style Agent tabs, accessible icon controls, compact typography, light/dark/mobile behavior, draft-safe switching, busy/composition gates, atomic profile reveal and stable current-disk/historical comparison direction. Do not replace accepted behavior with a redesign without approval; verify affected browser scenarios.

## Source-only publication

`.gitignore` denies new files by default. Audit and explicitly allowlist new source files; never force-add runtime data. Exclude `history.git`, backups, locks, runtime token JSON, logs, credentials/env/config, caches, app binaries, obsolete editor assets and actual user context documents (even in a private repository). Never copy ancestor/home `AGENTS.md` into this repository. Do not assign a license to original code without the owner's consent.
