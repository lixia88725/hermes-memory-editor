#!/usr/bin/env python3
"""Main chrome regression; real backend with synthetic homes only."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from test_history import Fixture, SCRATCH, fingerprint


class MainLayoutTests(unittest.TestCase):
    def test_layout_status_switching_and_actions(self):
        from playwright.sync_api import sync_playwright, expect
        f = Fixture()
        artifacts = Path(os.environ.get('LAYOUT_ARTIFACTS') or tempfile.mkdtemp(prefix='main-layout-check-', dir=SCRATCH))
        # Reads create target locks; prime both profiles before fingerprinting.
        for profile in ('alpha', 'beta'):
            for key in ('memory', 'user', 'soul', 'agents'):
                f.read(key, profile)
        artifacts.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={'width': 1440, 'height': 900})
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                def fictional_paths(route):
                    response = route.fetch()
                    data = response.json()
                    for key in ('home', 'workspace'):
                        if data.get(key):
                            data[key] = data[key].replace(str(f.root), '/fictional/hermes')
                    route.fulfill(response=response, json=data)
                page.route('**/api/meta?*', fictional_paths)
                page.goto(f.origin + '/#token=' + f.state.token)
                expect(page.locator('#save-all')).to_be_enabled()
                before = fingerprint(f.root)
                alpha = page.get_by_role('tab', name='alpha', exact=True)
                beta = page.get_by_role('tab', name='beta', exact=True)
                expect(alpha.locator('#connection')).to_have_count(1)
                expect(beta.locator('#connection')).to_have_count(0)
                expect(alpha).to_have_attribute('aria-describedby', 'connection-description')
                self.assertNotIn('Connected', page.locator('body').inner_text())
                page.evaluate('window.originalConnection=document.getElementById("connection")')
                width = alpha.bounding_box()['width']
                colors = []
                for value, label in [(True, '编辑器连接正常'), (False, '编辑器连接中断'), (None, '正在连接')]:
                    page.evaluate('(v)=>setOnline(v)', value)
                    expect(page.locator('#connection')).to_have_attribute('title', label)
                    expect(page.locator('#connection-description')).to_have_text(label)
                    colors.append(page.locator('#connection').evaluate('(e)=>getComputedStyle(e).backgroundColor'))
                    self.assertEqual(width, alpha.bounding_box()['width'])
                self.assertEqual(len(set(colors)), 3)
                page.locator('#refresh-profiles').click()
                expect(page.locator('#save-all')).to_be_enabled()
                self.assertTrue(page.evaluate('originalConnection===document.getElementById("connection")'))
                alpha.focus()
                page.keyboard.press('ArrowRight')
                expect(beta).to_be_focused()
                expect(alpha).to_have_attribute('aria-selected', 'true')
                page.keyboard.press('Enter')
                expect(beta).to_have_attribute('aria-selected', 'true')
                expect(page.locator('#save-all')).to_be_enabled()
                expect(beta.locator('#connection')).to_have_count(1)
                self.assertTrue(page.locator('#workspace').get_attribute('title').endswith('workspace-beta'))
                page.locator('#mode-memory').click()
                page.locator('#ed-memory textarea').fill('synthetic draft')
                alpha.click()
                expect(page.locator('#switch-dialog')).to_be_visible()
                page.locator('#switch-cancel').click()
                expect(beta.locator('#connection')).to_have_count(1)
                expect(page.locator('#ed-memory textarea')).to_have_value('synthetic draft')
                alpha.click()
                page.locator('#switch-discard').click()
                expect(alpha).to_have_attribute('aria-selected', 'true')
                expect(page.locator('#save-all')).to_be_enabled()
                self.assertEqual(before, fingerprint(f.root))
                page.locator('#save-all').click()  # no-op save, real backend
                expect(page.locator('#save-all')).to_be_enabled()
                self.assertEqual(before, fingerprint(f.root))
                page.get_by_role('button', name='Dream', exact=True).click()
                expect(page.locator('#dream-dialog')).to_be_visible()
                expect(page.locator('#dream-close')).to_be_enabled()
                page.locator('#dream-close').click()
                expect(page.locator('#dream-dialog')).not_to_be_visible()
                page.reload()
                expect(page.locator('#save-all')).to_be_enabled()
                geometry = page.locator('#toolbar,#agent-bar,.meta,#panels,.panel-header,.editor-wrap').evaluate_all('(es)=>es.map(e=>({id:e.id,cls:e.className,y:e.getBoundingClientRect().y,height:e.getBoundingClientRect().height}))')
                (artifacts / 'after.json').write_text(json.dumps(geometry, indent=2))
                for theme in ('light', 'dark'):
                    page.emulate_media(color_scheme='dark' if theme == 'light' else 'light')
                    page.locator('[data-theme-choice="'+theme+'"]').click()
                    expect(page.locator('html')).to_have_attribute('data-theme', theme)
                    for width in (1440, 900, 390, 320):
                        page.set_viewport_size({'width': width, 'height': 900})
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                        bar = page.locator('#agent-bar').bounding_box()
                        meta = page.locator('.meta').bounding_box()
                        panels = page.locator('#panels').bounding_box()
                        self.assertGreaterEqual(meta['y'], bar['y'] + bar['height'])
                        self.assertGreaterEqual(panels['y'], meta['y'] + meta['height'])
                        starts = page.locator('.editor-wrap').evaluate_all('(es)=>es.map(e=>e.getBoundingClientRect().y)')
                        if width == 1440:
                            self.assertEqual(len(set(starts)), 1)
                        self.assertEqual(page.locator('#panels').evaluate('(e)=>getComputedStyle(e).gridTemplateColumns.split(" ").length'), 4 if width > 999 else 2 if width > 600 else 1)
                        # Allow the existing button color transitions to settle for review.
                        page.wait_for_timeout(200)
                        page.screenshot(path=str(artifacts / ('after-%s-%s.png' % (theme, width))))
                self.assertEqual(errors, [])
                browser.close()
                print('LAYOUT_ARTIFACTS=' + str(artifacts))
        finally:
            f.close()

    def test_warning_paths_failure_and_status_node_survive(self):
        from playwright.sync_api import sync_playwright, expect
        f = Fixture()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={'width': 320, 'height': 900})
                def meta(route):
                    response = route.fetch()
                    data = response.json()
                    if 'profile=alpha' in route.request.url:
                        data.update(home='/fictional/' + 'long-home/' * 30, workspace='/fictional/' + 'long-workspace/' * 30,
                                    shared_workspace=['fictional-agent'], config_warning='Synthetic configuration warning: ' + 'readable warning ' * 35)
                    route.fulfill(response=response, json=data)
                page.route('**/api/meta?*', meta)
                page.goto(f.origin + '/#token=' + f.state.token)
                expect(page.locator('#save-all')).to_be_enabled()
                expect(page.locator('#config-warning')).to_be_visible()
                expect(page.locator('#shared-workspace')).to_be_visible()
                self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                self.assertEqual(page.locator('.meta-path').first.evaluate('(e)=>getComputedStyle(e).opacity'), '0.5')
                self.assertEqual(page.locator('#config-warning').evaluate('(e)=>getComputedStyle(e).opacity'), '1')
                for key in ('home', 'workspace'):
                    e = page.locator('#' + key)
                    self.assertEqual(e.text_content(), e.get_attribute('title'))
                    self.assertTrue(e.evaluate('(e)=>e.scrollWidth>e.clientWidth'))
                page.get_by_role('tab', name='beta', exact=True).click()
                expect(page.locator('#save-all')).to_be_enabled()
                expect(page.locator('#config-warning')).not_to_be_visible()
                expect(page.locator('#shared-workspace')).not_to_be_visible()
                page.route('**/api/**', lambda route: route.abort())
                page.get_by_role('tab', name='alpha', exact=True).click()
                expect(page.locator('#connection')).to_have_class('disconnected')
                expect(page.locator('#save-all')).to_be_enabled()
                expect(page.locator('#home')).to_have_text('')
                expect(page.locator('.agent-tab[aria-selected="true"] #connection')).to_have_count(1)
                page.unroute('**/api/**')
                page.locator('#refresh-profiles').click()
                expect(page.locator('#save-all')).to_be_enabled()
                expect(page.locator('.agent-tab[aria-selected="true"] #connection')).to_have_count(1)
                browser.close()
        finally:
            f.close()


if __name__ == '__main__':
    unittest.main()
