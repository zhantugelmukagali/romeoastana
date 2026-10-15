"""Real Chromium, synthetic MoySklad host. Not a live provider acceptance test."""
import base64,json,os,unittest
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]
ORDER="11111111-1111-4111-8111-111111111111"
NEXT="22222222-2222-4222-8222-222222222222"
HOST="""<!doctype html><iframe title="БонусПро" src="https://widget.test/widget" style="width:100%;height:650px"
onload="openOrder('11111111-1111-4111-8111-111111111111')"></iframe>
<script>
let seq=100;
window.updates=[];
window.send=m=>document.querySelector('iframe').contentWindow.postMessage(m,'https://widget.test');
window.openOrder=id=>send({name:'Open',messageId:++seq,extensionPoint:'document.customerorder.edit',objectId:id});
window.addEventListener('message',e=>{
if(e.data.name==='UserContextRequest')send({name:'UserContextResponse',correlationId:e.data.messageId,token:'synthetic'});
if(e.data.name==='UpdateRequest'){updates.push(e.data);send({name:'UpdateResponse',correlationId:e.data.messageId});}
});
</script>"""

class WidgetBrowserTests(unittest.TestCase):
    def setUp(self):
        self.runtime=sync_playwright().start();self.browser=self.runtime.chromium.launch()
        self.context=self.browser.new_context(viewport={"width":1000,"height":800})
        self.page=self.context.new_page();self.context.route("**/*",self.route)
        self.quote_overrides={};self.order_error=None;self.session_count=0;self.prepares=[];self.verifies=0;self.prepared=False;self.saved=False;self.lose=False;self.delay_change=False;self.reject_prepare=None;self.save_during_prepare=False;self.host_error=False
    def tearDown(self):
        self.context.close();self.browser.close();self.runtime.stop()
    def route(self,route):
        url=route.request.url
        if url=="https://online.moysklad.ru/host-sender":
            return route.fulfill(content_type="text/html",body="""<script>
            window.deliver=(target,message)=>target.postMessage(message,'https://widget.test');
            </script>""")
        if url.startswith("https://online.moysklad.ru/"):return route.fulfill(content_type="text/html",body=HOST.replace(
            "send({name:'UpdateResponse',correlationId:e.data.messageId});",
            "send({name:'InvalidMessageError',correlationId:null,invalidMessage:{messageId:e.data.messageId},errors:[{code:1002}]});"
            if self.host_error else "send({name:'UpdateResponse',correlationId:e.data.messageId});"))
        if not url.startswith("https://widget.test/"):return route.abort()
        path=url.split("https://widget.test",1)[1]
        if path=="/api/session":
            self.session_count+=1
            return route.fulfill(json={"token":"synthetic-session-"+str(self.session_count)})
        if path.endswith("/prepare"):
            self.prepares.append(route.request.post_data_json)
            if self.reject_prepare:return route.fulfill(status=self.reject_prepare,json={"error":"operation rejected"})
            self.prepared=True
            if self.save_during_prepare:
                self.saved=True
                self.page.evaluate("send({name:'Save',messageId:131,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
            if self.lose:return route.abort()
            if self.delay_change:
                self.page.evaluate("send({name:'Change',messageId:120,objectState:{}})")
            return route.fulfill(json={"id":"attempt","order_id":ORDER,"points":1000,"state":"verifying",
                "update_state":{"positions":[{"id":"line","discount":10}],"attributes":[{"id":"marker","value":"proof"}]},"replayed":False})
        if path.endswith("/verify"):
            self.verifies+=1
            return route.fulfill(json={"id":"attempt","order_id":ORDER,"points":1000,
                "state":"committed" if self.saved else "verifying","verified":self.saved})
        if path.startswith("/api/widget/orders/"):
            if self.order_error:return route.fulfill(status=self.order_error,json={"error":"unavailable"})
            if self.prepared and path.endswith(ORDER):
                return route.fulfill(json={"redemption":{"id":"attempt","order_id":ORDER,"points":1000,"state":"verifying"}})
            return route.fulfill(json={"name":"0001","saved_source_hash":"a"*64,"eligible_minor":1000000,"total_minor":1200000,
                "can_prepare":self.quote_overrides.get("can_prepare",True),"quote":{"balance":5000,"max_spend_points":3000,"blocked_reason":None,**self.quote_overrides.get("quote",{})}})
        file=ROOT/"web"/("widget.html" if path=="/widget" else path.lstrip("/"))
        if file.is_file():
            return route.fulfill(content_type={".html":"text/html",".css":"text/css",".js":"text/javascript"}[file.suffix],body=file.read_text())
        return route.fulfill(status=404)
    def open(self):
        self.page.goto("https://online.moysklad.ru/test-host");self.frame=self.page.frame_locator("iframe")
        self.frame.get_by_text("5000 бонусов",exact=True).wait_for()
    def apply(self):
        self.frame.get_by_label("Списать бонусы",exact=True).fill("1000")
        self.frame.get_by_role("button",name="Применить к заказу",exact=True).click()
    def test_host_ack_is_not_a_success_until_saved_verification(self):
        self.open();self.apply()
        self.frame.get_by_text("Зарезервировано 1000 бонусов. Итоговое списание ещё не подтверждено.",exact=True).wait_for()
        self.assertEqual(self.page.evaluate("updates.length"),1)
        self.assertEqual(self.verifies,0)
        self.saved=True
        self.page.evaluate("send({name:'Save',messageId:130,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.get_by_text("Списано 1000 бонусов.",exact=True).wait_for()
        self.assertEqual(len(self.prepares),1);self.assertEqual(self.verifies,1)
    def test_unsaved_change_blocks_apply_even_after_refresh(self):
        self.open()
        self.page.evaluate("send({name:'Change',messageId:120,objectState:{}})")
        self.frame.get_by_text("В заказе есть изменения. Сначала сохраните его в МойСклад.",exact=True).wait_for()
        self.assertTrue(self.frame.get_by_role("button",name="Применить к заказу",exact=True).is_disabled())
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.get_by_text("В заказе есть изменения. Сначала сохраните его в МойСклад.",exact=True).wait_for()
        self.assertTrue(self.frame.get_by_role("button",name="Применить к заказу",exact=True).is_disabled())
        self.assertEqual(self.prepares,[])
    def test_lost_response_keeps_unknown_state_without_second_mutation(self):
        self.lose=True;self.open();self.apply()
        self.frame.get_by_text("Статус резервирования неизвестен. Обновите данные заказа перед повторной попыткой.",exact=True).wait_for()
        self.assertEqual(self.page.evaluate("updates.length"),0)
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.get_by_text("Для заказа уже есть операция. Проверьте её по сохранённым данным.",exact=True).wait_for()
        self.assertEqual(len(self.prepares),1)
    def test_host_reuse_resets_points_and_state_between_orders(self):
        self.open();self.frame.get_by_label("Списать бонусы",exact=True).fill("1000")
        self.page.evaluate("openOrder('"+NEXT+"')")
        self.frame.get_by_text("5000 бонусов",exact=True).wait_for()
        self.assertEqual(self.frame.get_by_label("Списать бонусы",exact=True).input_value(),"")
        self.assertEqual(self.page.frames[1].evaluate("localStorage.length+sessionStorage.length"),0)
    def test_change_during_prepare_does_not_apply_stale_positions(self):
        self.delay_change=True;self.open();self.apply()
        self.frame.get_by_text("Данные изменились или запрос уже выполнялся. Проверьте сохранение; резерв сохранён.",exact=True).wait_for()
        self.assertEqual(self.page.evaluate("updates.length"),0)
        self.assertEqual(len(self.prepares),1)

    def test_explicit_rejection_does_not_invent_reservation_and_requires_fresh_quote(self):
        self.open()
        for code in (403,409,422):
            with self.subTest(status=code):
                self.reject_prepare=code
                self.apply()
                self.frame.locator("#status[data-error='true']").wait_for()
                self.assertFalse(self.frame.locator("#pending").is_visible())
                self.assertTrue(self.frame.locator("#apply").is_disabled())
                self.assertEqual(self.page.evaluate("updates.length"),0)
                self.frame.get_by_role("button",name="Обновить",exact=True).click()
                self.frame.get_by_text("5000 бонусов",exact=True).wait_for()
        self.assertEqual(len(self.prepares),3)

    def test_save_during_prepare_is_verified_after_response_without_host_mutation(self):
        self.save_during_prepare=True;self.open();self.apply()
        self.frame.get_by_text("Списано 1000 бонусов.",exact=True).wait_for()
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.verifies,1)
        self.assertEqual(self.page.evaluate("updates.length"),0)

    def test_host_rejection_keeps_real_reservation_and_reports_without_waiting_for_timeout(self):
        self.host_error=True;self.open();self.apply()
        self.frame.locator("#status[data-error='true']").wait_for(timeout=5000)
        self.frame.get_by_text("Зарезервировано 1000 бонусов. Итоговое списание ещё не подтверждено.",exact=True).wait_for()
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.page.evaluate("updates.length"),1)
        self.assertEqual(self.verifies,0)

    def test_create_page_does_not_send_unsupported_feedback(self):
        self.open()
        self.page.evaluate("""window.feedback=0;window.addEventListener('message',e=>{if(e.data.name==='OpenFeedback')feedback++});
        send({name:'Open',messageId:190,extensionPoint:'document.customerorder.create',objectId:null});""")
        self.frame.get_by_text("Сначала сохраните заказ с выбранным покупателем в МойСклад.",exact=True).wait_for()
        self.assertEqual(self.page.evaluate("feedback"),0)

    def test_wrong_origin_or_source_cannot_supply_open_message(self):
        self.open()
        self.page.frames[1].evaluate("window.postMessage({name:'Open',messageId:999,extensionPoint:'document.customerorder.create'},'*')")
        self.assertTrue(self.frame.get_by_text("5000 бонусов",exact=True).is_visible())
        self.assertEqual(self.prepares,[])
    def test_gwt_sibling_open_context_update_and_save_complete_the_saved_readback_flow(self):
        self.open()
        self.page.frames[1].evaluate("""()=>{
          window.addEventListener('message',event=>{
            if(event.data?.name==='UserContextResponse')window.hostSender={
              direct:event.source===parent,sibling:event.source.parent===parent};
          });
        }""")
        self.page.evaluate("""async()=>{
          const iframe=document.createElement('iframe');iframe.id='gwt';iframe.hidden=true;
          iframe.src='https://online.moysklad.ru/host-sender';
          await new Promise(resolve=>{iframe.onload=resolve;document.body.append(iframe)});
          window.send=m=>iframe.contentWindow.deliver(document.querySelector('iframe[src="https://widget.test/widget"]').contentWindow,m);
          window.gwtOpened=false;
          window.addEventListener('message',e=>{if(e.data?.name==='OpenFeedback')window.gwtOpened=true});
          openOrder('11111111-1111-4111-8111-111111111111');
        }""")
        self.page.wait_for_function("window.gwtOpened===true")
        self.frame=self.page.frame_locator('iframe[src="https://widget.test/widget"]')
        self.frame.get_by_text("5000 бонусов",exact=True).wait_for()
        self.apply()
        self.frame.get_by_text("Зарезервировано 1000 бонусов. Итоговое списание ещё не подтверждено.",exact=True).wait_for()
        self.saved=True
        self.page.evaluate("send({name:'Save',messageId:140,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.get_by_text("Списано 1000 бонусов.",exact=True).wait_for()
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.verifies,1)
        self.assertEqual(self.page.frames[1].evaluate("window.hostSender"),{"direct":False,"sibling":True})

    def test_same_origin_unrelated_popup_cannot_replace_the_host_document(self):
        self.open()
        self.page.frames[1].evaluate("""()=>{
          window.addEventListener('message',event=>{
            if(event.data?.name==='Open'&&event.data.messageId===999)window.unrelatedSeen=true;
          });
        }""")
        self.page.evaluate("""async()=>{
          const popup=window.open('https://online.moysklad.ru/host-sender','unrelated-host-window');
          await new Promise(resolve=>popup.onload=resolve);
          popup.deliver(document.querySelector('iframe').contentWindow,{
            name:'Open',messageId:999,extensionPoint:'document.customerorder.create',objectId:null});
        }""")
        self.page.frames[1].wait_for_function("window.unrelatedSeen===true")
        self.assertTrue(self.frame.get_by_text("5000 бонусов",exact=True).is_visible())
        self.assertEqual(self.prepares,[])

    def test_compact_width_and_maximum_preview(self):
        self.page.set_viewport_size({"width":390,"height":844});self.open()
        self.frame.get_by_role("button",name="Максимум",exact=True).click()
        self.assertEqual(self.frame.get_by_label("Списать бонусы",exact=True).input_value(),"3000")
        self.assertTrue(self.page.frames[1].evaluate("document.documentElement.scrollWidth<=innerWidth"))
        height=self.page.frames[1].evaluate("document.body.getBoundingClientRect().height")
        self.assertLessEqual(height,560)
        if os.environ.get("GITHUB_ACTIONS")=="true":
            root=ROOT/"test-results";root.mkdir(exist_ok=True)
            body=self.page.frames[1].locator("body")
            body.screenshot(path=str(root/"bonuspro-order-widget.png"))
            print("BP_VISUAL_WIDGET="+base64.b64encode(body.screenshot(type="jpeg",quality=65)).decode(),flush=True)



    def test_invalid_amounts_disable_apply_and_keep_financial_preview_accurate(self):
        self.open();points=self.frame.get_by_label("Списать бонусы",exact=True)
        button=self.frame.get_by_role("button",name="Применить к заказу",exact=True)
        self.assertTrue(button.is_disabled())
        for value in ("0","3001","1.5","9007199254740993"):
            points.fill(value)
            self.assertTrue(button.is_disabled())
            self.assertEqual(points.get_attribute("aria-invalid"),"true")
        points.fill("1000")
        self.assertTrue(button.is_enabled())
        self.assertEqual(points.get_attribute("aria-invalid"),"false")
        actual="".join(self.frame.locator("#payable").inner_text().split())
        self.assertIn("11000,00₸",actual)
        self.assertEqual(self.prepares,[])

    def test_enter_applies_only_once_and_success_requires_verified_saved_order(self):
        self.open();points=self.frame.get_by_label("Списать бонусы",exact=True)
        points.fill("1000");points.press("Enter")
        self.frame.get_by_role("heading",name="Осталось сохранить заказ",exact=True).wait_for()
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.frame.locator("#pending").get_attribute("data-state"),"waiting")
        self.frame.get_by_role("button",name="Проверить сохранение",exact=True).click()
        self.frame.locator("#status[data-error=true]").wait_for()
        self.assertNotEqual(self.frame.locator("#pending").get_attribute("data-state"),"done")
        self.saved=True
        self.page.evaluate("send({name:'Save',messageId:230,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.get_by_role("heading",name="Бонусы списаны",exact=True).wait_for()
        self.assertEqual(len(self.prepares),1)

    def test_failed_refresh_invalidates_old_quote_and_disables_all_spending(self):
        self.open();self.frame.get_by_label("Списать бонусы",exact=True).fill("1000")
        self.order_error=500
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.locator("#status[data-error=true]").wait_for()
        self.assertFalse(self.frame.locator("#summary").is_visible())
        self.assertTrue(self.frame.locator("#apply").is_disabled())
        self.assertTrue(self.frame.locator("#maximum-button").is_disabled())
        self.assertEqual(self.prepares,[])

    def test_zero_balance_explains_how_to_earn_without_offering_spending(self):
        self.quote_overrides={"quote":{"balance":0,"max_spend_points":0}}
        self.page.goto("https://online.moysklad.ru/test-host");self.frame=self.page.frame_locator("iframe")
        self.frame.get_by_text("У покупателя пока нет бонусов. Начисление — после полной оплаты и отгрузки.",exact=True).wait_for()
        self.assertTrue(self.frame.locator("#points").is_disabled())
        self.assertTrue(self.frame.locator("#apply").is_disabled())
        self.assertEqual(self.prepares,[])

    def test_admin_review_blocks_even_a_positive_quote_limit(self):
        self.quote_overrides={"quote":{"blocked_reason":"return_review"}}
        self.open()
        self.frame.get_by_text("Операция требует проверки администратора.",exact=True).wait_for()
        self.assertTrue(self.frame.locator("#points").is_disabled())
        self.assertTrue(self.frame.locator("#maximum-button").is_disabled())
        self.assertEqual(self.prepares,[])

    def test_unconfigured_program_never_offers_a_spend(self):
        self.quote_overrides={"can_prepare":False}
        self.open()
        self.frame.get_by_text("Списание пока выключено: настройка и проверка магазина не завершены.",exact=True).wait_for()
        self.assertTrue(self.frame.locator("#points").is_disabled())
        self.assertEqual(self.prepares,[])

    def test_old_order_unauthorized_response_cannot_clear_new_order_session(self):
        self.open();held=[]
        def delayed(route):
            if route.request.url.endswith(ORDER) and not held:
                held.append(route);return
            return self.route(route)
        self.context.route("**/api/widget/orders/*",delayed)
        with self.page.expect_request("**/api/widget/orders/"+ORDER):
            self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.page.evaluate("openOrder('"+NEXT+"')")
        self.frame.get_by_text("5000 бонусов",exact=True).wait_for()
        self.assertEqual(self.session_count,2)
        held[0].fulfill(status=401,json={"error":"expired old order"})
        self.page.wait_for_timeout(100)
        with self.page.expect_request("**/api/widget/orders/"+NEXT) as request:
            self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.assertEqual(request.value.headers.get("authorization"),"Bearer synthetic-session-2")
        self.frame.get_by_text("5000 бонусов",exact=True).wait_for()
        self.assertEqual(self.session_count,2)

if __name__=="__main__":unittest.main()
