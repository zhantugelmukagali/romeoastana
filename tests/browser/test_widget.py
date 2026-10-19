"""Real Chromium, synthetic MoySklad host. Not a live provider acceptance test."""
import base64,json,os,re,unittest
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
        self.resumes=[];self.reject_resume=None;self.lose_resume=False;self.change_during_resume=False;self.change_during_verify=False;self.save_during_resume=False;self.resume_hash="b"*64;self.resume_allowed=True
        self.personal=None;self.prepared_personal=False
        self.order_error_details={};self.reject_prepare_details={};self.verify_error=None;self.verify_error_details={};self.prepared_points=1000
        self.order_error_code="unavailable";self.total_minor=1200000;self.eligible_minor=1000000;self.quote_overrides={};self.order_error=None;self.session_count=0;self.prepares=[];self.verifies=0;self.prepared=False;self.saved=False;self.lose=False;self.delay_change=False;self.reject_prepare=None;self.save_during_prepare=False;self.host_error=False
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
            if self.reject_prepare:return route.fulfill(status=self.reject_prepare,json={"error":"operation rejected",**self.reject_prepare_details})
            self.prepared=True;self.prepared_points=route.request.post_data_json["points"];self.prepared_personal=route.request.post_data_json.get("apply_personal",False)
            if self.save_during_prepare:
                self.saved=True
                self.page.evaluate("send({name:'Save',messageId:131,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
            if self.lose:return route.abort()
            if self.delay_change:
                self.page.evaluate("send({name:'Change',messageId:120,objectState:{}})")
            return route.fulfill(json={"id":"attempt","order_id":ORDER,"points":self.prepared_points,"state":"verifying",
                "update_state":self.prepared_update(),"replayed":False,"recoverable":True,**self.personal_operation()})
        if path.endswith("/resume"):
            self.resumes.append(route.request.post_data_json)
            if self.reject_resume:return route.fulfill(status=self.reject_resume,json={"error":"operation conflict","reason":"customer_missing"})
            if self.lose_resume:return route.abort()
            if self.change_during_resume:
                self.page.evaluate("send({name:'Change',messageId:151,objectState:{}})")
            if self.save_during_resume:
                self.page.evaluate("send({name:'Save',messageId:152,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
            return route.fulfill(json={"id":"attempt","order_id":ORDER,"points":self.prepared_points,"state":"verifying","recoverable":True,
                "update_state":self.prepared_update(),**self.personal_operation()})
        if path.endswith("/verify"):
            self.verifies+=1
            if self.change_during_verify:
                self.page.evaluate("send({name:'Change',messageId:153,objectState:{}})")
            if self.verify_error:return route.fulfill(status=self.verify_error,json={"error":"unsupported_saved_order",**self.verify_error_details})
            return route.fulfill(json={"id":"attempt","order_id":ORDER,"points":self.prepared_points,
                "state":"committed" if self.saved else "verifying","verified":self.saved,"recoverable":not self.saved and self.resume_allowed,
                **({} if self.saved or not self.resume_allowed else {"resume_source_hash":self.resume_hash}),**self.personal_operation()})
        if path.startswith("/api/widget/orders/"):
            if self.order_error:return route.fulfill(status=self.order_error,json={"error":self.order_error_code,**self.order_error_details})
            if self.prepared and path.endswith(ORDER):
                return route.fulfill(json={"redemption":{"id":"attempt","order_id":ORDER,"points":self.prepared_points,"state":"verifying","recoverable":True,**self.personal_operation()}})
            return route.fulfill(json={"name":"0001","saved_source_hash":"a"*64,"eligible_minor":self.eligible_minor,"total_minor":self.total_minor,
                "can_prepare":self.quote_overrides.get("can_prepare",True),"quote":{"balance":5000,"max_spend_points":3000,"blocked_reason":None,"potential_earn_points":500,**self.quote_overrides.get("quote",{})},**({"personal_discount":self.personal} if self.personal else {})})
        file=ROOT/"web"/("widget.html" if path=="/widget" else path.lstrip("/"))
        if file.is_file():
            return route.fulfill(content_type={".html":"text/html",".css":"text/css",".js":"text/javascript"}[file.suffix],body=file.read_text())
        return route.fulfill(status=404)

    def personal_operation(self):
        return {"apply_personal":True,"application_id":"attempt","personal_discount_minor":self.personal["discount_minor"],
                "personal_rate_bps":self.personal["rate_bps"]} if self.prepared_personal else {}
    def prepared_update(self):
        positions=([{"id":"line","price":self.personal["eligible_minor_after"]-self.prepared_points*100},{"id":"delivery"}]
                   if self.prepared_personal else [{"id":"line","discount":self.prepared_points/100}])
        return {"positions":positions,"attributes":[{"id":"marker","value":"proof"}]}
    def configure_personal(self,stack=False,empty=False):
        if empty:self.quote_overrides={"quote":{"balance":0,"available":0,"max_spend_points":0}}
        self.personal={"enabled":True,"policy_version":7,"assignment_version":3,"rate_bps":2000,
                       "external_policy":"best","allow_bonus_stack":stack,"discount_minor":200000,
                       "total_minor_after":1000000,"eligible_minor_after":800000,"can_apply":True,
                       "quote":{"balance":0 if empty else 5000,"available":0 if empty else 5000,"reserved":0,
                                "max_spend_points":2400 if stack and not empty else 0,"blocked_reason":None,
                                "potential_earn_points":400,"payable_minor":1000000,"source_hash":"c"*64}}
    def open_personal(self):
        self.page.goto("https://online.moysklad.ru/test-host");self.frame=self.page.frame_locator("iframe")
        self.frame.get_by_label("Скидка клиента 20%",exact=True).wait_for()

    def open(self):
        self.page.goto("https://online.moysklad.ru/test-host");self.frame=self.page.frame_locator("iframe")
        self.frame.get_by_text(re.compile(r"5\s?000 бонусов")).wait_for()
    def apply(self):
        self.frame.get_by_label("Списать бонусы",exact=True).fill("1000")
        self.frame.locator("#apply").click()
    def test_host_ack_is_not_a_success_until_saved_verification(self):
        self.open();self.apply()
        self.frame.get_by_text(re.compile(r"1\s?000 бонусов в резерве")).wait_for()
        self.assertEqual(self.page.evaluate("updates.length"),1)
        self.assertEqual(self.verifies,0)
        self.saved=True
        self.page.evaluate("send({name:'Save',messageId:130,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.locator("#pending[data-state=done]").wait_for()
        self.assertEqual(len(self.prepares),1);self.assertEqual(self.verifies,1)
    def test_unsaved_change_blocks_apply_even_after_refresh(self):
        self.open()
        self.page.evaluate("send({name:'Change',messageId:120,objectState:{}})")
        self.frame.get_by_text("Сохраните изменения в заказе.",exact=True).wait_for()
        self.assertTrue(self.frame.locator("#apply").is_disabled())
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.get_by_text("Сохраните изменения в заказе.",exact=True).wait_for()
        self.assertTrue(self.frame.locator("#apply").is_disabled())
        self.assertEqual(self.prepares,[])
    def test_lost_response_keeps_unknown_state_without_second_mutation(self):
        self.lose=True;self.open();self.apply()
        self.frame.get_by_text("Обновите данные. Не применяйте бонусы повторно.",exact=True).wait_for()
        self.assertEqual(self.page.evaluate("updates.length"),0)
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.get_by_text("Сохраните заказ в МойСклад, затем продолжите списание.",exact=True).wait_for()
        self.assertEqual(len(self.prepares),1)
    def test_host_reuse_resets_points_and_state_between_orders(self):
        self.open();self.frame.get_by_label("Списать бонусы",exact=True).fill("1000")
        self.page.evaluate("openOrder('"+NEXT+"')")
        self.frame.get_by_text(re.compile(r"5\s?000 бонусов")).wait_for()
        self.assertEqual(self.frame.get_by_label("Списать бонусы",exact=True).input_value(),"")
        self.assertEqual(self.page.frames[1].evaluate("localStorage.length+sessionStorage.length"),0)
    def test_change_during_prepare_does_not_apply_stale_positions(self):
        self.delay_change=True;self.open();self.apply()
        self.frame.get_by_text("Заказ изменился. Резерв сохранён — проверьте операцию.",exact=True).wait_for()
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
                self.frame.get_by_text(re.compile(r"5\s?000 бонусов")).wait_for()
        self.assertEqual(len(self.prepares),3)

    def test_save_during_prepare_is_verified_after_response_without_host_mutation(self):
        self.save_during_prepare=True;self.open();self.apply()
        self.frame.locator("#pending[data-state=done]").wait_for()
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.verifies,1)
        self.assertEqual(self.page.evaluate("updates.length"),0)

    def test_host_rejection_keeps_real_reservation_and_reports_without_waiting_for_timeout(self):
        self.host_error=True;self.open();self.apply()
        self.frame.locator("#status[data-error='true']").wait_for(timeout=5000)
        self.frame.get_by_text(re.compile(r"1\s?000 бонусов в резерве")).wait_for()
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.page.evaluate("updates.length"),1)
        self.assertEqual(self.verifies,0)

    def test_create_page_does_not_send_unsupported_feedback(self):
        self.open()
        self.page.evaluate("""window.feedback=0;window.addEventListener('message',e=>{if(e.data.name==='OpenFeedback')feedback++});
        send({name:'Open',messageId:190,extensionPoint:'document.customerorder.create',objectId:null});""")
        self.frame.get_by_text("Выберите покупателя и сохраните заказ.",exact=True).wait_for()
        self.assertEqual(self.page.evaluate("feedback"),0)

    def test_wrong_origin_or_source_cannot_supply_open_message(self):
        self.open()
        self.page.frames[1].evaluate("window.postMessage({name:'Open',messageId:999,extensionPoint:'document.customerorder.create'},'*')")
        self.assertTrue(self.frame.get_by_text(re.compile(r"5\s?000 бонусов")).is_visible())
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
        self.frame.get_by_text(re.compile(r"5\s?000 бонусов")).wait_for()
        self.apply()
        self.frame.get_by_text(re.compile(r"1\s?000 бонусов в резерве")).wait_for()
        self.saved=True
        self.page.evaluate("send({name:'Save',messageId:140,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.locator("#pending[data-state=done]").wait_for()
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
        self.assertTrue(self.frame.get_by_text(re.compile(r"5\s?000 бонусов")).is_visible())
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
        button=self.frame.locator("#apply")
        self.assertTrue(button.is_disabled())
        for value in ("0","3001","1.5","9007199254740993"):
            points.fill(value)
            self.assertTrue(button.is_disabled())
            self.assertEqual(points.get_attribute("aria-invalid"),"true")
        points.fill("1000")
        self.assertTrue(button.is_enabled())
        self.assertEqual(points.get_attribute("aria-invalid"),"false")
        actual="".join(self.frame.locator("#payable").inner_text().split())
        self.assertIn("11000₸",actual)
        self.assertEqual(self.prepares,[])

    def test_enter_applies_only_once_and_success_requires_verified_saved_order(self):
        self.open();points=self.frame.get_by_label("Списать бонусы",exact=True)
        points.fill("1000");points.press("Enter")
        self.frame.get_by_role("heading",name="Сохраните заказ",exact=True).wait_for()
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.frame.locator("#pending").get_attribute("data-state"),"waiting")
        self.frame.get_by_role("button",name="Проверить сохранение",exact=True).click()
        self.frame.locator("#pending[data-state=review]").wait_for()
        self.assertNotEqual(self.frame.locator("#pending").get_attribute("data-state"),"done")
        self.saved=True
        self.page.evaluate("send({name:'Save',messageId:230,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.get_by_role("heading",name="Списано",exact=True).wait_for()
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
        self.frame.locator("#balance").filter(has_text=re.compile("^0 бонусов$")).wait_for()
        self.assertFalse(self.frame.locator("#spend-form").is_visible())
        self.assertFalse(self.frame.locator("#apply").is_visible())
        self.assertTrue(self.frame.locator("#balances").is_visible())
        self.assertTrue(self.frame.locator("#points").is_disabled())
        self.assertTrue(self.frame.locator("#apply").is_disabled())
        self.assertEqual(self.prepares,[])

    def test_admin_review_blocks_even_a_positive_quote_limit(self):
        self.quote_overrides={"quote":{"blocked_reason":"return_review"}}
        self.open()
        self.frame.get_by_text("Списание приостановлено. Нужен администратор.",exact=True).wait_for()
        self.assertTrue(self.frame.locator("#points").is_disabled())
        self.assertTrue(self.frame.locator("#maximum-button").is_disabled())
        self.assertEqual(self.prepares,[])

    def test_unconfigured_program_never_offers_a_spend(self):
        self.quote_overrides={"can_prepare":False}
        self.open()
        self.frame.get_by_text("Программа ещё не настроена. Нужен администратор.",exact=True).wait_for()
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
        self.frame.get_by_text(re.compile(r"5\s?000 бонусов")).wait_for()
        self.assertEqual(self.session_count,2)
        held[0].fulfill(status=401,json={"error":"expired old order"})
        self.page.wait_for_timeout(100)
        with self.page.expect_request("**/api/widget/orders/"+NEXT) as request:
            self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.assertEqual(request.value.headers.get("authorization"),"Bearer synthetic-session-2")
        self.frame.get_by_text(re.compile(r"5\s?000 бонусов")).wait_for()
        self.assertEqual(self.session_count,2)


    def test_large_order_preview_keeps_minor_units_and_fits_narrow_widget(self):
        self.page.set_viewport_size({"width":390,"height":844})
        self.total_minor=8999999999999999;self.eligible_minor=self.total_minor
        self.quote_overrides={"quote":{"balance":9000000000000000,"max_spend_points":26999999999999}}
        self.page.goto("https://online.moysklad.ru/test-host");self.frame=self.page.frame_locator("iframe")
        self.frame.locator("#balance").wait_for()
        self.frame.get_by_role("button",name="Максимум",exact=True).click()
        actual="".join(self.frame.locator("#payable").inner_text().split())
        self.assertIn("63000000000000,99₸",actual)
        self.assertTrue(self.page.frames[1].evaluate("document.documentElement.scrollWidth<=innerWidth"))
        self.assertLessEqual(self.page.frames[1].evaluate("document.body.getBoundingClientRect().height"),560)
        self.assertEqual(self.prepares,[])


    def test_widget_controls_stay_inside_narrow_document_columns(self):
        self.open()
        for width in (320,360,390,520,760):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width":width,"height":720})
                self.page.evaluate("document.querySelector(\'iframe\').style.height=\'380px\'")
                self.page.frames[1].evaluate("()=>new Promise(requestAnimationFrame)")
                self.frame.get_by_role("button",name="Максимум",exact=True).click()
                self.assertTrue(self.page.frames[1].evaluate("""()=>{
                    const box=document.querySelector('main').getBoundingClientRect();
                    return document.documentElement.scrollWidth<=innerWidth &&
                      [...document.querySelectorAll('.controls>*')].every(el=>{const r=el.getBoundingClientRect();return r.left>=box.left&&r.right<=box.right;});
                }"""))
        metrics=self.page.frames[1].evaluate("""()=>{
            const main=document.querySelector('main').getBoundingClientRect();
            return {height:main.height,applyBottom:document.querySelector('#apply').getBoundingClientRect().bottom,width:innerWidth};
        }""")
        print("BP_WIDGET_LAYOUT="+json.dumps(metrics),flush=True)
        self.assertLessEqual(metrics["height"],380)
        self.assertEqual(self.prepares,[])

    def test_slow_order_read_does_not_fail_at_the_old_fifteen_second_timeout(self):
        self.open()
        self.page.clock.install()
        held=[]
        self.context.route("**/api/widget/orders/*",lambda route:held.append(route))
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.get_by_text("Загружаем бонусы…",exact=True).wait_for()
        self.page.clock.run_for(16000)
        self.assertEqual(len(held),1)
        self.assertTrue(self.frame.get_by_role("button",name="Обновить",exact=True).is_disabled())
        self.frame.get_by_text("Загружаем бонусы…",exact=True).wait_for()
        self.route(held[0])
        self.frame.get_by_text(re.compile(r"5\s?000 бонусов")).wait_for()
        self.assertFalse(self.frame.get_by_role("button",name="Обновить",exact=True).is_disabled())


    def test_zero_balance_is_compact_and_shows_the_server_earning_estimate(self):
        self.quote_overrides={"quote":{"balance":0,"available":0,"max_spend_points":0,"potential_earn_points":5000}}
        self.total_minor=10000000;self.eligible_minor=10000000
        self.page.set_viewport_size({"width":390,"height":700})
        self.page.goto("https://online.moysklad.ru/test-host");self.frame=self.page.frame_locator("iframe")
        self.frame.locator("#balance").filter(has_text=re.compile("^0 бонусов$")).wait_for()
        self.frame.get_by_text(re.compile(r"\+5\s?000 бонусов")).wait_for()
        self.assertFalse(self.frame.locator("#spend-form").is_visible())
        self.assertFalse(self.frame.locator("#apply").is_visible())
        self.assertTrue(self.frame.locator("#balances").is_visible())
        self.assertLessEqual(self.page.frames[1].evaluate("document.body.getBoundingClientRect().height"),300)
        self.assertEqual(self.prepares,[])
        if os.environ.get("GITHUB_ACTIONS")=="true":
            root=ROOT/"test-results";root.mkdir(exist_ok=True)
            body=self.page.frames[1].locator("body")
            body.screenshot(path=str(root/"bonuspro-widget-empty.png"))
            print("BP_VISUAL_WIDGET_EMPTY="+base64.b64encode(body.screenshot(type="jpeg",quality=75)).decode(),flush=True)

    def test_rules_are_collapsed_and_do_not_interrupt_the_spending_form(self):
        self.open()
        self.assertFalse(self.frame.locator("#conditions").get_attribute("open"))
        self.assertFalse(self.frame.get_by_text("1 бонус = 1 ₸. Доставка и услуги не участвуют.",exact=True).is_visible())
        self.frame.get_by_text("Условия",exact=True).click()
        self.frame.get_by_text("1 бонус = 1 ₸. Доставка и услуги не участвуют.",exact=True).wait_for()
        self.assertEqual(self.prepares,[])

    def test_existing_saved_operation_is_verified_automatically_without_another_prepare(self):
        self.open()
        reference=self.frame.locator("#order-label").inner_text()
        self.assertEqual(reference,"Заказ №0001")
        self.apply()
        self.frame.get_by_text(re.compile(r"1\s?000 бонусов в резерве")).wait_for()
        self.saved=True
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.locator("#pending[data-state=done]").wait_for()
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.verifies,1)
        self.assertEqual(self.frame.locator("#order-label").inner_text(),reference)
        self.assertFalse(self.frame.locator("#verify").is_visible())
        if os.environ.get("GITHUB_ACTIONS")=="true":
            body=self.page.frames[1].locator("body")
            (ROOT/"test-results").mkdir(exist_ok=True)
            body.screenshot(path=str(ROOT/"test-results/bonuspro-widget-done.png"))
            print("BP_VISUAL_WIDGET_DONE="+base64.b64encode(body.screenshot(type="jpeg",quality=75)).decode(),flush=True)

    def test_reserved_balance_is_explained_without_a_second_active_spend_form(self):
        self.quote_overrides={"quote":{"balance":5000,"available":3500,"max_spend_points":3000}}
        self.open()
        self.frame.get_by_text(re.compile(r"В резерве: 1\s?500")).wait_for()
        self.apply()
        self.frame.get_by_role("heading",name="Сохраните заказ",exact=True).wait_for()
        self.assertFalse(self.frame.locator("#summary").is_visible())
        self.assertFalse(self.frame.locator("#apply").is_visible())


    def test_primary_action_fits_the_real_widget_height_on_narrow_screens(self):
        self.open()
        for width in (320,360,390,520):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width":width,"height":720})
                self.page.evaluate("document.querySelector('iframe').style.height='380px'")
                self.page.frames[1].evaluate("()=>new Promise(requestAnimationFrame)")
                metrics=self.page.frames[1].evaluate("""()=>{
                    const rect=document.querySelector('#apply').getBoundingClientRect();
                    return {bottom:rect.bottom,height:rect.height,width:innerWidth,body:document.body.getBoundingClientRect().height};
                }""")
                print("BP_WIDGET_NARROW="+json.dumps(metrics),flush=True)
                self.assertLessEqual(metrics["bottom"],380)
                self.assertLessEqual(metrics["body"],380)
                self.assertGreaterEqual(metrics["height"],40)
                self.assertTrue(self.page.frames[1].evaluate("document.documentElement.scrollWidth<=innerWidth"))
        self.assertEqual(self.prepares,[])

    def test_reservation_and_verified_result_fit_the_narrow_widget(self):
        self.page.set_viewport_size({"width":320,"height":720})
        self.open()
        self.page.evaluate("document.querySelector('iframe').style.height='380px'")
        self.apply()
        self.frame.get_by_text(re.compile(r"1\s?000 бонусов в резерве")).wait_for()
        self.page.frames[1].evaluate("scrollTo(0,0)")
        bounds=self.frame.locator("#verify").bounding_box()
        frame_bounds=self.page.locator("iframe").bounding_box()
        self.assertLessEqual(bounds["y"]+bounds["height"],frame_bounds["y"]+380)
        self.assertGreaterEqual(bounds["height"],38)
        self.assertEqual(len(self.prepares),1)
        self.saved=True
        self.frame.get_by_role("button",name="Проверить сохранение",exact=True).click()
        self.frame.locator("#pending[data-state=done]").wait_for()
        self.assertFalse(self.frame.locator("#verify").is_visible())
        self.assertTrue(self.page.frames[1].evaluate("document.documentElement.scrollWidth<=innerWidth"))
        self.assertLessEqual(self.page.frames[1].evaluate("document.body.getBoundingClientRect().height"),380)
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.verifies,1)

    def test_provider_and_operator_access_errors_show_the_right_next_step(self):
        self.order_error=403
        for code,message in (
            ("moysklad_permissions_required","Нет доступа к данным МойСклад. Проверьте подключение решения."),
            ("operator_access_required","Нет доступа к бонусам. Обратитесь к администратору."),
        ):
            with self.subTest(code=code):
                self.order_error_code=code
                self.page.goto("https://online.moysklad.ru/test-host")
                self.frame=self.page.frame_locator("iframe")
                self.frame.get_by_text(message,exact=True).wait_for()
                self.assertFalse(self.frame.locator("#apply").is_visible())
        self.assertEqual(self.prepares,[])

    def test_initial_422_reports_order_check_instead_of_an_invented_discount_failure(self):
        self.order_error=422
        self.order_error_details={"error":"unsupported_saved_order","reason":"saved_order_invalid",
            "stage":"prepare","trace_id":"ab12cd34ef56","message":"private-customer-document"}
        self.page.goto("https://online.moysklad.ru/test-host");self.frame=self.page.frame_locator("iframe")
        status=self.frame.locator("#status[data-error=true]");status.wait_for()
        text=status.inner_text()
        self.assertIn("Не удалось проверить данные заказа",text)
        self.assertIn("BP-ab12cd34ef56",text)
        self.assertNotIn("НДС",text)
        self.assertNotIn("Скидка не рассчитана",text)
        self.assertNotIn("private-customer-document",text)
        self.assertFalse(self.frame.locator("#summary").is_visible())
        self.assertEqual(self.prepares,[])
        self.assertEqual(self.page.evaluate("updates.length"),0)

    def test_load_422_preserves_specific_safe_tax_or_customer_action(self):
        for reason,expected in (("vat_mode_unsupported","Этот режим НДС пока не поддерживается. Нужна проверка администратора."),
                                ("customer_missing","Выберите покупателя и сохраните заказ.")):
            with self.subTest(reason=reason):
                self.order_error=422;self.order_error_details={"reason":reason}
                self.page.goto("https://online.moysklad.ru/test-host");self.frame=self.page.frame_locator("iframe")
                self.frame.get_by_text(expected,exact=True).wait_for()
                self.assertFalse(self.frame.locator("#spend-form").is_visible())
        self.assertEqual(self.prepares,[])

    def test_unrepresentable_amount_preserves_input_and_allows_a_different_amount(self):
        self.open()
        self.reject_prepare=422
        self.reject_prepare_details={"reason":"discount_not_representable","trace_id":"ab12cd34ef56"}
        self.apply()
        self.frame.locator("#status[data-error=true]").wait_for()
        self.assertTrue(self.frame.locator("#summary").is_visible())
        self.assertFalse(self.frame.locator("#pending").is_visible())
        self.assertEqual(self.frame.get_by_label("Списать бонусы",exact=True).input_value(),"1000")
        self.assertFalse(self.frame.locator("#apply").is_disabled())
        self.assertEqual(self.page.evaluate("updates.length"),0)
        self.reject_prepare=None
        self.frame.get_by_label("Списать бонусы",exact=True).fill("500")
        self.frame.locator("#apply").click()
        self.frame.locator("#pending").wait_for()
        self.assertEqual([p["points"] for p in self.prepares],[1000,500])
        self.assertEqual(self.page.evaluate("updates.length"),1)

    def test_fulfilled_order_keeps_balance_visible_without_spending_controls(self):
        self.quote_overrides={"quote":{"blocked_reason":"order_fulfilled","max_spend_points":0}}
        self.open()
        self.frame.get_by_text("По заказу уже есть оплата или отгрузка. Новое списание недоступно.",exact=True).wait_for()
        self.assertTrue(self.frame.locator("#balances").is_visible())
        self.assertFalse(self.frame.locator("#spend-form").is_visible())
        self.assertFalse(self.frame.locator("#apply").is_visible())
        self.assertEqual(self.prepares,[])

    def test_linked_document_notice_does_not_claim_a_payment_was_completed(self):
        self.quote_overrides={"quote":{"blocked_reason":"linked_documents_present","max_spend_points":0}}
        self.open()
        text=self.frame.locator("#status").inner_text()
        self.assertIn("связанные документы",text)
        self.assertNotIn("уже есть оплата",text)
        self.assertFalse(self.frame.locator("#apply").is_visible())
        self.assertEqual(self.prepares,[])

    def test_verify_422_retains_the_reservation_and_does_not_offer_a_new_spend(self):
        self.open();self.apply()
        self.frame.locator("#pending").wait_for()
        self.verify_error=422
        self.verify_error_details={"reason":"saved_order_invalid","trace_id":"ab12cd34ef56"}
        self.frame.get_by_role("button",name="Проверить сохранение",exact=True).click()
        status=self.frame.locator("#status[data-error=true]");status.wait_for()
        self.assertIn("Резерв сохранён",status.inner_text())
        self.assertIn("BP-ab12cd34ef56",status.inner_text())
        self.assertTrue(self.frame.locator("#pending").is_visible())
        self.assertFalse(self.frame.locator("#apply").is_visible())
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.page.evaluate("updates.length"),1)


    def test_typed_verification_error_never_suggests_editing_a_reserved_order(self):
        self.open();self.apply()
        self.frame.locator("#pending").wait_for()
        self.verify_error=422
        self.verify_error_details={"reason":"customer_missing","trace_id":"ab12cd34ef56"}
        self.frame.get_by_role("button",name="Проверить сохранение",exact=True).click()
        self.frame.get_by_text("Не удалось проверить сохранённое списание. Резерв сохранён — нужен администратор. Код BP-ab12cd34ef56.",exact=True).wait_for()
        self.assertNotIn("Выберите покупателя",self.frame.locator("#status").inner_text())
        self.assertTrue(self.frame.locator("#pending").is_visible())
        self.assertFalse(self.frame.locator("#apply").is_visible())
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.page.evaluate("updates.length"),1)


    def recover_lost_prepare(self):
        self.lose=True;self.open();self.apply()
        self.frame.get_by_text("Обновите данные. Не применяйте бонусы повторно.",exact=True).wait_for()
        self.assertFalse(self.frame.locator("#resume").is_visible())
        self.lose=False
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.get_by_text("Сохраните заказ в МойСклад, затем продолжите списание.",exact=True).wait_for()
        self.frame.locator("#resume").wait_for()
        self.assertTrue(self.frame.locator("#resume").is_disabled())

    def save_for_resume(self):
        with self.page.expect_request("**/api/widget/orders/"+ORDER+"/verify"):
            self.page.evaluate("send({name:'Save',messageId:150,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.locator("#resume:not([disabled])").wait_for()

    def test_lost_prepare_resumes_the_existing_plan_only_after_a_fresh_save(self):
        self.recover_lost_prepare()
        self.frame.get_by_role("button",name="Проверить сохранение",exact=True).click()
        self.frame.locator("#verify:not([disabled])").wait_for()
        self.assertTrue(self.frame.locator("#resume").is_disabled())
        self.assertEqual(self.resumes,[])
        self.save_for_resume()
        self.frame.get_by_role("button",name="Продолжить списание",exact=True).click()
        self.frame.get_by_role("heading",name="Сохраните заказ",exact=True).wait_for()
        self.assertEqual(self.resumes,[{"attempt_id":"attempt","source_hash":"b"*64}])
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.page.evaluate("updates.length"),1)
        self.assertFalse(self.frame.locator("#apply").is_visible())
        self.assertFalse(self.frame.locator("#resume").is_visible())
        self.saved=True
        self.page.evaluate("send({name:'Save',messageId:154,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.locator("#pending[data-state=done]").wait_for()
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.locator("#pending[data-state=done]").wait_for()
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(len(self.resumes),1)
        self.assertEqual(self.page.evaluate("updates.length"),1)

    def test_change_during_recovery_verification_cannot_authorize_resume(self):
        self.recover_lost_prepare()
        self.change_during_verify=True
        with self.page.expect_request("**/api/widget/orders/"+ORDER+"/verify"):
            self.page.evaluate("send({name:'Save',messageId:150,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.locator("#verify:not([disabled])").wait_for()
        self.assertTrue(self.frame.locator("#resume").is_disabled())
        self.assertEqual(self.resumes,[])
        self.assertEqual(self.page.evaluate("updates.length"),0)
        self.assertEqual(len(self.prepares),1)

    def test_change_during_resume_never_dispatches_stale_positions(self):
        self.recover_lost_prepare();self.save_for_resume()
        self.change_during_resume=True
        self.frame.get_by_role("button",name="Продолжить списание",exact=True).click()
        self.frame.get_by_text("Заказ изменился. Резерв сохранён — проверьте операцию.",exact=True).wait_for()
        self.assertTrue(self.frame.locator("#resume").is_disabled())
        self.assertTrue(self.frame.locator("#pending").is_visible())
        self.assertEqual(len(self.resumes),1)
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.page.evaluate("updates.length"),0)

    def test_save_during_resume_is_verified_without_dispatching_stale_positions(self):
        self.recover_lost_prepare();self.save_for_resume()
        verifies=self.verifies
        self.save_during_resume=True
        self.frame.get_by_role("button",name="Продолжить списание",exact=True).click()
        self.frame.locator("#verify:not([disabled])").wait_for()
        self.assertEqual(self.verifies,verifies+1)
        self.assertTrue(self.frame.locator("#resume").is_disabled())
        self.assertEqual(len(self.resumes),1)
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.page.evaluate("updates.length"),0)

    def test_resume_rejections_keep_the_reservation_and_require_another_save(self):
        self.recover_lost_prepare()
        for code in (400,401,403,404,409,422):
            with self.subTest(status=code):
                self.reject_resume=None
                self.save_for_resume()
                self.reject_resume=code
                self.frame.get_by_role("button",name="Продолжить списание",exact=True).click()
                self.frame.locator("#status[data-error=true]").wait_for()
                self.assertTrue(self.frame.locator("#resume").is_disabled())
                self.assertTrue(self.frame.locator("#pending").is_visible())
                self.assertFalse(self.frame.locator("#apply").is_visible())
                self.assertNotIn("Выберите покупателя",self.frame.locator("#status").inner_text())
                self.assertEqual(self.page.evaluate("updates.length"),0)
        self.assertEqual(len(self.resumes),6)
        self.assertEqual(len(self.prepares),1)

    def test_lost_resume_response_does_not_redispatch_without_another_saved_verification(self):
        self.recover_lost_prepare();self.save_for_resume()
        self.lose_resume=True
        self.frame.get_by_role("button",name="Продолжить списание",exact=True).click()
        self.frame.locator("#status[data-error=true]").wait_for()
        self.assertTrue(self.frame.locator("#resume").is_disabled())
        self.frame.get_by_role("button",name="Проверить сохранение",exact=True).click()
        self.frame.locator("#verify:not([disabled])").wait_for()
        self.assertTrue(self.frame.locator("#resume").is_disabled())
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.locator("#verify:not([disabled])").wait_for()
        self.assertTrue(self.frame.locator("#resume").is_disabled())
        self.assertEqual(len(self.resumes),1)
        self.assertEqual(self.page.evaluate("updates.length"),0)
        self.lose_resume=False;self.save_for_resume()
        self.frame.get_by_role("button",name="Продолжить списание",exact=True).click()
        self.frame.get_by_role("heading",name="Сохраните заказ",exact=True).wait_for()
        self.assertEqual(len(self.resumes),2)
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.page.evaluate("updates.length"),1)

    def test_new_open_consumes_the_previous_recovery_save_authorization(self):
        self.recover_lost_prepare();self.save_for_resume()
        with self.page.expect_request("**/api/widget/orders/"+ORDER+"/verify"):
            self.page.evaluate("openOrder('"+ORDER+"')")
        self.frame.locator("#verify:not([disabled])").wait_for()
        self.assertTrue(self.frame.locator("#resume").is_disabled())
        self.assertEqual(self.resumes,[])
        self.assertEqual(self.page.evaluate("updates.length"),0)

    def test_resume_uses_the_latest_verified_source_after_a_new_save(self):
        self.recover_lost_prepare();self.save_for_resume()
        self.page.evaluate("send({name:'Change',messageId:155,objectState:{}})")
        self.assertTrue(self.frame.locator("#resume").is_disabled())
        self.resume_hash="c"*64;self.save_for_resume()
        self.frame.get_by_role("button",name="Продолжить списание",exact=True).click()
        self.frame.get_by_role("heading",name="Сохраните заказ",exact=True).wait_for()
        self.assertEqual(self.resumes,[{"attempt_id":"attempt","source_hash":"c"*64}])
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.page.evaluate("updates.length"),1)


    def test_restoring_an_unsafe_mismatch_needs_only_one_new_save_before_resume(self):
        self.resume_allowed=False;self.lose=True;self.open();self.apply()
        self.frame.get_by_text("Обновите данные. Не применяйте бонусы повторно.",exact=True).wait_for()
        self.lose=False
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.locator("#pending[data-state=review]").wait_for()
        self.frame.locator("#verify:not([disabled])").wait_for()
        self.assertFalse(self.frame.locator("#resume").is_visible())
        # The current saved order is unsafe and has no resume source hash.
        with self.page.expect_request("**/api/widget/orders/"+ORDER+"/verify"):
            self.page.evaluate("send({name:'Save',messageId:156,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.locator("#verify:not([disabled])").wait_for()
        self.assertFalse(self.frame.locator("#resume").is_visible())
        self.assertEqual(self.resumes,[])
        # Restoring the immutable original and saving once obtains fresh proof.
        self.resume_allowed=True;self.resume_hash="d"*64
        self.save_for_resume()
        self.frame.get_by_role("button",name="Продолжить списание",exact=True).click()
        self.frame.get_by_role("heading",name="Сохраните заказ",exact=True).wait_for()
        self.assertEqual(self.resumes,[{"attempt_id":"attempt","source_hash":"d"*64}])
        self.assertEqual(len(self.prepares),1)
        self.assertEqual(self.page.evaluate("updates.length"),1)

    def test_unconfigured_rules_409_shows_the_administrator_action(self):
        self.order_error=409;self.order_error_code="program_not_configured"
        self.page.goto("https://online.moysklad.ru/test-host");self.frame=self.page.frame_locator("iframe")
        self.frame.get_by_text("Программа ещё не настроена. Нужен администратор.",exact=True).wait_for()
        self.assertNotIn("Заказ или баланс изменился",self.frame.locator("#status").inner_text())
        self.assertFalse(self.frame.locator("#apply").is_visible())
        self.assertEqual(self.prepares,[])


    def open_integer_price_plan(self,*,lose_prepare=False):
        # One 99 990 KZT item plus an unchanged zero-cost delivery line.
        # A two-decimal percentage cannot express exactly 5 000 points here.
        self.total_minor=9999000;self.eligible_minor=9999000
        self.quote_overrides={"quote":{"max_spend_points":5000}}
        positions=[{"id":"line","price":9499000},{"id":"delivery"}]
        update={"positions":positions,"attributes":[{"id":"marker","value":"proof"}]}
        def prepare(route):
            self.prepares.append(route.request.post_data_json)
            self.prepared=True;self.prepared_points=route.request.post_data_json["points"]
            if lose_prepare:return route.abort()
            return route.fulfill(json={"id":"attempt","order_id":ORDER,"points":self.prepared_points,
                "state":"verifying","recoverable":True,"replayed":False,"update_state":update})
        def resume(route):
            self.resumes.append(route.request.post_data_json)
            return route.fulfill(json={"id":"attempt","order_id":ORDER,"points":self.prepared_points,
                "state":"verifying","recoverable":True,"replayed":True,"update_state":update})
        self.context.route("**/api/widget/orders/"+ORDER+"/prepare",prepare)
        self.context.route("**/api/widget/orders/"+ORDER+"/resume",resume)
        self.page.goto("https://online.moysklad.ru/test-host");self.frame=self.page.frame_locator("iframe")
        self.frame.locator("#balance").filter(has_text=re.compile(r"5\s?000 бонусов")).wait_for()
        return update

    def test_integer_minor_price_plan_reaches_host_unchanged_and_requires_saved_verification(self):
        expected=self.open_integer_price_plan()
        self.frame.get_by_label("Списать бонусы",exact=True).fill("5000")
        self.assertEqual("".join(self.frame.locator("#payable").inner_text().split()),"94990₸")
        self.frame.locator("#apply").click()
        self.frame.get_by_role("heading",name="Сохраните заказ",exact=True).wait_for()
        actual=self.page.evaluate("updates[0].updateState")
        self.assertEqual(actual,expected)
        self.assertIsInstance(actual["positions"][0]["price"],int)
        self.assertNotIn("discount",actual["positions"][0])
        self.assertFalse(self.frame.locator("#pending[data-state=done]").is_visible())
        self.assertEqual(self.verifies,0)
        self.saved=True
        self.page.evaluate("send({name:'Save',messageId:930,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.locator("#pending[data-state=done]").wait_for()
        self.assertEqual([p["points"] for p in self.prepares],[5000])
        self.assertEqual(self.verifies,1)
        self.assertEqual(self.page.evaluate("updates.length"),1)

    def test_lost_integer_price_plan_is_recovered_without_recalculating_or_second_prepare(self):
        expected=self.open_integer_price_plan(lose_prepare=True)
        self.frame.get_by_label("Списать бонусы",exact=True).fill("5000")
        self.frame.locator("#apply").click()
        self.frame.get_by_text("Обновите данные. Не применяйте бонусы повторно.",exact=True).wait_for()
        self.assertEqual(self.page.evaluate("updates.length"),0)
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.locator("#resume").wait_for()
        self.assertTrue(self.frame.locator("#resume").is_disabled())
        self.page.evaluate("send({name:'Save',messageId:931,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.locator("#resume:not([disabled])").wait_for()
        self.frame.locator("#resume").click()
        self.frame.get_by_role("heading",name="Сохраните заказ",exact=True).wait_for()
        self.assertEqual(self.page.evaluate("updates[0].updateState"),expected)
        self.assertEqual(self.resumes,[{"attempt_id":"attempt","source_hash":self.resume_hash}])
        self.assertEqual([p["points"] for p in self.prepares],[5000])
        self.assertFalse(self.frame.locator("#pending[data-state=done]").is_visible())
        self.saved=True
        self.page.evaluate("send({name:'Save',messageId:932,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.locator("#pending[data-state=done]").wait_for()
        self.assertEqual(self.page.evaluate("updates.length"),1)
        self.assertEqual(len(self.prepares),1)


    def test_personal_only_zero_balance_applies_once_and_waits_for_saved_verification(self):
        self.configure_personal(empty=True);self.open_personal()
        self.assertFalse(self.frame.locator("#spend-form").is_visible())
        self.assertEqual("".join(self.frame.locator("#personal-value").inner_text().split()),"−2000₸")
        self.assertEqual("".join(self.frame.locator("#payable").inner_text().split()),"10000₸")
        self.frame.get_by_role("button",name="Применить скидку",exact=True).click()
        self.frame.get_by_role("heading",name="Сохраните заказ",exact=True).wait_for()
        self.assertEqual(len(self.prepares),1)
        self.assertEqual({k:self.prepares[0][k] for k in ("points","apply_personal","expected_policy_version","expected_assignment_version","source_hash")},
                         {"points":0,"apply_personal":True,"expected_policy_version":7,"expected_assignment_version":3,"source_hash":"a"*64})
        self.assertEqual(self.page.evaluate("updates[0].updateState.positions"),[{"id":"line","price":800000},{"id":"delivery"}])
        self.assertFalse(self.frame.locator("#pending-bonus").is_visible())
        self.assertNotIn("резерв",self.frame.locator("#pending").inner_text())
        self.assertEqual(self.verifies,0)
        self.saved=True
        self.page.evaluate("send({name:'Save',messageId:180,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.get_by_role("heading",name="Скидка применена",exact=True).wait_for()
        self.assertEqual(self.frame.locator("#pending").get_attribute("data-state"),"done")
        self.assertNotIn("Списано",self.frame.locator("#pending").inner_text())
        self.assertEqual(len(self.prepares),1);self.assertEqual(self.verifies,1)

    def test_combined_personal_and_bonus_use_discounted_limit_and_one_update(self):
        self.configure_personal(stack=True);self.open_personal()
        field=self.frame.get_by_label("Списать бонусы",exact=True)
        self.assertEqual(field.get_attribute("max"),"2400")
        field.fill("500")
        self.assertEqual("".join(self.frame.locator("#payable").inner_text().split()),"9500₸")
        self.assertEqual("".join(self.frame.locator("#earn-value").inner_text().split()),"+400бонусов")
        self.frame.get_by_role("button",name="Применить",exact=True).click()
        self.frame.get_by_text("500 бонусов в резерве",exact=True).wait_for()
        self.assertEqual(len(self.prepares),1);self.assertEqual(self.prepares[0]["points"],500)
        self.assertTrue(self.prepares[0]["apply_personal"])
        self.assertEqual(self.page.evaluate("updates.length"),1)
        self.assertEqual(self.page.evaluate("updates[0].updateState.positions"),[{"id":"line","price":750000},{"id":"delivery"}])
        self.saved=True
        self.page.evaluate("send({name:'Save',messageId:181,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.get_by_role("heading",name="Скидка применена",exact=True).wait_for()
        self.frame.get_by_text("500 бонусов списано",exact=True).wait_for()

    def test_personal_opt_out_restores_original_bonus_quote_and_legacy_prepare(self):
        self.configure_personal();self.open_personal()
        self.frame.get_by_label("Скидка клиента 20%",exact=True).uncheck()
        field=self.frame.get_by_label("Списать бонусы",exact=True)
        self.assertEqual(field.get_attribute("max"),"3000")
        self.assertTrue(self.frame.locator("#apply").is_disabled())
        field.fill("1000")
        self.assertEqual("".join(self.frame.locator("#payable").inner_text().split()),"11000₸")
        self.frame.get_by_role("button",name="Применить",exact=True).click()
        self.frame.get_by_text(re.compile(r"1\s?000 бонусов в резерве")).wait_for()
        self.assertNotIn("apply_personal",self.prepares[0])
        self.assertEqual(self.prepares[0]["source_hash"],"a"*64)

    def test_lost_personal_prepare_recovers_same_application_only_after_current_save(self):
        self.configure_personal(empty=True);self.lose=True;self.open_personal()
        self.frame.get_by_role("button",name="Применить скидку",exact=True).click()
        self.frame.get_by_text("Обновите данные. Не применяйте скидку повторно.",exact=True).wait_for()
        self.assertNotIn("резерв",self.frame.locator("#pending").inner_text())
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.get_by_role("button",name="Продолжить применение",exact=True).wait_for()
        self.assertTrue(self.frame.get_by_role("button",name="Продолжить применение",exact=True).is_disabled())
        self.page.evaluate("send({name:'Save',messageId:182,extensionPoint:'document.customerorder.edit',objectId:'"+ORDER+"'})")
        self.frame.get_by_role("button",name="Продолжить применение",exact=True).click()
        self.frame.get_by_role("heading",name="Сохраните заказ",exact=True).wait_for()
        self.assertEqual(len(self.prepares),1);self.assertEqual(len(self.resumes),1)
        self.assertEqual(self.resumes[0],{"attempt_id":"attempt","source_hash":self.resume_hash})
        self.assertEqual(self.page.evaluate("updates.length"),1)
        self.assertEqual(self.page.evaluate("updates[0].updateState.positions"),[{"id":"line","price":800000},{"id":"delivery"}])

    def test_personal_prepare_conflict_requires_new_quote_and_current_versions(self):
        self.configure_personal(empty=True);self.reject_prepare=409;self.open_personal()
        self.frame.get_by_role("button",name="Применить скидку",exact=True).click()
        self.frame.get_by_text("Заказ или баланс изменился. Обновите данные.",exact=True).wait_for()
        self.assertFalse(self.frame.locator("#pending").is_visible())
        self.assertEqual(self.page.evaluate("updates.length"),0);self.assertEqual(len(self.prepares),1)
        self.personal["policy_version"]=8;self.reject_prepare=None
        self.frame.get_by_role("button",name="Обновить",exact=True).click()
        self.frame.get_by_role("button",name="Применить скидку",exact=True).click()
        self.frame.get_by_role("heading",name="Сохраните заказ",exact=True).wait_for()
        self.assertEqual(self.prepares[1]["expected_policy_version"],8)
        self.assertNotEqual(self.prepares[0]["request_id"],self.prepares[1]["request_id"])

    def test_personal_projection_failure_keeps_original_bonus_spending_available(self):
        self.configure_personal();self.personal.update(can_apply=False,blocked_reason="discount_not_representable")
        self.open()
        self.frame.get_by_text("Персональная скидка не рассчитана точно. Бонусы можно списать отдельно.",exact=True).wait_for()
        self.assertFalse(self.frame.locator("#personal-row").is_visible())
        self.apply()
        self.frame.get_by_text(re.compile(r"1\s?000 бонусов в резерве")).wait_for()
        self.assertNotIn("apply_personal",self.prepares[0])

    def test_personal_combined_primary_action_fits_widget_at_all_supported_widths(self):
        self.configure_personal(stack=True);self.open_personal()
        for width in (320,360,400,520,760):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width":width,"height":720})
                self.page.evaluate("document.querySelector('iframe').style.height='380px'")
                self.page.frames[1].evaluate("()=>new Promise(requestAnimationFrame)")
                metrics=self.page.frames[1].evaluate("""()=>{
                    const main=document.querySelector('main').getBoundingClientRect(),button=document.querySelector('#apply').getBoundingClientRect();
                    return {body:document.body.getBoundingClientRect().height,bottom:button.bottom,height:button.height};
                }""")
                self.assertLessEqual(metrics["body"],380);self.assertLessEqual(metrics["bottom"],380)
                self.assertGreaterEqual(metrics["height"],40)
                self.assertTrue(self.page.frames[1].evaluate("document.documentElement.scrollWidth<=innerWidth"))

if __name__=="__main__":unittest.main()


