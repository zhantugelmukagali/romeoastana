import json,unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]
HOST="""<!doctype html><iframe src="https://widget.test/reconciliation" style="width:100%;height:1000px"></iframe>
<script>addEventListener('message',e=>{if(e.data.name==='UserContextRequest')
e.source.postMessage({name:'UserContextResponse',correlationId:e.data.messageId,token:'synthetic'},'https://widget.test');});</script>"""
class ReconciliationBrowserTests(unittest.TestCase):
    def setUp(self):
        self.runtime=sync_playwright().start();self.browser=self.runtime.chromium.launch()
        self.ctx=self.browser.new_context(viewport={"width":390,"height":844});self.page=self.ctx.new_page()
        self.decisions=[];self.conflict=False;self.resolved=False
        self.ctx.route("**/*",self.route)
    def tearDown(self):self.ctx.close();self.browser.close();self.runtime.stop()
    def route(self,r):
        url=r.request.url
        if url.startswith("https://online.moysklad.ru/"):return r.fulfill(content_type="text/html",body=HOST)
        if not url.startswith("https://widget.test/"):return r.abort()
        path=url.split("https://widget.test",1)[1]
        if path=="/api/session":return r.fulfill(json={"token":"synthetic"})
        if path=="/api/me":return r.fulfill(json={"role":"admin"})
        if path=="/api/admin/automation":return r.fulfill(json={"issues":[]})
        if path.startswith("/api/admin/settlements?"):
            items=[] if self.resolved else [dict(id="case",order_id="order",order_name="123",earn_delta=-500,refund_delta=0,
                                               total_delta=-500,available=0,state="pending",reason=None)]
            return r.fulfill(json={"items":items,"next_cursor":None})
        if path.endswith("/resolve"):
            self.decisions.append(r.request.post_data_json)
            if self.conflict:return r.fulfill(status=409,json={"error":"conflict"})
            self.resolved=True;return r.fulfill(json={"state":"waived"})
        file=ROOT/"web"/("reconciliation.html" if path=="/reconciliation" else path.lstrip("/"))
        if file.is_file():return r.fulfill(content_type="text/html" if file.suffix==".html" else "text/css" if file.suffix==".css" else "text/javascript",body=file.read_text())
        return r.fulfill(status=404)
    def open(self):
        self.page.goto("https://online.moysklad.ru/test");self.frame=self.page.frame_locator("iframe")
        self.frame.get_by_role("heading",name="Заказ 123",exact=True).wait_for()
    def test_no_debt_explicit_reason_and_mobile_layout(self):
        self.open()
        self.assertFalse(self.frame.get_by_role("button",name="Выполнить пересчёт",exact=True).is_enabled())
        self.frame.get_by_role("button",name="Сохранить бонусы клиенту",exact=True).click()
        self.frame.get_by_label("Причина решения").fill("Согласовано с клиентом")
        self.frame.get_by_role("button",name="Подтвердить",exact=True).click()
        self.frame.get_by_text("Операций в этом разделе нет.",exact=True).wait_for()
        self.assertEqual(self.decisions,[{"decision":"waive","reason":"Согласовано с клиентом"}])
        self.assertTrue(self.page.frames[1].evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.assertEqual(self.page.frames[1].evaluate("localStorage.length+sessionStorage.length"),0)
    def test_conflict_keeps_dialog_without_false_success(self):
        self.conflict=True;self.open()
        self.frame.get_by_role("button",name="Сохранить бонусы клиенту",exact=True).click()
        self.frame.get_by_label("Причина решения").fill("Проверено")
        self.frame.get_by_role("button",name="Подтвердить",exact=True).click()
        self.frame.get_by_text("Документы изменились или бонусов недостаточно. Обновите список и проверьте решение.",exact=True).wait_for()
        self.assertTrue(self.frame.get_by_role("dialog").is_visible())

if __name__=="__main__":unittest.main()
