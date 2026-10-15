"""Chromium cabinet UX with synthetic host and API, never live financial acceptance."""
import base64,json,os,unittest
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]
CUSTOMER="11111111-1111-4111-8111-111111111111"
TX="22222222-2222-4222-8222-222222222222"
NAME="Алия <img src=x onerror=alert(1)>"
HOST="""<!doctype html><meta charset="utf-8"><style>body{margin:0}iframe{border:0;width:100%;height:1100px}</style>
<iframe title="БонусПро" src="https://widget.test/admin"></iframe><script>
addEventListener('message',e=>{if(e.data?.name==='UserContextRequest')
e.source.postMessage({name:'UserContextResponse',correlationId:e.data.messageId,token:'synthetic'},'https://widget.test')});
</script>"""

class PortalBrowserTests(unittest.TestCase):
    def setUp(self):
        self.runtime=sync_playwright().start();self.browser=self.runtime.chromium.launch()
        self.context=self.browser.new_context(viewport={"width":1440,"height":1050})
        self.page=self.context.new_page();self.context.route("**/*",self.route)
        self.sessions=0;self.role="admin";self.state="Activated";self.empty=False;self.calls=[]
    def tearDown(self):
        self.context.close();self.browser.close();self.runtime.stop()
    def route(self,route):
        url=route.request.url
        if url.startswith("https://online.moysklad.ru/"):return route.fulfill(content_type="text/html; charset=utf-8",body=HOST)
        if not url.startswith("https://widget.test/"):return route.abort()
        path=urlsplit(url).path;query=parse_qs(urlsplit(url).query)
        if path.startswith("/api/"):
            self.calls.append(path)
            if path=="/api/session":
                self.sessions+=1;return route.fulfill(json={"token":"synthetic-session"})
            if path=="/api/me":return route.fulfill(json={"role":self.role})
            if path=="/api/admin/overview":return route.fulfill(json={
                "customers":0 if self.empty else 124,"balance":"0" if self.empty else "184500",
                "reserved":"0" if self.empty else "1200","credited":"0" if self.empty else "296700",
                "redeemed":"0" if self.empty else "112200","attention":0,"state":self.state,
                "rules":{"earn_bps":500,"spend_bps":3000,"version":1},"generated_at":"2026-10-15T05:00:00Z"})
            if path=="/api/admin/customers":return route.fulfill(json={"items":[] if self.empty else [
                {"id":CUSTOMER,"name":NAME,"phone":"+7 700 111 22 33","balance":1500,"available":1200,"reserved":300,"enrolled":True}],"next_offset":None})
            if path=="/api/admin/customers/"+CUSTOMER:return route.fulfill(json={
                "id":CUSTOMER,"name":NAME,"phone":"+7 700 111 22 33","balance":1500,"available":1200,"reserved":300,"enrolled":True})
            if path=="/api/admin/activity":return route.fulfill(json={"items":[] if self.empty else [
                {"id":TX,"kind":"credit","points":1500,"created_at":"2026-10-15T04:00:00Z","customer_id":CUSTOMER,"order_id":None}],"next_cursor":None})
            return route.fulfill(status=404,json={"error":"missing fixture"})
        file=ROOT/"web"/("index.html" if path=="/admin" else path.lstrip("/"))
        if file.is_file():return route.fulfill(content_type={".html":"text/html; charset=utf-8",".css":"text/css",".js":"text/javascript"}[file.suffix],body=file.read_text())
        return route.fulfill(status=404)
    def open(self):
        self.page.goto("https://online.moysklad.ru/test-host")
        self.frame=self.page.frame_locator("iframe")
        self.frame.get_by_text("Программа запущена",exact=True).wait_for() if self.state=="Activated" else self.frame.get_by_text("Требуется настройка",exact=True).wait_for()
    def shot(self,name):
        if os.environ.get("GITHUB_ACTIONS")=="true":
            path=ROOT/"test-results";path.mkdir(exist_ok=True)
            body=self.page.frames[1].locator("body")
            body.screenshot(path=str(path/name))
            label="MOBILE" if "mobile" in name else "DESKTOP"
            print("BP_VISUAL_"+label+"="+base64.b64encode(body.screenshot(type="jpeg",quality=60)).decode(),flush=True)
    def test_real_numbers_navigation_and_desktop_screenshot(self):
        self.open()
        self.assertEqual(self.frame.locator("#total-balance").inner_text().replace("\u00a0","").replace("\u202f",""),"184500")
        self.assertEqual(self.frame.locator("#rule-earn").inner_text(),"5%")
        self.assertEqual(self.frame.get_by_role("navigation",name="Основная навигация").count(),1)
        self.frame.get_by_text("Начисление",exact=True).wait_for()
        self.assertTrue(self.page.frames[1].evaluate("getComputedStyle(document.querySelector(\'.panel\')).backgroundColor===\'rgb(255, 255, 255)\'"))
        self.shot("bonuspro-overview-desktop.png")
        self.frame.get_by_role("link",name="Клиенты",exact=True).click()
        self.frame.get_by_role("heading",name="Клиенты и бонусы",exact=True).wait_for()
        self.frame.get_by_text(NAME,exact=True).wait_for()
        self.assertEqual(self.sessions,1)
    def test_customer_search_balance_history_and_untrusted_name_are_safe(self):
        self.open();self.frame.get_by_role("link",name="Клиенты",exact=True).click()
        self.frame.get_by_label("Имя или телефон клиента").fill("700")
        self.frame.get_by_role("button",name="Найти",exact=True).click()
        self.frame.get_by_text("Результаты поиска: 700",exact=True).wait_for()
        self.frame.get_by_role("button",name="Открыть",exact=True).click()
        self.frame.get_by_role("heading",name=NAME,exact=True).wait_for()
        self.assertEqual(self.frame.locator("#customer-available").inner_text().replace("\u00a0","").replace("\u202f",""),"1200")
        self.frame.locator("#customer-history").get_by_text("Начисление",exact=True).wait_for()
        self.assertEqual(self.frame.locator("img").count(),0)
        self.frame.get_by_role("button",name="Закрыть карточку клиента",exact=True).click()
        self.assertFalse(self.frame.get_by_role("dialog").is_visible())
    def test_setup_and_empty_state_do_not_invent_financial_activity(self):
        self.state="SettingsRequired";self.empty=True;self.open()
        self.frame.get_by_role("heading",name="Настройте программу перед первой продажей",exact=True).wait_for()
        self.frame.get_by_text("Операций пока нет",exact=True).wait_for()
        self.assertEqual(self.frame.locator("#total-balance").inner_text(),"0")
        self.frame.get_by_role("link",name="Клиенты",exact=True).click()
        self.frame.get_by_text("Клиенты не найдены",exact=True).wait_for()
        self.frame.get_by_label("Имя или телефон клиента").fill("a")
        self.frame.get_by_role("button",name="Найти",exact=True).click()
        self.frame.get_by_text("Введите минимум два символа.",exact=True).wait_for()
    def test_mobile_layout_and_order_widget_help(self):
        self.page.set_viewport_size({"width":390,"height":844});self.open()
        self.frame.get_by_role("button",name="Как пользоваться",exact=True).click()
        self.frame.get_by_role("heading",name="Бонусы в заказе покупателя",exact=True).wait_for()
        self.frame.get_by_role("button",name="Закрыть инструкцию",exact=True).click()
        self.assertTrue(self.page.frames[1].evaluate("document.documentElement.scrollWidth<=innerWidth"))
        self.assertEqual(self.page.frames[1].evaluate("localStorage.length+sessionStorage.length"),0)
        self.shot("bonuspro-overview-mobile.png")
    def test_worker_role_cannot_load_customer_directory_or_metrics(self):
        self.role="worker"
        self.page.goto("https://online.moysklad.ru/test-host");self.frame=self.page.frame_locator("iframe")
        self.frame.get_by_text("Кабинет доступен администратору МойСклад. Сотрудники работают с бонусами в виджете заказа.",exact=True).wait_for()
        self.assertFalse(self.frame.locator("#workspace").is_visible())
        self.assertNotIn("/api/admin/overview",self.calls)
        self.assertNotIn("/api/admin/customers",self.calls)

if __name__=="__main__":unittest.main()
