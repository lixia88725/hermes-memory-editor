"""Synthetic run-now dispatch. Never contacts the real gateway or jobs."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import dream
import server as app
import test_dream
from test_history import fingerprint, SCRATCH


class TriggerTests(test_dream.DreamTests):
    # Inherit existing read/apply/security checks as regression coverage.
    def target(self, row):
        return {k:row[k] for k in ('profile','job_id','version')}

    def trigger_api(self, payload, headers=None):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1',self.f.state.port,timeout=40)
        hdr = {'X-Hermes-Editor-Token':self.f.state.token,'Origin':self.f.origin,'Content-Type':'application/json'}
        hdr.update(headers or {})
        conn.request('POST','/api/dream/trigger',body=json.dumps(payload),headers=hdr)
        response=conn.getresponse(); result=(response.status,json.loads(response.read())); conn.close(); return result

    def seed(self):
        self.assertTrue(self.apply(self.snapshot())[1]['results'][0]['ok'])
        return self.snapshot()['rows'][0]

    def test_scope_fresh_enabled_exact_id_and_security(self):
        row = self.seed(); before = fingerprint(self.f.root)
        original = dream.Dream.worker
        calls = []
        def worker(obj, identity, action, job_id=None, **extra):
            if action == 'trigger':
                calls.append((identity['home'],job_id)); return {'status':'accepted','receipt':{'session_id':'synthetic','admission_id':'synthetic'}}
            return original(obj,identity,action,job_id,**extra)
        with patch.object(dream.Dream,'worker',worker):
            code,result = self.trigger_api({'targets':[self.target(row)]})
            self.assertEqual(code,200); self.assertEqual(result['results'][0]['status'],'accepted')
            wrong = dict(self.target(row), profile='beta')
            self.assertEqual(self.trigger_api({'targets':[wrong]})[1]['results'][0]['status'],'failed')
            self.assertEqual(len(calls),1)
            for headers, status in [({'Origin':'http://evil.invalid'},403),({'X-Hermes-Editor-Token':''},401)]:
                self.assertEqual(self.trigger_api({'targets':[self.target(row)]},headers=headers)[0],status)
            self.assertEqual(self.f.api('/dream_trigger.py')[0],404)
            self.assertEqual(fingerprint(self.f.root),before)
            self.official('alpha','pause_job',row['job_id'])
            self.assertEqual(self.trigger_api({'targets':[self.target(row)]})[1]['results'][0]['status'],'failed')
            paused = self.snapshot()['rows'][0]
            self.assertEqual(self.trigger_api({'targets':[self.target(paused)]})[1]['results'][0]['status'],'failed')
            self.assertEqual(len(calls),1)

    def test_partial_unknown_and_duplicate_request_validation(self):
        row = self.seed()
        self.assertEqual(self.trigger_api({'targets':[self.target(row)]*2})[0],400)
        self.assertEqual(self.trigger_api({'targets':[dict(self.target(row),enabled=True)]})[0],400)
        original = dream.Dream.worker
        def worker(obj, identity, action, job_id=None, **extra):
            if action == 'trigger':
                raise subprocess.TimeoutExpired('synthetic',45)
            return original(obj,identity,action,job_id,**extra)
        with patch.object(dream.Dream,'worker',worker):
            result = self.trigger_api({'targets':[self.target(row),dict(self.target(row),profile='beta')]})[1]
        self.assertEqual([r['status'] for r in result['results']],['unknown','failed'])

    def test_browser_saved_enabled_lightning_confirmation_and_drafts(self):
        from playwright.sync_api import sync_playwright, expect
        row = self.seed()
        self.official('alpha','create_job','unrelated never triggered','0 1 * * *','Dream')
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1050})
            errors=[]; page.on('pageerror',lambda e: errors.append(str(e)))
            page.goto(self.f.origin+'/#token='+self.f.state.token)
            expect(page.locator('#mode-agents')).to_be_enabled()
            page.locator('#dream-open').click()
            expect(page.get_by_label('Run Dream alpha',exact=True)).to_be_enabled(timeout=30000)
            expect(page.get_by_label('Run Dream beta',exact=True)).to_be_disabled()
            pending=[]; page.route('**/api/dream/trigger',lambda route:pending.append(route))
            page.get_by_label('Settings alpha',exact=True).click()
            saves=[]; page.route('**/api/dream/apply',lambda r:saves.append(r))
            page.get_by_label('Enable alpha',exact=True).uncheck()
            expect(page.get_by_label('Run Dream alpha',exact=True)).to_be_disabled()
            expect(page.locator('#dream-run-all')).to_be_disabled()
            page.wait_for_timeout(100)
            self.assertEqual(len(saves),1)
            page.get_by_label('Enable alpha',exact=True).check()
            saves.pop().continue_()
            for _ in range(180):
                page.wait_for_timeout(200)
                if saves: saves.pop().continue_()
                if page.locator('[data-profile="alpha"] .dream-save-state').inner_text()=='已保存': break
            expect(page.locator('[data-profile="alpha"] .dream-save-state')).to_have_text('已保存',timeout=60000)
            row=self.snapshot()['rows'][0]
            expect(page.locator('#dream-run-all')).to_be_enabled()
            confirmations=[]
            def cancel(d): confirmations.append(d.message); d.dismiss()
            page.once('dialog',cancel); page.locator('#dream-run-all').click()
            self.assertEqual(pending,[])
            self.assertIn('1 个',confirmations[0]); self.assertIn('• alpha',confirmations[0]); self.assertNotIn('• beta',confirmations[0])
            self.assertIn('USER.md / MEMORY.md',confirmations[0])
            page.once('dialog',lambda d:d.accept()); page.locator('#dream-run-all').click()
            expect(page.locator('#dream-run-all')).to_be_disabled()
            expect(page.locator('#dream-close')).to_be_disabled()
            self.assertEqual(len(pending),1)
            payload=pending[0].request.post_data_json
            self.assertEqual(payload,{'targets':[self.target(row)]})
            pending.pop().fulfill(status=200,content_type='application/json',body=json.dumps({'results':[{'profile':'alpha','status':'accepted','receipt':{'session_id':'synthetic','admission_id':'synthetic'}}]}))
            expect(page.locator('#dream-message')).to_contain_text('尚未确认完成')
            expect(page.locator('#dream-run-all')).to_be_disabled()
            expect(page.get_by_label('Enable alpha',exact=True)).to_be_checked()
            expect(page.locator('#dream-apply,#dream-dialog footer')).to_have_count(0)
            page.set_viewport_size({'width':390,'height':844})
            page.evaluate("document.documentElement.dataset.theme='dark'")
            expect(page.locator('#dream-close')).to_be_in_viewport()
            image=self.f.root/'dream-run-now-synthetic.png'
            page.locator('#dream-dialog').screenshot(path=str(image)); print('TRIGGER_SCREENSHOT='+str(image))
            self.assertEqual(errors,[])
            browser.close()

    def adapter_probe(self, row, mode):
        identity = self.d.row('alpha', self.d.load())['_identity']
        repo = Path.home()/'.hermes/hermes-agent'
        # Only discovery/connection (transport) are replaced. Official store
        # lookup, GatewayClient RPC serialization, audit and locking are real.
        script = r'''
import asyncio, contextlib, json, sys
from types import SimpleNamespace
from unittest.mock import patch
from hermes_cli.gateway_client import GatewayClient
import hermes_cli.gateway_runtime as runtime
import hermes_cli.gateway_client as client
import dream_trigger
req=json.load(sys.stdin); mode=req.pop('mode'); sent=[]
if mode=='bindingrace': req['binding']['workspace'] += '-rebound'
if mode=='stalerace': req['revision']='changed-after-read'
if mode=='createdrace':
 from pathlib import Path
 Path(req['home'],'memories/USER.md').write_text('Created after snapshot')
if mode=='deletedrace':
 from pathlib import Path
 Path(req['home'],'memories/MEMORY.md').unlink()
if mode=='unreadable':
 from pathlib import Path
 original_read=Path.read_bytes
 def denied(path):
  if path.name=='MEMORY.md': raise PermissionError('Synthetic admission denied')
  return original_read(path)
 Path.read_bytes=denied
class Wire:
 async def send(self, raw):
  frame=json.loads(raw); sent.append(frame)
  if frame['method']=='cron.status':
   assert frame['params']=={'session_id':'synthetic-owner-session','admission_id':'synthetic-owner-admission'}
   gateway.pending[frame['id']].set_result({'status':'queued'})
   return
  assert frame['method']=='cron.submit'
  assert set(frame['params'])=={'job_id','request_id','extra_prompt'}
  assert frame['params']['job_id']==req['job_id']
  assert frame['params']['extra_prompt'] is None
  assert len(frame['params']['request_id'])==32
  if mode=='timeout': return
  gateway.pending[frame['id']].set_result({'session_id':'synthetic-owner-session','admission_id':'synthetic-owner-admission'})
gateway=GatewayClient(Wire())
@contextlib.asynccontextmanager
async def transport(endpoint=None, **kw):
 assert endpoint is not None
 yield gateway
endpoint=SimpleNamespace(profile_id=req['home'])
with patch.object(runtime,'discover_gateway_endpoint',return_value=SimpleNamespace(state='absent' if mode=='absent' else 'ready',endpoint=endpoint)), patch.object(client,'connect_gateway',transport):
 if mode=='timeout':
  real=gateway.rpc
  async def quick(method, **kw):
   kw['_timeout']=0.01
   return await real(method,**kw)
  gateway.rpc=quick
 try: result=dream_trigger.execute(req)
 except Exception as e: result={'error':str(e)}
print(json.dumps({'result':result,'sent':sent}))
'''
        request = dict(binding=identity,home=identity['home'],generation=identity['generation'],job_id=row['job_id'],revision=self.d.row('alpha',self.d.load())['_cron_revision'],mode=mode)
        result = subprocess.run([str(repo/'venv/bin/python'),'-c',script],cwd=Path(__file__).parent,
            env=dict(os.environ,HERMES_HOME=identity['home'],PYTHONPATH=str(repo)+os.pathsep+str(Path(__file__).parent),PYTHONDONTWRITEBYTECODE='1'),
            input=json.dumps(request),capture_output=True,text=True,timeout=40)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_partial_memory_official_admission_and_races(self):
        row = self.seed()
        user = self.remove_doc('user')
        for key in ('soul','agents'): self.remove_doc(key)
        row = self.snapshot()['rows'][0]
        before = fingerprint(self.f.root)
        probe = self.adapter_probe(row, 'accept')
        self.assertEqual(probe['result']['status'], 'accepted', probe)
        self.assertEqual(before, fingerprint(self.f.root))
        for mode in ('unreadable', 'createdrace'):
            probe = self.adapter_probe(row, mode)
            self.assertIn('changed', probe['result']['error'], probe)
            self.assertEqual(probe['sent'], [])
        self.assertTrue(user.exists())
        self.assertEqual(self.trigger_api({'targets':[self.target(row)]})[1]['results'][0]['status'], 'failed')
        memory = self.remove_doc('memory')
        row = self.snapshot()['rows'][0]
        self.assertEqual(self.adapter_probe(row, 'accept')['result']['status'], 'accepted')
        user.unlink()
        row = self.snapshot()['rows'][0]
        self.assertTrue(row['enabled'])
        probe = self.adapter_probe(row, 'accept')
        self.assertIn('Neither USER nor MEMORY', probe['result']['error'])
        self.assertEqual(probe['sent'], [])
        self.assertEqual(self.trigger_api({'targets':[self.target(row)]})[1]['results'][0]['status'], 'failed')
        memory.write_text('Fictional restored memory')
        row = self.snapshot()['rows'][0]
        probe = self.adapter_probe(row, 'deletedrace')
        self.assertIn('changed', probe['result']['error'])
        self.assertEqual(probe['sent'], [])

    def test_real_official_rpc_composition_and_dispatch_receipt(self):
        row=self.seed(); before=fingerprint(self.f.root)
        probe=self.adapter_probe(row,'accept')
        self.assertEqual(probe['result']['status'],'accepted',probe)
        self.assertEqual([f['method'] for f in probe['sent']],['cron.submit','cron.status'])
        self.assertEqual(probe['result']['receipt']['admission_id'],'synthetic-owner-admission')
        self.assertEqual(before,fingerprint(self.f.root),'admission adapter must not mutate schedule/config/docs')
        probe=self.adapter_probe(row,'timeout')
        self.assertEqual(probe['result']['status'],'unknown',probe); self.assertEqual(len(probe['sent']),1)
        probe=self.adapter_probe(row,'absent')
        self.assertIn('No ready canonical gateway',probe['result']['error']); self.assertEqual(probe['sent'],[])
        for mode in ('bindingrace','stalerace'):
            probe=self.adapter_probe(row,mode)
            self.assertIn('changed',probe['result']['error']); self.assertEqual(probe['sent'],[])
        self.assertEqual(before,fingerprint(self.f.root))
        self.official('alpha','pause_job',row['job_id'])
        probe=self.adapter_probe(self.snapshot()['rows'][0],'accept')
        self.assertIn('not enabled',probe['result']['error']); self.assertEqual(probe['sent'],[])


if __name__=='__main__':
    unittest.main(verbosity=2)
