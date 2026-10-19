
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]
HOST="""<!doctype html><iframe src="https://widget.test/staff" style="width:100%;height:1000px"></iframe>
<script>addEventListener('message',e=>{if(e.data.name==='UserContextRequest')e.source.postMessage(
{name:'UserContextResponse',correlationId:e.data.messageId,token:'synthetic'},'https://widget.test');});</script>"""
class StaffBrowserTests(unittest.TestCase):
    def setUp(self):
        self.pw=sync_playwright().start();self.browser=self.pw.chromium.launch();self.ctx=self.browser.new_context(viewport={"width":390,"height":844})
        self.page=self.ctx.new_page();self.updates=[];self.enabled=False;self.held_operators=None;self.hold_operators=False;self.ctx.route("**/*",self.route)
    def tearDown(self):self.ctx.close();self.browser.close();self.pw.stop()
    def route(self,r):
        url=r.request.url
        if url.startswith("https://online.moysklad.ru/"):return r.fulfill(content_type="text/html",body=HOST)
        if not url.startswith("https://widget.test/"):return r.abort()
        path=url.split("https://widget.test",1)[1]
        if path=="/api/session":return r.fulfill(json={"token":"synthetic"})
        if path=="/api/me":return r.fulfill(json={"role":"admin"})
        if path=="/api/admin/operators":
            data={"items":[dict(user_id="user",login="florist@example.test",enabled=self.enabled,version=len(self.updates),reason=None)]}
            if self.hold_operators:
                self.hold_operators=False;self.held_operators=(r,data);return
            return r.fulfill(json=data)
        if path=="/api/admin/operators/user":
            self.updates.append(r.request.post_data_json);self.enabled=r.request.post_data_json["enabled"]
            return r.fulfill(json={"enabled":self.enabled,"version":len(self.updates)})
        file=ROOT/"web"/("staff.html" if path=="/staff" else path.lstrip("/"))
        if file.is_file():return r.fulfill(content_type="text/html" if file.suffix==".html" else "text/css" if file.suffix==".css" else "text/javascript",body=file.read_text())
        return r.fulfill(status=404)
    def test_explicit_grant_revocation_reason_and_mobile(self):
        self.page.goto("https://online.moysklad.ru/test");f=self.page.frame_locator("iframe")
        f.get_by_role("button",name="Разрешить доступ",exact=True).click()
        f.get_by_label("Причина изменения доступа").fill("Назначен флорист")
        f.get_by_role("button",name="Подтвердить",exact=True).click()
        f.get_by_role("button",name="Отозвать доступ",exact=True).click()
        f.get_by_label("Причина изменения доступа").fill("Закончил работу")
        f.get_by_role("button",name="Подтвердить",exact=True).click()
        f.get_by_role("button",name="Разрешить доступ",exact=True).wait_for()
        self.assertEqual([x["enabled"] for x in self.updates],[True,False])
        self.assertEqual([x["expected_version"] for x in self.updates],[0,1])
        self.assertTrue(self.page.frames[1].evaluate("document.documentElement.scrollWidth<=innerWidth"))


    def test_older_refresh_cannot_restore_access_state_after_save(self):
        self.page.goto("https://online.moysklad.ru/test");f=self.page.frame_locator("iframe")
        f.get_by_role("button",name="Разрешить доступ",exact=True).wait_for()
        self.hold_operators=True
        with self.page.expect_request("https://widget.test/api/admin/operators"):
            f.get_by_role("button",name="Обновить",exact=True).click()
        f.get_by_role("button",name="Разрешить доступ",exact=True).click()
        self.assertTrue(f.get_by_role("dialog",name="Разрешить доступ к бонусам аккаунта",exact=True).is_visible())
        f.get_by_label("Причина изменения доступа").fill("Назначен флорист")
        f.get_by_role("button",name="Подтвердить",exact=True).click()
        f.get_by_role("button",name="Отозвать доступ",exact=True).wait_for()
        self.assertIsNotNone(self.held_operators)
        held,data=self.held_operators
        with self.page.expect_response("https://widget.test/api/admin/operators") as response:
            held.fulfill(json=data)
        response.value.finished()
        self.page.frames[1].evaluate("()=>new Promise(requestAnimationFrame)")
        self.assertTrue(f.get_by_role("button",name="Отозвать доступ",exact=True).is_visible())
        self.assertEqual(f.get_by_role("button",name="Разрешить доступ",exact=True).count(),0)
        self.assertEqual(len(self.updates),1)

if __name__=="__main__":unittest.main()
