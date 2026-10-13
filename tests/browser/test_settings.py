"""Settings UX exercised in Chromium with synthetic host/API transport."""
import json
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]
HOST="""<!doctype html><iframe title="БонусПро" src="https://widget.test/settings" style="width:100%;height:1100px"></iframe>
<script>window.addEventListener('message',e=>{if(e.data.name==='UserContextRequest')
e.source.postMessage({name:'UserContextResponse',correlationId:e.data.messageId,token:'synthetic'},'https://widget.test');});</script>"""


class SettingsBrowserTests(unittest.TestCase):
    def setUp(self):
        self.runtime=sync_playwright().start()
        self.browser=self.runtime.chromium.launch()
        self.context=self.browser.new_context(viewport={"width":1280,"height":1200})
        self.page=self.context.new_page()
        self.current=None;self.saved=[];self.drop_first=False;self.conflict=False
        self.context.route("**/*",self.route)

    def tearDown(self):
        self.context.close();self.browser.close();self.runtime.stop()

    def route(self,route):
        url=route.request.url
        if url.startswith("https://online.moysklad.ru/"):return route.fulfill(content_type="text/html",body=HOST)
        if not url.startswith("https://widget.test/"):return route.abort()
        path=url.split("https://widget.test",1)[1]
        if path=="/api/session":return route.fulfill(json={"token":"synthetic-session"})
        if path=="/api/admin/widget/setup":return route.fulfill(json={"configured":True})
        if path=="/api/me":return route.fulfill(json={"role":"admin"})
        if path.startswith("/api/admin/rules/history"):return route.fulfill(json={"items":[self.current] if self.current else [],"next_cursor":None})
        if path=="/api/admin/rules":
            if route.request.method=="PUT":
                body=route.request.post_data_json;self.saved.append(body)
                if self.conflict:return route.fulfill(status=409,json={"error":"conflict"})
                self.current={**body,"version":1,"actor_id":"synthetic-user","created_at":"2026-10-14T00:00:00Z"}
                if self.drop_first and len(self.saved)==1:return route.abort()
            return route.fulfill(json={"current":self.current})
        file=ROOT/"web"/("settings.html" if path=="/settings" else path.lstrip("/"))
        if file.is_file():
            mime="text/html" if file.suffix==".html" else ("text/css" if file.suffix==".css" else "text/javascript")
            return route.fulfill(content_type=mime,body=file.read_text())
        return route.fulfill(status=404)

    def open(self):
        self.page.goto("https://online.moysklad.ru/test-host")
        self.frame=self.page.frame_locator("iframe")
        self.frame.get_by_text("Сохраните первоначальные правила.",exact=True).wait_for()

    def test_widget_setup_does_not_claim_sales_are_active(self):
        self.open()
        self.frame.get_by_role("button",name="Подготовить виджет",exact=True).click()
        self.frame.get_by_text("Виджет подготовлен. Это не включает рабочие списания.",exact=True).wait_for()

    def test_initial_rules_and_goods_only_message(self):
        self.open()
        self.frame.get_by_text("Участвуют только товары. Доставка не входит в сумму начисления и списания.",exact=True).wait_for()
        self.frame.get_by_label("Начисление, %",exact=True).fill("7.25")
        self.frame.get_by_role("button",name="Сохранить правила",exact=True).click()
        self.frame.get_by_text("Правила сохранены. Версия 1.",exact=True).wait_for()
        self.assertEqual((self.saved[0]["earn_bps"],self.saved[0]["spend_bps"],self.saved[0]["delivery_policy"]),(725,3000,"exclude"))
        self.assertNotIn("actor_id",self.saved[0])

    def test_lost_response_retry_reuses_request_id(self):
        self.drop_first=True;self.open()
        button=self.frame.get_by_role("button",name="Сохранить правила",exact=True)
        button.click()
        self.frame.get_by_text("Нет ответа сервера. Повторите сохранение: повтор не создаст второе изменение.",exact=True).wait_for()
        button.click()
        self.frame.get_by_text("Правила сохранены. Версия 1.",exact=True).wait_for()
        self.assertEqual(len(self.saved),2)
        self.assertEqual(self.saved[0],self.saved[1])

    def test_conflict_requires_reload_and_mobile_has_no_overflow(self):
        self.conflict=True;self.page.set_viewport_size({"width":390,"height":844});self.open()
        self.frame.get_by_role("button",name="Сохранить правила",exact=True).click()
        self.frame.get_by_text("Правила изменились. Загрузите актуальные правила и проверьте значения перед сохранением.",exact=True).wait_for()
        self.assertEqual(len(self.saved),1)
        self.assertTrue(self.page.frames[1].evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.assertEqual(self.page.frames[1].evaluate("localStorage.length+sessionStorage.length"),0)

if __name__=="__main__":unittest.main()
