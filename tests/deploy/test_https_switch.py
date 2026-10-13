import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("switch_https", Path(__file__).resolve().parents[2] / "deploy/switch_https.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SwitchTests(unittest.TestCase):
    def test_success_keeps_old_stopped(self):
        calls = []
        module.switch(calls.append, lambda: calls.append(["probe"]))
        self.assertEqual(calls[0], ["docker", "stop", "--time", "20", module.OLD_WEB])
        self.assertEqual(calls[-1], ["probe"])
        self.assertFalse(any(c[:2] == ["docker", "start"] for c in calls))

    def test_tls_failure_restores_old(self):
        calls = []
        def bad_probe():
            raise RuntimeError("TLS failed")
        with self.assertRaisesRegex(RuntimeError, "TLS failed"):
            module.switch(calls.append, bad_probe)
        self.assertEqual(calls[-2], [*module.COMPOSE, "stop", "web"])
        self.assertEqual(calls[-1], ["docker", "start", module.OLD_WEB])

    def test_start_failure_restores_old(self):
        calls = []
        def runner(args):
            calls.append(args)
            if "up" in args:
                raise RuntimeError("port unavailable")
        with self.assertRaisesRegex(RuntimeError, "port unavailable"):
            module.switch(runner, lambda: self.fail("probe must not run"))
        self.assertEqual(calls[-1], ["docker", "start", module.OLD_WEB])

    def test_rollback_attempted_even_if_new_stop_fails(self):
        calls = []
        def runner(args):
            calls.append(args)
            if args == [*module.COMPOSE, "stop", "web"]:
                raise RuntimeError("stop failure")
        def bad_probe():
            raise RuntimeError("probe failed")
        with self.assertRaisesRegex(RuntimeError, "Rollback attempted"):
            module.switch(runner, bad_probe)
        self.assertEqual(calls[-1], ["docker", "start", module.OLD_WEB])

if __name__ == "__main__":
    unittest.main()
