"""Settings UX exercised in Chromium with synthetic host/API transport."""
import base64,json,os
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

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
        self.unverified=[];self.setup_error=None;self.setup_codes=None;self.setup_details={};self.setup_calls=0;self.activation_error=None;self.activation_drop_first=False;self.activation_drop_after_commit=False;self.activation_state=False;self.automation_error=False;self.widget_configured=False;self.automation_config=None;self.activations=[];self.hooks_calls=0;self.hooks_drop=False
        self.current=None;self.saved=[];self.drop_first=False;self.conflict=False
        self.policy={"version":0,"enabled":False,"external_policy":"skip","allow_bonus_stack":False}
        self.policy_saved=[];self.policy_requests={};self.policy_conflict=False;self.policy_drop_first=False
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
            self.setup_calls+=1
            if self.setup_error:return route.fulfill(status=self.setup_error[0],json={
                "error":self.setup_error[1],"provider_error_codes":self.setup_codes,**self.setup_details})
            self.widget_configured=True
            return route.fulfill(json={"configured":True})
        if path=="/api/admin/automation":
            if self.automation_error:return route.fulfill(status=503,json={"error":"service temporarily unavailable"})
            if route.request.method=="POST":
                self.automation_config={"starts_at":"2026-10-14T00:00:00Z","heartbeat_at":None,"last_error":None}
                return route.fulfill(json={"configured":True,"starts_at":self.automation_config["starts_at"]})
            return route.fulfill(json={"configuration":self.automation_config,"orders":{},"issues":[],"unverified":self.unverified})
        if path=="/api/admin/activate":
            self.activations.append(route.request.post_data_json)
            if self.activation_error:return route.fulfill(status=409,json={"error":self.activation_error})
            if self.activation_drop_first and len(self.activations)==1:return route.abort()
            self.activation_state=True
            self.automation_config={"starts_at":"2026-10-14T00:00:00Z","heartbeat_at":None,"last_error":None}
            if self.activation_drop_after_commit:return route.abort()
            return route.fulfill(json={"state":"Activated","starts_at":self.automation_config["starts_at"]})
        if path=="/api/admin/readiness":
            complete=[bool(self.current),self.widget_configured,self.activation_state,False]
            ids=["rules","widget","activation","processing"]
            labels=["Правила бонусов сохранены","Настройка виджета сохранена","Программа запущена","Заказы обрабатываются автоматически"]
            targets=["rules-form","widget-settings","launch-settings","processing-settings"]
            return route.fulfill(json={"ready":False,"checks":[{"id":k,"label":label,"target":target,"complete":ok} for k,label,target,ok in zip(ids,labels,targets,complete)]})
        if path=="/api/admin/personal-discounts/policy/history":
            return route.fulfill(json={"items":[self.policy] if self.policy["version"] else [],"next_cursor":None})
        if path=="/api/admin/personal-discounts/policy":
            if route.request.method=="PUT":
                body=route.request.post_data_json;self.policy_saved.append(body)
                if self.policy_conflict:return route.fulfill(status=409,json={"error":"conflict"})
                if body["request_id"] not in self.policy_requests:
                    self.policy={**body,"version":self.policy["version"]+1,"actor_id":"synthetic-user","created_at":"2026-10-19T00:00:00Z"}
                    self.policy_requests[body["request_id"]]=self.policy
                if self.policy_drop_first and len(self.policy_saved)==1:return route.abort()
                return route.fulfill(json={"current":self.policy_requests[body["request_id"]]})
            return route.fulfill(json={"current":self.policy})
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
        self.frame.locator("#workspace").wait_for(state="visible")

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

    def save_rules_and_prepare_widget(self):
        self.frame.get_by_role("tab",name="Правила",exact=True).click()
        self.frame.get_by_role("button",name="Сохранить правила",exact=True).click()
        self.frame.get_by_text("Правила сохранены. Версия 1.",exact=True).wait_for()
        self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        self.frame.get_by_role("button",name="Подготовить виджет",exact=True).click()
        self.frame.get_by_text("Виджет подготовлен. Это не включает рабочие списания.",exact=True).wait_for()
        expect(self.frame.locator("#activate-program")).to_be_enabled()

    def acknowledge_activation(self):
        self.frame.get_by_label("Остатки сверены и перенесены либо начинаем без остатков").check()
        self.frame.get_by_label("Прежняя программа больше не обрабатывает новые покупки").check()
        self.frame.get_by_label("Основание запуска").fill("Сверено с магазином")

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
        self.open();self.save_rules_and_prepare_widget();button=self.frame.get_by_role("button",name="Запустить программу",exact=True)
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


        for width in (320,360,390,700,701,1024):
            self.page.set_viewport_size({"width":width,"height":844})
            self.page.frames[1].evaluate("()=>new Promise(requestAnimationFrame)")
            earn=self.frame.get_by_label("Начисление, %",exact=True).bounding_box()
            spend=self.frame.get_by_label("Максимальное списание, %",exact=True).bounding_box()
            self.assertLessEqual(abs(earn["y"]-spend["y"]),1)
            self.assertTrue(self.page.frames[1].evaluate("document.documentElement.scrollWidth<=innerWidth"))
        if os.environ.get("GITHUB_ACTIONS")=="true":
            directory=ROOT/"test-results";directory.mkdir(exist_ok=True)
            for label,width,height in (("DESKTOP",1280,800),("MOBILE",390,844)):
                self.page.set_viewport_size({"width":width,"height":height})
                self.page.frames[1].evaluate("document.querySelector('.app-content').scrollTop=0")
                self.page.screenshot(path=str(directory/("bonuspro-rules-"+label.lower()+".png")))
                print("BP_VISUAL_RULES_"+label+"="+base64.b64encode(self.page.screenshot(type="jpeg",quality=65)).decode(),flush=True)


    def test_widget_setup_errors_explain_the_actual_failure_and_allow_retry(self):
        self.open();self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        button=self.frame.get_by_role("button",name="Подготовить виджет",exact=True)
        cases=[(403,"moysklad_permissions_required","BP-WIDGET-ACCESS"),
               (403,"moysklad_token_rejected","BP-WIDGET-TOKEN"),
               (422,"widget_metadata_invalid","BP-WIDGET-METADATA"),
               (422,"widget_metadata_rejected","BP-WIDGET-REQUEST"),
               (409,"widget_binding_changed","BP-WIDGET-BINDING")]
        for status,code,text in cases:
            self.setup_error=(status,code);button.click()
            self.frame.locator("#setup-status").get_by_text(text,exact=False).wait_for()
            self.assertTrue(button.is_enabled())
        self.setup_error=None;button.click()
        self.frame.get_by_text("Виджет подготовлен. Это не включает рабочие списания.",exact=True).wait_for()

    def test_widget_access_denial_shows_only_numeric_diagnostics_without_claiming_missing_descriptor(self):
        self.open();self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        button=self.frame.get_by_role("button",name="Подготовить виджет",exact=True)
        self.setup_error=(403,"moysklad_permissions_required")
        for code in (1016,1070):
            self.setup_codes=[code,code,True,"1056","synthetic-token",-1,1000000,1070.5]
            button.click()
            self.frame.locator("#setup-status").get_by_text("Коды API МойСклад: "+str(code)+".",exact=False).wait_for()
            message=self.frame.locator("#setup-status").inner_text()
            self.assertIn("BP-WIDGET-ACCESS",message)
            self.assertNotIn("useOwnAttributeMetadata",message)
            self.assertNotIn("не выдан доступ",message)
            self.assertNotIn("Обновите дескриптор",message)
            self.assertNotIn("synthetic-token",message)
            self.assertNotIn("1056",message)
            self.assertTrue(button.is_enabled())

    def test_widget_setup_expired_session_shows_reconnect_instead_of_hidden_error(self):
        self.open();self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        self.setup_error=(401,"authentication required")
        self.frame.get_by_role("button",name="Подготовить виджет",exact=True).click()
        self.frame.locator("#status").get_by_text("Сеанс завершён. Подключитесь снова и повторите настройку виджета.",exact=True).wait_for()
        self.assertFalse(self.frame.locator("#workspace").is_visible())
        self.assertTrue(self.frame.get_by_role("button",name="Повторить подключение",exact=True).is_visible())


    def test_readiness_rules_action_reveals_rules_panel(self):
        self.open()
        self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        self.frame.locator('#readiness-list a[href="#rules-form"]').click()
        self.frame.get_by_role("tabpanel",name="Правила",exact=True).wait_for()
        self.assertEqual(self.frame.get_by_role("tab",name="Правила",exact=True).get_attribute("aria-selected"),"true")
        self.assertTrue(self.frame.get_by_role("button",name="Сохранить правила",exact=True).is_visible())


    def test_personal_policy_saves_real_settings_and_explicit_stacking_formula(self):
        self.page.set_viewport_size({"width":320,"height":844});self.open()
        self.frame.get_by_role("tab",name="Персональные скидки",exact=True).click()
        self.assertFalse(self.frame.get_by_label("Использовать персональные скидки",exact=True).is_checked())
        self.frame.get_by_label("Использовать персональные скидки",exact=True).check()
        self.frame.get_by_label("Если в позиции уже есть скидка",exact=True).select_option("sequential")
        self.frame.locator("#personal-example").get_by_text("10 000 ₸ × 90% × 80% = 7 200 ₸",exact=False).wait_for()
        self.frame.get_by_label("Разрешить списание бонусов вместе с персональной скидкой",exact=True).check()
        self.frame.get_by_text("Лимит списания рассчитывается от товаров после персональной скидки.",exact=True).wait_for()
        self.frame.get_by_label("Причина изменения персональных скидок",exact=True).fill("Правила согласованы")
        self.frame.get_by_role("button",name="Сохранить настройки скидок",exact=True).click()
        self.frame.get_by_text("Настройки скидок сохранены. Версия 1.",exact=True).wait_for()
        self.assertEqual(len(self.policy_saved),1)
        self.assertEqual({k:self.policy_saved[0][k] for k in ("enabled","external_policy","allow_bonus_stack","expected_version","reason")},
                         {"enabled":True,"external_policy":"sequential","allow_bonus_stack":True,"expected_version":0,"reason":"Правила согласованы"})
        self.assertEqual(self.saved,[])
        self.assertTrue(self.page.frames[1].evaluate("document.documentElement.scrollWidth<=innerWidth"))

    def test_personal_policy_lost_response_reuses_request_and_conflict_requires_reload(self):
        self.policy_drop_first=True;self.open()
        self.frame.get_by_role("tab",name="Персональные скидки",exact=True).click()
        self.frame.get_by_label("Использовать персональные скидки",exact=True).check()
        self.frame.get_by_label("Причина изменения персональных скидок",exact=True).fill("Включаем клиентам")
        button=self.frame.get_by_role("button",name="Сохранить настройки скидок",exact=True)
        button.click()
        self.frame.locator("#personal-policy-status").get_by_text("Нет ответа сервера.",exact=False).wait_for()
        button.click()
        self.frame.get_by_text("Настройки скидок сохранены. Версия 1.",exact=True).wait_for()
        self.assertEqual(self.policy_saved[0],self.policy_saved[1])
        if os.environ.get("GITHUB_ACTIONS")=="true":
            self.page.set_viewport_size({"width":1100,"height":900})
            self.page.frames[1].evaluate("document.querySelector('.app-content').scrollTop=0")
            directory=ROOT/"test-results";directory.mkdir(exist_ok=True)
            self.page.screenshot(path=str(directory/"bonuspro-personal-settings.png"))
            print("PERSONAL_SETTINGS_SCREENSHOT_DATA="+base64.b64encode(self.page.screenshot(type="jpeg",quality=65)).decode(),flush=True)
        self.policy_conflict=True
        self.frame.get_by_label("Причина изменения персональных скидок",exact=True).fill("Меняем правило")
        self.frame.get_by_label("Если в позиции уже есть скидка",exact=True).select_option("best")
        button.click()
        self.frame.get_by_text("Настройки скидок изменились. Загрузите актуальные настройки и проверьте значения.",exact=True).wait_for()
        self.assertFalse(button.is_enabled())
        self.assertEqual(self.frame.get_by_label("Если в позиции уже есть скидка",exact=True).input_value(),"best")
        self.policy_conflict=False
        self.frame.get_by_role("button",name="Загрузить настройки скидок",exact=True).click()
        self.frame.get_by_text("Персональные скидки включены. Версия 1.",exact=True).wait_for()
        self.assertTrue(button.is_enabled())
        self.assertEqual(self.frame.get_by_label("Если в позиции уже есть скидка",exact=True).input_value(),"skip")


    def test_automation_pending_personal_only_discount_has_no_zero_bonus_claim(self):
        self.unverified=[{"id":"application","order_id":"order","points":0}]
        self.open();self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        self.frame.get_by_role("button",name="Проверить обработку",exact=True).click()
        self.frame.locator("#automation-details").get_by_text("Персональная скидка — ожидает подтверждения",exact=False).wait_for()
        self.assertNotIn("0 бонусов",self.frame.locator("#automation-details").inner_text())
        self.assertTrue(self.frame.locator("#automation-details").get_by_role("link",name="Открыть заказ",exact=True).is_visible())


    def test_activation_stays_disabled_until_rules_and_widget_are_ready(self):
        self.open();self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        button=self.frame.locator("#activate-program")
        expect(button).to_be_disabled()
        self.frame.get_by_text("Сначала сохраните правила и подготовьте виджет.",exact=True).wait_for()
        self.frame.get_by_role("tab",name="Правила",exact=True).click()
        self.frame.get_by_role("button",name="Сохранить правила",exact=True).click()
        self.frame.get_by_text("Правила сохранены. Версия 1.",exact=True).wait_for()
        self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        self.frame.get_by_text("Сначала подготовьте виджет.",exact=True).wait_for()
        expect(button).to_be_disabled()
        self.setup_error=(503,"widget_metadata_temporarily_unavailable")
        self.setup_details={"retry_reason":"timeout","retry_after":1}
        self.frame.locator("#setup-widget").click()
        self.frame.locator("#setup-status").get_by_text("BP-WIDGET-TIMEOUT",exact=False).wait_for()
        expect(button).to_be_disabled()
        self.assertEqual(self.activations,[])
        self.setup_error=None
        expect(self.frame.locator("#setup-widget")).to_be_enabled(timeout=5000)
        self.frame.locator("#setup-widget").click()
        expect(button).to_be_enabled()

    def test_widget_rate_limit_waits_before_retry_and_recovers_without_duplicate_clicks(self):
        self.open();self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        self.setup_error=(503,"widget_metadata_temporarily_unavailable")
        self.setup_details={"retry_reason":"rate_limited","retry_after":2,"provider_status":429}
        button=self.frame.locator("#setup-widget")
        button.click()
        self.frame.locator("#setup-status").get_by_text("BP-WIDGET-RATE",exact=False).wait_for()
        expect(button).to_be_disabled()
        self.assertEqual(self.setup_calls,1)
        self.assertEqual(self.activations,[])
        self.setup_error=None
        expect(button).to_be_enabled(timeout=5000)
        button.click()
        self.frame.get_by_text("Виджет подготовлен. Это не включает рабочие списания.",exact=True).wait_for()
        self.assertEqual(self.setup_calls,2)

    def test_activation_conflict_has_no_false_rules_message_and_preserves_retry_request(self):
        self.open();self.save_rules_and_prepare_widget();self.acknowledge_activation()
        self.activation_error="operation conflict"
        button=self.frame.locator("#activate-program");button.click()
        self.frame.locator("#activation-status").get_by_text("Не удалось завершить запуск.",exact=False).wait_for()
        self.assertNotIn("Правила изменились",self.frame.locator("#activation-status").inner_text())
        self.assertTrue(self.frame.get_by_label("Остатки сверены и перенесены либо начинаем без остатков").is_checked())
        self.activation_error=None;button.click()
        self.frame.locator("#activation-status").get_by_text("Программа запущена.",exact=False).wait_for()
        self.assertEqual(self.activations[0],self.activations[1])

    def test_changed_activation_configuration_requires_new_acknowledgement_and_intent(self):
        self.open();self.save_rules_and_prepare_widget();self.acknowledge_activation()
        self.activation_error="activation_configuration_changed"
        button=self.frame.locator("#activate-program");button.click()
        self.frame.get_by_text("Настройки изменились во время запуска. Проверьте правила и заново подтвердите условия запуска.",exact=True).wait_for()
        self.assertFalse(self.frame.get_by_label("Остатки сверены и перенесены либо начинаем без остатков").is_checked())
        self.assertFalse(self.frame.get_by_label("Прежняя программа больше не обрабатывает новые покупки").is_checked())
        self.activation_error=None;button.click()
        self.frame.get_by_text("Подтвердите сверку остатков, отключение прежней программы и укажите основание.",exact=True).wait_for()
        self.assertEqual(len(self.activations),1)
        self.acknowledge_activation();button.click()
        self.frame.locator("#activation-status").get_by_text("Программа запущена.",exact=False).wait_for()
        self.assertNotEqual(self.activations[0]["request_id"],self.activations[1]["request_id"])

    def test_lost_activation_response_reuses_same_intent_and_success_survives_followup_failure(self):
        self.open();self.save_rules_and_prepare_widget();self.acknowledge_activation()
        self.activation_drop_first=True
        button=self.frame.locator("#activate-program");button.click()
        self.frame.locator("#activation-status").get_by_text("Запуск не подтверждён.",exact=False).wait_for()
        self.automation_error=True;button.click()
        self.frame.locator("#activation-status").get_by_text("Программа запущена.",exact=False).wait_for()
        self.frame.locator("#activation-status").get_by_text("Статус обработки пока недоступен",exact=False).wait_for()
        self.assertNotIn("Запуск не подтверждён",self.frame.locator("#activation-status").inner_text())
        self.assertEqual(self.activations[0],self.activations[1])
        expect(button).to_be_hidden()


    def test_lost_activation_response_after_commit_is_confirmed_by_readiness_without_retry(self):
        self.open();self.save_rules_and_prepare_widget();self.acknowledge_activation()
        self.activation_drop_after_commit=True
        button=self.frame.locator("#activate-program");button.click()
        self.frame.get_by_text("Программа уже запущена. Повторный запуск не требуется.",exact=True).wait_for()
        self.assertNotIn("Запуск не подтверждён",self.frame.locator("#activation-status").inner_text())
        expect(button).to_be_hidden()
        self.assertEqual(len(self.activations),1)
        self.frame.get_by_role("button",name="Обновить статус",exact=True).click()
        self.frame.get_by_text("Программа уже запущена. Повторный запуск не требуется.",exact=True).wait_for()
        self.assertEqual(len(self.activations),1)


    def test_metadata_request_rejection_is_permanent_and_shows_only_safe_provider_diagnostics(self):
        self.open();self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        button=self.frame.locator("#setup-widget")
        self.setup_error=(422,"widget_metadata_rejected")
        for operation,label,status,code in (("metadata_read","чтение поля",412,1040),
                                            ("metadata_create","создание поля",412,3000)):
            self.setup_details={"provider_operation":operation,"provider_status":status,
                                "message":"synthetic-token https://private.test"}
            self.setup_codes=[code,True,"synthetic-token",-1,1000000]
            button.click()
            message=self.frame.locator("#setup-status")
            message.get_by_text("BP-WIDGET-REQUEST",exact=False).wait_for()
            expect(button).to_be_enabled()
            text=message.inner_text()
            self.assertIn("Этап: "+label+".",text)
            self.assertIn("HTTP "+str(status)+".",text)
            self.assertIn("Коды API МойСклад: "+str(code)+".",text)
            self.assertNotIn("Повторите через",text)
            self.assertNotIn("ещё обрабатывает",text)
            self.assertNotIn("synthetic-token",text)
            self.assertNotIn("private.test",text)
            expect(self.frame.locator("#activate-program")).to_be_disabled()
        self.assertEqual(self.setup_calls,2)
        self.assertEqual(self.activations,[])

    def test_documented_metadata_concurrent_change_keeps_retry_and_finite_diagnostics(self):
        self.open();self.frame.get_by_role("tab",name="Подключение",exact=True).click()
        self.setup_error=(503,"widget_metadata_temporarily_unavailable")
        self.setup_details={"retry_reason":"conflict","retry_after":1,
                            "provider_status":412,"provider_operation":"metadata_create"}
        self.setup_codes=[1065]
        button=self.frame.locator("#setup-widget");button.click()
        self.frame.locator("#setup-status").get_by_text("Коды API МойСклад: 1065.",exact=False).wait_for()
        self.assertIn("Настройка поля изменилась параллельно.",self.frame.locator("#setup-status").inner_text())
        expect(button).to_be_disabled()
        self.setup_error=None
        expect(button).to_be_enabled(timeout=5000)
        button.click()
        self.frame.get_by_text("Виджет подготовлен. Это не включает рабочие списания.",exact=True).wait_for()
        self.assertEqual(self.setup_calls,2)

if __name__=="__main__":unittest.main()
