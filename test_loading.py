#!/usr/bin/env python3
"""Loading regression checks. All writes and screenshots use synthetic fixtures."""
import concurrent.futures
import hashlib
import os
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from test_history import Fixture, SCRATCH, fingerprint
import server as app


class ConfigReadTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        with app._CONFIG_CACHE_LOCK:
            app._CONFIG_CACHE.clear()

    def tearDown(self):
        self.f.close()

    def test_concurrent_reads_parse_once_and_return_independent_projections(self):
        real_run = app.subprocess.run
        with patch.object(app.subprocess, 'run', wraps=real_run) as run:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(app.read_memory_config, [self.f.homes['alpha']] * 8))
            self.assertEqual(run.call_count, 1)
        results[0]['terminal_cwd'] = 'not shared mutable state'
        self.assertNotEqual(results[0], results[1])
        for cached in app._CONFIG_CACHE.values():
            self.assertEqual(set(cached), set(app._default_memory_config()))

    def test_equal_size_equal_mtime_change_rebinds_immediately(self):
        f = self.f
        before = f.read('agents')
        path = f.homes['alpha'] / 'config.yaml'
        stat = path.stat()
        old = path.read_bytes()
        # Existing beta workspace has an extra character? Use equal-size sibling.
        new_workspace = f.root / 'workspace-omega'
        new_workspace.mkdir()
        (new_workspace / 'AGENTS.md').write_text('new isolated workspace\n')
        updated = old.replace(b'workspace-alpha', b'workspace-omega')
        self.assertEqual(len(old), len(updated))
        path.write_bytes(updated)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(f.save('must not write', 'agents', doc=before)[0], 409)
        self.assertNotEqual(f.read('agents')['target_id'], before['target_id'])
        self.assertEqual((new_workspace / 'AGENTS.md').read_text(), 'new isolated workspace\n')

    def test_failures_not_cached_and_missing_not_confused_with_invalid(self):
        home = self.f.homes['alpha']
        with patch.object(app.subprocess, 'run', side_effect=subprocess.TimeoutExpired('yaml', 15)):
            self.assertTrue(app.read_memory_config(home)['config_error'])
        self.assertFalse(app.read_memory_config(home)['config_error'])
        path = home / 'config.yaml'
        path.write_text('memory: invalid\n')
        self.assertTrue(app.read_memory_config(home)['config_error'])
        path.write_bytes(b'')
        with patch.object(app.subprocess, 'run', side_effect=subprocess.TimeoutExpired('yaml', 15)):
            self.assertTrue(app.read_memory_config(home)['config_error'])
        path.unlink()
        missing = app.read_memory_config(home)
        self.assertFalse(missing['config_error'])
        self.assertIn('not found', missing['config_warning'])
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('fixture')):
            self.assertIn('PermissionError', app.read_memory_config(home)['config_error'])

    def test_projection_cache_is_bounded_and_document_bytes_are_never_cached(self):
        f = self.f
        with patch.object(app, '_CONFIG_CACHE_LIMIT', 2):
            for n in range(4):
                (f.homes['alpha'] / 'config.yaml').write_text('memory:\n  memory_char_limit: %s\n' % n)
                app.read_memory_config(f.homes['alpha'])
            self.assertEqual(len(app._CONFIG_CACHE), 2)
        f = self.f
        original = f.read('soul')
        target = f.get_state().paths['soul']
        target.write_text('fresh external synthetic content\n')
        current = f.read('soul')
        self.assertNotEqual(current['hash'], original['hash'])
        self.assertEqual(current['hash'], hashlib.sha256(target.read_bytes()).hexdigest())


class LoadingBrowserTests(unittest.TestCase):
    def test_concurrent_reads_single_paint_reveal_and_partial_failure(self):
        from playwright.sync_api import sync_playwright, expect
        f = Fixture()
        seen = []
        real_get = app.Handler.do_GET
        def delayed_get(handler):
            path = handler.path.split('?')[0]
            if path.startswith('/api/read/'):
                seen.append((path, time.monotonic()))
                if path.endswith('agents'):
                    time.sleep(.4)
                if path.endswith('user'):
                    handler._error(503, 'isolated read unavailable')
                    return
            return real_get(handler)
        try:
            with patch.object(app.Handler, 'do_GET', delayed_get), sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                requests = []
                page.on('request', lambda r: requests.append(r.url.split('?')[0]))
                page.add_init_script("""window.partialFrames=[];function sample(){
                  const states=['memory','user','soul','agents'].map(k=>document.getElementById('state-'+k)?.textContent||'');
                  const loaded=states.filter(s=>s.startsWith('Loaded')).length;
                  if(loaded>0&&loaded<3)partialFrames.push(states);
                  requestAnimationFrame(sample)}requestAnimationFrame(sample);""")
                page.goto(f.origin + '/#token=' + f.state.token)
                expect(page.locator('#mode-agents')).to_be_enabled()
                self.assertEqual(len(seen), 4)
                self.assertLess(max(t for _, t in seen)-min(t for _, t in seen), .25)
                self.assertEqual(page.evaluate('window.partialFrames'), [])
                for suffix in ('/js/lute/lute.min.js', '/js/i18n/zh_CN.js'):
                    self.assertEqual(sum(u.endswith(suffix) for u in requests), 1)
                expect(page.locator('#mode-user')).to_be_disabled()
                expect(page.locator('#state-user')).to_have_text('isolated read unavailable')
                expect(page.locator('#blocker-user')).to_contain_text('Read failed')
                expect(page.locator('#stats-user')).to_have_text('—')
                self.assertFalse(page.evaluate('files.user.readable'))
                self.assertFalse(page.evaluate('KEYS.some(k=>files[k].dirty)'))
                before = fingerprint(f.root)
                page.evaluate('saveAll()')
                self.assertEqual(before, fingerprint(f.root), 'no-op Save must not write')
                page.screenshot(path=str(f.root / 'loading-desktop.png'))
                page.set_viewport_size({'width': 390, 'height': 1000})
                page.screenshot(path=str(f.root / 'loading-phone.png'))
                browser.close()
        finally:
            f.close()

    def test_stale_epoch_cannot_adopt_delayed_batch(self):
        from playwright.sync_api import sync_playwright
        f = Fixture()
        real_get = app.Handler.do_GET
        started = threading.Event()
        def delayed_get(handler):
            if handler.path.startswith('/api/read/'):
                started.set()
                time.sleep(.4)
            return real_get(handler)
        try:
            with patch.object(app.Handler, 'do_GET', delayed_get), sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(f.origin + '/#token=' + f.state.token)
                self.assertTrue(started.wait(5))
                page.evaluate('epoch++')
                page.wait_for_timeout(800)
                self.assertFalse(page.evaluate('KEYS.some(k=>files[k].readable)'))
                self.assertTrue(page.evaluate("KEYS.every(k=>document.getElementById('blocker-'+k).classList.contains('show'))"))
                browser.close()
        finally:
            f.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
