#!/usr/bin/env python3
"""Real backend + browser checks; all writes confined to a fresh scratch root.
Run: /usr/bin/python3 test_history.py
Requires installed git and (for BrowserTests) installed Playwright/Chromium.
"""
import concurrent.futures
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

SOURCE = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCE))
import server as app

SCRATCH = Path.home() / '.hermes/cache/scratch'


def fingerprint(root):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


class Fixture:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix='memory-editor-history-test-', dir=str(SCRATCH)))
        print('\nFIXTURE_ROOT=' + str(self.root))
        self.editor = self.root / 'editor'
        self.editor.mkdir()
        for name in ('editor.html', *app.STATIC_FILES):
            (self.editor / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, self.editor / name)
        self.homes = {}
        for name in ('alpha', 'beta'):
            home = self.root / 'profiles' / name
            (home / 'memories').mkdir(parents=True)
            workspace = self.root / ('workspace-' + name)
            workspace.mkdir()
            (home / 'config.yaml').write_text('terminal:\n  cwd: ' + str(workspace) + '\nmemory:\n  memory_char_limit: 2200\n  user_char_limit: 1375\n')
            (home / '.env').write_text('NOT_A_REAL_SECRET=do-not-snapshot\n')
            (home / 'sessions.json').write_text('do-not-snapshot-session')
            for key, path in app.file_paths(home, workspace).items():
                path.write_text(name + ' ' + key + ' original\n')
            self.homes[name] = home
        self.patches = [patch.object(app, 'EDITOR_DIR', self.editor),
                        patch.object(app, 'HTML_PATH', self.editor / 'editor.html'),
                        patch.object(app, 'DAEMON_MODE', True),
                        patch.object(app, 'REMOTE_ORIGIN', ''),
                        patch.object(app, 'discover_profiles', lambda: [{'name': n, 'home': str(h)} for n,h in self.homes.items()])]
        for p in self.patches:
            p.start()
        self.state = self.get_state()
        self.http = app.http.server.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.state.port = self.http.server_address[1]
        self.http.editor_state = self.state
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.origin = 'http://127.0.0.1:%s' % self.state.port

    def get_state(self, name='alpha'):
        state = app.EditorState(self.homes[name], token='isolated-test-token')
        state.profile = name
        return state

    def api(self, path, data=None, headers=None, profile='alpha'):
        if data is None:
            path += ('&' if '?' in path else '?') + urlencode({'profile': profile})
        else:
            data = {'profile': profile, **data}
        hdr = {'X-Hermes-Editor-Token': self.state.token, 'Origin': self.origin,
               'Content-Type': 'application/json'}
        hdr.update(headers or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.state.port, timeout=40)
        conn.request('POST' if data is not None else 'GET', path,
                     body=json.dumps(data) if data is not None else None, headers=hdr)
        response = conn.getresponse()
        result = (response.status, json.loads(response.read()))
        conn.close()
        return result

    def read(self, key='memory', profile='alpha'):
        code, doc = self.api('/api/read/' + key, profile=profile)
        assert code == 200 and doc['readable'], doc
        return doc

    def save(self, text, key='memory', profile='alpha', doc=None, note=''):
        d = doc or self.read(key, profile)
        return self.api('/api/save', {'key': key, 'content': text, 'base_hash': d['hash'],
                                    'target_id': d['target_id'], 'note': note}, profile=profile)

    def history(self, doc, profile='alpha'):
        return self.api('/api/history/' + doc['key'] + '?' + urlencode({'target_id': doc['target_id']}), profile=profile)

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        for p in reversed(self.patches):
            p.stop()
        # Kept for independent inspection; scratch cleanup owns retention.


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()

    def tearDown(self):
        self.f.close()

    def test_roundtrip_external_preview_restore_noop_and_legacy(self):
        f = self.f
        doc = f.read()
        before = fingerprint(f.root)
        self.assertEqual(f.history(doc)[1]['versions'], [])
        self.assertEqual(before, fingerprint(f.root))
        self.assertFalse((f.editor / 'history.git').exists())
        code, saved = f.save('changed\n', note='first note')
        self.assertEqual(code, 200, saved)
        versions = f.history(saved)[1]['versions']
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]['note'], 'first note')
        original = versions[-1]['revision']
        legacy = f.editor / 'backups/alpha/memory/legacy-unbound.md'
        legacy.write_text('legacy fixture retained')
        before = fingerprint(f.root)
        code, preview = f.api('/api/preview/memory?' + urlencode({'target_id': doc['target_id'], 'revision': original}))
        self.assertEqual(code, 200, preview)
        self.assertEqual(preview['content'], doc['content'])
        self.assertEqual(preview['current_content'], 'changed\n')
        self.assertEqual(preview['current_hash'], saved['hash'])
        self.assertEqual(preview['comparison'], [['replace', 0, 1, 0, 1]])
        self.assertIn('-changed', preview['diff'])
        self.assertEqual(before, fingerprint(f.root), 'Preview must write nothing')
        self.assertTrue(f.save('changed\n')[1]['unchanged'])
        self.assertEqual(len(f.history(saved)[1]['versions']), 2)
        target = f.get_state().paths['memory']
        target.write_text('external current\n')
        stale = {'key': 'memory', 'revision': original, 'target_id': doc['target_id'], 'base_hash': saved['hash'], 'confirmed': True}
        self.assertEqual(f.api('/api/restore-version', stale)[0], 409)
        fresh = f.read()
        payload = {**stale, 'base_hash': fresh['hash']}
        self.assertEqual(f.api('/api/restore-version', {**payload, 'confirmed': False})[0], 400)
        code, restored = f.api('/api/restore-version', payload)
        self.assertEqual(code, 200, restored)
        self.assertEqual(target.read_text(), doc['content'])
        versions = f.history(restored)[1]['versions']
        self.assertEqual(len(versions), 4)
        self.assertEqual(versions[0]['kind'], 'restore')
        external = f.api('/api/preview/memory?' + urlencode({'target_id': doc['target_id'], 'revision': versions[1]['revision']}))[1]
        self.assertEqual(external['content'], 'external current\n')
        # Restore same bytes still appends a version, never reset.
        self.assertEqual(f.api('/api/restore-version', {**payload, 'base_hash': restored['hash']})[0], 200)
        self.assertEqual(len(f.history(restored)[1]['versions']), 5)
        latest = f.read()
        self.assertEqual(f.api('/api/restore', {'key': 'memory', 'backup': latest['backups'][0]['name'], 'target_id': latest['target_id'], 'base_hash': latest['hash']})[0], 200)
        self.assertTrue(f.read()['backups'])
        # Only snapshots and explicitly permitted filenames, no .git in homes.
        history = app.LocalHistory(f.editor)
        paths = history.git('ls-tree', '-r', '--name-only', 'refs/heads/main').stdout.decode().splitlines()
        self.assertEqual(paths, ['snapshots/' + doc['target_id'] + '/memory.md'])
        self.assertFalse(list((f.root / 'profiles').rglob('.git')))
        self.assertFalse(history.git('remote').stdout)
        self.assertEqual(legacy.read_text(), 'legacy fixture retained')
        self.assertFalse(any(b['name'] == legacy.name for b in f.read()['backups']))

    def test_preview_line_boundaries_and_empty_content(self):
        f = self.f
        target = f.get_state().paths['soul']
        for old, current in [('', 'new\n'), ('old\n', ''), ('same\n', 'same'),
                             ('one\r\ntwo\u2028three\n', 'one\r\ntwo changed\u2028three\n')]:
            target.write_bytes(old.encode('utf-8'))
            code, saved = f.save(current, 'soul')
            self.assertEqual(code, 200, saved)
            versions = f.history(saved)[1]['versions']
            revision = versions[1]['revision']
            before = fingerprint(f.root)
            code, preview = f.api('/api/preview/soul?' + urlencode({'target_id': saved['target_id'], 'revision': revision}))
            self.assertEqual(code, 200, preview)
            self.assertEqual(preview['content'], old)
            self.assertEqual(preview['current_content'], current)
            self.assertEqual(preview['current_hash'], hashlib.sha256(current.encode()).hexdigest())
            current_lines, old_lines = current.splitlines(True), old.splitlines(True)
            for tag, a, b, c, d in preview['comparison']:
                if tag == 'equal':
                    self.assertEqual(current_lines[a:b], old_lines[c:d])
            self.assertEqual(before, fingerprint(f.root))

    def test_four_documents_profiles_targets_and_security(self):
        f = self.f
        revisions = {}
        for profile in ('alpha', 'beta'):
            for key in ('memory', 'user', 'soul', 'agents'):
                code, doc = f.save(profile + ' ' + key + ' changed', key, profile)
                self.assertEqual(code, 200, doc)
                versions = f.history(doc, profile)[1]['versions']
                self.assertEqual(len(versions), 2)
                revisions[(profile, key)] = (doc, versions[0]['revision'])
        alpha, revision = revisions[('alpha', 'memory')]
        beta, _ = revisions[('beta', 'memory')]
        self.assertEqual(f.api('/api/preview/memory?' + urlencode({'target_id': beta['target_id'], 'revision': revision}), profile='beta')[0], 400)
        before = fingerprint(f.root)
        self.assertNotEqual(f.api('/api/restore-version', {'key': 'memory', 'revision': revision, 'target_id': beta['target_id'], 'base_hash': beta['hash'], 'confirmed': True}, profile='beta')[0], 200)
        self.assertEqual(before, fingerprint(f.root))
        self.assertEqual(f.api('/api/preview/user?' + urlencode({'target_id': alpha['target_id'], 'revision': revision}))[0], 409)
        self.assertEqual(f.api('/api/status', headers={'X-Hermes-Editor-Token': ''})[0], 401)
        self.assertEqual(f.api('/api/save', {}, headers={'Origin': 'https://evil.invalid'})[0], 403)
        self.assertEqual(f.api('/api/status', headers={'Host': 'evil.invalid'})[0], 403)
        self.assertEqual(f.save('x' * 2201)[0], 413)
        self.assertEqual(f.api('/api/restore-version', {'confirmed': True, 'revision': None})[0], 400)
        history = app.LocalHistory(f.editor)
        tree = history.git('ls-tree', '-r', '--name-only', 'refs/heads/main').stdout.decode().splitlines()
        self.assertEqual(len(tree), 8)
        self.assertTrue(all(p.startswith('snapshots/') and p.endswith(('/memory.md','/user.md','/soul.md','/agents.md')) for p in tree))
        # Equal content at a rebound workspace is NOT the same target.
        old = f.read('agents')
        home = f.homes['alpha']
        new_workspace = f.root / 'new-workspace'
        new_workspace.mkdir()
        (new_workspace / 'AGENTS.md').write_text(old['content'])
        (home / 'config.yaml').write_text('terminal:\n  cwd: ' + str(new_workspace) + '\n')
        self.assertEqual(f.save('no', 'agents', doc=old)[0], 409)
        self.assertEqual(f.history(old)[0], 409)
        self.assertEqual(f.history(f.read('agents'))[1]['versions'], [])

    def test_locks_concurrent_saves_symlinks_and_git_failure(self):
        f = self.f
        doc = f.read()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(lambda text: f.save(text, doc=doc)[0], ['one', 'two']))
        self.assertEqual(sorted(codes), [200, 409])
        state = f.get_state()
        source = state.paths['soul']
        linked = f.root / 'linked-soul.md'
        source.rename(linked)
        linked.chmod(0o640)
        source.symlink_to(linked)
        code, saved = f.save('linked update', 'soul')
        self.assertEqual(code, 200, saved)
        self.assertTrue(source.is_symlink())
        self.assertEqual(linked.read_text(), 'linked update')
        mode = linked.stat().st_mode & 0o777
        self.assertEqual(mode, 0o640)
        # Rebinding a symlink to equal bytes invalidates read/history/restore IDs.
        replacement = f.root / 'other-soul.md'
        replacement.write_text(saved['content'])
        source.unlink()
        source.symlink_to(replacement)
        self.assertEqual(f.save('must conflict', 'soul', doc=saved)[0], 409)
        self.assertEqual(f.history(saved)[0], 409)
        self.assertEqual(f.history(f.read('soul'))[1]['versions'], [])
        latest = f.read()
        with patch.dict(os.environ, {'PATH': ''}):
            code, error = f.save('missing git must not write', doc=latest)
        self.assertEqual(code, 500)
        self.assertIn('Local Git history unavailable', error['error'])
        self.assertEqual(state.paths['memory'].read_text(), latest['content'])
        # Git unavailable before overwrite fails closed, real context untouched.
        with patch.object(app.LocalHistory, 'record', side_effect=RuntimeError('isolated injected Git failure')):
            self.assertEqual(f.save('blocked', doc=latest)[0], 500)
        self.assertEqual(state.paths['memory'].read_text(), latest['content'])
        # Failure after the atomic write must report saved + explicit warning.
        real_record = app.LocalHistory.record
        def fail_after(instance, key, target, raw, kind, *args, **kwargs):
            if kind == 'save':
                raise RuntimeError('isolated post-write Git failure')
            return real_record(instance, key, target, raw, kind, *args, **kwargs)
        with patch.object(app.LocalHistory, 'record', fail_after):
            code, result = f.save('disk saved')
        self.assertEqual(code, 200)
        self.assertTrue(result['saved'])
        self.assertIn('new Git version failed', result['history_warning'])
        self.assertEqual(state.paths['memory'].read_text(), 'disk saved')


    def test_static_asset_allowlist_and_symlink_rejection(self):
        f=self.f
        for name,content_type in app.STATIC_FILES.items():
            conn=http.client.HTTPConnection('127.0.0.1',f.state.port)
            conn.request('GET','/'+name)
            response=conn.getresponse();body=response.read();conn.close()
            self.assertEqual(response.status,200,name)
            self.assertEqual(response.getheader('Content-Type'),content_type)
            self.assertEqual(body,(SOURCE/name).read_bytes())
            self.assertIn("img-src data:",response.getheader('Content-Security-Policy'))
        for path in ('/server.py','/vendor/vditor/package.json','/vendor/../server.py','/vendor/%2e%2e/server.py','/backups','/history.git/config'):
            conn=http.client.HTTPConnection('127.0.0.1',f.state.port)
            conn.request('GET',path);response=conn.getresponse();response.read();conn.close()
            self.assertEqual(response.status,404,path)
        asset=f.editor/'vditor-adapter.js';asset.unlink();asset.symlink_to(f.homes['alpha']/'.env')
        conn=http.client.HTTPConnection('127.0.0.1',f.state.port)
        conn.request('GET','/vditor-adapter.js');response=conn.getresponse();body=response.read();conn.close()
        self.assertEqual(response.status,404)
        self.assertNotIn(b'do-not-snapshot',body)


class BrowserTests(unittest.TestCase):
    def test_semantic_comparison_and_draft_safety(self):
        from playwright.sync_api import sync_playwright, expect
        f = Fixture()
        try:
            old = '# Working notes\nShared context stays neutral.\nPrevious guidance.\n<img src=x onerror="window.historyInjection=1">\n'
            current = '# Working notes\nShared context stays neutral.\nUpdated guidance for the next session.\n' + ('A long changed passage stays readable and wraps within its pane. ' * 12) + '\n'
            target = f.get_state().paths['soul']
            target.write_text(old)
            code, saved = f.save(current, 'soul', note='<svg onload="window.historyInjection=1">')
            self.assertEqual(code, 200, saved)
            versions = f.history(saved)[1]['versions']
            original, latest = versions[-1]['revision'], versions[0]['revision']
            artifacts = Path(tempfile.mkdtemp(prefix='memory-editor-semantic-screens-', dir=str(SCRATCH)))
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(f.origin + '/#token=' + f.state.token)
                expect(page.locator('#mode-agents')).to_be_enabled()
                for key in ('memory','user','soul','agents'):
                    page.locator('#mode-'+key).click()
                for sample in ('', '\n\n', 'same', 'one\r\ntwo\u2028three\n', 'x\v\f\x1c\x85y'):
                    self.assertEqual(page.evaluate('(s)=>historyLines(s)', sample), sample.splitlines(True))
                page.locator('#ed-soul textarea').fill('UNSAVED DRAFT - never a comparison baseline')
                before = fingerprint(f.root)
                page.locator('[data-history="soul"]').click()
                expect(page.locator('#history-summary')).to_have_text('两个版本内容相同')
                expect(page.locator('#history-draft')).to_be_visible()
                expect(page.locator('#history-restore')).to_be_disabled()
                self.assertEqual(page.locator('#history-current code').all_text_contents(), current.splitlines(True))
                page.locator('#history-list').select_option(original)
                expect(page.locator('#history-selected code')).to_have_text(old.splitlines(True))
                self.assertEqual(''.join(page.locator('#history-selected code').all_text_contents()), old)
                self.assertEqual(''.join(page.locator('#history-current code').all_text_contents()), current)
                expect(page.locator('#history-current .current-only')).to_have_count(2)
                expect(page.locator('#history-selected .historical-only')).to_have_count(2)
                expect(page.locator('#history-timestamp')).not_to_have_text('尚未选择版本')
                self.assertEqual(page.locator('#history-preview img, #history-preview svg').count(), 0)
                self.assertIsNone(page.evaluate('window.historyInjection'))
                page.locator('#history-changes').click()
                expect(page.locator('#history-current code')).to_have_count(2)
                expect(page.locator('#history-selected code')).to_have_count(2)
                self.assertNotIn('Shared context stays neutral.', page.locator('#history-preview').inner_text())
                page.locator('#history-full').click()
                expect(page.locator('#history-current code')).to_have_count(4)
                # Selection never swaps the current and historical sides.
                page.locator('#history-list').select_option(latest)
                expect(page.locator('#history-summary')).to_have_text('两个版本内容相同')
                self.assertEqual(''.join(page.locator('#history-current code').all_text_contents()), current)
                page.locator('#history-changes').click()
                expect(page.locator('#history-current .history-empty')).to_contain_text('没有当前新增内容')
                page.locator('#history-full').click()
                page.locator('#history-list').select_option(original)
                expect(page.locator('#history-current .current-only')).to_have_count(2)
                for scheme in ('light', 'dark'):
                    page.emulate_media(color_scheme=scheme)
                    for width in (1440, 800, 390, 320):
                        page.set_viewport_size({'width': width, 'height': 1000})
                        panes = page.locator('.history-pane').evaluate_all('(els)=>els.map(e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,right:r.right,bottom:r.bottom}})')
                        if width > 600:
                            self.assertEqual(panes[0]['y'], panes[1]['y'])
                            self.assertLess(panes[0]['x'], panes[1]['x'])
                        else:
                            self.assertGreaterEqual(panes[1]['y'], panes[0]['bottom'])
                        self.assertTrue(all(r['x'] >= 0 and r['right'] <= width for r in panes))
                        self.assertTrue(page.locator('#history-dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth'))
                        self.assertTrue(page.locator('.history-content').first.evaluate('(e)=>e.scrollWidth<=e.clientWidth'))
                        if width in (1440, 390):
                            page.screenshot(path=str(artifacts / ('semantic-%s-%s.png' % (scheme, width))))
                        page.locator('#history-restore').scroll_into_view_if_needed()
                        restore_bounds = page.locator('#history-restore').bounding_box()
                        self.assertGreaterEqual(restore_bounds['y'], 0)
                        self.assertLessEqual(restore_bounds['y'] + restore_bounds['height'], 1000)
                        page.locator('#history-dialog').evaluate('(e)=>e.scrollTop=0')
                # Preview, mode switches and Escape have zero file effects.
                self.assertEqual(before, fingerprint(f.root))
                page.keyboard.press('Escape')
                expect(page.locator('#ed-soul textarea')).to_have_value('UNSAVED DRAFT - never a comparison baseline')
                self.assertEqual(before, fingerprint(f.root))
                # Resolve draft explicitly through Reload; history cannot discard it.
                page.once('dialog', lambda d: d.accept())
                page.locator('[data-reload="soul"]').click()
                expect(page.locator('#ed-soul textarea')).to_have_value(current)
                page.locator('[data-history="soul"]').click()
                expect(page.locator('#history-restore')).to_be_enabled()
                page.locator('#history-list').select_option(original)
                expect(page.locator('#history-selected code')).to_have_text(old.splitlines(True))
                # A changed disk baseline is not silently adopted on selection.
                target.write_text('external content\n')
                page.locator('#history-list').select_option(latest)
                expect(page.locator('#history-error')).to_contain_text('磁盘内容已变化')
                expect(page.locator('#history-restore')).to_be_disabled()
                self.assertEqual(page.locator('#history-current').text_content(), current)
                page.locator('#history-close').click()
                page.locator('[data-history="soul"]').click()
                expect(page.locator('#history-current code')).to_have_text('external content\n')
                expect(page.locator('#history-restore')).to_be_enabled()
                page.locator('#history-list').select_option(original)
                expect(page.locator('#history-selected code')).to_have_text(old.splitlines(True))
                messages = []
                def accept_confirmation(d):
                    messages.append(d.message)
                    d.accept()
                page.once('dialog', accept_confirmation)
                page.locator('#history-restore').click()
                expect(page.locator('#state-soul')).to_have_class('file-state conflict')
                self.assertEqual(target.read_text(), 'external content\n')
                self.assertIn(str(target), messages[0])
                self.assertIn('所选时间：', messages[0])
                self.assertIn('当前已保存内容会先保留', messages[0])
                self.assertEqual(errors, [])
                browser.close()
                print('\nSEMANTIC_ARTIFACTS=' + str(artifacts))
        finally:
            f.close()

    def test_real_browser(self):
        from playwright.sync_api import sync_playwright, expect
        f = Fixture()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(f.origin + '/#token=' + f.state.token)
                expect(page.locator('#mode-agents')).to_be_enabled()
                for key in ('memory','user','soul','agents'):
                    page.locator('#mode-'+key).click()
                expect(page.locator('#profile')).to_have_value('alpha')
                page.locator('#ed-memory textarea').fill('unsaved draft preserved during preview')
                initial = fingerprint(f.root)
                page.locator('[data-history="memory"]').click()
                expect(page.locator('#history-error')).to_contain_text('暂无历史版本')
                page.keyboard.press('Meta+s')
                # A following real request forms a barrier after the key handler.
                f.history(f.read())
                self.assertEqual(initial, fingerprint(f.root))
                self.assertFalse((f.editor / 'history.git').exists())
                page.locator('#history-close').click()
                expect(page.locator('#ed-memory textarea')).to_have_value('unsaved draft preserved during preview')
                page.locator('#ed-memory textarea').fill('browser changed\n')
                page.locator('#save-all').click()
                expect(page.locator('#state-memory')).to_have_text('Saved · version recorded')
                self.assertEqual(f.get_state().paths['memory'].read_text(), 'browser changed\n')
                before = fingerprint(f.root)
                page.locator('[data-history="memory"]').click()
                expect(page.locator('#history-list option')).to_have_count(2)
                expect(page.locator('#history-restore')).to_be_enabled()
                original = page.locator('#history-list option').last.get_attribute('value')
                page.locator('#history-list').select_option(original)
                expect(page.locator('#history-current code')).to_have_text('browser changed\n')
                expect(page.locator('#history-selected code')).to_have_text('alpha memory original\n')
                expect(page.locator('#history-full')).to_have_attribute('aria-pressed', 'true')
                expect(page.locator('#history-current .change-label')).to_have_text('当前新增')
                expect(page.locator('#history-selected .change-label')).to_have_text('历史中有，当前已移除')
                page.locator('#history-changes').click()
                expect(page.locator('#history-changes')).to_have_attribute('aria-pressed', 'true')
                expect(page.locator('#history-full')).to_have_attribute('aria-pressed', 'false')
                expect(page.locator('#history-selected code')).to_have_text('alpha memory original\n')
                self.assertEqual(before, fingerprint(f.root))
                # Cancellation does not write.
                page.once('dialog', lambda dialog: dialog.dismiss())
                page.locator('#history-restore').click()
                self.assertEqual(before, fingerprint(f.root))
                # Successful explicit restore appends and updates actual disk/UI.
                page.locator('[data-history="memory"]').click()
                expect(page.locator('#history-restore')).to_be_enabled()
                page.locator('#history-list').select_option(original)
                expect(page.locator('#history-preview')).to_contain_text('alpha memory original')
                page.once('dialog', lambda dialog: dialog.accept())
                page.locator('#history-restore').click()
                expect(page.locator('#state-memory')).to_have_text('Restored · new version recorded')
                expect(page.locator('#ed-memory textarea')).to_have_value('alpha memory original\n')
                self.assertEqual(f.get_state().paths['memory'].read_text(), 'alpha memory original\n')
                page.locator('[data-history="memory"]').click()
                expect(page.locator('#history-list option')).to_have_count(3)
                expect(page.locator('#history-restore')).to_be_enabled()
                # External edits after preview must conflict, not overwrite.
                page.locator('#history-list').select_option(original)
                expect(page.locator('#history-selected code')).to_have_text('alpha memory original\n')
                f.get_state().paths['memory'].write_text('external browser conflict\n')
                page.once('dialog', lambda dialog: dialog.accept())
                page.locator('#history-restore').click()
                expect(page.locator('#state-memory')).to_have_class('file-state conflict')
                self.assertEqual(f.get_state().paths['memory'].read_text(), 'external browser conflict\n')
                page.locator('[data-reload="memory"]').click()
                expect(page.locator('#state-memory')).to_have_text('Loaded')
                page.get_by_role('tab', name='beta', exact=True).click()
                expect(page.locator('#ed-agents textarea')).to_have_value('beta agents original\n')
                page.locator('[data-history="memory"]').click()
                expect(page.locator('#history-error')).to_contain_text('暂无历史版本')
                page.keyboard.press('Escape')
                page.locator('#ed-memory textarea').fill('beta browser change')
                page.get_by_role('tab', name='alpha', exact=True).click()
                expect(page.locator('#switch-dialog')).to_be_visible()
                page.locator('#switch-cancel').click()
                expect(page.locator('#profile')).to_have_value('beta')
                page.locator('#save-all').click()
                expect(page.locator('#state-memory')).to_have_text('Saved · version recorded')
                page.get_by_role('tab', name='alpha', exact=True).click()
                expect(page.locator('#ed-agents textarea')).to_have_value('alpha agents original\n')
                # Responsive compact headers and popup, light and dark.
                geometry = []
                for scheme in ('light', 'dark'):
                    page.emulate_media(color_scheme=scheme)
                    for width in (1440, 1100, 800, 390, 320):
                        page.set_viewport_size({'width': width, 'height': 1000})
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                        heights = page.locator('.panel-header').evaluate_all('(els)=>els.map(e=>e.getBoundingClientRect().height)')
                        self.assertLess(max(heights), 110)
                        page.locator('[data-history="memory"]').click()
                        expect(page.locator('#history-restore')).to_be_enabled()
                        self.assertTrue(page.locator('#history-dialog').evaluate('(e)=>e.getBoundingClientRect().right<=innerWidth'))
                        geometry.append({'scheme': scheme, 'width': width, 'headers': heights})
                        if width in (1440, 390):
                            page.screenshot(path=str(f.root / ('history-%s-%s.png' % (scheme, width))))
                        page.locator('#history-close').click()
                self.assertEqual(errors, [])
                (f.root / 'geometry.json').write_text(json.dumps(geometry, indent=2))
                browser.close()
                print('\nBROWSER_ARTIFACTS=' + str(f.root))
        finally:
            f.close()


class VditorIntegrationTests(unittest.TestCase):
    def test_exact_source_ir_input_lifecycle_and_backend(self):
        from playwright.sync_api import sync_playwright, expect
        import time
        f = Fixture()
        artifacts = Path(tempfile.mkdtemp(prefix='memory-editor-vditor-screens-', dir=str(SCRATCH)))
        try:
            original = '# 中文API 标题\r\n\r\n\r\n- 一级\r\n    - 二级\r\n\r\n| 项目 | 内容 |\r\n| --- | --- |\r\n| Ω | 🦊 |\r\n\r\n```js\r\nconst separator = "§";\r\nconsole.log("你好 🌏");\r\n```\r\n\r\n[链接](https://example.com/a?q=中文#§)\r\n\r\n§\r\n\r\n结束'
            target = f.get_state().paths['memory']
            original_bytes = b'\xef\xbb\xbf' + original.encode()
            target.write_bytes(original_bytes)
            f.get_state('beta').paths['agents'].unlink()
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1600, 'height': 1000}, device_scale_factor=2)
                errors, requests, failed = [], [], []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('request', lambda r: requests.append((r.url, r.method)))
                page.on('requestfailed', lambda r: failed.append((r.url, r.failure)))
                page.goto(f.origin + '/#token=' + f.state.token)
                expect(page.locator('#mode-agents')).to_be_enabled()
                value = lambda key='memory': page.evaluate('(k)=>editors[k].getValue()', key)
                dirty = lambda key='memory': page.evaluate('(k)=>files[k].dirty', key)
                def source(key='memory'):
                    b = page.locator('#mode-'+key)
                    if b.get_attribute('aria-pressed') == 'false': b.click()
                    return page.locator('#source-'+key)
                def ir(key='memory'):
                    b = page.locator('#mode-'+key)
                    if b.get_attribute('aria-pressed') == 'true': b.click()
                    return page.locator('#rich-'+key+' .vditor-ir [contenteditable=true]')
                def end(key='memory'):
                    e=ir(key)
                    e.focus()
                    e.evaluate('(e)=>{const r=document.createRange();r.selectNodeContents(e.lastElementChild);r.collapse(false);const s=getSelection();s.removeAllRanges();s.addRange(r)}')
                    return e
                self.assertEqual(value(), original)
                self.assertFalse(dirty())
                self.assertTrue(page.locator('#rich-memory h1').inner_text().endswith('中文API 标题'))
                self.assertGreater(page.locator('#rich-memory table td').count(), 0)
                self.assertGreater(page.locator('#rich-memory pre').count(), 0)
                self.assertEqual(page.locator('#rich-memory .vditor-ir .vditor-reset').evaluate('(e)=>getComputedStyle(e).fontSize'), '13px')
                self.assertEqual(page.locator('#rich-memory h1').evaluate('(e)=>getComputedStyle(e).fontSize'), '19px')
                self.assertEqual(page.locator('#rich-memory h1').evaluate('(e)=>getComputedStyle(e,"::before").display'), 'none')
                before = fingerprint(f.root)
                for key in ('memory','user','soul','agents'):
                    source(key); ir(key)
                self.assertEqual(value(), original)
                self.assertFalse(dirty())
                page.locator('#save-all').click()
                expect(page.locator('#global-status')).to_have_text('No changes')
                self.assertEqual(before, fingerprint(f.root))
                self.assertEqual(target.read_bytes(), original_bytes)
                # Even an explicit unchanged API submission preserves BOM and creates no history.
                code, noop=f.save(original)
                self.assertEqual(code,200,noop)
                self.assertTrue(noop['unchanged'])
                self.assertEqual(before,fingerprint(f.root))
                # Source loading itself never normalizes CRLF; actual native input may.
                source().focus()
                page.keyboard.press('ControlOrMeta+End')
                page.keyboard.insert_text(' source修改')
                source_value=value()
                self.assertIn('source修改',source_value)
                self.assertTrue(dirty())
                ir();source();self.assertEqual(value(),source_value);self.assertTrue(dirty())
                page.keyboard.press('ControlOrMeta+s')
                expect(page.locator('#state-memory')).to_have_text('Saved · version recorded')
                self.assertEqual(target.read_bytes(),source_value.encode())
                page.reload();expect(page.locator('#mode-agents')).to_be_enabled()
                self.assertEqual(value(),source_value);self.assertFalse(dirty())
                # Real IR input, browser selection and native Vditor undo restore exact baseline.
                end();page.wait_for_timeout(200);page.keyboard.insert_text(' IR追加');page.wait_for_timeout(300)
                self.assertTrue(dirty());self.assertIn('IR追加',value())
                expect(page.locator('#state-memory')).to_contain_text('IR may normalize')
                page.keyboard.press('Shift+ArrowLeft')
                self.assertEqual(page.evaluate('getSelection().toString()'),'加')
                page.keyboard.press('ArrowRight');page.keyboard.press('ControlOrMeta+z');page.wait_for_timeout(350)
                self.assertEqual(value(),source_value);self.assertFalse(dirty())
                # Synthetic composition guards save until both compositionend and settled input.
                e=end();e.dispatch_event('compositionstart',{'data':''})
                self.assertTrue(page.evaluate('composing()'))
                page.keyboard.insert_text(' 组合提交')
                page.evaluate('saveAll()')
                self.assertEqual(target.read_bytes(),source_value.encode())
                expect(page.locator('#global-status')).to_contain_text('Finish text composition')
                e.dispatch_event('compositionend',{'data':'组合提交'});page.wait_for_timeout(350)
                self.assertFalse(page.evaluate('composing()'));self.assertIn('组合提交',value());self.assertTrue(dirty())
                # Real clipboard paste event, processed by Vditor, without clipboard permissions.
                e=end()
                e.evaluate('(e)=>{const d=new DataTransfer();d.setData("text/plain"," 粘贴Ω🦊");e.dispatchEvent(new ClipboardEvent("paste",{clipboardData:d,bubbles:true,cancelable:true}))}')
                page.wait_for_timeout(350)
                edited=value();self.assertIn('粘贴Ω🦊',edited)
                for token in ['```js','const separator = "§";','console.log("你好 🌏");','[链接](https://example.com/a?q=中文#§)','\n§\n','Ω','🦊']:
                    self.assertIn(token,edited)
                # Native source undo and switching retain the dirty draft.
                s=source();s.focus();page.keyboard.press('ControlOrMeta+End');page.keyboard.insert_text(' 可撤销')
                self.assertIn('可撤销',value());page.keyboard.press('ControlOrMeta+z');self.assertEqual(value(),edited)
                ir();self.assertTrue(dirty());self.assertEqual(value(),edited)
                # Capture edits immediately before Save; freeze actual contenteditable during real slow save.
                end();page.keyboard.insert_text(' 紧接保存')
                snapshot=value()
                entered,release=threading.Event(),threading.Event()
                real_save=app.EditorState.save_document
                def delayed(instance,*args,**kwargs):
                    entered.set()
                    if not release.wait(20): raise RuntimeError('test release timeout')
                    return real_save(instance,*args,**kwargs)
                with patch.object(app.EditorState,'save_document',delayed):
                    page.locator('#save-all').click()
                    try:
                        self.assertTrue(entered.wait(5))
                        expect(page.locator('#save-all')).to_have_text('Saving…')
                        expect(page.locator('#save-all')).to_be_disabled()
                        expect(page.locator('#rich-memory .vditor-ir [role=textbox]')).to_have_attribute('contenteditable','false')
                        expect(page.get_by_role('tab', name='alpha', exact=True)).to_have_attribute('aria-disabled','true')
                        expect(page.locator('#mode-memory')).to_be_disabled()
                    finally: release.set()
                    expect(page.locator('#save-all')).to_have_text('Save')
                expect(page.locator('#state-memory')).to_have_text('Saved · version recorded')
                self.assertEqual(target.read_text(),snapshot)
                self.assertFalse(dirty())
                page.locator('[data-reload=memory]').click();expect(page.locator('#state-memory')).to_have_text('Loaded')
                self.assertEqual(value(),snapshot)
                # Native source freezes too, failed real Git capture preserves draft and can recover.
                source().fill('recoverable draft\n')
                with patch.object(app.LocalHistory,'record',side_effect=RuntimeError('isolated injected Git failure')):
                    page.locator('#save-all').click()
                    expect(page.locator('#state-memory')).to_contain_text('isolated injected Git failure')
                    expect(page.locator('#save-all')).to_have_text('Save')
                self.assertTrue(dirty());self.assertEqual(value(),'recoverable draft\n');self.assertEqual(target.read_text(),snapshot)
                expect(source()).to_be_enabled()
                page.locator('#save-all').click();expect(page.locator('#state-memory')).to_have_text('Saved · version recorded')
                self.assertEqual(target.read_text(),'recoverable draft\n')
                # Effective entry budget, duplicate removal, Unicode code points and over-budget rejection.
                source().fill('  alpha  \n§\nalpha\n§\n🦊')
                expect(page.locator('#stats-memory')).to_contain_text('budget 9/2200')
                page.locator('#save-all').click();expect(page.locator('#state-memory')).to_have_text('Saved · version recorded')
                base=target.read_bytes()
                source().fill('x'*2201)
                page.get_by_role('tab', name='beta', exact=True).click();expect(page.locator('#switch-dialog')).to_be_visible()
                page.locator('#switch-save').click()
                expect(page.locator('#state-memory')).to_contain_text('budget exceeded')
                expect(page.locator('#profile')).to_have_value('alpha')
                self.assertEqual(target.read_bytes(),base);self.assertTrue(dirty())
                # Cancel / explicit discard, missing-file readonly, refresh and Save-and-switch.
                page.get_by_role('tab', name='beta', exact=True).click();page.locator('#switch-cancel').click()
                self.assertEqual(value(),'x'*2201)
                page.get_by_role('tab', name='beta', exact=True).click();page.locator('#switch-discard').click()
                expect(page.locator('#state-agents')).to_have_text('Unavailable')
                expect(page.locator('#mode-agents')).to_be_disabled()
                expect(page.locator('#source-agents')).to_be_disabled()
                expect(page.locator('#rich-agents .vditor-ir [role=textbox]')).to_have_attribute('contenteditable','false')
                expect(page.locator('#stats-agents')).to_have_text('—')
                self.assertFalse(f.get_state('beta').paths['agents'].exists())
                page.locator('#refresh-profiles').click();expect(page.get_by_role('tab')).to_have_count(2)
                source().fill('beta saved on switch\n')
                page.get_by_role('tab', name='alpha', exact=True).click();page.locator('#switch-save').click()
                expect(page.locator('#profile')).to_have_value('alpha');expect(page.locator('#mode-agents')).to_be_enabled()
                self.assertEqual(f.get_state('beta').paths['memory'].read_text(),'beta saved on switch\n')
                self.assertEqual(target.read_bytes(),base)
                # Real external conflict preserves local draft; read recovery uses same live backend.
                source().fill('local conflict draft')
                target.write_text('external disk\n')
                page.evaluate('checkStatus()')
                expect(page.locator('#state-memory')).to_have_class('file-state conflict')
                page.locator('#save-all').click();expect(page.locator('#state-memory')).to_have_class('file-state conflict')
                self.assertEqual(value(),'local conflict draft');self.assertEqual(target.read_text(),'external disk\n')
                page.once('dialog',lambda d:d.accept());page.locator('[data-reload=memory]').click()
                expect(page.locator('#state-memory')).to_have_text('Loaded');self.assertEqual(value(),'external disk\n')
                # Clean status refresh remains the heartbeat and picks up external edits.
                target.write_text('external clean refresh\n')
                page.evaluate('checkStatus()');expect(source()).to_have_value('external clean refresh\n')
                # Same real restore flow also reloads the rendered IR DOM.
                ir();page.locator('[data-history=memory]').click()
                expect(page.locator('#history-restore')).to_be_enabled()
                old_revision=page.locator('#history-list option').last.get_attribute('value')
                page.locator('#history-list').select_option(old_revision)
                expect(page.locator('#history-selected code')).to_have_text(original.splitlines(True))
                page.once('dialog',lambda d:d.accept());page.locator('#history-restore').click()
                expect(page.locator('#state-memory')).to_have_text('Restored · new version recorded')
                self.assertEqual(value(),original);self.assertEqual(target.read_bytes(),original_bytes)
                self.assertFalse(dirty());self.assertGreater(page.locator('#rich-memory table td').count(),0)
                geometry=[]
                for scheme in ('light','dark'):
                    page.emulate_media(color_scheme=scheme)
                    for width in (1600,1100,800,390,320):
                        page.set_viewport_size({'width':width,'height':1000})
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                        heights=page.locator('.panel-header').evaluate_all('(els)=>els.map(e=>e.getBoundingClientRect().height)')
                        self.assertLess(max(heights),110)
                        self.assertTrue(page.locator('#mode-memory').evaluate('(e)=>e.scrollWidth<=e.clientWidth'))
                        geometry.append({'scheme':scheme,'width':width,'headers':heights})
                        if width in (1600,390):page.screenshot(path=str(artifacts/('editor-%s-%s.png'%(scheme,width))))
                # A failed read is unavailable, not an empty editable document; retry recovers.
                soul_target=f.get_state().paths['soul'];soul_raw=soul_target.read_bytes();soul_target.unlink()
                page.locator('[data-reload=soul]').click();expect(page.locator('#state-soul')).to_have_text('Unavailable')
                expect(page.locator('#mode-soul')).to_be_disabled();expect(page.locator('#stats-soul')).to_have_text('—')
                self.assertFalse(soul_target.exists())
                soul_target.write_bytes(soul_raw)
                page.locator('[data-reload=soul]').click();expect(page.locator('#state-soul')).to_have_text('Loaded')
                expect(page.locator('#mode-soul')).to_be_enabled()
                self.assertEqual(errors,[])
                self.assertTrue(all(url.startswith(f.origin+'/') for url,method in requests))
                # CSP blocks even same-origin document images and all remote images.
                source().fill('# Image probe\n\n![remote](https://example.com/never-load.png)\n\n![local](/api/status)\n\n```mermaid\ngraph TD; A-->B\n```\n\n$$x+y$$\n')
                before_requests=len(requests)
                ir();page.wait_for_timeout(350)
                self.assertTrue(any('never-load.png' in u and 'csp' in why.lower() for u,why in failed))
                self.assertFalse(any('/js/' in url for url,method in requests[before_requests:]))
                self.assertIsNone(page.evaluate('window.historyInjection'))
                (artifacts/'geometry.json').write_text(json.dumps(geometry,indent=2))
                (artifacts/'network.json').write_text(json.dumps({'requests':requests,'failed':failed},indent=2))
                print('\nVDITOR_ARTIFACTS='+str(artifacts))
                browser.close()
        finally:
            f.close()


class AgentTabTests(unittest.TestCase):
    def test_tabs_keyboard_refresh_missing_busy_and_mobile(self):
        from playwright.sync_api import sync_playwright, expect
        f = Fixture()
        artifacts = Path(tempfile.mkdtemp(prefix='memory-editor-agent-tabs-screens-', dir=str(SCRATCH)))
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000}, device_scale_factor=2)
                errors=[]
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(f.origin+'/#token='+f.state.token)
                alpha=page.get_by_role('tab', name='alpha', exact=True)
                beta=page.get_by_role('tab', name='beta', exact=True)
                expect(alpha).to_have_attribute('aria-disabled','false')
                expect(page.get_by_role('tablist',name='Agent',exact=True)).to_be_visible()
                self.assertEqual(page.get_by_role('combobox',name='Agent',exact=True).count(),0)
                expect(alpha).to_have_attribute('aria-selected','true')
                expect(page.get_by_role('tabpanel',name='alpha',exact=True)).to_be_visible()
                # First reads create lock files; warm both synthetic targets before fingerprinting.
                for key in ('memory','user','soul','agents'):f.read(key,profile='beta')
                before=fingerprint(f.root)
                alpha.focus(); page.keyboard.press('ArrowRight')
                expect(beta).to_be_focused(); expect(alpha).to_have_attribute('aria-selected','true')
                page.keyboard.press('Home');expect(alpha).to_be_focused()
                page.keyboard.press('End');expect(beta).to_be_focused()
                page.keyboard.press('ArrowRight');expect(alpha).to_be_focused()
                page.keyboard.press('ArrowLeft');expect(beta).to_be_focused()
                page.keyboard.press('Enter')
                expect(beta).to_have_attribute('aria-selected','true')
                expect(page.locator('#state-agents')).to_have_text('Loaded')
                self.assertEqual(before,fingerprint(f.root))
                page.locator('#mode-memory').click()
                page.locator('#source-memory').fill('beta draft retained')
                alpha.click();expect(page.locator('#switch-dialog')).to_be_visible()
                page.keyboard.press('Escape')
                expect(beta).to_have_attribute('aria-selected','true')
                expect(page.locator('#source-memory')).to_have_value('beta draft retained')
                self.assertEqual(before,fingerprint(f.root))
                alpha.click();page.locator('#switch-discard').click()
                expect(alpha).to_have_attribute('aria-selected','true')
                expect(page.locator('#state-agents')).to_have_text('Loaded')
                page.locator('#source-memory').fill('alpha persisted before switch')
                beta.click();page.locator('#switch-save').click()
                expect(beta).to_have_attribute('aria-selected','true')
                expect(page.locator('#state-agents')).to_have_text('Loaded')
                self.assertEqual(f.get_state().paths['memory'].read_text(),'alpha persisted before switch')
                # Refresh discovers names from the real endpoint without loading or changing drafts.
                for i in range(12):f.homes['agent-long-name-%02d'%i]=f.homes['alpha']
                page.locator('#source-memory').fill('beta missing profile draft')
                page.locator('#refresh-profiles').click()
                expect(page.get_by_role('tab')).to_have_count(14)
                expect(beta).to_have_attribute('aria-selected','true')
                expect(page.locator('#source-memory')).to_have_value('beta missing profile draft')
                removed=f.homes.pop('beta')
                page.locator('#refresh-profiles').click()
                missing=page.get_by_role('tab',name='beta (unavailable)',exact=True)
                expect(missing).to_have_attribute('aria-selected','true')
                expect(missing).to_have_attribute('aria-disabled','true')
                expect(page.locator('#save-all')).to_be_disabled()
                expect(page.locator('#source-memory')).to_have_value('beta missing profile draft')
                f.homes['beta']=removed
                page.locator('#refresh-profiles').click()
                expect(beta).to_have_attribute('aria-disabled','false')
                # Pending real read freezes tabs, refresh and source; a forged event cannot switch.
                entered,release=threading.Event(),threading.Event()
                real_read=app.EditorState.read_document
                def delayed(instance,*args,**kwargs):
                    entered.set()
                    if not release.wait(20):raise RuntimeError('test read release timeout')
                    return real_read(instance,*args,**kwargs)
                with patch.object(app.EditorState,'read_document',delayed):
                    page.once('dialog',lambda d:d.accept())
                    page.locator('[data-reload=memory]').click()
                    try:
                        self.assertTrue(entered.wait(5))
                        expect(alpha).to_have_attribute('aria-disabled','true')
                        expect(page.locator('#refresh-profiles')).to_be_disabled()
                        expect(page.locator('#source-memory')).to_be_disabled()
                        alpha.dispatch_event('click')
                        expect(beta).to_have_attribute('aria-selected','true')
                    finally:release.set()
                    expect(page.locator('#state-memory')).to_have_text('Loaded')
                page.reload()
                expect(alpha).to_have_attribute('aria-disabled','false')
                # Shared panel lives immediately below the seam; active fill matches it.
                for scheme in ('light','dark'):
                    page.emulate_media(color_scheme=scheme)
                    for width in (1440,1100,800,390,320):
                        page.set_viewport_size({'width':width,'height':1000})
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                        self.assertTrue(page.locator('#agent-tabs').evaluate('(e)=>e.scrollWidth>e.clientWidth'))
                        self.assertTrue(page.locator('#refresh-profiles').evaluate('(e)=>{const r=e.getBoundingClientRect();return r.x>=0&&r.right<=innerWidth}'))
                        self.assertTrue(page.evaluate('document.querySelector("#agent-bar").getBoundingClientRect().bottom===document.querySelector(".meta").getBoundingClientRect().top && document.querySelector(".meta").getBoundingClientRect().bottom===document.querySelector("#panels").getBoundingClientRect().top'))
                        if width in (1440,390):page.screenshot(path=str(artifacts/('tabs-%s-%s.png'%(scheme,width))))
                # Touch/mobile context: real tap, scrolling within tab strip, last document reachable.
                context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True,device_scale_factor=2)
                mobile=context.new_page();mobile.goto(f.origin+'/#token='+f.state.token)
                expect(mobile.get_by_role('tab',name='alpha',exact=True)).to_have_attribute('aria-disabled','false')
                mobile.get_by_role('tab',name='beta',exact=True).tap()
                expect(mobile.get_by_role('tab',name='beta',exact=True)).to_have_attribute('aria-selected','true')
                expect(mobile.locator('#state-agents')).to_have_text('Loaded')
                mobile.locator('#panel-agents').scroll_into_view_if_needed()
                expect(mobile.locator('#panel-agents')).to_be_in_viewport()
                self.assertTrue(mobile.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                self.assertEqual(errors,[])
                context.close();browser.close()
                print('\nAGENT_TAB_ARTIFACTS='+str(artifacts))
        finally:f.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
