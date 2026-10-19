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

if __name__=="__main__":unittest.main()
