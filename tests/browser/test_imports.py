import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]
HOST="""<!doctype html><iframe src="https://widget.test/migration" style="width:100%;height:1100px"></iframe>
<script>addEventListener('message',e=>{if(e.data.name==='UserContextRequest')e.source.postMessage(
{name:'UserContextResponse',correlationId:e.data.messageId,token:'synthetic'},'https://widget.test');});</script>"""
class ImportBrowserTests(unittest.TestCase):
    def setUp(self):
        self.pw=sync_playwright().start();self.browser=self.pw.chromium.launch()
        self.ctx=self.browser.new_context(viewport={"width":390,"height":844});self.page=self.ctx.new_page()
        self.commits=[];self.previews=[];self.ctx.route("**/*",self.route)
    def tearDown(self):self.ctx.close();self.browser.close();self.pw.stop()
    def route(self,r):
        url=r.request.url
        if url.startswith("https://online.moysklad.ru/"):return r.fulfill(content_type="text/html",body=HOST)
        if not url.startswith("https://widget.test/"):return r.abort()
        path=url.split("https://widget.test",1)[1]
        if path=="/api/session":return r.fulfill(json={"token":"synthetic"})
        if path=="/api/me":return r.fulfill(json={"role":"admin"})
        if path=="/api/admin/imports":return r.fulfill(json={"items":[]})
        if path.endswith("/preview"):
            self.previews.append(r.request.post_data_json)
            return r.fulfill(json={"id":"batch","digest":"a"*64,"total":123,
                "rows":[{"name":"Customer","counterparty_id":"11111111-1111-4111-8111-111111111111","points":123}]})
        if path.endswith("/commit"):
            self.commits.append(r.request.post_data_json);return r.fulfill(json={"committed":True,"total":123,"customers":1})
        file=ROOT/"web"/("migration.html" if path=="/migration" else path.lstrip("/"))
        if file.is_file():return r.fulfill(content_type="text/html" if file.suffix==".html" else "text/css" if file.suffix==".css" else "text/javascript",body=file.read_text())
        return r.fulfill(status=404)
    def test_preview_confirmation_and_mobile_layout(self):
        self.page.goto("https://online.moysklad.ru/test");f=self.page.frame_locator("iframe")
        f.get_by_label("Файл остатков").set_input_files({"name":"balances.csv","mimeType":"text/csv",
            "buffer":b"counterparty_id,bonus_balance\n11111111-1111-4111-8111-111111111111,123\n"})
        f.get_by_role("button",name="Проверить файл",exact=True).click()
        f.get_by_text("Проверка завершена. Балансы ещё не изменены.",exact=True).wait_for()
        self.assertEqual(self.commits,[]);self.assertEqual(len(self.previews),1)
        self.assertFalse(f.get_by_role("button",name="Перенести остатки",exact=True).is_enabled())
        f.get_by_label("Клиенты и общая сумма сверены с исходной выгрузкой").check()
        f.get_by_role("button",name="Перенести остатки",exact=True).click()
        f.get_by_text("Укажите основание переноса.",exact=True).wait_for();self.assertEqual(self.commits,[])
        f.get_by_label("Основание переноса").fill("Сверено по ведомости")
        f.get_by_role("button",name="Перенести остатки",exact=True).click()
        f.get_by_text("Перенесено 123 бонусов для 1 клиентов.",exact=True).wait_for()
        self.assertEqual(self.commits,[{"digest":"a"*64,"reason":"Сверено по ведомости"}])
        self.assertTrue(self.page.frames[1].evaluate("document.documentElement.scrollWidth<=innerWidth"))
        self.assertEqual(self.page.frames[1].evaluate("localStorage.length+sessionStorage.length"),0)

if __name__=="__main__":unittest.main()
