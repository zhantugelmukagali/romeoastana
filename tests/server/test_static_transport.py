"""Actual ASGI/static responses must preserve bytes, lengths, ranges and headers."""
import unittest
import asyncio
from pathlib import Path
from uuid import uuid4
from fastapi.testclient import TestClient
from bonuspro.api.app import SecurityHeaders, create_app
from bonuspro.security import Secrets
from bonuspro.settings import Settings

ROOT = Path(__file__).resolve().parents[2]

def application():
    secrets = Secrets(str(uuid4()), "ci.transport", "ci-secret-" + "x"*40, b"k"*32)
    config = Settings("https://testserver", secrets,
                      "postgresql://unused@localhost/unused", "postgresql://unused@localhost/unused")
    return create_app(config, store=object(), vendor=object())


class StaticBodyTests(unittest.TestCase):
    def setUp(self):
        self.app = application()

    def test_every_script_and_stylesheet_has_complete_body_and_declared_length(self):
        with TestClient(self.app, base_url="https://testserver") as client:
            for path in sorted((ROOT/"web/assets").iterdir()):
                if not path.is_file():
                    continue
                response = client.get("/assets/"+path.name)
                self.assertEqual(response.status_code, 200, path.name)
                self.assertEqual(response.content, path.read_bytes(), path.name)
                self.assertEqual(int(response.headers["content-length"]), len(response.content), path.name)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_static_head_has_length_without_body_and_range_is_exact(self):
        data = (ROOT/"web/assets/bridge.js").read_bytes()
        with TestClient(self.app, base_url="https://testserver") as client:
            head = client.head("/assets/bridge.js")
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.content, b"")
            self.assertEqual(int(head.headers["content-length"]), len(data))
            partial = client.get("/assets/bridge.js", headers={"Range": "bytes=0-7"})
            self.assertEqual(partial.status_code, 206)
            self.assertEqual(partial.content, data[:8])
            self.assertEqual(int(partial.headers["content-length"]), 8)
            self.assertEqual(partial.headers["cache-control"], "no-store")

    def test_html_and_error_responses_keep_security_headers(self):
        with TestClient(self.app, base_url="https://testserver") as client:
            response = client.get("/admin")
            self.assertEqual(response.content, (ROOT/"web/index.html").read_bytes())
            for response in (response, client.get("/assets/missing.js")):
                self.assertEqual(response.headers["referrer-policy"], "no-referrer")
                self.assertIn("frame-ancestors https://online.moysklad.ru",
                              response.headers["content-security-policy"])
            self.assertEqual(response.status_code, 404)


class AsgiTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_body_is_complete_until_connection_really_closes(self):
        app = application()
        events = []
        first = True
        completed = asyncio.Event()
        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type":"http.request", "body":b"", "more_body":False}
            await completed.wait()
            return {"type":"http.disconnect"}
        async def send(message):
            events.append(dict(message))
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed.set()
        scope = {"type":"http", "asgi":{"version":"3.0","spec_version":"2.3"},
                 "http_version":"1.1", "method":"GET", "scheme":"https",
                 "path":"/assets/bridge.js", "raw_path":b"/assets/bridge.js",
                 "root_path":"", "query_string":b"", "headers":[(b"host",b"testserver")],
                 "server":("testserver",443), "client":("127.0.0.1",12345)}
        await app(scope, receive, send)
        start = next(e for e in events if e["type"]=="http.response.start")
        body = b"".join(e.get("body",b"") for e in events if e["type"]=="http.response.body")
        self.assertEqual(start["status"],200)
        self.assertEqual(body,(ROOT/"web/assets/bridge.js").read_bytes())
        self.assertEqual(int(dict(start["headers"])[b"content-length"]),len(body))
        self.assertFalse(events[-1].get("more_body",False))

    async def test_headers_do_not_consume_receive_or_change_stream_chunks(self):
        chunks = [b"first", b"second", b"third"]
        async def upstream(scope, receive, send):
            await send({"type":"http.response.start","status":200,
                        "headers":[(b"content-length",b"16")]})
            for i, chunk in enumerate(chunks):
                await send({"type":"http.response.body","body":chunk,"more_body":i<2})
        async def receive():
            raise AssertionError("Header middleware must not read incoming messages")
        events = []
        async def send(message):
            events.append(message)
        await SecurityHeaders(upstream)({"type":"http"},receive,send)
        self.assertEqual([e["body"] for e in events[1:]],chunks)
        self.assertEqual(dict(events[0]["headers"])[b"content-length"],b"16")
        self.assertEqual(dict(events[0]["headers"])[b"x-content-type-options"],b"nosniff")

    async def test_file_send_extension_is_forwarded_without_streaming_conversion(self):
        file_event = {"type":"http.response.pathsend","path":"/synthetic/file.js"}
        async def upstream(scope, receive, send):
            await send({"type":"http.response.start","status":200,"headers":[(b"content-length",b"10")]})
            await send(file_event)
        async def receive():
            raise AssertionError("No receive call expected")
        events = []
        async def send(message):
            events.append(message)
        await SecurityHeaders(upstream)({"type":"http"},receive,send)
        self.assertIs(events[1],file_event)
        self.assertEqual(dict(events[0]["headers"])[b"content-length"],b"10")
