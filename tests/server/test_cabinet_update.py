import importlib.util,os,tempfile,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location("cabinet_update",ROOT/"deploy/update_cabinet.py")
update=importlib.util.module_from_spec(spec);spec.loader.exec_module(update)

class CabinetUpdateTests(unittest.TestCase):
    def test_environment_replacement_requires_one_immutable_image(self):
        for text in ("PUBLIC_ORIGIN=https://example.test\n","BONUSPRO_IMAGE=old\nBONUSPRO_IMAGE=duplicate\n"):
            with self.assertRaises(ValueError):update.replacement(text,"bonuspro:"+"a"*12)
        with self.assertRaises(ValueError):update.replacement("BONUSPRO_IMAGE=old\n","bonuspro:latest")
        result=update.replacement("PUBLIC_ORIGIN=https://example.test\nBONUSPRO_IMAGE=bonuspro:old\n","bonuspro:"+"b"*12)
        self.assertIn("PUBLIC_ORIGIN=https://example.test",result)
        self.assertEqual(result.count("BONUSPRO_IMAGE="),1)
    def test_public_verification_failure_restores_previous_environment_and_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);env=root/".env.release";old="BONUSPRO_IMAGE=bonuspro:"+"a"*12+"\nPUBLIC_ORIGIN=https://example.test\n";env.write_text(old);env.chmod(0o600)
            calls=[]
            def execute(args,**kwargs):calls.append(args)
            def fail(source):raise RuntimeError("synthetic static mismatch")
            with patch.object(update,"run",execute),self.assertRaises(update.UpdateFailed):
                update.switch(root,root,"bonuspro:"+"b"*12,verify=fail)
            self.assertEqual(env.read_text(),old)
            self.assertEqual(sum("up" in args for args in calls),2)
            backups=list(root.glob(".env.release.before-cabinet-*"))
            self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_text(),old)
            self.assertEqual(env.stat().st_mode&0o777,0o600)
            self.assertFalse(any("migrate" in args for args in calls))
    def test_success_keeps_new_image_and_config_failure_never_restarts_app(self):
        for failed in (False,True):
            with self.subTest(failed=failed),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);env=root/".env.release";old="BONUSPRO_IMAGE=bonuspro:"+"a"*12+"\n";env.write_text(old);env.chmod(0o600);calls=[]
                def execute(args,**kwargs):
                    calls.append(args)
                    if failed:raise RuntimeError("invalid compose")
                with patch.object(update,"run",execute):
                    if failed:
                        with self.assertRaises(update.UpdateFailed):update.switch(root,root,"bonuspro:"+"b"*12,verify=lambda _:None)
                    else:update.switch(root,root,"bonuspro:"+"b"*12,verify=lambda _:None)
                self.assertEqual(sum("up" in args for args in calls),0 if failed else 1)
                self.assertEqual(env.read_text(),old if failed else "BONUSPRO_IMAGE=bonuspro:"+"b"*12+"\n")


    def test_revision_reads_work_when_deploying_another_owners_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            subprocess.run(["git","init","--quiet",str(root)],check=True)
            subprocess.run(["git","-C",str(root),"-c","user.name=CI","-c","user.email=ci@example.test",
                            "commit","--quiet","--allow-empty","-m","isolated fixture"],check=True)
            with patch.dict(os.environ,{"GIT_TEST_ASSUME_DIFFERENT_OWNER":"1"}):
                revision=update.source_revision(root)
            self.assertRegex(revision,r"^[0-9a-f]{40}$")

if __name__=="__main__":unittest.main()
