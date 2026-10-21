import importlib.util,json,subprocess,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location("finish_release",ROOT/"deploy/finish_release.py")
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)

class DeliveryTests(unittest.TestCase):
    def test_failed_upgrade_never_starts_processing_or_provider_probe(self):
        calls=[]
        def run(args,**kwargs):
            calls.append(args);raise subprocess.CalledProcessError(1,args)
        with self.assertRaises(subprocess.CalledProcessError):
            release.finish(ROOT,ROOT,str(uuid4()),runner=run)
        self.assertEqual(len(calls),1)
    def test_failed_read_only_check_keeps_a_protected_report_and_never_claims_ready(self):
        calls=[]
        report=dict(read_only=True,business_acceptance_verified=False,
                    ready_for_live_acceptance=False,failures=[dict(stage="widget",code="widget_not_configured")])
        def run(args,**kwargs):
            calls.append(args)
            if len(calls)==1:self.assertIn("--defer-processing",args)
            if len(calls)==2:self.assertEqual(kwargs["timeout"],900)
            return SimpleNamespace(returncode=1 if args[0]=="docker" else 0,stdout=json.dumps(report))
        with tempfile.TemporaryDirectory() as directory,patch("builtins.print") as output:
            root=Path(directory)
            self.assertFalse(release.finish(root,ROOT,str(uuid4()),runner=run))
            files=list((root/".release-reports").glob("*.json"))
            self.assertEqual(len(files),1)
            self.assertEqual(json.loads(files[0].read_text()),report)
            self.assertEqual(files[0].stat().st_mode&0o777,0o600)
            self.assertNotIn("READY_FOR_ACCEPTANCE",str(output.call_args_list))
        self.assertEqual(len(calls),3)
    def test_a_forged_acceptance_claim_or_malformed_report_cannot_pass(self):
        for data in ("not JSON",json.dumps(dict(read_only=True,business_acceptance_verified=True,
                                                ready_for_live_acceptance=True))):
            def run(args,**kwargs):return SimpleNamespace(returncode=0,stdout=data)
            with tempfile.TemporaryDirectory() as directory,patch("builtins.print"):
                with self.assertRaises(release.DeliveryFailed):
                    release.finish(Path(directory),ROOT,str(uuid4()),runner=run)
                self.assertFalse((Path(directory)/".release-reports").exists())


    def test_failed_worker_confirmation_stops_delivery_before_probe_or_acceptance_claim(self):
        calls=[]
        def execute(args,**kwargs):
            calls.append(args)
            if len(calls)==2:raise subprocess.CalledProcessError(1,args)
            return SimpleNamespace(returncode=0,stdout="")
        with tempfile.TemporaryDirectory() as directory,patch("builtins.print") as output:
            with self.assertRaises(subprocess.CalledProcessError):
                release.finish(Path(directory),ROOT,str(uuid4()),runner=execute)
            self.assertNotIn("READY_FOR_ACCEPTANCE",str(output.call_args_list))
            self.assertFalse((Path(directory)/".release-reports").exists())
        self.assertEqual(len(calls),2)
        self.assertIn("--defer-processing",calls[0])
        self.assertTrue(str(calls[1][1]).endswith("start_processing.py"))

    def test_single_enabled_account_is_resolved_once_before_update_and_passed_to_all_stages(self):
        old,new=str(uuid4()),str(uuid4());calls=[]
        report=dict(read_only=True,business_acceptance_verified=False,ready_for_live_acceptance=True)
        def execute(args,**kwargs):
            calls.append(args);return SimpleNamespace(returncode=0,stdout=json.dumps(report))
        rows=[dict(tenant_id=old,state="Uninstalled"),dict(tenant_id=new,state="Activated")]
        with tempfile.TemporaryDirectory() as directory,patch.object(release,"installation_rows",return_value=rows) as select,patch("builtins.print"):
            self.assertTrue(release.finish(Path(directory),ROOT,runner=execute,replace_inactive_worker=True))
            select.assert_called_once()
        self.assertEqual(len(calls),3)
        for args in calls:
            self.assertEqual(args[args.index("--tenant")+1],new);self.assertNotIn(old,args)
        self.assertNotIn("--replace-inactive-worker",calls[0])
        self.assertIn("--replace-inactive-worker",calls[1])
        self.assertNotIn("--replace-inactive-worker",calls[2])

    def test_ambiguous_or_missing_account_stops_before_public_update(self):
        rows=[dict(tenant_id=str(uuid4()),state=state) for state in ("Activated","SettingsRequired")]
        for installations in ([],rows):
            with patch.object(release,"installation_rows",return_value=installations),patch("builtins.print"):
                execute=__import__("unittest.mock",fromlist=["Mock"]).Mock()
                with self.assertRaises(release.DeliveryFailed):release.finish(ROOT,ROOT,runner=execute)
                execute.assert_not_called()

    def test_prepare_only_updates_shared_application_without_account_resolution_or_processing(self):
        calls=[]
        def execute(args,**kwargs):
            calls.append(args);self.assertTrue(kwargs["check"])
            return SimpleNamespace(returncode=0,stdout="")
        with tempfile.TemporaryDirectory() as directory,patch("builtins.print") as output,patch.object(release,"resolve_release_tenant") as resolve,patch.object(release,"installation_rows") as rows:
            root=Path(directory)
            self.assertTrue(release.finish(root,ROOT,runner=execute,prepare_only=True,replace_inactive_worker=True))
            resolve.assert_not_called();rows.assert_not_called()
            self.assertFalse((root/".release-reports").exists())
            self.assertIn("APPLICATION_UPDATED_SETUP_PENDING",str(output.call_args_list))
            for forbidden in ("READY_FOR_ACCEPTANCE","CABINET_READY","PROCESSING_READY"):
                self.assertNotIn(forbidden,str(output.call_args_list))
        self.assertEqual(len(calls),1)
        self.assertTrue(str(calls[0][1]).endswith("deploy/update_cabinet.py"))
        self.assertIn("--defer-processing",calls[0])
        self.assertNotIn("--tenant",calls[0]);self.assertNotIn("--replace-inactive-worker",calls[0])

    def test_prepare_only_validates_optional_account_without_database_selection(self):
        tenant=str(uuid4());calls=[]
        def execute(args,**kwargs):
            calls.append(args);return SimpleNamespace(returncode=0,stdout="")
        with patch("builtins.print"),patch.object(release,"resolve_release_tenant") as resolve,patch.object(release,"installation_rows") as rows:
            self.assertTrue(release.finish(ROOT,ROOT,tenant.upper(),runner=execute,prepare_only=True))
            resolve.assert_not_called();rows.assert_not_called()
            with self.assertRaises(ValueError):release.finish(ROOT,ROOT,"not-a-uuid",runner=execute,prepare_only=True)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][calls[0].index("--tenant")+1],tenant)

    def test_failed_preparation_propagates_and_never_claims_prepared_or_ready(self):
        calls=[]
        def execute(args,**kwargs):
            calls.append(args);raise subprocess.CalledProcessError(1,args)
        with tempfile.TemporaryDirectory() as directory,patch("builtins.print") as output,patch.object(release,"resolve_release_tenant") as resolve:
            with self.assertRaises(subprocess.CalledProcessError):
                release.finish(Path(directory),ROOT,runner=execute,prepare_only=True)
            resolve.assert_not_called()
            self.assertFalse((Path(directory)/".release-reports").exists())
            for forbidden in ("APPLICATION_UPDATED_SETUP_PENDING","READY_FOR_ACCEPTANCE","CABINET_READY"):
                self.assertNotIn(forbidden,str(output.call_args_list))
        self.assertEqual(len(calls),1)

    def test_prepare_only_cli_returns_zero_after_successful_preparation(self):
        with patch.object(release.sys,"argv",["finish_release.py","--prepare-only"]),patch.object(release.os,"geteuid",return_value=0),patch.object(release.os,"umask"),patch.object(release,"finish",return_value=True) as finish:
            with self.assertRaises(SystemExit) as exit:release.main()
        self.assertEqual(exit.exception.code,0)
        self.assertTrue(finish.call_args.kwargs["prepare_only"])
        self.assertIsNone(finish.call_args.args[2])

if __name__=="__main__":unittest.main()
