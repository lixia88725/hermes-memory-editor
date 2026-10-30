"""One admission to an already-running canonical Hermes owner; never starts one.

Separate from the read/apply sandbox: no disk writes, no subprocesses, only the
official local owner transport. No polling, detached runners, or schedule edits.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import socket
import sys
import uuid


def execute(req):
    home = Path(req['home'])
    def check_home():
        if home.is_symlink() or home.resolve(strict=True) != home:
            raise ValueError('Profile home changed')
        st = home.stat()
        if [st.st_dev, st.st_ino] != req['generation']:
            raise ValueError('Profile home generation changed')
        cron = home / 'cron'
        if cron.resolve() != cron or (cron / 'jobs.json').is_symlink():
            raise ValueError('Cron store binding changed')
    def check_binding():
        import server
        import yaml
        # This isolated interpreter already has PyYAML; reuse exactly the
        # editor's validation without spawning its stdlib-launcher YAML probe.
        server._parse_memory_config = lambda raw: server._memory_config_from_mapping(yaml.safe_load(raw))
        state = server.EditorState(home)
        from dream import context_identity
        actual, problem = context_identity(state)
        if actual != req['binding']:
            raise ValueError('Profile/context binding changed before admission')
        if problem:
            raise ValueError(problem)
    check_home()
    def audit(event, args):
        if event == 'open' and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise PermissionError('Dream trigger forbids disk writes')
        if event in ('os.mkdir','os.remove','os.rmdir','os.rename','os.link','os.symlink','os.chmod','os.chown','os.utime','os.truncate','subprocess.Popen','os.system'):
            raise PermissionError('Dream trigger forbids writes and processes')
        if event == 'socket.connect':
            sock, address = args
            if sock.family == socket.AF_UNIX:
                return  # official private peer transport authenticates owner UID
            if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
                raise PermissionError('Dream trigger permits local owner only')
    sys.addaudithook(audit)
    from hermes_cli.web_server_cron import _cron_store_scope
    from hermes_cli.gateway_runtime import discover_gateway_endpoint, attachable
    from hermes_cli.gateway_client import connect_gateway, GatewayRPCError
    from dream_worker import digest
    result = discover_gateway_endpoint(home)
    if result.state != 'ready' or not attachable(result):
        raise ValueError('No ready canonical gateway for this exact Profile; no gateway will be started')
    endpoint = result.endpoint
    # The official store lock serializes pause/delete with admission. Open an
    # existing lock read-only (no cron creation or housekeeping on this path).
    import fcntl
    with _cron_store_scope(home) as jobs:
        jobs.ensure_dirs = lambda: None
        with open(jobs._jobs_lock_file(), 'r') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            check_home()
            check_binding()
            job = jobs.get_job(req['job_id'])
            if not job or digest(job) != req['revision']:
                raise ValueError('Cron changed before admission; reopen Dream')
            if not job.get('enabled', True) or job.get('state') in ('paused','completed','failed','cancelled') or job.get('paused_at'):
                raise ValueError('Saved Dream task is not enabled')
            request_id = uuid.uuid4().hex
            async def submit():
                attempted = False
                receipt = None
                try:
                    async with connect_gateway(endpoint=endpoint) as client:
                        check_home()
                        check_binding()
                        attempted = True
                        receipt = await client.rpc('cron.submit', job_id=job['id'], request_id=request_id, extra_prompt=None, _timeout=15)
                        if not isinstance(receipt, dict) or not receipt.get('session_id') or not receipt.get('admission_id'):
                            raise ValueError('Invalid owner receipt')
                        state = await client.rpc('cron.status', session_id=receipt['session_id'], admission_id=receipt['admission_id'], _timeout=10)
                        if state.get('status') not in ('queued','started','terminal'):
                            raise ValueError('Owner admission could not be verified')
                        return {'status':'accepted', 'request_id':request_id, 'receipt':receipt, 'owner_state':state['status']}
                except GatewayRPCError:
                    # Owner errors can follow a committed admission. Do not
                    # assume non-admission without recovery; leave it unknown.
                    return {'status':'unknown', 'request_id':request_id, 'receipt':receipt, 'error':'Owner refused or admission outcome is unknown; do not retry automatically'}
                except Exception:
                    return {'status':'unknown' if attempted else 'failed', 'request_id':request_id, 'receipt':receipt,
                            'error':'Gateway admission unverified; do not retry automatically' if attempted else 'Cannot attach to the ready gateway; nothing submitted'}
            return asyncio.run(submit())


if __name__ == '__main__':
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = execute(json.load(sys.stdin))
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({'error':str(exc)}))
        sys.exit(1)
