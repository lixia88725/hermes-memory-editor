#!/usr/bin/env python3
"""Dream integration tests: only synthetic homes; no production scheduler/jobs.
Run /usr/bin/python3 test_dream.py. Uses the installed official Hermes adapter.
"""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import server as app
import dream
from test_history import Fixture, SCRATCH, fingerprint


class DreamTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.d = dream.Dream(app)
        self.before_docs = {str(p): p.read_bytes() for h in self.f.homes.values() for p in h.rglob('*.md')}

    def tearDown(self):
        for path, data in self.before_docs.items():
            self.assertEqual(Path(path).read_bytes(), data, 'Dream must never execute memory writes')
        self.f.close()

    def snapshot(self):
        code, result = self.f.api('/api/dream')
        self.assertEqual(code, 200, result)
        for row in result['rows']:
            self.assertNotIn('error', row, row)
        return result

    def apply(self, snapshot, **kwargs):
        row = snapshot['rows'][0]
        change = dict(profile=row['profile'], version=row['version'], enabled=True,
                      time=row['time'], modules=row['modules'], replace_prompt=False)
        change.update(kwargs)
        return self.f.api('/api/dream/apply', {'library_version':snapshot['library_version'], 'changes':[change]})

    def official(self, profile, operation, *args):
        home = self.f.homes[profile]
        self.assertTrue(SCRATCH in home.parents)
        repo = Path.home() / '.hermes/hermes-agent'
        script = "import json,sys; from pathlib import Path; from hermes_cli.web_server_cron import _cron_store_scope; r=json.load(sys.stdin);\nwith _cron_store_scope(Path(r['home'])) as j:\n print(json.dumps(getattr(j,r['op'])(*r['args'])))"
        result = subprocess.run([str(repo/'venv/bin/python'), '-c', script], cwd=repo,
            env=dict(os.environ, HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE='1'),
            input=json.dumps({'home':str(home), 'op':operation, 'args':args}), capture_output=True, text=True, timeout=40, check=True)
        return json.loads(result.stdout)

    def test_snapshot_discovers_once_but_each_open_is_fresh(self):
        discover = app.discover_profiles
        with patch.object(app, 'discover_profiles', wraps=discover) as scan:
            self.snapshot()
            self.assertEqual(scan.call_count, 1)
            self.snapshot()
            self.assertEqual(scan.call_count, 2)

    def test_snapshot_rejects_removed_or_rebound_later_home(self):
        for kind in ('deleted', 'rebound', 'symlink'):
            with self.subTest(kind=kind):
                home = self.f.homes['beta']; old = home.with_name('beta-old')
                worker = self.d.worker
                def dispatch(identity, *args, **kwargs):
                    if identity['home'] == str(self.f.homes['alpha']):
                        home.rename(old)
                        if kind == 'rebound': home.mkdir()
                        if kind == 'symlink': home.symlink_to(old, target_is_directory=True)
                    return worker(identity, *args, **kwargs)
                try:
                    with patch.object(self.d, 'worker', side_effect=dispatch) as calls:
                        rows = self.d.snapshot()['rows']
                    self.assertIn('error', rows[1], rows)
                    self.assertEqual(calls.call_count, 1)
                finally:
                    if home.is_symlink(): home.unlink()
                    elif home.exists(): home.rmdir()
                    old.rename(home)

    def native_script(self, script):
        repo = Path.home()/'.hermes/hermes-agent'
        result = subprocess.run([str(repo/'venv/bin/python'), '-c', script], cwd=repo,
            env=dict(os.environ, HERMES_HOME=str(self.f.homes['alpha']),
                     PYTHONPATH=str(Path(__file__).parent), PYTHONDONTWRITEBYTECODE='1'),
            capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_read_worker_avoids_web_imports_and_audit_denies_side_effects(self):
        before = fingerprint(self.f.root)
        script = '''
import importlib.abc, sys, json, os, socket, subprocess
from pathlib import Path
class NoWeb(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('fastapi', 'pydantic') or fullname == 'hermes_cli.web_server_cron':
            raise AssertionError('web dependency imported: '+fullname)
sys.meta_path.insert(0, NoWeb())
import dream_worker, contextlib
home = Path(os.environ['HERMES_HOME']); st = home.stat()
checks = []
def forbidden_read(job_id):
    operations = [lambda: (home/'forbidden').write_text('bad'),
                  lambda: (home/'forbidden-dir').mkdir(),
                  lambda: subprocess.run(['true']),
                  lambda: socket.create_connection(('127.0.0.1', 9))]
    for operation in operations:
        try: operation()
        except PermissionError: checks.append(True)
        else: raise AssertionError('audit allowed side effect')
    return None
native_scope = dream_worker._cron_store_scope
@contextlib.contextmanager
def instrumented_scope(home):
    with native_scope(home) as jobs:
        jobs.get_job = forbidden_read
        yield jobs
dream_worker._cron_store_scope = instrumented_scope
result = dream_worker.execute(dict(action='read', home=str(home), generation=[st.st_dev, st.st_ino], job_id='abcdef123456'))
assert result['job'] is None
print(json.dumps(checks))
'''
        self.assertEqual(self.native_script(script), [True]*4)
        self.assertEqual(before, fingerprint(self.f.root))

    def test_native_scope_matches_official_nested_restore_and_no_latching(self):
        script = '''
import json, os
from pathlib import Path
from cron import jobs
from hermes_constants import get_hermes_home, get_hermes_home_override
from hermes_cli.web_server_cron import _cron_store_scope as official
from dream_worker import _cron_store_scope as lean
alpha = Path(os.environ['HERMES_HOME']); beta = alpha.with_name('beta')
def identity():
    return (str(get_hermes_home()), str(jobs.get_cron_output_dir()), get_hermes_home_override())
initial = identity()
for scope in (official, lean):
    with scope(alpha) as j:
        assert j is jobs
        a = identity()
        try:
            with scope(beta):
                assert get_hermes_home() == beta
                assert jobs.get_cron_output_dir() == beta/'cron/output'
                raise ValueError('synthetic unwind')
        except ValueError: pass
        assert identity() == a
    assert identity() == initial
assert os.environ['HERMES_HOME'] == str(alpha)
print(json.dumps(True))
'''
        self.assertTrue(self.native_script(script))

    def test_apply_rediscovers_removed_profile_after_snapshot(self):
        snap = self.snapshot()
        self.f.homes.pop('alpha')
        result = self.apply(snap)[1]
        self.assertFalse(result['results'][0]['ok'])
        self.assertIn('removed', result['results'][0]['error'])

    def test_open_preview_and_disabled_apply_are_read_only(self):
        before = fingerprint(self.f.root)
        snap = self.snapshot()
        code, preview = self.f.api('/api/dream/preview', {'profile':'alpha','modules':['dedup','concise']})
        self.assertEqual(code, 200)
        self.assertIn(dream.PREFIX, preview['prompt'])
        self.assertIn(str(self.f.homes['alpha']), preview['prompt'])
        self.assertNotIn(str(self.f.homes['beta']), preview['prompt'])
        self.assertEqual(before, fingerprint(self.f.root))
        code, result = self.apply(snap, enabled=False)
        self.assertTrue(result['results'][0]['ok'], result)
        self.assertFalse((self.f.homes['alpha']/'cron').exists())
        self.assertFalse((self.f.editor/'dream-state.json').exists())

    def test_create_pause_resume_and_isolation_real_official_adapter(self):
        beta_before = fingerprint(self.f.homes['beta'])
        code, result = self.apply(self.snapshot(), time='04:15')
        self.assertEqual(code, 200)
        self.assertTrue(result['results'][0]['ok'], result)
        snap = self.snapshot(); row = snap['rows'][0]
        job = self.official('alpha','get_job',row['job_id'])
        self.assertEqual(job['schedule']['expr'], '15 4 * * *')
        self.assertEqual(job['enabled_toolsets'], ['file','memory'])
        self.assertEqual(job['deliver'], 'local')
        self.assertEqual(job['workdir'], str(self.f.root/'workspace-alpha'))
        self.assertIn('运行绑定', job['prompt'])
        self.assertEqual(beta_before, fingerprint(self.f.homes['beta']))
        self.assertTrue(self.apply(snap, enabled=False)[1]['results'][0]['ok'])
        self.assertFalse(self.snapshot()['rows'][0]['enabled'])
        self.assertTrue(self.apply(self.snapshot(), enabled=True)[1]['results'][0]['ok'])
        self.assertTrue(self.snapshot()['rows'][0]['enabled'])

    def test_unrelated_named_job_never_adopted_or_mutated(self):
        unrelated = self.official('alpha','create_job','synthetic unrelated', '0 1 * * *','Dream')
        before = self.official('alpha','get_job', unrelated['id'])
        snap = self.snapshot()
        self.assertIsNone(snap['rows'][0]['job_id'])
        self.assertTrue(self.apply(snap)[1]['results'][0]['ok'])
        self.assertNotEqual(self.snapshot()['rows'][0]['job_id'], unrelated['id'])
        self.assertEqual(before, self.official('alpha','get_job', unrelated['id']))

    def test_default_exact_id_adoption_preserves_prompt_schedule_pins(self):
        self.f.homes['default'] = self.f.homes.pop('alpha')
        existing = self.official('default','create_job','custom untouched prompt', '45 1 * * *','Not a Dream name')
        self.official('default','update_job', existing['id'], {'model':'synthetic-model','provider':'synthetic-provider'})
        with patch.object(dream, 'DEFAULT_JOB_ID', existing['id']):
            before = fingerprint(self.f.root)
            snap = self.snapshot()
            row = next(r for r in snap['rows'] if r['profile']=='default')
            self.assertEqual(row['job_id'], existing['id'])
            self.assertEqual(before, fingerprint(self.f.root))
            self.assertTrue(row['custom_prompt'])
            result = self.f.api('/api/dream/apply', {'library_version':snap['library_version'], 'changes':[dict(profile='default',version=row['version'],enabled=False,modules=row['modules'],time=row['time'])]})[1]
            self.assertTrue(result['results'][0]['ok'], result)
            actual = self.official('default','get_job',existing['id'])
            self.assertEqual(actual['prompt'], 'custom untouched prompt')
            self.assertEqual(actual['schedule'], existing['schedule'])
            self.assertEqual(actual['model'], 'synthetic-model')
            self.assertEqual(actual['provider'], 'synthetic-provider')

    def test_stale_cron_and_profile_binding_conflicts(self):
        self.assertTrue(self.apply(self.snapshot())[1]['results'][0]['ok'])
        snap = self.snapshot(); row = snap['rows'][0]
        self.official('alpha','update_job',row['job_id'],{'prompt':'external synthetic change'})
        result = self.apply(snap, enabled=False)[1]
        self.assertFalse(result['results'][0]['ok'])
        self.assertTrue(self.snapshot()['rows'][0]['custom_prompt'])
        self.assertTrue(self.apply(self.snapshot(), enabled=False)[1]['results'][0]['ok'])
        home = self.f.homes['alpha']; (home/'config.yaml').write_text('terminal:\n  cwd: '+str(self.f.root/'workspace-beta')+'\n')
        snap = self.f.api('/api/dream')[1]
        self.assertIn('binding changed', snap['rows'][0]['error'])

    def test_batch_partial_failure_and_input_validation(self):
        snap = self.snapshot()
        changes = [dict(profile=r['profile'],version=r['version'],enabled=True,time='05:10',modules=r['modules']) for r in snap['rows']]
        changes[1]['time']='25:99'
        result = self.f.api('/api/dream/apply', {'library_version':snap['library_version'], 'changes':changes})[1]
        self.assertEqual([r['ok'] for r in result['results']], [True,False], result)
        self.assertFalse((self.f.homes['beta']/'cron').exists())
        for value in ('../../default', 'missing'):
            self.assertEqual(self.f.api('/api/dream/preview', {'profile':value,'modules':['dedup']})[0],400)
        self.assertEqual(self.f.api('/api/dream/preview', {'profile':'alpha','modules':['not-saved']})[0],400)
        self.assertEqual(self.f.api('/api/dream/apply', {'library_version':snap['library_version'],'changes':changes+changes})[0],400)

    def test_library_persistence_preview_and_explicit_prompt_replacement(self):
        self.assertTrue(self.apply(self.snapshot())[1]['results'][0]['ok'])
        snap = self.snapshot(); before = self.official('alpha','get_job',snap['rows'][0]['job_id'])
        modules = [*snap['modules'], {'id':'check','name':'核对','text':'报告需要确认的问题。'}]
        code, saved = self.f.api('/api/dream/library', {'library_version':snap['library_version'],'modules':modules})
        self.assertEqual(code,200,saved)
        self.assertEqual(dream.Dream(app).load()['modules'], modules)
        self.assertEqual(before,self.official('alpha','get_job',before['id']))
        snap = self.snapshot()
        result = self.apply(snap,modules=['check'])[1]
        self.assertFalse(result['results'][0]['ok'])
        self.assertTrue(self.apply(snap,modules=['check'],replace_prompt=True)[1]['results'][0]['ok'])
        prompt = self.snapshot()['rows'][0]['prompt']
        self.assertIn('**核对**',prompt); self.assertNotIn('**去重**',prompt)
        code,_ = self.f.api('/api/dream/library', {'library_version':'stale','modules':[]})
        self.assertEqual(code,409)
        code,_ = self.f.api('/api/dream/library', {'library_version':snap['library_version'],'modules':[{'id':'../escape','name':'X','text':'X'}]})
        self.assertEqual(code,400)

    def remove_doc(self, key, profile='alpha'):
        path = app.EditorState(self.f.homes[profile]).paths[key]
        self.before_docs.pop(str(path), None)
        path.unlink()
        return path

    def test_partial_files_enable_and_no_auto_creation(self):
        for missing in ('user', 'memory'):
            with self.subTest(missing=missing):
                path = self.remove_doc(missing)
                snap = self.snapshot(); row = snap['rows'][0]
                self.assertEqual(row['problem'], '')
                self.assertEqual(row['files'][missing]['state'], 'missing')
                self.assertTrue(self.apply(snap)[1]['results'][0]['ok'])
                self.assertFalse(path.exists())
                self.assertIn('缺失的跳过', self.snapshot()['rows'][0]['prompt'])
                self.assertTrue(self.apply(self.snapshot(), enabled=False)[1]['results'][0]['ok'])
                path.write_text('')  # empty existing files count as readable
                self.assertEqual(self.snapshot()['rows'][0]['files'][missing]['state'], 'readable')

    def test_both_missing_refuse_enable_allow_disabled_time_and_existing_disable(self):
        self.assertTrue(self.apply(self.snapshot())[1]['results'][0]['ok'])
        for key in ('user', 'memory'): self.remove_doc(key)
        snap = self.snapshot(); row = snap['rows'][0]
        self.assertTrue(row['enabled'])  # reads never pause an enabled job
        self.assertIn('Neither USER nor MEMORY', row['problem'])
        self.assertFalse(self.apply(snap)[1]['results'][0]['ok'])
        self.assertTrue(self.apply(snap, enabled=False, time='05:45')[1]['results'][0]['ok'])
        self.assertFalse(self.snapshot()['rows'][0]['enabled'])
        for key in ('user', 'memory'): self.assertFalse(app.EditorState(self.f.homes['alpha']).paths[key].exists())

    def test_both_missing_pending_preferences_no_cron(self):
        for key in ('user', 'memory'): self.remove_doc(key)
        snap = self.snapshot()
        self.assertFalse(self.apply(snap)[1]['results'][0]['ok'])
        self.assertTrue(self.apply(snap, enabled=False, time='06:10')[1]['results'][0]['ok'])
        self.assertEqual(self.snapshot()['rows'][0]['time'], '06:10')
        self.assertFalse((self.f.homes['alpha']/'cron').exists())

    def test_optional_references_missing_or_unreadable_not_blockers(self):
        for key in ('soul', 'agents'): self.remove_doc(key)
        self.assertEqual(self.snapshot()['rows'][0]['problem'], '')
        state = app.EditorState(self.f.homes['alpha'])
        soul = state.paths['soul']; soul.write_text('')
        original = Path.read_bytes
        def read(path):
            if path == soul: raise PermissionError('Synthetic denied SOUL')
            return original(path)
        with patch.object(Path, 'read_bytes', read):
            identity, problem = dream.context_identity(state)
        self.assertEqual(problem, '')
        self.assertEqual(identity['files']['soul']['state'], 'unreadable')
        self.assertIn('PermissionError', identity['files']['soul']['error'])

    def test_unreadable_memory_is_not_missing_and_all_files_reported(self):
        state = app.EditorState(self.f.homes['alpha']); original = Path.read_bytes
        def read(path):
            if path in (state.paths['user'], state.paths['memory']): raise PermissionError('Synthetic denied')
            return original(path)
        with patch.object(Path, 'read_bytes', read):
            snap = self.snapshot(); row = snap['rows'][0]
            for key in ('user', 'memory'):
                self.assertEqual(row['files'][key]['state'], 'unreadable')
                self.assertIn(key.upper()+'.md cannot be read', row['problem'])
            self.assertFalse(self.apply(snap)[1]['results'][0]['ok'])

    def test_missing_creation_deletion_and_parent_rebinding_stale_conflicts(self):
        path = self.remove_doc('user'); snap = self.snapshot()
        path.write_text('new existing document')
        self.assertFalse(self.apply(snap)[1]['results'][0]['ok'])
        self.assertEqual(path.read_text(), 'new existing document')
        snap = self.snapshot(); path.unlink()
        self.assertFalse(self.apply(snap)[1]['results'][0]['ok'])
        self.assertTrue(self.apply(self.snapshot())[1]['results'][0]['ok'])
        snap = self.snapshot(); parent = path.parent
        parent.rename(parent.with_name('old-memories'))
        parent.mkdir(); (parent/'MEMORY.md').write_text('new target')
        # This deliberate synthetic move is not a Dream write.
        for p in list(self.before_docs):
            if str(parent) in p: self.before_docs.pop(p)
        result = self.f.api('/api/dream')[1]['rows'][0]
        self.assertIn('binding changed', result['error'])
        self.assertFalse(self.apply(snap)[1]['results'][0]['ok'])

    def test_disabled_configuration_and_legacy_saved_identity_prompt_preserved(self):
        self.assertTrue(self.apply(self.snapshot())[1]['results'][0]['ok'])
        data = self.d.load(); row = self.d.row('alpha', data)
        legacy = dict(row['_identity']); legacy.pop('files')
        data['profiles']['alpha']['identity'] = dream.digest(legacy)
        self.d.store(data)
        job = row['job_id']; self.official('alpha', 'update_job', job, {'prompt':'Old strict necessary-file prompt'})
        snap = self.snapshot(); self.assertTrue(snap['rows'][0]['prompt_notice'])
        self.assertTrue(self.apply(snap, enabled=False)[1]['results'][0]['ok'])
        self.remove_doc('user')
        self.assertTrue(self.apply(self.snapshot())[1]['results'][0]['ok'])
        self.assertEqual(self.official('alpha','get_job',job)['prompt'], 'Old strict necessary-file prompt')
        config = self.f.homes['alpha']/'config.yaml'
        config.write_text(config.read_text()+'\nmemory:\n  memory_enabled: false\n')
        identity, problem = dream.context_identity(app.EditorState(self.f.homes['alpha']))
        self.assertIn('disabled', problem)
        self.assertFalse(identity['config']['memory_enabled'])

    def test_disabled_pending_selections_persist_without_creating_cron(self):
        result = self.apply(self.snapshot(), enabled=False, time='05:45', modules=['dedup'])[1]
        self.assertTrue(result['results'][0]['ok'], result)
        snap = self.snapshot()
        self.assertEqual(snap['rows'][0]['modules'], ['dedup'])
        self.assertEqual(snap['rows'][0]['time'], '05:45')
        self.assertFalse((self.f.homes['alpha']/'cron').exists())
        self.assertTrue(self.apply(snap)[1]['results'][0]['ok'])
        self.assertEqual(self.snapshot()['rows'][0]['time'], '05:45')

    def test_default_missing_binding_and_cross_profile_id_never_fallback(self):
        self.f.homes['default'] = self.f.homes.pop('alpha')
        unrelated = self.official('beta','create_job','synthetic other profile','0 1 * * *','Dream')
        before = fingerprint(self.f.root)
        with patch.object(dream, 'DEFAULT_JOB_ID', None):
            row = next(r for r in self.f.api('/api/dream')[1]['rows'] if r['profile']=='default')
            self.assertIn('adoption requires',row['error'])
        with patch.object(dream, 'DEFAULT_JOB_ID', unrelated['id']):
            row = next(r for r in self.f.api('/api/dream')[1]['rows'] if r['profile']=='default')
            self.assertIn('Managed job missing',row['error'])
        self.assertEqual(before,fingerprint(self.f.root))

    def test_cron_symlink_refusal_and_malformed_modules(self):
        (self.f.homes['alpha']/'cron').symlink_to(self.f.homes['beta'],target_is_directory=True)
        before = fingerprint(self.f.homes['beta'])
        row = self.f.api('/api/dream')[1]['rows'][0]
        self.assertIn('symlink',row['error'])
        self.assertEqual(before,fingerprint(self.f.homes['beta']))
        (self.f.homes['alpha']/'cron').unlink()
        self.assertEqual(self.f.api('/api/dream/preview',{'profile':'alpha','modules':[{}]})[0],400)
        self.assertEqual(self.f.api('/api/dream/apply',{'library_version':self.snapshot()['library_version'],'changes':[{'profile':{}}]})[0],400)

    def test_security_token_origin_static_allowlist_and_symlink(self):
        self.assertEqual(self.f.api('/api/dream',headers={'X-Hermes-Editor-Token':''})[0],401)
        self.assertEqual(self.f.api('/api/dream/preview',{'modules':['dedup']},headers={'Origin':'http://evil.invalid'})[0],403)
        self.assertEqual(self.f.api('/dream-state.json')[0],404)
        self.assertEqual(self.f.api('/dream_worker.py')[0],404)
        (self.f.editor/'dream-state.json').symlink_to(self.f.homes['alpha']/'config.yaml')
        self.assertEqual(self.f.api('/api/dream')[0],503)


class DreamBrowserTests(unittest.TestCase):
    def open(self, page, f):
        from playwright.sync_api import expect
        page.goto(f.origin+'/#token='+f.state.token)
        expect(page.locator('#mode-agents')).to_be_enabled()
        page.locator('#dream-open').click()
        expect(page.get_by_label('Settings alpha',exact=True)).to_be_enabled(timeout=60000)

    def settled(self, page, profile='alpha'):
        from playwright.sync_api import expect
        expect(page.locator('[data-profile="'+profile+'"] .dream-save-state')).to_have_text('已保存',timeout=60000)

    def test_typography_panes_long_names_errors_and_expanded_prompt(self):
        import shutil
        from playwright.sync_api import sync_playwright, expect
        f = Fixture()
        long_name = 'visual-design-with-a-deliberately-long-agent-name'
        try:
            for name in ('creative-studio', 'engineering', 'finance', 'health', 'review', long_name):
                home = f.root / 'profiles' / name
                shutil.copytree(f.homes['beta'], home)
                f.homes[name] = home
            for key in ('memory', 'user'):
                f.get_state('finance').paths[key].unlink()
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width':1440,'height':1050})
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                self.open(page, f)
                before = fingerprint(f.root)
                for theme in ('light', 'dark'):
                    page.emulate_media(color_scheme='dark' if theme == 'light' else 'light')
                    page.locator('[data-theme-choice="'+theme+'"]').evaluate('(e)=>e.click()')
                    expect(page.locator('html')).to_have_attribute('data-theme', theme)
                    for width in (1440, 900, 390, 320):
                        page.set_viewport_size({'width':width,'height':1050 if width > 760 else 844})
                        page.locator('#dream-close').click()
                        page.locator('#dream-open').click()
                        expect(page.get_by_label('Settings alpha',exact=True)).to_be_enabled(timeout=60000)
                        expect(page.locator('.dream-empty')).to_be_visible()
                        if width > 760:
                            self.assertTrue(page.locator('#dream-detail').evaluate('(e)=>Math.abs(e.getBoundingClientRect().height-e.parentElement.getBoundingClientRect().height)<1'))
                        page.get_by_label('Settings alpha',exact=True).click()
                        self.assertEqual(page.locator('#dream-dialog th').first.evaluate('(e)=>getComputedStyle(e).fontSize'), '13px')
                        self.assertEqual(page.locator('.dream-agent-name').first.evaluate('(e)=>getComputedStyle(e).fontSize'), '13px')
                        self.assertGreaterEqual(float(page.locator('.dream-checks small').first.evaluate('(e)=>parseFloat(getComputedStyle(e).fontSize)')),13)
                        self.assertTrue(page.locator('.dream-checks strong').first.evaluate('(e)=>parseFloat(getComputedStyle(e).fontSize)<=parseFloat(getComputedStyle(document.querySelector("#dream-detail-title")).fontSize)'))
                        if width > 760:
                            self.assertTrue(page.locator('.dream-section-heading').evaluate('(e)=>Math.abs(e.getBoundingClientRect().top-document.querySelector("thead").getBoundingClientRect().top)<1'))
                            self.assertTrue(page.locator('.dream-section-heading').evaluate('(e)=>Math.abs(e.getBoundingClientRect().height-document.querySelector("thead").getBoundingClientRect().height)<2'))
                        page.get_by_label('Settings finance',exact=True).click()
                        expect(page.locator('.dream-issue').first).to_be_visible()
                        page.get_by_label('Settings '+long_name,exact=True).click()
                        expect(page.locator('#dream-detail-title')).to_have_text(long_name)
                        page.locator('#dream-preview summary').click()
                        expect(page.locator('#dream-prompt')).not_to_have_text('正在生成…',timeout=60000)
                        expect(page.locator('#dream-prompt')).not_to_be_empty()
                        self.assertTrue(page.locator('#dream-prompt').evaluate('(e)=>e.scrollWidth<=e.clientWidth'))
                        self.assertTrue(page.locator('#dream-dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth'))
                        self.assertTrue(page.locator('html').evaluate('(e)=>e.scrollWidth<=innerWidth'))
                        page.locator('.dream-table-wrap').evaluate('(e)=>e.scrollLeft=e.scrollWidth')
                        page.locator('#dream-run-all').scroll_into_view_if_needed()
                        expect(page.locator('#dream-run-all')).to_be_in_viewport()
                self.assertEqual(before, fingerprint(f.root), 'Visual inspection must not write fixture settings/documents')
                self.assertEqual(errors, [])
                browser.close()
        finally:
            f.close()

    def test_autosave_queue_focus_profiles_close_and_responsive_themes(self):
        from playwright.sync_api import sync_playwright, expect
        f = Fixture(); f.homes['creative-buddy'] = f.homes.pop('beta')
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1050})
                errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
                self.open(page,f)
                before=fingerprint(f.root)
                page.get_by_label('Settings alpha',exact=True).click()
                expect(page.locator('#dream-apply,#dream-dialog footer,#dream-library,#dream-dialog [role=tab]')).to_have_count(0)
                self.assertEqual(before,fingerprint(f.root))
                pending=[]; applied=[]
                page.route('**/api/dream/apply',lambda r:pending.append(r))
                page.on('request',lambda r:applied.append(r.post_data_json) if r.url.endswith('/api/dream/apply') else None)
                page.get_by_label('Enable alpha',exact=True).check()
                expect(page.locator('[data-profile="alpha"] .dream-save-state')).to_have_text('保存中…')
                page.wait_for_timeout(100)
                self.assertEqual(len(pending),1)
                expect(page.get_by_label('Enable alpha',exact=True)).to_be_focused()
                time=page.get_by_label('Nightly time alpha',exact=True)
                time.fill('04:20'); expect(time).to_be_focused()
                time.fill('04:25'); expect(time).to_be_enabled()
                page.get_by_label('Nightly time creative-buddy',exact=True).fill('05:10')
                expect(page.locator('#dream-detail-title')).to_have_text('creative-buddy')
                expect(page.locator('#dream-run-all')).to_be_disabled()
                expect(page.get_by_label('Run Dream alpha',exact=True)).to_be_disabled()
                # Release only real fixture backend requests; later revisions must advance.
                pending.pop(0).continue_()
                for _ in range(180):
                    page.wait_for_timeout(200)
                    if pending: pending.pop(0).continue_()
                    if all(page.locator('[data-profile="'+n+'"] .dream-save-state').inner_text()=='已保存' for n in ('alpha','creative-buddy')): break
                self.settled(page); self.settled(page,'creative-buddy')
                self.assertGreaterEqual(len(applied),3)
                a=[x['changes'][0] for x in applied if x['changes'][0]['profile']=='alpha']
                self.assertEqual(a[-1]['time'],'04:25'); self.assertNotEqual(a[0]['version'],a[-1]['version'])
                actual=f.api('/api/dream')[1]
                self.assertEqual([r['time'] for r in actual['rows']],['04:25','05:10'])
                page.unroute('**/api/dream/apply')
                expect(page.get_by_label('Nightly time creative-buddy',exact=True)).to_be_focused()
                # Closing flushes debounce and blocks dismissal until verified.
                time.fill('04:30'); page.locator('#dream-close').click()
                expect(page.locator('#dream-dialog')).to_be_visible()
                self.settled(page)
                self.assertEqual(f.api('/api/dream')[1]['rows'][0]['time'],'04:30')
                page.locator('#dream-close').click(); expect(page.locator('#dream-dialog')).not_to_be_visible()
                page.locator('#dream-open').click(); expect(page.get_by_label('Settings alpha',exact=True)).to_be_enabled(timeout=60000)
                page.get_by_label('Settings alpha',exact=True).click()
                for theme in ('light','dark'):
                    page.locator('html').evaluate('(e,t)=>e.dataset.theme=t',theme)
                    self.assertEqual(page.get_by_label('Enable alpha',exact=True).evaluate('(e)=>getComputedStyle(e).backgroundColor'),'rgb(39, 131, 75)')
                    self.assertEqual(page.get_by_label('Enable creative-buddy',exact=True).evaluate('(e)=>getComputedStyle(e).backgroundColor'),'rgb(133, 136, 141)')
                    page.locator('#dream-dialog').screenshot(path=str(f.root/('dream-autosave-'+theme+'-desktop.png')))
                for width in (390,320,768):
                    page.set_viewport_size({'width':width,'height':844})
                    self.assertTrue(page.locator('#dream-dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth'))
                    page.locator('#dream-run-all').scroll_into_view_if_needed()
                    expect(page.locator('#dream-run-all')).to_be_in_viewport()
                    page.locator('.dream-table-wrap').evaluate('(e)=>e.scrollLeft=0')
                    page.locator('#dream-preview summary').scroll_into_view_if_needed()
                    expect(page.locator('#dream-preview summary')).to_be_in_viewport()
                    page.locator('.dream-scroll').evaluate('(e)=>e.scrollTop=0')
                    page.locator('#dream-dialog').screenshot(path=str(f.root/('dream-autosave-dark-'+str(width)+'.png')))
                self.assertEqual(errors,[])
                print('DREAM_AUTOSAVE_SCREENSHOTS='+str(f.root))
                browser.close()
        finally: f.close()

    def test_partial_memory_autosave_and_both_absent_guard(self):
        from playwright.sync_api import sync_playwright, expect
        f = Fixture()
        try:
            (f.homes['alpha']/'memories/USER.md').unlink()
            for key in ('USER','MEMORY'): (f.homes['beta']/('memories/'+key+'.md')).unlink()
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1050})
                errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
                self.open(page,f)
                page.get_by_label('Settings alpha',exact=True).click()
                expect(page.get_by_label('Enable alpha',exact=True)).to_be_enabled()
                expect(page.locator('#dream-detail')).to_contain_text('USER 缺失，将跳过')
                page.get_by_label('Enable alpha',exact=True).check(); self.settled(page)
                self.assertTrue(f.api('/api/dream')[1]['rows'][0]['enabled'])
                self.assertFalse((f.homes['alpha']/'memories/USER.md').exists())
                page.locator('#dream-preview summary').click()
                expect(page.locator('#dream-prompt')).to_contain_text('缺失的跳过')
                page.get_by_label('Settings beta',exact=True).click()
                expect(page.get_by_label('Enable beta',exact=True)).to_be_disabled()
                expect(page.locator('#dream-detail')).to_contain_text('尚无 USER 或 MEMORY，无法启用')
                page.get_by_label('Nightly time beta',exact=True).fill('06:25'); self.settled(page,'beta')
                self.assertFalse((f.homes['beta']/'cron').exists())
                image=f.root/'dream-partial-memory.png'
                page.locator('#dream-dialog').screenshot(path=str(image))
                print('PARTIAL_MEMORY_SCREENSHOT='+str(image))
                self.assertEqual(errors,[]); browser.close()
        finally: f.close()

    def test_invalid_time_empty_modules_failure_retry_and_custom_prompt(self):
        from playwright.sync_api import sync_playwright, expect
        f=Fixture()
        try:
            helper=DreamTests(); helper.f=f
            snap=f.api('/api/dream')[1]; r=snap['rows'][0]
            result=f.api('/api/dream/apply',{'library_version':snap['library_version'],'changes':[dict(profile='alpha',version=r['version'],enabled=True,time='03:30',modules=r['modules'])]})[1]
            job=result['results'][0]['job_id']; custom='Fictional custom review prompt. Keep this exact text.'
            helper.official('alpha','update_job',job,{'prompt':custom})
            missing=f.homes['beta']/'memories/USER.md'; missing.unlink()
            (f.homes['beta']/'memories/MEMORY.md').unlink()
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True); page=browser.new_page(viewport={'width':1440,'height':1050})
                errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
                self.open(page,f)
                writes=[]; page.on('request',lambda r:writes.append(r.post_data_json) if r.url.endswith('/api/dream/apply') else None)
                page.get_by_label('Settings beta',exact=True).click()
                expect(page.get_by_label('Enable beta',exact=True)).to_be_disabled()
                time=page.get_by_label('Nightly time beta',exact=True)
                time.fill(''); time.press_sequentially('25:99',delay=100)
                expect(time).to_be_focused(); expect(time).to_have_attribute('aria-invalid','true')
                page.wait_for_timeout(600); self.assertEqual(writes,[])
                time.fill(''); time.press_sequentially('06:45',delay=100)
                expect(time).to_be_focused(); self.settled(page,'beta')
                self.assertEqual(f.api('/api/dream')[1]['rows'][1]['time'],'06:45')
                self.assertFalse(missing.exists()); self.assertFalse((f.homes['beta']/'cron').exists())
                page.locator('[data-profile="alpha"] .dream-health').click()
                time=page.get_by_label('Nightly time alpha',exact=True)
                time.fill('0'); page.get_by_label('Enable alpha',exact=True).uncheck()
                expect(time).to_have_value('0'); expect(time).to_have_attribute('aria-invalid','true')
                expect(page.locator('[data-profile="alpha"] .dream-save-state')).to_have_text('未保存',timeout=60000)
                actual=f.api('/api/dream')[1]['rows'][0]
                self.assertFalse(actual['enabled']); self.assertEqual(actual['time'],'03:30')
                self.assertEqual(helper.official('alpha','get_job',job)['prompt'],custom)
                time.fill('04:10'); self.settled(page)
                self.assertEqual(helper.official('alpha','get_job',job)['prompt'],custom)
                # Return to the baseline in one input turn: no write, preserve custom prompt.
                n=len(writes)
                page.locator('#dream-detail .dream-checks').evaluate('(e)=>{const i=e.querySelectorAll("input")[1];i.click();i.click()}')
                page.wait_for_timeout(650); self.assertEqual(len(writes),n)
                self.assertEqual(helper.official('alpha','get_job',job)['prompt'],custom)
                # Empty selections are explicit invalid drafts and never reach backend.
                page.locator('#dream-detail .dream-checks').evaluate('(e)=>{e.querySelectorAll("input:checked").forEach(i=>i.click())}')
                expect(page.locator('#dream-module-error')).to_contain_text('至少选择一个')
                page.wait_for_timeout(650); self.assertEqual(len(writes),n)
                # Failure pauses the queue, retains edits, exposes only an error retry.
                def fail(route): route.fulfill(status=200,content_type='application/json',body=json.dumps({'results':[{'profile':'alpha','ok':False,'error':'Synthetic temporary failure'}]}))
                page.route('**/api/dream/apply',fail)
                page.get_by_label('Module alpha 去重',exact=True).check()
                expect(page.locator('[data-profile="alpha"] .dream-save-state')).to_have_text('未保存 · 失败',timeout=60000)
                page.wait_for_timeout(800); count=len(writes)
                time.fill('05:15'); page.wait_for_timeout(800); self.assertEqual(len(writes),count)
                expect(page.get_by_label('Retry save alpha',exact=True)).to_be_visible()
                page.locator('#dream-dialog').screenshot(path=str(f.root/'dream-autosave-failure.png'))
                page.unroute('**/api/dream/apply',fail)
                page.get_by_label('Retry save alpha',exact=True).click(); self.settled(page)
                self.assertEqual(f.api('/api/dream')[1]['rows'][0]['time'],'05:15')
                self.assertIn('**去重**',helper.official('alpha','get_job',job)['prompt'])
                self.assertNotIn('**精简**',helper.official('alpha','get_job',job)['prompt'])
                # Once a previous selection was saved, returning to original composes anew.
                page.get_by_label('Module alpha 精简',exact=True).check(); self.settled(page)
                self.assertIn('**精简**',helper.official('alpha','get_job',job)['prompt'])
                # Real optimistic conflict never overwrites external state or retries itself.
                helper.official('alpha','update_job',job,{'prompt':'External fictional change'})
                time.fill('07:15')
                expect(page.locator('[data-profile="alpha"] .dream-save-state')).to_have_text('未保存 · 失败',timeout=60000)
                count=len(writes); page.wait_for_timeout(900); self.assertEqual(len(writes),count)
                self.assertEqual(helper.official('alpha','get_job',job)['prompt'],'External fictional change')
                page.once('dialog',lambda d:d.dismiss()); page.locator('#dream-close').click()
                expect(page.locator('#dream-dialog')).to_be_visible(); expect(time).to_have_value('07:15')
                page.once('dialog',lambda d:d.accept()); page.locator('#dream-close').click()
                expect(page.locator('#dream-dialog')).not_to_be_visible()
                self.assertEqual(errors,[]); browser.close()
        finally: f.close()

    def test_readback_failure_does_not_drop_other_agent_drafts_or_retry(self):
        from playwright.sync_api import sync_playwright, expect
        f=Fixture()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True); page=browser.new_page()
                errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
                self.open(page,f)
                held=[]; writes=[]
                def hold_first_readback(route):
                    if held: route.continue_()
                    else: held.append(route)
                page.route('**/api/dream?*',hold_first_readback)
                page.on('request',lambda r:writes.append(r.post_data_json) if r.url.endswith('/api/dream/apply') else None)
                page.get_by_label('Enable alpha',exact=True).check()
                for _ in range(150):
                    page.wait_for_timeout(100)
                    if held: break
                self.assertEqual(len(held),1, str(errors)+str(writes)+page.locator('#dream-rows').inner_text())
                expect(page.locator('[data-profile="alpha"] .dream-save-state')).to_have_text('保存中…')
                expect(page.locator('#dream-run-all')).to_be_disabled()
                expect(page.get_by_label('Run Dream alpha',exact=True)).to_be_disabled()
                page.get_by_label('Nightly time alpha',exact=True).fill('04:40')
                page.get_by_label('Nightly time beta',exact=True).fill('05:40')
                # Pending settings must warn on unload, including the readback phase.
                self.assertTrue(page.evaluate("(()=>{const e=new Event('beforeunload',{cancelable:true});window.dispatchEvent(e);return e.defaultPrevented})()"))
                page.wait_for_timeout(650)
                held[0].fulfill(status=503,content_type='application/json',body=json.dumps({'error':'Synthetic readback unavailable'}))
                expect(page.locator('[data-profile="alpha"] .dream-save-state')).to_have_text('未保存 · 失败',timeout=60000)
                self.settled(page,'beta')
                page.unroute('**/api/dream?*')
                expect(page.get_by_label('Nightly time beta',exact=True)).to_be_focused()
                page.get_by_label('Settings alpha',exact=True).click()
                expect(page.get_by_label('Nightly time alpha',exact=True)).to_have_value('04:40')
                expect(page.get_by_label('Retry save alpha',exact=True)).to_be_visible()
                count=len(writes); page.wait_for_timeout(900); self.assertEqual(len(writes),count)
                self.assertEqual([x['changes'][0]['profile'] for x in writes],['alpha','beta'])
                actual=f.api('/api/dream')[1]
                self.assertTrue(actual['rows'][0]['enabled'])
                self.assertNotEqual(actual['rows'][0]['time'],'04:40')
                self.assertEqual(actual['rows'][1]['time'],'05:40')
                # Explicit discard only drops the unsaved draft, never undoes saved enablement.
                page.once('dialog',lambda d:d.accept()); page.locator('#dream-close').click()
                self.assertFalse(page.evaluate("(()=>{const e=new Event('beforeunload',{cancelable:true});window.dispatchEvent(e);return e.defaultPrevented})()"))
                page.locator('#dream-open').click()
                expect(page.get_by_label('Settings alpha',exact=True)).to_be_enabled(timeout=60000)
                expect(page.get_by_label('Enable alpha',exact=True)).to_be_checked()
                # A removed library reference stays visible and preserves its saved prompt.
                page.locator('#dream-close').click()
                snap=f.api('/api/dream')[1]
                f.api('/api/dream/library',{'library_version':snap['library_version'],'modules':[m for m in snap['modules'] if m['id']!='dedup']})
                page.locator('#dream-open').click()
                expect(page.get_by_label('Settings alpha',exact=True)).to_be_enabled(timeout=60000)
                page.get_by_label('Settings alpha',exact=True).click()
                expect(page.locator('#dream-detail')).to_contain_text('有引用的模块已删除')
                page.locator('#dream-preview summary').click()
                expect(page.locator('#dream-prompt')).to_contain_text('**去重**')
                self.assertEqual(errors,[]); browser.close()
        finally: f.close()

    def test_preview_disclosure_loading_errors_and_stale_responses(self):
        from playwright.sync_api import sync_playwright, expect
        f = Fixture()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width':1440,'height':1050})
                errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
                writes=[]
                page.on('request',lambda r:writes.append(r.url) if r.method=='POST' and not r.url.endswith('/preview') else None)
                page.goto(f.origin+'/#token='+f.state.token)
                expect(page.locator('#mode-agents')).to_be_enabled()
                page.locator('#dream-open').click()
                expect(page.get_by_label('Settings alpha',exact=True)).to_be_enabled(timeout=30000)
                page.get_by_label('Settings alpha',exact=True).click()
                before = fingerprint(f.root)
                pending=[]
                page.route('**/api/dream/preview',lambda r:pending.append(r))
                summary=page.locator('#dream-preview summary')
                self.assertEqual(summary.count(),1)
                self.assertEqual(page.locator('#dream-preview-button').count(),0)
                summary.click()
                expect(page.locator('#dream-prompt')).to_have_text('正在生成…')
                self.assertEqual(len(pending),1)
                summary.click()
                with page.expect_response('**/api/dream/preview'):
                    pending.pop().fulfill(status=200,content_type='application/json',body=json.dumps({'prompt':'STALE CLOSED PREVIEW'}))
                expect(page.locator('#dream-preview')).not_to_have_attribute('open','')
                expect(page.locator('#dream-prompt')).to_have_text('')
                summary.click()
                expect(page.locator('#dream-prompt')).to_have_text('正在生成…')
                with page.expect_response('**/api/dream/preview'):
                    pending.pop().fulfill(status=400,content_type='application/json',body=json.dumps({'error':'Synthetic preview failure'}))
                expect(page.locator('#dream-prompt')).to_contain_text('无法生成提示词：')
                expect(page.locator('#dream-prompt')).to_contain_text('Synthetic preview failure')
                summary.click(); summary.click()
                expect(page.locator('#dream-prompt')).to_have_text('正在生成…')
                page.locator('#dream-detail .dream-checks').evaluate('(e)=>{const i=e.querySelectorAll("input")[1];i.click();i.click()}')
                expect(page.locator('#dream-preview')).not_to_have_attribute('open','')
                with page.expect_response('**/api/dream/preview'):
                    pending.pop().fulfill(status=200,content_type='application/json',body=json.dumps({'prompt':'STALE MODULE PREVIEW'}))
                expect(page.locator('#dream-prompt')).to_have_text('')
                expect(page.get_by_label('Module alpha 精简',exact=True)).to_be_checked()
                summary.click()
                expect(page.locator('#dream-prompt')).to_have_text('正在生成…')
                page.get_by_label('Settings beta',exact=True).click()
                with page.expect_response('**/api/dream/preview'):
                    pending.pop().fulfill(status=200,content_type='application/json',body=json.dumps({'prompt':'STALE AGENT PREVIEW'}))
                expect(page.locator('#dream-prompt')).to_have_text('')
                page.unroute('**/api/dream/preview')
                page.locator('#dream-preview summary').click()
                expect(page.locator('#dream-prompt')).to_contain_text('运行绑定')
                self.assertEqual(before,fingerprint(f.root))
                page.once('dialog',lambda d:d.accept()); page.locator('#dream-close').click()
                self.assertEqual(before,fingerprint(f.root))
                self.assertEqual(writes,[])
                self.assertEqual(errors,[])
                browser.close()
        finally:
            f.close()


if __name__=='__main__':
    unittest.main(verbosity=2)
