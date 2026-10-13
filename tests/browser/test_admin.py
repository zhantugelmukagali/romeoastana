"""Browser UX tests use a fake host transport, not a real MoySklad installation."""
import json
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]
CASE={"id":"11111111-1111-4111-8111-111111111111","return_id":"22222222-2222-4222-8222-222222222222",
      "wallet_id":"33333333-3333-4333-8333-333333333333","points":1000,"balance":300,"available":300,
      "state":"pending","resolution_reason":None}
HOST="""<!doctype html><iframe title="БонусПро" src="https://widget.test/admin" style="width:100%;height:850px"></iframe>
<script>window.addEventListener('message', e => {if(e.data.name==='UserContextRequest') {
e.source.postMessage({name:'UserContextResponse',correlationId:e.data.messageId,token:'synthetic-context'},'https://widget.test');
}});</script>"""


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.runtime=sync_playwright().start()
        self.browser=self.runtime.chromium.launch()
        self.context=self.browser.new_context(viewport={"width":1280,"height":960})
        self.page=self.context.new_page()
        self.pending=[dict(CASE)]
        self.decisions=[]
        self.context.route("**/*",self.route)

    def tearDown(self):
        self.context.close()
        self.browser.close()
        self.runtime.stop()

    def route(self,route):
        url=route.request.url
        if url.startswith("https://online.moysklad.ru/"):
            return route.fulfill(content_type="text/html",body=HOST)
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
            return route.fulfill(json={"items":self.pending,"next_cursor":None})
        file=ROOT/"web"/("index.html" if path=="/admin" else path.lstrip("/"))
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

    def test_direct_open_requires_moysklad(self):
        self.page.goto("https://widget.test/admin")
        self.page.get_by_text("Откройте БонусПро из МойСклад.",exact=True).wait_for()
        self.assertFalse(self.page.get_by_role("button",name="Обновить",exact=True).is_visible())
