"""Settings UX exercised in Chromium with synthetic host/API transport."""
import base64,json,os
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]
HOST="""<!doctype html><style>html,body{margin:0;height:100%;overflow:hidden}iframe{border:0;width:100%;height:100vh}</style><iframe title="БонусПро" src="https://widget.test/settings"></iframe>
<script>window.addEventListener('message',e=>{if(e.data.name==='UserContextRequest')
e.source.postMessage({name:'UserContextResponse',correlationId:e.data.messageId,token:'synthetic'},'https://widget.test');});</script>"""


class SettingsBrowserTests(unittest.TestCase):
    def setUp(self):
        self.runtime=sync_playwright().start()
        self.browser=self.runtime.chromium.launch()
        self.context=self.browser.new_context(viewport={"width":1280,"height":1200})
        self.page=self.context.new_page()
        self.widget_configured=False;self.automation_config=None;self.activations=[];self.hooks_calls=0;self.hooks_drop=False
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
        if path=="/api/admin/webhooks/setup":
            self.hooks_calls+=1
            if self.hooks_drop and self.hooks_calls==1:return route.abort()
            return route.fulfill(json={"configured":True,"hook_count":21})
        if path=="/api/admin/widget/setup":
            self.widget_configured=True
            return route.fulfill(json={"configured":True})
        if path=="/api/admin/automation":
            if route.request.method=="POST":
                self.automation_config={"starts_at":"2026-10-14T00:00:00Z","heartbeat_at":None,"last_error":None}
                return route.fulfill(json={"configured":True,"starts_at":self.automation_config["starts_at"]})
            return route.fulfill(json={"configuration":self.automation_config,"orders":{},"issues":[]})
        if path=="/api/admin/activate":
            self.activations.append(route.request.post_data_json)
            self.automation_config={"starts_at":"2026-10-14T00:00:00Z","heartbeat_at":None,"last_error":None}
            return route.fulfill(json={"state":"Activated","starts_at":self.automation_config["starts_at"]})
        if path=="/api/admin/readiness":
            complete=[bool(self.current),self.widget_configured,bool(self.activations),False]
            ids=["rules","widget","activation","processing"]
            labels=["Правила бонусов сохранены","Настройка виджета сохранена","Программа запущена","Заказы обрабатываются автоматически"]
            targets=["rules-form","widget-settings","launch-settings","processing-settings"]
            return route.fulfill(json={"ready":False,"checks":[{"id":k,"label":label,"target":target,"complete":ok} for k,label,target,ok in zip(ids,labels,targets,complete)]})
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

    def test_webhook_setup_retry_and_narrow_layout(self):
        self.hooks_drop=True;self.open();self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        self.page.set_viewport_size({"width":390,"height":1200})
        button=self.frame.get_by_role("button",name="Подключить уведомления",exact=True)
        button.click()
        self.frame.get_by_text("Подключение не подтверждено.",exact=False).wait_for()
        button.click()
        self.frame.get_by_text("Уведомления подключены. Автоматическую обработку проверьте ниже.",exact=True).wait_for()
        self.assertEqual(self.hooks_calls,2)
        frame=self.page.frames[1]
        self.assertTrue(frame.evaluate("document.documentElement.scrollWidth<=innerWidth+1"))

    def test_widget_setup_does_not_claim_sales_are_active(self):
        self.open();self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        self.frame.get_by_role("button",name="Подготовить виджет",exact=True).click()
        self.frame.get_by_text("Виджет подготовлен. Это не включает рабочие списания.",exact=True).wait_for()

    def test_operational_checklist_has_one_activation_path_and_real_processing_state(self):
        self.open();self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        self.assertEqual(self.frame.locator("#configure-automation").count(),0)
        self.assertEqual(self.frame.locator("#readiness-list li").count(),4)
        self.frame.get_by_text("Настройка виджета сохранена — требуется действие",exact=True).wait_for()
        self.frame.get_by_role("button",name="Подготовить виджет",exact=True).click()
        self.frame.get_by_text("Настройка виджета сохранена — выполнено",exact=True).wait_for()
        self.frame.get_by_text("Заказы обрабатываются автоматически — требуется действие",exact=True).wait_for()
        self.frame.get_by_role("button",name="Проверить обработку",exact=True).click()
        self.frame.get_by_text("Начало программы ещё не зафиксировано.",exact=True).wait_for()

    def test_activation_requires_explicit_cutover_confirmation(self):
        self.open();self.frame.get_by_role("tab",name="Подключение",exact=True).click();button=self.frame.get_by_role("button",name="Запустить программу",exact=True)
        button.click()
        self.frame.get_by_text("Подтвердите сверку остатков, отключение прежней программы и укажите основание.",exact=True).wait_for()
        self.assertEqual(self.activations,[])
        self.frame.get_by_label("Остатки сверены и перенесены либо начинаем без остатков").check()
        self.frame.get_by_label("Прежняя программа больше не обрабатывает новые покупки").check()
        self.frame.get_by_label("Основание запуска").fill("Сверено с магазином")
        button.click();self.frame.get_by_text("Программа запущена.",exact=False).wait_for()
        self.assertEqual(len(self.activations),1)
        self.assertEqual(self.activations[0]["reason"],"Сверено с магазином")
        self.assertTrue(self.activations[0]["balances_reviewed"])
        self.assertNotIn("tenant_id",self.activations[0])

    def test_initial_rules_and_goods_only_message(self):
        self.open()
        self.frame.get_by_text("Только товары, без доставки",exact=True).wait_for()
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


    def test_rules_are_short_and_example_recalculates_without_saving(self):
        self.open()
        self.assertTrue(self.frame.get_by_role("tabpanel",name="Правила",exact=True).is_visible())
        self.assertFalse(self.frame.get_by_role("button",name="Запустить программу",exact=True).is_visible())
        self.frame.get_by_label("Покупка на сумму, ₸",exact=True).fill("20000")
        self.assertEqual("".join(self.frame.locator("#example-earn").inner_text().split()),"1000")
        self.assertEqual("".join(self.frame.locator("#example-spend").inner_text().split()),"6000")
        self.frame.get_by_label("Начисление, %",exact=True).fill("7.25")
        self.assertEqual("".join(self.frame.locator("#example-earn").inner_text().split()),"1450")
        self.assertEqual(self.saved,[])
        self.frame.get_by_role("tab",name="История изменений",exact=True).click()
        self.assertTrue(self.frame.get_by_role("tabpanel",name="История изменений",exact=True).is_visible())
        self.frame.get_by_role("tab",name="История изменений",exact=True).press("Home")
        self.assertTrue(self.frame.get_by_role("tabpanel",name="Правила",exact=True).is_visible())


        if os.environ.get("GITHUB_ACTIONS")=="true":
            directory=ROOT/"test-results";directory.mkdir(exist_ok=True)
            for label,width,height in (("DESKTOP",1280,800),("MOBILE",390,844)):
                self.page.set_viewport_size({"width":width,"height":height})
                self.page.frames[1].evaluate("document.querySelector('.app-content').scrollTop=0")
                self.page.screenshot(path=str(directory/("bonuspro-rules-"+label.lower()+".png")))
                print("BP_VISUAL_RULES_"+label+"="+base64.b64encode(self.page.screenshot(type="jpeg",quality=65)).decode(),flush=True)

if __name__=="__main__":unittest.main()
