import importlib.util,unittest
from pathlib import Path
from uuid import uuid4
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"deploy"))
from start_processing import select_tenant,check_image,worker_override,ProcessingFailed,processing_confirmed,wait_for_report,ensure_worker_override

class ProcessingStartTests(unittest.TestCase):
    def test_only_enabled_selected_accounts_are_started(self):
        a,b=str(uuid4()),str(uuid4())
        rows=[dict(tenant_id=a,state="Activated"),dict(tenant_id=b,state="Suspended")]
        self.assertEqual(select_tenant(rows),a)
        with self.assertRaises(ProcessingFailed):select_tenant(rows,b)
        with self.assertRaises(ProcessingFailed):select_tenant([])
        rows.append(dict(tenant_id=b,state="SettingsRequired"))
        with self.assertRaises(ProcessingFailed):select_tenant(rows)
        self.assertEqual(select_tenant(rows,b),b)
    def test_worker_cannot_run_a_different_or_mutable_image(self):
        config={"services":{"worker":{"image":"bonuspro:abcdef012345"}}}
        check_image(config,"bonuspro:abcdef012345")
        for image in ("bonuspro:latest","bonuspro:111111111111"):
            with self.assertRaises(ProcessingFailed):check_image(config,image)
        with self.assertRaises(ProcessingFailed):check_image({},"bonuspro:abcdef012345")
    def test_override_does_not_contain_keys_or_activate_an_account(self):
        tenant=str(uuid4());text=worker_override(tenant)
        self.assertIn(tenant,text);self.assertIn("--health",text)
        for forbidden in ("SECRET","ENCRYPTION","PASSWORD","activate","migrate"):
            self.assertNotIn(forbidden,text)

    def test_only_a_new_successful_cycle_confirms_processing_startup(self):
        baseline="2026-10-19T10:00:00+00:00"
        report=dict(ready=True,last_processing_at="2026-10-19T10:00:01+00:00")
        self.assertTrue(processing_confirmed(report,baseline))
        for heartbeat in (None,"2026-10-19T09:59:59+00:00",baseline,"2026-10-19T10:00:01"):
            self.assertFalse(processing_confirmed({**report,"last_processing_at":heartbeat},baseline))
        self.assertFalse(processing_confirmed({**report,"ready":False},baseline))

    def test_startup_wait_is_bounded_and_does_not_activate_an_inactive_program(self):
        baseline="2026-10-19T10:00:00+00:00"
        before=dict(ready=True,last_processing_at=baseline,checks=[dict(id="activation",complete=True)])
        after={**before,"last_processing_at":"2026-10-19T10:00:01+00:00"}
        calls=[];reports=[before,after]
        def fetch():
            calls.append(True)
            return reports.pop(0)
        self.assertEqual(wait_for_report(fetch,baseline,clock=lambda:0,pause=lambda _:None),after)
        self.assertEqual(len(calls),2)
        moments=[0,61]
        self.assertEqual(wait_for_report(lambda:before,baseline,clock=lambda:moments.pop(0),pause=lambda _:self.fail("expired startup must not wait")),before)
        inactive={**before,"ready":False,"checks":[dict(id="activation",complete=False)]}
        self.assertEqual(wait_for_report(lambda:inactive,baseline,pause=lambda _:self.fail("startup must not activate a program")),inactive)

    def test_only_exact_legacy_managed_override_is_upgraded_and_backed_up(self):
        import tempfile
        tenant=str(uuid4());text=worker_override(tenant);legacy=text.replace("start_period: 600s","start_period: 45s")
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"compose.processing.yaml";path.write_text(legacy);path.chmod(0o640)
            backup=ensure_worker_override(path,tenant)
            self.assertEqual(path.read_text(),text)
            self.assertEqual(backup.read_text(),legacy)
            self.assertEqual(backup.stat().st_mode&0o777,0o600)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertIsNone(ensure_worker_override(path,tenant))
            self.assertEqual(len(list(path.parent.glob("*.before-health-*"))),1)

    def test_custom_or_other_tenant_override_is_never_replaced(self):
        import tempfile
        tenant=str(uuid4())
        for text in (worker_override(tenant).replace("interval: 15s","interval: 17s"),
                     worker_override(str(uuid4())).replace("start_period: 600s","start_period: 45s")):
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/"compose.processing.yaml";path.write_text(text)
                with self.assertRaises(ProcessingFailed):ensure_worker_override(path,tenant)
                self.assertEqual(path.read_text(),text)
                self.assertFalse(list(path.parent.glob("*.before-health-*")))

    def test_worker_first_cycle_grace_and_wait_cover_the_bounded_document_graph(self):
        from unittest.mock import patch
        import start_processing as processing
        self.assertIn("start_period: 600s",worker_override(str(uuid4())))
        with patch.object(processing,"run") as execute:
            processing.start_worker(["docker","compose"])
        args,kwargs=execute.call_args
        self.assertEqual(args[0][-3:],["--wait-timeout","720","worker"])
        self.assertEqual(kwargs["timeout"],900)

    def test_unconfirmed_fresh_report_is_failure_and_never_prints_processing_ready(self):
        from unittest.mock import patch
        import start_processing as processing
        report=dict(ready=False,last_processing_at=None)
        with patch("builtins.print") as output:
            with self.assertRaises(ProcessingFailed):
                processing.require_fresh_processing(report,"2026-10-20T10:00:00+00:00")
        self.assertIn("PROCESSING_STARTED",str(output.call_args_list))
        self.assertNotIn("PROCESSING_READY",str(output.call_args_list))

    def test_unconfirmed_cli_exits_nonzero_and_leaves_started_worker_running(self):
        import json,re
        from unittest.mock import patch
        from contextlib import ExitStack
        import start_processing as processing
        tenant=str(uuid4());image="bonuspro:"+"b"*12;calls=[]
        expected=re.search(r'if schema!="([0-9]{4})"',Path(processing.__file__).read_text()).group(1)
        report=dict(ready=False,last_processing_at=None,checked_at="2026-10-20T10:00:00+00:00",
                    checks=[dict(id="activation",complete=True)])
        def execute(args,**kwargs):
            calls.append(args)
            if "SELECT version_num" in str(args[-1]):return expected
            if "SELECT COALESCE" in str(args[-1]):return json.dumps([dict(tenant_id=tenant,state="Activated",has_rules=True,has_widget=True,has_cutoff=True)])
            if args[:2]==["docker","inspect"]:
                return "healthy" if args[-1]=="{{.State.Health.Status}}" else image
            if "config" in args and "--format" in args:return json.dumps(dict(services=dict(worker=dict(image=image))))
            if args[:2]==["docker","ps"]:return ""
            if args[:2]==["docker","exec"]:return json.dumps(report)
        with ExitStack() as stack:
            stack.enter_context(patch.object(processing.sys if hasattr(processing,"sys") else __import__("sys"),"argv",
                                            ["start_processing.py","--root","/isolated/release","--tenant",tenant]))
            stack.enter_context(patch.object(processing.os,"geteuid",return_value=0))
            stack.enter_context(patch.object(processing.os,"umask"))
            stack.enter_context(patch.object(processing,"run",side_effect=execute))
            stack.enter_context(patch.object(processing,"prepare_worker_override",return_value=(Path("/isolated/release/compose.processing.yaml"),None)))
            started=stack.enter_context(patch.object(processing,"start_worker"))
            stack.enter_context(patch.object(processing,"wait_for_report",return_value=report))
            output=stack.enter_context(patch("builtins.print"))
            with self.assertRaises(SystemExit) as failure:processing.main()
            self.assertIn("PROCESSING_NOT_CONFIRMED",str(failure.exception))
            started.assert_called_once()
            self.assertIn("PROCESSING_STARTED",str(output.call_args_list))
            self.assertNotIn("PROCESSING_READY",str(output.call_args_list))
        self.assertFalse(any(args[:2]==["docker","stop"] for args in calls))

    def test_handoff_requires_explicit_consent_and_a_known_inactive_previous_account(self):
        import start_processing as processing
        old,new=str(uuid4()),str(uuid4())
        rows=[dict(tenant_id=new,state="Activated"),dict(tenant_id=old,state="Suspended")]
        self.assertIsNone(processing.require_worker_handoff(rows,new,new))
        with self.assertRaises(ProcessingFailed):processing.require_worker_handoff(rows,new,old)
        for state in ("Suspended","Uninstalled"):
            rows[1]["state"]=state
            self.assertEqual(processing.require_worker_handoff(rows,new,old,True),old)
        for state in ("Activated","SettingsRequired","unknown"):
            rows[1]["state"]=state
            with self.assertRaises(ProcessingFailed):processing.require_worker_handoff(rows,new,old,True)
        for previous in (None,"not-a-uuid",str(uuid4())):
            with self.assertRaises(ProcessingFailed):processing.require_worker_handoff(rows,new,previous,True)

    def test_incomplete_selected_setup_fails_before_worker_or_override_changes(self):
        import start_processing as processing
        from unittest.mock import patch
        from contextlib import ExitStack
        tenant=str(uuid4())
        complete=dict(tenant_id=tenant,state="Activated",has_rules=True,has_widget=True,has_cutoff=True)
        processing.require_setup([complete],tenant)
        variants=[{**complete,"state":"SettingsRequired"}]+[{**complete,key:False} for key in ("has_rules","has_widget","has_cutoff")]
        for row in variants:
            with self.subTest(row=row),ExitStack() as stack:
                stack.enter_context(patch.object(__import__("sys"),"argv",["start_processing.py","--tenant",tenant,"--replace-inactive-worker"]))
                stack.enter_context(patch.object(processing.os,"geteuid",return_value=0))
                stack.enter_context(patch.object(processing.os,"umask"))
                stack.enter_context(patch.object(processing,"installation_rows",return_value=[row]))
                source=Path(processing.__file__).read_text()
                import re
                schema=re.search(r'if schema!="([0-9]{4})"',source).group(1)
                execute=stack.enter_context(patch.object(processing,"run",return_value=schema))
                change=stack.enter_context(patch.object(processing,"prepare_worker_override"))
                with self.assertRaises(SystemExit) as failure:processing.main()
                self.assertIn("PROCESSING_SETUP_REQUIRED",str(failure.exception))
                change.assert_not_called()
                self.assertEqual(execute.call_count,1)

    def test_exact_old_managed_override_handoff_is_protected_and_backed_up(self):
        import tempfile
        old,new=str(uuid4()),str(uuid4())
        for legacy in (False,True):
            current=worker_override(old)
            if legacy:current=current.replace("start_period: 600s","start_period: 45s")
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/"compose.processing.yaml";path.write_text(current)
                backup=ensure_worker_override(path,new,old)
                self.assertEqual(path.read_text(),worker_override(new))
                self.assertEqual(backup.read_text(),current)
                self.assertEqual(backup.stat().st_mode&0o777,0o600)
                self.assertEqual(path.stat().st_mode&0o777,0o600)
                self.assertIn(".before-tenant-",backup.name)

    def test_handoff_rejects_missing_or_custom_override_without_stopping_worker(self):
        import tempfile
        import start_processing as processing
        from unittest.mock import patch
        old,new=str(uuid4()),str(uuid4())
        rows=[dict(tenant_id=old,state="Uninstalled"),dict(tenant_id=new,state="Activated")]
        for current in (None,worker_override(old).replace("interval: 15s","interval: 17s"),worker_override(new)):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);path=root/"compose.processing.yaml"
                if current is not None:path.write_text(current)
                with patch.object(processing,"run") as execute,patch.object(processing,"locked_worker_handoff") as lock:
                    with self.assertRaises(ProcessingFailed):
                        processing.prepare_worker_override(root,rows,new,old,"old-container",True)
                    execute.assert_not_called();lock.assert_not_called()
                self.assertEqual(path.read_text() if path.exists() else None,current)
                self.assertFalse(list(root.glob("*.before-tenant-*")))

    def test_inactive_handoff_stops_old_process_and_writes_only_while_lifecycle_locked(self):
        import tempfile
        import start_processing as processing
        from unittest.mock import patch
        from contextlib import contextmanager
        old,new=str(uuid4()),str(uuid4())
        rows=[dict(tenant_id=old,state="Suspended"),dict(tenant_id=new,state="Activated")]
        events=[];locked=False
        @contextmanager
        def lock(previous,selected):
            nonlocal locked
            self.assertEqual((previous,selected),(old,new));locked=True;events.append("locked")
            try:yield
            finally:locked=False;events.append("released")
        def stop(args,**kwargs):
            self.assertTrue(locked);self.assertEqual(args,["docker","stop","--timeout","120","old-container"])
            self.assertEqual(kwargs["timeout"],180);events.append("stopped")
        real_backup=processing.backup_worker_override
        def backup(*args):
            self.assertTrue(locked);events.append("backed_up");return real_backup(*args)
        real_write=processing.atomic_write
        def write(*args):
            self.assertTrue(locked);events.append("rewritten");return real_write(*args)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/"compose.processing.yaml";original=worker_override(old);path.write_text(original)
            with patch.object(processing,"locked_worker_handoff",side_effect=lock),patch.object(processing,"run",side_effect=stop),patch.object(processing,"backup_worker_override",side_effect=backup),patch.object(processing,"atomic_write",side_effect=write):
                target,backup=processing.prepare_worker_override(root,rows,new,old,"old-container",True)
            self.assertEqual(events,["locked","backed_up","stopped","rewritten","released"])
            self.assertEqual(target,path);self.assertEqual(backup.read_text(),original)
            self.assertEqual(path.read_text(),worker_override(new))

    def test_changed_handoff_eligibility_never_stops_or_rewrites(self):
        import tempfile
        import start_processing as processing
        from unittest.mock import patch
        old,new=str(uuid4()),str(uuid4())
        rows=[dict(tenant_id=old,state="Suspended"),dict(tenant_id=new,state="Activated")]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/"compose.processing.yaml";original=worker_override(old);path.write_text(original)
            with patch.object(processing,"locked_worker_handoff",side_effect=ProcessingFailed("changed")),patch.object(processing,"run") as execute:
                with self.assertRaises(ProcessingFailed):processing.prepare_worker_override(root,rows,new,old,"old-container",True)
                execute.assert_not_called()
            self.assertEqual(path.read_text(),original);self.assertFalse(list(root.glob("*.before-tenant-*")))

    def test_handoff_subprocess_protocol_holds_then_closes_the_transaction(self):
        import start_processing as processing
        import subprocess
        from unittest.mock import patch
        real_popen=subprocess.Popen;children=[];entered=[]
        script=("import sys\n"
                "for line in sys.stdin:\n"
                " if line.strip()==\"SELECT 'HANDOFF_LOCKED';\": print('HANDOFF_LOCKED',flush=True)\n"
                " if line.strip()=='\\\\q': raise SystemExit(0)\n")
        def launch(command,**kwargs):
            self.assertEqual(command[:4],["docker","exec","-i","bonuspro-release-db-1"])
            self.assertIs(kwargs["stderr"],subprocess.DEVNULL)
            child=real_popen([sys.executable,"-u","-c",script],**kwargs);children.append(child);return child
        with patch.object(processing.subprocess,"Popen",side_effect=launch):
            with processing.locked_worker_handoff(str(uuid4()),str(uuid4())):
                self.assertIsNone(children[0].poll());entered.append(True)
        self.assertEqual(entered,[True]);self.assertEqual(children[0].returncode,0)

    def test_handoff_failed_subprocess_never_enters_mutation_section(self):
        import start_processing as processing
        import subprocess
        from unittest.mock import patch
        real_popen=subprocess.Popen;children=[]
        def launch(command,**kwargs):
            child=real_popen([sys.executable,"-c","raise SystemExit(7)"],**kwargs);children.append(child);return child
        with patch.object(processing.subprocess,"Popen",side_effect=launch):
            with self.assertRaises(ProcessingFailed):
                with processing.locked_worker_handoff(str(uuid4()),str(uuid4())):
                    self.fail("failed lock acquisition must not change the worker")
        self.assertIsNotNone(children[0].returncode);self.assertNotEqual(children[0].returncode,0)
