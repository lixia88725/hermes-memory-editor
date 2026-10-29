#!/usr/bin/env python3
"""macOS launcher helper: run the editor and surface startup failures."""

import subprocess
import traceback
from pathlib import Path

import server

LOG_PATH = Path("/tmp/hermes-memory-editor.log")


def show_error(message: str) -> None:
    safe = message.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'display dialog "' + safe + '" buttons {"OK"} default button "OK" '
        'with icon stop with title "Hermes Memory Editor"'
    )
    subprocess.run(["/usr/bin/osascript", "-e", script], check=False)


def main() -> None:
    try:
        code = server.main()
        if code:
            show_error(f"Hermes Memory Editor could not start. See {LOG_PATH}")
    except Exception:
        LOG_PATH.write_text(traceback.format_exc(), encoding="utf-8")
        show_error(f"Hermes Memory Editor failed to start. See {LOG_PATH}")


if __name__ == "__main__":
    main()
