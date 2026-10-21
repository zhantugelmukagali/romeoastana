"""Browser UX tests use a fake host transport, not a real MoySklad installation."""
import json
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]
CASE={"id":"11111111-1111-4111-8111-111111111111","return_id":"22222222-2222-4222-8222-222222222222",
      "wallet_id":"33333333-3333-4333-8333-333333333333","points":1000,"balance":300,"available":300,
      "state":"pending","resolution_reason":None}
HOST="""<!doctype html><iframe title="БонусПро" src="https://widget.test/returns" style="width:100%;height:850px"></iframe>
<script>window.addEventListener('message', e => {if(e.data.name==='UserContextRequest') {
e.source.postMessage({name:'UserContextResponse',correlationId:e.data.messageId,token:'synthetic-context'},'https://widget.test');
}});</script>"""


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.runtime=sync_playwright().start()
        self.browser=self.runtime.chromium.launch()
        self.context=self.browser.new_context(viewport={"width":1280,"height":960})
        self.page=self.context.new_page()
        self.host=HOST
        self.pending=[dict(CASE)]
        self.decisions=[];self.hold_returns=False;self.held_returns=None
        self.context.route("**/*",self.route)

    def tearDown(self):
        self.context.close()
        self.browser.close()
        self.runtime.stop()

    def route(self,route):
        url=route.request.url
        if url=="https://online.moysklad.ru/host-sender":
            return route.fulfill(content_type="text/html",body="""<script>
            window.deliver=(target,message)=>target.postMessage(message,'https://widget.test');
            </script>""")
        if url.startswith("https://online.moysklad.ru/"):
            return route.fulfill(content_type="text/html",body=self.host)
        if not url.startswith("https://widget.test/"):
            return route.abort()
        path=url.split("https://widget.test",1)[1]
        if path.startswith("/api/"):
            if path=="/api/session":
                return route.fulfill(json={"token":"synthetic-session","expires_in":900})
            if path=="/api/me":
                return route.fulfill(json={"role":"admin"})
            if path.endswith("/resolve"):
                self.decisions.append(route.request.post_data_json)
                self.pending=[]
                return route.fulfill(json={"state":"waived"})
            data={"items":list(self.pending),"next_cursor":None}
            if self.hold_returns:
                self.hold_returns=False;self.held_returns=(route,data);return
            return route.fulfill(json=data)
        file=ROOT/"web"/("returns.html" if path=="/returns" else path.lstrip("/"))
        if file.is_file():
            mime="text/html" if file.suffix==".html" else ("text/css" if file.suffix==".css" else "text/javascript")
            return route.fulfill(content_type=mime,body=file.read_text())
        return route.fulfill(status=404)

    def open(self):
        self.page.goto("https://online.moysklad.ru/test-host")
        self.frame=self.page.frame_locator("iframe")
        self.frame.get_by_role("heading",name="Возвраты",exact=True).wait_for()
        self.frame.get_by_role("button",name="Сохранить бонусы клиенту").wait_for()

    def test_admin_reason_required_and_single_audited_submission(self):
        self.open()
        self.assertFalse(self.frame.get_by_role("button",name="Отменить начисление",exact=True).is_enabled())
        self.frame.get_by_role("button",name="Сохранить бонусы клиенту",exact=True).click()
        self.frame.get_by_label("Причина решения").fill("  ")
        self.frame.get_by_role("button",name="Подтвердить",exact=True).click()
        self.frame.get_by_text("Укажите причину решения.",exact=True).wait_for()
        self.assertEqual(self.decisions,[])
        self.frame.get_by_label("Причина решения").fill("Возврат согласован с руководителем")
        self.frame.get_by_role("button",name="Подтвердить",exact=True).click()
        self.frame.get_by_text("Возвратов в этом разделе нет.",exact=True).wait_for()
        self.assertEqual(self.decisions,[{"decision":"waive","reason":"Возврат согласован с руководителем"}])

    def test_cancel_and_mobile_layout(self):
        self.page.set_viewport_size({"width":390,"height":844})
        self.open()
        self.frame.get_by_role("button",name="Сохранить бонусы клиенту",exact=True).click()
        self.frame.get_by_role("button",name="Отмена",exact=True).click()
        self.assertFalse(self.frame.get_by_role("dialog").is_visible())
        self.assertEqual(self.decisions,[])
        frame=self.page.frames[1]
        self.assertTrue(frame.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
        self.assertEqual(frame.evaluate("localStorage.length"),0)
        self.assertEqual(frame.evaluate("sessionStorage.length"),0)

    def test_gwt_sibling_frame_can_deliver_verified_origin_context(self):
        self.context.add_init_script("""if(location.hostname==='widget.test'){
          window.addEventListener('message',event=>{
            if(event.data?.name==='UserContextResponse')window.hostSender={
              direct:event.source===parent,sibling:event.source.parent===parent};
          });
        }""")
        self.host="""<!doctype html><script>
        let ready;const relayReady=new Promise(resolve=>ready=resolve);
        window.addEventListener('message',async e=>{
          if(e.data?.name!=='UserContextRequest')return;
          await relayReady;
          document.querySelector('#gwt').contentWindow.deliver(e.source,{
            name:'UserContextResponse',correlationId:e.data.messageId,token:'synthetic-context'});
        });
        </script>
        <iframe title="БонусПро" src="https://widget.test/returns" style="width:100%;height:850px"></iframe>
        <iframe id="gwt" hidden src="https://online.moysklad.ru/host-sender" onload="ready()"></iframe>"""
        self.page.goto("https://online.moysklad.ru/test-host")
        frame=self.page.frame_locator('iframe[src="https://widget.test/returns"]')
        frame.get_by_role("button",name="Сохранить бонусы клиенту",exact=True).wait_for()
        self.assertEqual(self.page.frames[1].evaluate("window.hostSender"),{"direct":False,"sibling":True})
        self.assertEqual(self.page.frames[1].evaluate("localStorage.length+sessionStorage.length"),0)

    def test_direct_open_requires_moysklad(self):
        self.page.goto("https://widget.test/returns")
        self.page.get_by_text("Откройте БонусПро из МойСклад.",exact=True).wait_for()
        self.assertFalse(self.page.get_by_role("button",name="Обновить",exact=True).is_visible())

    def test_context_request_waits_for_host_iframe_load_registration(self):
        self.host="""<!doctype html><script>
        let ready=false;
        window.addEventListener('message',e=>{
          if(ready && e.data.name==='UserContextRequest')
            e.source.postMessage({name:'UserContextResponse',
              correlationId:e.data.messageId,token:'synthetic-context'},'https://widget.test');
        });
        </script><iframe title="БонусПро" src="https://widget.test/returns"
          onload="ready=true;this.contentWindow.postMessage({name:'Open',messageId:90},'https://widget.test')"
          style="width:100%;height:850px"></iframe>"""
        self.open()
        self.assertTrue(self.frame.get_by_role("button",name="Сохранить бонусы клиенту",exact=True).is_visible())

    def test_null_correlation_host_error_is_specific_and_does_not_echo_private_fields(self):
        self.host="""<!doctype html><script>
        window.addEventListener('message',e=>{
          if(e.data.name==='UserContextRequest')
            e.source.postMessage({name:'InvalidMessageError',correlationId:null,
              invalidMessage:{name:e.data.name,messageId:e.data.messageId},
              errors:[{code:1004,error:'private-token-value-do-not-echo'}]},'https://widget.test');
        });
        </script><iframe title="БонусПро" src="https://widget.test/returns"
          style="width:100%;height:850px"></iframe>"""
        self.page.goto("https://online.moysklad.ru/test-host")
        frame=self.page.frame_locator("iframe")
        frame.get_by_text("МойСклад отклонил подключение. Код BP-CTX-1004.",exact=True).wait_for()
        self.assertNotIn("private-token-value",self.page.frames[1].locator("body").inner_text())
        self.assertTrue(frame.get_by_role("button",name="Повторить подключение",exact=True).is_visible())

    def test_untrusted_source_does_not_authorize_context_and_timeout_has_only_flags(self):
        self.host="""<!doctype html><script>
        window.addEventListener('message',e=>{
          if(e.data.name==='UserContextRequest')
            e.source.postMessage({name:'UnrelatedMessage',correlationId:e.data.messageId+1,
              token:'private-token-value-do-not-echo'},'https://widget.test');
        });
        </script><iframe title="БонусПро" src="https://widget.test/returns"
          style="width:100%;height:850px"></iframe>"""
        self.context.add_init_script("""if(location.hostname==='widget.test'){
          const timer=window.setTimeout.bind(window);
          window.setTimeout=(f,ms,...args)=>timer(f,ms===10000?500:ms,...args);
        }""")
        self.page.goto("https://online.moysklad.ru/test-host")
        frame=self.page.frame_locator("iframe")
        frame.get_by_role("heading",name="Возвраты",exact=True).wait_for()
        self.page.frames[1].evaluate("""window.postMessage({
          name:'UserContextResponse',correlationId:1,token:'private-token-value-do-not-echo'
        },'*')""")
        frame.get_by_text("МойСклад не ответил. Повторите подключение. Код BP-CTX-P1H1O0R0S0E0.",exact=True).wait_for()
        body=self.page.frames[1].locator("body").inner_text()
        self.assertNotIn("private-token-value",body)
        self.assertFalse(frame.get_by_role("button",name="Обновить",exact=True).is_visible())


    def test_refresh_disables_previous_return_actions_without_enabling_insufficient_balance(self):
        self.open();self.hold_returns=True
        with self.page.expect_request("https://widget.test/api/admin/returns?state=pending"):
            self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.assertFalse(self.frame.get_by_role("button",name="Сохранить бонусы клиенту",exact=True).is_enabled())
        self.page.frames[1].evaluate("document.querySelector('#cases .actions button:last-child').click()")
        self.assertFalse(self.frame.get_by_role("dialog").is_visible())
        self.assertEqual(self.decisions,[])
        self.assertIsNotNone(self.held_returns)
        held,data=self.held_returns
        with self.page.expect_response("https://widget.test/api/admin/returns?state=pending") as response:
            held.fulfill(json=data)
        response.value.json()
        self.frame.get_by_role("button",name="Сохранить бонусы клиенту",exact=True).click()
        self.assertFalse(self.frame.get_by_role("button",name="Отменить начисление",exact=True).is_enabled())
        self.frame.get_by_label("Причина решения").fill("Возврат согласован")
        self.frame.get_by_role("button",name="Подтвердить",exact=True).click()
        self.frame.get_by_text("Возвратов в этом разделе нет.",exact=True).wait_for()
        self.assertEqual(self.decisions,[{"decision":"waive","reason":"Возврат согласован"}])

