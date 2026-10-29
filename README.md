# Hermes Memory Editor

**English** | [简体中文](README.zh-CN.md)

**View, edit, and restore your Hermes Agent context files in a browser.**

If you use multiple Hermes Agents and want to adjust their memories, user preferences, behavior, or workspace rules, this tool brings the relevant files together on one page—without hunting through folders or switching editors.

This is an independent tool built for personal use. **It is not an official Hermes product or a chat client.**

## Preview

![Hermes Memory Editor — four-panel Markdown editor with Agent tabs](docs/images/editor-demo.png)

*Actual application UI with fictional English sample content and generic paths. No personal documents or real Agent configurations are shown.*

## What can it do?

Each Agent has four side-by-side editing panels:

| File | What it manages |
|---|---|
| `MEMORY.md` | Long-term memories stored by the Agent |
| `USER.md` | Information about the user and their preferences |
| `SOUL.md` | The Agent's behavior and communication style |
| `AGENTS.md` | Rules in the root of the Agent's configured workspace |

- **Switch Agents with tabs.** Select a Profile using folder-style tabs.
- **Edit formatted Markdown in place.** Headings, lists, and tables render as you edit. Switch to source mode whenever needed.
- **Check capacity.** See MEMORY and USER character budgets before saving.
- **Recover earlier versions.** Changed saves create local Git history. Compare versions and explicitly confirm a restore.
- **Avoid accidental overwrites.** Get a reminder before switching with unsaved edits, and a conflict warning if another program has changed the file.
- **Keep documents local.** Editor assets ship with the repository. Document versions stay on your machine and are not automatically uploaded to GitHub.

## Getting started

The current version is developed and tested primarily on **macOS with Chromium**. It is not yet a cross-platform, one-click application.

You will need:

- An installed and configured [Hermes Agent](https://github.com/NousResearch/hermes-agent). The current implementation depends on internal Profile discovery code at `~/.hermes/hermes-agent` and PyYAML in that installation's `venv/bin/python`.
- Python 3 and Git.
- Existing context files. The editor does not automatically create missing files.

```bash
git clone https://github.com/lixia88725/hermes-memory-editor.git
cd hermes-memory-editor
python3 launcher.py
```

The launcher opens the local editor in your browser. Choose an Agent, edit its documents, and click **Save** or press **⌘/Ctrl + S**.

> A different Hermes installation path or version may require adaptation. This repository contains source code, not a packaged macOS `.app`. See the [detailed guide](GUIDE.md) for startup behavior, dependencies, and optional private Tailscale access.

## Before you use it

- **Save changes real files on your machine.** Prefer editing while the corresponding Agent is stopped, then start a new session. Saving does not update an already-running conversation's context.
- **Shared workspaces mean shared rules.** The editor flags Agents that use the same `AGENTS.md`. Changes affect every Agent using that file.
- **Rendered editing may normalize Markdown.** Actual edits can change blank lines, list indentation, or table alignment. Opening a document or saving without changes does not rewrite it.
- **Version history is not a full backup.** It captures versions during editor saves, not every change made by other programs, and cannot protect against disk failure.
- **Do not expose the editor directly to the public internet.** It is intended for localhost or a trusted private network, not multi-user hosting.
- Real Chinese input-method candidate windows, Safari, and physical touchscreen devices still need further validation.

## Development and verification

The backend uses Python's standard-library HTTP server. The frontend uses HTML, CSS, JavaScript, and [Vditor](https://github.com/Vanessa219/vditor). No additional database is required.

After preparing the test dependencies described in the guide, including Playwright and Chromium:

```bash
python3 test_history.py
python3 test_loading.py
```

Tests use isolated sample files. Do not use real Agent documents for save or restore tests.

- [Detailed usage and maintenance guide](GUIDE.md)
- [Agent development guidelines](AGENTS.md)
- [Third-party dependencies and licenses](notices/THIRD-PARTY-NOTICES.txt)

## Privacy and licensing

This repository contains application source, tests, and required dependencies. **It does not contain personal memories, user profiles, document history, backups, or access tokens.** The application's source repository and its runtime `history.git` are separate; never commit the latter.

Third-party dependencies retain their respective licenses. The project's original code does not yet have an open-source license; making the repository public does not grant unrestricted rights to copy, modify, or redistribute it.
