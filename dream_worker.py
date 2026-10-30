#!/usr/bin/env python3
"""Short-lived Hermes interpreter adapter. stdin/stdout JSON; never a scheduler.

Reuses the official Web UI's context-local store scope, official job mutations
and scheduler registration. Exact IDs only; never the dashboard's ID fallback.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def execute(req):
    # Official config loaders can take a last-known-good backup on import.
    # This adapter must not inherit that side effect: read/preview deny every
    # filesystem write; apply allows writes only inside the exact cron store.
    home_path = Path(req['home'])
    if str(home_path.resolve(strict=True)) != req['home'] or home_path.is_symlink():
        raise ValueError('Profile home changed')
    st = home_path.stat()
    if [st.st_dev, st.st_ino] != req['generation']:
        raise ValueError('Profile home generation changed')
    cron_path = home_path / 'cron'
    if cron_path.resolve() != cron_path or (cron_path / 'jobs.json').is_symlink():
        raise ValueError('Cron store must not be rebound through a symlink')
    write_root = cron_path if req['action'] == 'apply' else None
    def writable(path):
        if isinstance(path, int):
            return True  # already-open stdin/stdout/stderr
        try:
            resolved = Path(os.fsdecode(path)).resolve()
            return write_root is not None and (resolved == write_root or write_root in resolved.parents)
        except (TypeError, ValueError):
            return False
    def audit(event, args):
        if event == 'open':
            path, mode, flags = args
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) and not writable(path):
                raise PermissionError('Dream adapter forbids non-cron writes')
        elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.chown', 'os.utime', 'os.truncate'):
            if not writable(args[0]):
                raise PermissionError('Dream adapter forbids non-cron writes')
        elif event in ('os.rename', 'os.link', 'os.symlink'):
            if not writable(args[0]) or not writable(args[1]):
                raise PermissionError('Dream adapter forbids non-cron writes')
        elif event in ('subprocess.Popen', 'os.system', 'socket.connect'):
            raise PermissionError('Dream adapter does not execute jobs or contact services')
    sys.addaudithook(audit)
    from hermes_cli.web_server_cron import _cron_store_scope
    home = Path(req['home'])
    if str(home.resolve(strict=True)) != req['home'] or home.is_symlink():
        raise ValueError('Profile home changed')
    st = home.stat()
    if [st.st_dev, st.st_ino] != req['generation']:
        raise ValueError('Profile home generation changed')
    with _cron_store_scope(home) as jobs:
        if req['action'] == 'read':
            # load_jobs otherwise creates cron/output directories and chmods
            # them, even for get_job. In this short-lived read-only process,
            # suppress directory housekeeping; the audit hook still blocks
            # any implicit database repair/normalisation write.
            jobs.ensure_dirs = lambda: None
        def current():
            # get_job does not load executions (list_jobs can create a sqlite DB).
            return jobs.get_job(req['job_id']) if req.get('job_id') else None
        def view():
            job = current()
            return {'job': job, 'revision': digest(job),
                    'heartbeat_age': jobs.get_ticker_heartbeat_age()}
        if req['action'] == 'read':
            return view()
        if req['action'] != 'apply':
            raise ValueError('Unknown operation')
        from cron.scheduler_provider import InProcessCronScheduler, resolve_cron_scheduler
        provider = resolve_cron_scheduler()
        # Never reconcile a remote registry against one profile's local store.
        if not isinstance(provider, InProcessCronScheduler):
            raise ValueError('Dream currently supports the built-in scheduler only')
        # Fail closed, sharing the official cross-process jobs lock. Mark nested
        # official operations as held, avoiding the core lock's timeout fallback.
        import fcntl
        jobs.ensure_dirs()
        with open(jobs._jobs_lock_file(), 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            jobs._jobs_lock_state.depth = 1
            try:
                before = current()
                if digest(before) != req['revision']:
                    raise ValueError('Cron changed; reopen Dream before applying')
                if req.get('job_id') and before is None:
                    raise ValueError('Managed job missing; refusing replacement')
                changes = req['changes']
                if before is None:
                    if not changes['enabled']:
                        return view()
                    from cron.scheduler import create_job_with_scheduler_registration
                    try:
                        created = create_job_with_scheduler_registration(
                            prompt=changes['prompt'], schedule=changes['schedule'],
                            name='Dream — memory review', deliver='local',
                            enabled_toolsets=['file', 'memory'], workdir=changes['workdir'],
                            paused=False, pinned=False)
                    except Exception as exc:
                        # Registration can save a job before reporting a failure.
                        if hasattr(exc, 'to_dict'):
                            return {'error': str(exc), 'partial': getattr(exc, 'to_dict')()}
                        raise
                    req['job_id'] = created['id']
                else:
                    updates = {k: changes[k] for k in ('prompt', 'schedule') if k in changes}
                    if updates:
                        jobs.update_job(before['id'], updates)
                    active = bool(before.get('enabled', True) and not before.get('paused_at') and before.get('state') != 'paused')
                    if changes['enabled'] != bool(active):
                        fn = jobs.resume_job if changes['enabled'] else jobs.pause_job
                        if not fn(before['id']):
                            raise ValueError('Cron activation change failed')
                provider.on_jobs_changed()
                return view()
            finally:
                jobs._jobs_lock_state.depth = 0
                fcntl.flock(lock, fcntl.LOCK_UN)


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        # Imports sometimes print diagnostics. Reserve stdout for the envelope.
        with contextlib.redirect_stdout(sys.stderr):
            result = execute(request)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        job_id = locals().get('request', {}).get('job_id')
        note = (' [job %s; reopen to verify actual state]' % job_id) if job_id else ''
        print(json.dumps({'error': str(exc) + note}, ensure_ascii=False))
        sys.exit(1)
