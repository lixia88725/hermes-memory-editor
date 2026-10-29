#!/usr/bin/env python3
"""Safe local editor for a Hermes profile and its root workspace context."""

from __future__ import annotations

import difflib
from collections import OrderedDict
import fcntl
import hashlib
import hmac
import ipaddress
import http.client
import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from local_history import LocalHistory

APP_ID = "hermes-memory-editor"
EDITOR_DIR = Path(__file__).resolve().parent
HTML_PATH = EDITOR_DIR / "editor.html"
STATIC_FILES = {'vditor-adapter.js': 'text/javascript; charset=utf-8', 'vditor-editor.css': 'text/css; charset=utf-8', 'vendor/vditor/dist/css/content-theme/dark.css': 'text/css; charset=utf-8', 'vendor/vditor/dist/css/content-theme/light.css': 'text/css; charset=utf-8', 'vendor/vditor/dist/index.css': 'text/css; charset=utf-8', 'vendor/vditor/dist/index.min.js': 'text/javascript; charset=utf-8', 'vendor/vditor/dist/js/i18n/zh_CN.js': 'text/javascript; charset=utf-8', 'vendor/vditor/dist/js/icons/ant.js': 'text/javascript; charset=utf-8', 'vendor/vditor/dist/js/lute/lute.min.js': 'text/javascript; charset=utf-8'}
ENTRY_DELIMITER = "\n§\n"
DEFAULT_LIMITS = {"memory": 2200, "user": 1375}
MAX_BODY_BYTES = 1_000_000
BACKUP_KEEP = 20
IDLE_SECONDS = 300
REMOTE_ORIGIN = os.environ.get("HERMES_EDITOR_REMOTE_ORIGIN", "").rstrip("/")
DAEMON_MODE = os.environ.get("HERMES_EDITOR_DAEMON") == "1"


def _profile_name(home: Path) -> str:
    default = (Path.home() / ".hermes").resolve()
    resolved = home.resolve()
    if resolved == default:
        return "default"
    if resolved.parent.name == "profiles":
        return resolved.name
    return resolved.name or "custom"


# Cache only validated editor fields, never document contents or whole YAML.
# Every call reads current bytes: inode/mtime aliases cannot hide a config edit.
_CONFIG_CACHE = OrderedDict()
_CONFIG_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE_LIMIT = 32


def read_memory_config(home: Path) -> Dict[str, Any]:
    try:
        raw = (home / "config.yaml").read_bytes()
    except FileNotFoundError:
        result = _default_memory_config()
        result["config_warning"] = "config.yaml not found; using Hermes defaults"
        return result
    except OSError as exc:
        result = _default_memory_config()
        result["config_error"] = f"Configuration unavailable: {type(exc).__name__}"
        result["config_warning"] = result["config_error"]
        return result
    digest = hashlib.sha256(raw).digest()
    # Serialize cache misses so simultaneous reads share one YAML subprocess.
    with _CONFIG_CACHE_LOCK:
        if digest in _CONFIG_CACHE:
            _CONFIG_CACHE.move_to_end(digest)
            return dict(_CONFIG_CACHE[digest])
        result = _parse_memory_config(raw)
        if not result["config_error"]:
            _CONFIG_CACHE[digest] = dict(result)
            if len(_CONFIG_CACHE) > _CONFIG_CACHE_LIMIT:
                _CONFIG_CACHE.popitem(last=False)
        return result


def _default_memory_config() -> Dict[str, Any]:
    return {
        "memory_enabled": True, "user_profile_enabled": True,
        "memory_char_limit": DEFAULT_LIMITS["memory"],
        "user_char_limit": DEFAULT_LIMITS["user"],
        "terminal_cwd": None, "config_warning": "", "config_error": "",
    }


def _parse_memory_config(raw: bytes) -> Dict[str, Any]:
    """Parse YAML with the installed Hermes interpreter; never guess on errors.

    The app launcher uses stdlib Python. Hermes' installed venv has PyYAML;
    importing the entire runtime utils module can require unavailable extras.
    Pass the bytes over stdin, not a path which could change during parsing.
    """
    result = _default_memory_config()
    try:
        interpreter = Path.home() / ".hermes/hermes-agent/venv/bin/python"
        command = "import sys,json,yaml; print(json.dumps(yaml.safe_load(sys.stdin.buffer.read())))"
        parsed = subprocess.run([str(interpreter), "-c", command], input=raw,
                                capture_output=True, timeout=15, check=True)
        config = json.loads(parsed.stdout)
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValueError("config root must be a mapping")
        for section in ("memory", "terminal"):
            if section in config and not isinstance(config[section], dict):
                raise ValueError(f"{section} must be a mapping")
        memory = config.get("memory", {})
        for name in ("memory_enabled", "user_profile_enabled"):
            if name in memory:
                if type(memory[name]) is not bool:
                    raise ValueError(f"memory.{name} must be a boolean")
                result[name] = memory[name]
        for name in ("memory_char_limit", "user_char_limit"):
            if name in memory:
                if type(memory[name]) is not int or memory[name] < 0:
                    raise ValueError(f"memory.{name} must be a non-negative integer")
                result[name] = memory[name]
        cwd = config.get("terminal", {}).get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
            raise ValueError("terminal.cwd must be a nonempty string or null")
        result["terminal_cwd"] = cwd
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        # Do not include subprocess stderr: it may echo unrelated config secrets.
        result["config_error"] = f"Configuration unavailable: {type(exc).__name__}"
        result["config_warning"] = result["config_error"]
        result["terminal_cwd"] = None
    return result


def file_paths(home: Path, workspace: Optional[Path]) -> Dict[str, Path]:
    return {
        "memory": home / "memories" / "MEMORY.md",
        "user": home / "memories" / "USER.md",
        "soul": home / "SOUL.md",
        "agents": workspace / "AGENTS.md" if workspace else None,
    }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_text(data: bytes) -> str:
    return data.decode("utf-8-sig")


def parse_entries(content: str) -> list[str]:
    entries = [part.strip() for part in content.split(ENTRY_DELIMITER) if part.strip()]
    return list(dict.fromkeys(entries))


def text_metrics(key: str, content: str, limit: Optional[int]) -> Dict[str, Any]:
    raw_chars = len(content)
    if key not in {"memory", "user"}:
        return {
            "raw_chars": raw_chars,
            "usage_chars": None,
            "limit": None,
            "entry_count": None,
            "canonical": None,
        }
    entries = parse_entries(content)
    normalized = ENTRY_DELIMITER.join(entries)
    return {
        "raw_chars": raw_chars,
        "usage_chars": len(normalized),
        "limit": limit,
        "entry_count": len(entries),
        "canonical": content.strip() == normalized,
    }


def _target_for_write(path: Path) -> Path:
    if path.is_symlink():
        return path.resolve(strict=True)
    return path


@contextmanager
def file_lock(path: Path, lock_path: Optional[Path] = None):
    lock_path = lock_path or path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def atomic_write_bytes(path: Path, data: bytes, mode: int, before_replace=None) -> None:
    """Pin the parent directory; a rebound parent cannot redirect the commit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    tmp_name = f".{path.name}.memory-editor-{secrets.token_hex(12)}"
    try:
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        if before_replace:
            parent_stat = path.parent.stat()
            pinned_stat = os.fstat(dir_fd)
            if (parent_stat.st_dev, parent_stat.st_ino) != (pinned_stat.st_dev, pinned_stat.st_ino):
                raise TargetChanged("Target parent changed before commit")
            before_replace()
        os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        with os.fdopen(fd, "rb") as handle:
            if handle.read() != data:
                raise OSError("full content verification failed")
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        os.close(dir_fd)


def _backup_dir(profile: str, key: str, target_id: str) -> Path:
    # Full identity isolates same-name homes, workspace generations and aliases.
    # Legacy profile/key/*.md are deliberately left untouched and unlisted.
    return EDITOR_DIR / "backups" / profile / key / target_id


def list_backups(profile: str, key: str, target_id: str) -> list[Dict[str, Any]]:
    folder = _backup_dir(profile, key, target_id)
    if not folder.is_dir():
        return []
    items = []
    for path in sorted(folder.glob("*.md"), key=lambda p: p.lstat().st_mtime_ns, reverse=True):
        st = path.lstat()
        if stat.S_ISREG(st.st_mode):
            items.append({"name": path.name, "mtime": st.st_mtime, "bytes": st.st_size})
    return items[:BACKUP_KEEP]


def make_backup(profile: str, key: str, data: bytes, target_id: str) -> str:
    folder = _backup_dir(profile, key, target_id)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    name = f"{stamp}-{secrets.token_hex(8)}-{sha256_bytes(data)[:10]}.md"
    target = folder / name
    atomic_write_bytes(target, data, 0o600)
    if target.read_bytes() != data:
        raise OSError("backup verification failed")
    # Retention runs only after a verified commit, never before reading restore.
    return str(target)


def trim_backups(profile: str, key: str, target_id: str, protected: set[str]) -> None:
    folder = _backup_dir(profile, key, target_id)
    backups = sorted(folder.glob("*.md"), key=lambda p: p.lstat().st_mtime_ns, reverse=True)
    keep = {p.name for p in backups if p.name in protected}
    for path in backups:
        if len(keep) >= BACKUP_KEEP:
            break
        keep.add(path.name)
    for old in backups:
        if old.name not in keep:
            old.unlink()


class TargetChanged(ValueError):
    pass


@contextmanager
def target_locks(path: Path, canonical: Path, runtime_locks: bool = True):
    """One canonical editor lock plus runtime-compatible alias .md.lock.

    Sort all lock names globally, regardless of profile or document key. The
    canonical .md.lock also coordinates ordinary runtime writes to the target.
    Hermes using another unknown alias only takes that alias's lock; such
    external aliases cannot be discovered globally by this application.
    """
    fingerprint = sha256_bytes(os.fsencode(str(canonical)))
    paths = {EDITOR_DIR / "locks" / f"{fingerprint}.lock"}
    # Workspace rules are not MemoryStore files; keep their locks inside the tool.
    if runtime_locks and path.name != "AGENTS.md":
        paths.update({path.with_suffix(path.suffix + ".lock"),
                      canonical.with_suffix(canonical.suffix + ".lock")})
    from contextlib import ExitStack
    # Parent-directory aliases can make differently spelled lock paths the
    # very same inode. Deduplicate their resolved names BEFORE flock, or this
    # process can block forever trying to lock its own separately opened fd.
    paths = {lock.resolve() for lock in paths}
    with ExitStack() as stack:
        for lock in sorted(paths, key=lambda p: str(p)):
            stack.enter_context(file_lock(canonical, lock))
        yield


class EditorState:
    def __init__(self, home: Path, token: Optional[str] = None):
        self.declared_home = home.expanduser().absolute()
        self.home = self.declared_home.resolve()
        self.profile = _profile_name(self.home)
        self.config = read_memory_config(self.home)
        configured_cwd = self.config.get("terminal_cwd")
        if configured_cwd:
            workspace = Path(os.path.expandvars(str(configured_cwd))).expanduser()
            if not workspace.is_absolute():
                self.workspace = None
                self.config["config_warning"] = "terminal.cwd must be an absolute path; AGENTS.md editing is disabled"
            else:
                self.workspace = workspace.resolve()
        else:
            self.workspace = None
            warning = self.config.get("config_warning", "")
            fallback = "Workspace not configured; AGENTS.md editing is disabled"
            self.config["config_warning"] = f"{warning}; {fallback}".strip("; ")
        self.paths = file_paths(self.home, self.workspace)
        self.token = token or secrets.token_urlsafe(32)
        self.port = 0
        self.server: Optional[http.server.ThreadingHTTPServer] = None
        self.timer: Optional[threading.Timer] = None
        self.runtime_file = EDITOR_DIR / f"runtime-{hashlib.sha256(str(self.home).encode()).hexdigest()[:12]}.json"

    def target_binding(self, key: str) -> Tuple[str, Path, Path]:
        if self.config["config_error"]:
            raise ValueError(self.config["config_error"])
        path = self.paths[key]
        if path is None:
            raise ValueError("Workspace not configured")
        canonical = path.resolve(strict=True)
        if not canonical.is_file():
            raise ValueError("Path is not a regular file")
        home_stat = self.home.stat()
        parent_stat = canonical.parent.stat()
        binding = {"version": 1, "home": str(self.home),
                   "target_parent_generation": [parent_stat.st_dev, parent_stat.st_ino],
                   "home_generation": [home_stat.st_dev, home_stat.st_ino],
                   "declared_home": str(self.declared_home), "key": key,
                   "declared_path": str(path), "canonical_target": str(canonical),
                   "workspace_binding": self.config["terminal_cwd"] if key == "agents" else None,
                   "workspace": str(self.workspace) if key == "agents" else None}
        target_id = sha256_bytes(json.dumps(binding, sort_keys=True).encode())
        return target_id, path, canonical

    def recheck_binding(self, key: str, target_id: str) -> "EditorState":
        fresh = EditorState(self.declared_home, token=self.token)
        fresh.profile = self.profile
        if fresh.target_binding(key)[0] != target_id:
            raise TargetChanged("Target binding changed; reload the document")
        return fresh

    def document_info(self, key: str, raw: bytes, target_id: str, path: Path,
                      canonical: Path) -> Dict[str, Any]:
        content = decode_text(raw)
        return {"key": key, "profile": self.profile, "path": str(path),
                "linked": path.is_symlink(),
                "actual_path": str(canonical) if path.is_symlink() else None,
                "exists": True, "readable": True, "content": content,
                "hash": sha256_bytes(raw), "target_id": target_id, "error": "",
                "enabled": self.enabled_for(key),
                "backups": list_backups(self.profile, key, target_id),
                **text_metrics(key, content, self.limit_for(key))}

    def enabled_for(self, key: str) -> Optional[bool]:
        if self.config["config_error"]:
            return None
        if key == "memory":
            return bool(self.config["memory_enabled"])
        if key == "user":
            return bool(self.config["user_profile_enabled"])
        return None

    def limit_for(self, key: str) -> Optional[int]:
        if key == "memory":
            return int(self.config["memory_char_limit"])
        if key == "user":
            return int(self.config["user_char_limit"])
        return None

    def touch(self) -> None:
        if DAEMON_MODE:
            return
        if self.timer:
            self.timer.cancel()
        if self.server:
            self.timer = threading.Timer(IDLE_SECONDS, self.server.shutdown)
            self.timer.daemon = True
            self.timer.start()

    def read_document(self, key: str) -> Dict[str, Any]:
        if not isinstance(key, str) or key not in self.paths:
            raise KeyError(key)
        path = self.paths[key]
        base = {"key": key, "profile": self.profile, "path": str(path) if path else "",
                "exists": False, "readable": False, "content": "", "hash": None,
                "linked": path.is_symlink() if path else False, "actual_path": None,
                "target_id": None, "backups": [], "enabled": self.enabled_for(key),
                **text_metrics(key, "", self.limit_for(key) if not self.config["config_error"] else None)}
        try:
            target_id, path, canonical = self.target_binding(key)
            with target_locks(path, canonical):
                fresh = self.recheck_binding(key, target_id)
                raw = canonical.read_bytes()
                self.recheck_binding(key, target_id)
                return fresh.document_info(key, raw, target_id, path, canonical)
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            base["error"] = f"Read unavailable: {exc}"
            return base

    def status(self) -> Dict[str, Any]:
        if self.config["config_error"]:
            return {"profile": self.profile, "files": {
                key: {"exists": False, "hash": None, "enabled": None,
                      "error": self.config["config_error"]} for key in self.paths},
                    "time": time.time()}
        files = {}
        for key, path in self.paths.items():
            try:
                raw = path.read_bytes() if path and path.exists() and path.is_file() else None
                files[key] = {
                    "exists": raw is not None,
                    "hash": sha256_bytes(raw) if raw is not None else None,
                    "target_id": self.target_binding(key)[0] if raw is not None else None,
                    "enabled": self.enabled_for(key),
                }
            except (OSError, ValueError, RuntimeError):
                files[key] = {"exists": bool(path and path.exists()), "hash": None, "target_id": None, "enabled": self.enabled_for(key)}
        return {"profile": self.profile, "files": files, "time": time.time()}

    def save_document(self, key: str, content: str, base_hash: str,
                      target_id: Optional[str] = None, note: str = '') -> Tuple[int, Dict[str, Any]]:
        if not isinstance(content, str):
            return 400, {"error": "content must be a string"}
        return self._commit_document(key, base_hash, target_id, content=content, note=note)

    def history_document(self, key, target_id, revision=None):
        if key not in self.paths:
            return 400, {'error': 'Unknown file'}
        try:
            binding, path, canonical = self.target_binding(key)
            if binding != target_id:
                return 409, {'error': 'Target binding changed; reload the document'}
            # Immutable Git reads do not create lock files or initialize Git.
            self.recheck_binding(key, target_id)
            history = LocalHistory(EDITOR_DIR)
            if revision is None:
                current = canonical.read_bytes()
                result = {'versions': history.versions(key, target_id),
                          'current_content': decode_text(current),
                          'current_hash': sha256_bytes(current)}
            else:
                current = canonical.read_bytes()
                _, result = history.preview(key, target_id, revision, current)
                result['current_hash'] = sha256_bytes(current)
                # Both panes and semantic changes share the exact preview bytes.
                result['current_content'] = decode_text(current)
                result['comparison'] = difflib.SequenceMatcher(
                    None, result['current_content'].splitlines(keepends=True),
                    result['content'].splitlines(keepends=True), autojunk=False
                ).get_opcodes()
            self.recheck_binding(key, target_id)
            return 200, {**result, 'target_id': target_id}
        except TargetChanged as exc:
            return 409, {'error': str(exc)}
        except (OSError, ValueError, RuntimeError) as exc:
            return 400, {'error': str(exc)}

    def restore_version(self, key, revision, base_hash, target_id, confirmed=False):
        if confirmed is not True:
            return 400, {'error': 'Explicit restore confirmation required'}
        if not isinstance(revision, str) or len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
            return 400, {'error': 'Invalid revision'}
        return self._commit_document(key, base_hash, target_id, revision=revision)

    def restore_document(self, key: str, backup_name: str, base_hash: str,
                         target_id: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        if (not isinstance(backup_name, str) or not backup_name.endswith(".md")
                or Path(backup_name).name != backup_name):
            return 400, {"error": "Invalid backup name"}
        return self._commit_document(key, base_hash, target_id, backup_name=backup_name)

    def _commit_document(self, key, base_hash, target_id, *, content=None,
                         backup_name=None, revision=None, note='') -> Tuple[int, Dict[str, Any]]:
        if not isinstance(note, str) or len(note) > 200:
            return 400, {'error': 'Version note must be at most 200 characters'}
        if not isinstance(key, str) or key not in self.paths:
            return 400, {"error": "Unknown file"}
        if (not isinstance(base_hash, str) or len(base_hash) != 64
                or any(c not in "0123456789abcdef" for c in base_hash)
                or not isinstance(target_id, str) or len(target_id) != 64
                or any(c not in "0123456789abcdef" for c in target_id)):
            return 400, {"error": "base_hash and target_id from a successful read are required"}
        try:
            if self.config["config_error"]:
                return 503, {"error": self.config["config_error"]}
            if self.paths[key] is None:
                return 404, {"error": "Workspace not configured"}
            binding, path, canonical = self.target_binding(key)
            if not hmac.compare_digest(binding, target_id):
                return 409, {"error": "Target binding changed; reload the document"}
            with target_locks(path, canonical):
                fresh = self.recheck_binding(key, target_id)
                current = canonical.read_bytes()
                decode_text(current)
                current_hash = sha256_bytes(current)
                if not hmac.compare_digest(current_hash, base_hash):
                    return 409, {"error": "File changed on disk after it was loaded",
                                 "current_hash": current_hash}
                if revision is not None:
                    encoded, _ = LocalHistory(EDITOR_DIR).preview(key, target_id, revision, current)
                    content = decode_text(encoded)
                elif backup_name is not None:
                    source = _backup_dir(self.profile, key, target_id) / backup_name
                    # No following a locally replaced backup symlink.
                    try:
                        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                    except FileNotFoundError:
                        return 404, {"error": "No backup bound to this target"}
                    with os.fdopen(fd, "rb") as handle:
                        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                            raise ValueError("Backup is not a regular file")
                        encoded = handle.read()
                    content = decode_text(encoded)
                else:
                    encoded = current if content == decode_text(current) else content.encode("utf-8")
                metrics = text_metrics(key, content, fresh.limit_for(key))
                if metrics["limit"] is not None and metrics["usage_chars"] > metrics["limit"]:
                    return 413, {"error": "Hermes character budget exceeded", **metrics}
                if encoded == current and backup_name is None and revision is None:
                    return 200, {**fresh.document_info(key, current, target_id, path, canonical),
                                 'saved': True, 'unchanged': True}
                # Fail closed before overwrite if current bytes cannot be recorded.
                history = LocalHistory(EDITOR_DIR)
                with file_lock(canonical, EDITOR_DIR / 'locks' / 'history.lock'):
                    history.record(key, target_id, current, 'before overwrite')
                # Restore source is already decoded, but keep its file too.
                backup = make_backup(self.profile, key, current, target_id)
                mode = stat.S_IMODE(canonical.stat().st_mode) or 0o600
                def before_replace():
                    self.recheck_binding(key, target_id)
                    if sha256_bytes(canonical.read_bytes()) != base_hash:
                        raise TargetChanged("File changed before commit")
                atomic_write_bytes(canonical, encoded, mode, before_replace=before_replace)
                self.recheck_binding(key, target_id)
                history_warning = ''
                try:
                    with file_lock(canonical, EDITOR_DIR / 'locks' / 'history.lock'):
                        history.record(key, target_id, encoded,
                                       'restore' if revision is not None or backup_name is not None else 'save',
                                       note or ('Restored ' + revision[:12] if revision else ''),
                                       force=revision is not None or backup_name is not None)
                except (OSError, RuntimeError) as exc:
                    # The disk write already succeeded; do not report it as unsaved.
                    history_warning = 'File saved, but new Git version failed: ' + str(exc)
                retention_warning = ""
                try:
                    protected = {Path(backup).name}
                    if backup_name:
                        protected.add(backup_name)
                    trim_backups(self.profile, key, target_id, protected)
                except OSError:
                    retention_warning = "Backup retention failed; extra backups retained"
                # Snapshot bytes only: no post-unlock read can adopt an unseen hash.
                info = fresh.document_info(key, encoded, target_id, path, canonical)
                result = {**info, "retention_warning": retention_warning, 'history_warning': history_warning}
                if backup_name is None and revision is None:
                    result.update(saved=True, backup=backup)
                else:
                    result.update(restored=True, rollback_backup=backup)
                return 200, result
        except TargetChanged as exc:
            return 409, {"error": str(exc)}
        except FileNotFoundError:
            return 404, {"error": "File is missing; implicit creation is disabled"}
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            return 500, {"error": f"Operation failed; any existing backups retained: {exc}"}


def discover_profiles() -> list[Dict[str, str]]:
    repo = Path.home() / ".hermes" / "hermes-agent"
    command = "from hermes_cli.profiles import _iter_named_profile_dirs, _get_default_hermes_home; import json; print(json.dumps([{'name':'default','home':str(_get_default_hermes_home())}]+[{'name':p.name,'home':str(p)} for p in _iter_named_profile_dirs()]))"
    result = subprocess.run([str(repo / "venv/bin/python"), "-c", command], cwd=str(repo), capture_output=True, text=True, timeout=15, check=True)
    return json.loads(result.stdout)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "HermesMemoryEditor/2"

    @property
    def state(self) -> EditorState:
        return self.server.editor_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src data:; font-src 'self'; media-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

    def _json(self, code: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        if REMOTE_ORIGIN and host == urllib.parse.urlsplit(REMOTE_ORIGIN).netloc:
            return self._remote_ok()
        return host in {f"127.0.0.1:{self.state.port}", f"localhost:{self.state.port}"}

    def _remote_ok(self) -> bool:
        # Private listener binds only the Tailscale IP, never LAN/all interfaces.
        return bool(REMOTE_ORIGIN and DAEMON_MODE
                    and ipaddress.ip_address(self.client_address[0]) in ipaddress.ip_network("100.64.0.0/10")
                    and self.headers.get("Host") == urllib.parse.urlsplit(REMOTE_ORIGIN).netloc)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Hermes-Editor-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, self.state.token)

    def _require_api_access(self, require_origin: bool = False) -> bool:
        if not self._host_ok():
            self._error(403, "Invalid Host")
            return False
        if not self._authorized():
            self._error(401, "Invalid editor token")
            return False
        allowed_origins = {f"http://127.0.0.1:{self.state.port}", f"http://localhost:{self.state.port}"}
        if REMOTE_ORIGIN:
            allowed_origins.add(REMOTE_ORIGIN)
        if require_origin and self.headers.get("Origin") not in allowed_origins:
            self._error(403, "Invalid Origin")
            return False
        self.state.touch()
        return True

    def _read_json(self) -> Optional[Dict[str, Any]]:
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self._error(415, "Content-Type must be application/json")
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "Invalid Content-Length")
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            self._error(413, "Invalid request size")
            return None
        self.connection.settimeout(10)
        try:
            body = self.rfile.read(length)
            data = json.loads(body)
        except (socket.timeout, UnicodeError, json.JSONDecodeError):
            self._error(400, "Invalid JSON request")
            return None
        if not isinstance(data, dict):
            self._error(400, "JSON object required")
            return None
        return data

    def target_state(self, name: str) -> EditorState:
        if not isinstance(name, str) or not name:
            raise ValueError("Explicit profile is required")
        matches = [p for p in discover_profiles() if p["name"] == name]
        if len(matches) != 1:
            raise ValueError("Unknown or removed Profile")
        target = EditorState(Path(matches[0]["home"]), token=self.state.token)
        target.profile = name
        return target

    def do_GET(self) -> None:
        if not self._host_ok():
            self._error(403, "Invalid Host or unauthorized Tailscale identity")
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in {"/", "/index.html"}:
            self._serve_file(HTML_PATH, "text/html; charset=utf-8")
            return
        if path == "/api/identity":
            self._json(200, {"app": APP_ID, "profile": self.state.profile, "port": self.state.port})
            return
        if path.startswith("/api/") and not self._require_api_access():
            return
        if path == "/api/profiles":
            try:
                self._json(200, {"profiles": discover_profiles(), "initial": self.state.profile})
            except Exception as exc:
                self._error(503, f"Profile discovery failed: {exc}")
            return
        target = self.state
        if path.startswith("/api/"):
            name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("profile", [None])[0]
            if not name:
                self._error(400, "Explicit profile is required")
                return
            try:
                target = self.target_state(name)
            except Exception as exc:
                self._error(404, str(exc))
                return
        if path == "/api/meta":
            shared = []
            if target.workspace:
                for item in discover_profiles():
                    other = EditorState(Path(item["home"]))
                    if other.profile != target.profile and other.workspace and other.paths["agents"].resolve() == target.paths["agents"].resolve():
                        shared.append(other.profile)
            self._json(200, {
                "app": APP_ID,
                "profile": target.profile,
                "home": str(target.home),
                "workspace": str(target.workspace) if target.workspace else None,
                "config_warning": target.config.get("config_warning", ""),
                "idle_seconds": 0 if DAEMON_MODE else IDLE_SECONDS,
                "shared_workspace": shared,
            })
        elif path == "/api/status":
            self._json(200, target.status())
        elif path.startswith("/api/read/"):
            key = path.rsplit("/", 1)[-1]
            if key not in target.paths:
                self._error(404, "Unknown file")
            else:
                self._json(200, target.read_document(key))
        elif path.startswith("/api/backups/"):
            key = path.rsplit("/", 1)[-1]
            if key not in target.paths:
                self._error(404, "Unknown file")
            else:
                self._json(200, {"backups": target.read_document(key)["backups"]})
        elif path.startswith('/api/history/') or path.startswith('/api/preview/'):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            key = path.rsplit('/', 1)[-1]
            revision = query.get('revision', [''])[0] if path.startswith('/api/preview/') else None
            code, result = target.history_document(key, query.get('target_id', [''])[0], revision)
            self._json(code, result)
        else:
            name = path.lstrip("/")
            if name in STATIC_FILES and not any(p.is_symlink() for p in (EDITOR_DIR / name).parents if p != EDITOR_DIR.parent) and not (EDITOR_DIR / name).is_symlink():
                self._serve_file(EDITOR_DIR / name, STATIC_FILES[name])
            else:
                self._error(404, "Not found")

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._error(404, "Asset not found")
            return
        data = path.read_bytes()
        if path == HTML_PATH and DAEMON_MODE:
            data = data.replace(b"close or hide this tab and the server exits after five minutes", b"private Tailscale access; editor stays available in the background")
        if path == HTML_PATH and self._remote_ok():
            # Bootstrap only inside the no-store page on the private listener.
            data = data.replace(b"const TOKEN = tokenParam ||", ("const TOKEN = " + json.dumps(self.state.token) + " || tokenParam ||").encode())
        self.send_response(200)
        self._headers(content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._require_api_access(require_origin=True):
            return
        data = self._read_json()
        if data is None:
            return
        try:
            target = self.target_state(data.get("profile"))
        except Exception as exc:
            self._error(404, str(exc))
            return
        if path == "/api/save":
            code, result = target.save_document(data.get("key"), data.get("content"), data.get("base_hash"), data.get("target_id"), data.get('note', ''))
            self._json(code, result)
        elif path == '/api/restore-version':
            code, result = target.restore_version(data.get('key'), data.get('revision'), data.get('base_hash'), data.get('target_id'), data.get('confirmed'))
            self._json(code, result)
        elif path == "/api/restore":
            code, result = target.restore_document(data.get("key"), data.get("backup"), data.get("base_hash"), data.get("target_id"))
            self._json(code, result)
        else:
            self._error(404, "Not found")

    def do_OPTIONS(self) -> None:
        self._error(405, "CORS is not enabled")


def _read_runtime(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _existing_instance(state: EditorState) -> Optional[str]:
    data = _read_runtime(state.runtime_file)
    if not data or data.get("home") != str(state.home):
        return None
    try:
        port = int(data["port"])
        token = str(data["token"])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.6)
        conn.request("GET", "/api/identity", headers={"Host": f"127.0.0.1:{port}"})
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
        if response.status == 200 and payload.get("app") == APP_ID and payload.get("profile") == state.profile:
            return f"http://127.0.0.1:{port}/#token={urllib.parse.quote(token)}"
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    try:
        state.runtime_file.unlink()
    except OSError:
        pass
    return None


def _write_runtime(state: EditorState) -> None:
    data = json.dumps({
        "app": APP_ID,
        "pid": os.getpid(),
        "port": state.port,
        "token": state.token,
        "profile": state.profile,
        "home": str(state.home),
    }, ensure_ascii=False).encode("utf-8")
    atomic_write_bytes(state.runtime_file, data, 0o600)


def main() -> int:
    if not HTML_PATH.is_file():
        print(f"editor.html not found: {HTML_PATH}", file=sys.stderr)
        return 1
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    state = EditorState(home)
    existing = _existing_instance(state)
    if existing:
        if not DAEMON_MODE:
            webbrowser.open(existing)
            return 0
        print("Daemon port already in use", file=sys.stderr)
        return 1

    base_port = int(os.environ.get("HERMES_EDITOR_PORT", "8766"))
    server = None
    for port in range(base_port, base_port + (1 if DAEMON_MODE else 10)):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
            state.port = port
            break
        except OSError:
            continue
    if server is None:
        print(f"No available port in {base_port}-{base_port + 9}", file=sys.stderr)
        return 1

    server.editor_state = state  # type: ignore[attr-defined]
    state.server = server
    remote_server = None
    if DAEMON_MODE and REMOTE_ORIGIN:
        remote = urllib.parse.urlsplit(REMOTE_ORIGIN)
        remote_server = http.server.ThreadingHTTPServer((remote.hostname, remote.port), Handler)
        remote_server.editor_state = state
        threading.Thread(target=remote_server.serve_forever, daemon=True).start()
    _write_runtime(state)
    state.touch()
    url = f"http://127.0.0.1:{state.port}/#token={urllib.parse.quote(state.token)}"
    print(f"Hermes Memory Editor ({state.profile}) -> http://127.0.0.1:{state.port}")
    if not DAEMON_MODE:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if state.timer:
            state.timer.cancel()
        server.server_close()
        if remote_server:
            remote_server.shutdown()
            remote_server.server_close()
        runtime = _read_runtime(state.runtime_file)
        if runtime and runtime.get("pid") == os.getpid():
            try:
                state.runtime_file.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
