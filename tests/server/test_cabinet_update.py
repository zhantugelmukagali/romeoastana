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


    def test_untracked_source_is_rejected_without_deleting_user_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            subprocess.run(["git","init","--quiet",str(root)],check=True)
            subprocess.run(["git","-C",str(root),"-c","user.name=CI","-c","user.email=ci@example.test",
                            "commit","--quiet","--allow-empty","-m","isolated fixture"],check=True)
            pending=root/"backend/unreviewed.py";pending.parent.mkdir();pending.write_text("UNREVIEWED = True\n")
            with self.assertRaises(update.UpdateFailed):
                update.source_revision(root)
            self.assertTrue(pending.exists())


    def invoke_standalone(self,schema,*,defer=False,worker_failure=False,lineage=True,database_lineage=True):
        from contextlib import ExitStack
        from uuid import uuid4
        image="bonuspro:"+"b"*12;tenant=str(uuid4());calls=[];phases=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            arguments=["update_cabinet.py","--root",str(root),"--tenant",tenant]
            if defer:arguments.append("--defer-processing")
            def execute(args,**kwargs):
                calls.append((args,kwargs))
                if len(args)>1 and str(args[1]).endswith("/start_processing.py"):
                    phases.append("worker")
                    if worker_failure:raise subprocess.CalledProcessError(1,args)
                if args[:3]==["docker","inspect","bonuspro-release-worker-1"]:
                    return image+"|true|healthy"
            with ExitStack() as stack:
                stack.enter_context(patch.object(update.sys,"argv",arguments))
                stack.enter_context(patch.object(update.os,"geteuid",return_value=0))
                stack.enter_context(patch.object(update.os,"umask"))
                stack.enter_context(patch.object(update.Path,"read_text",return_value='revision != [("0016",)]'))
                stack.enter_context(patch.object(update.Path,"is_file",return_value=lineage))
                stack.enter_context(patch.object(update,"database_schema",return_value=schema))
                stack.enter_context(patch.object(update,"compatible_database_lineage",return_value=database_lineage))
                stack.enter_context(patch.object(update,"source_revision",return_value="b"*40))
                stack.enter_context(patch.object(update,"run",side_effect=execute))
                stack.enter_context(patch.object(update,"upgrade_schema_and_switch",side_effect=lambda *a:phases.append("upgrade")))
                stack.enter_context(patch.object(update,"switch",side_effect=lambda *a:phases.append("switch")))
                rollback=stack.enter_context(patch.object(update,"rollback_compatible_migrations"))
                restore=stack.enter_context(patch.object(update,"restore_previous_services"))
                output=stack.enter_context(patch("builtins.print"))
                error=None
                try:update.main()
                except SystemExit as failure:error=str(failure)
                rollback.assert_not_called();restore.assert_not_called()
                messages=" ".join(str(call) for call in output.call_args_list)
            return dict(phases=phases,calls=calls,error=error,messages=messages,tenant=tenant,image=image)

    def test_standalone_update_always_confirms_matching_worker_after_app_update(self):
        for schema in ("0013","0014","0015","0016"):
            with self.subTest(schema=schema):
                result=self.invoke_standalone(schema)
                self.assertIsNone(result["error"])
                self.assertEqual(result["phases"],["switch" if schema=="0016" else "upgrade","worker"])
                command,options=next((a,k) for a,k in result["calls"] if len(a)>1 and str(a[1]).endswith("/start_processing.py"))
                self.assertEqual(command[-2:],["--tenant",result["tenant"]])
                self.assertEqual(options["timeout"],900)
                self.assertIn("CABINET_READY",result["messages"])

    def test_failed_processing_confirmation_keeps_updated_app_and_never_claims_cabinet_ready(self):
        for schema in ("0013","0014","0015","0016"):
            with self.subTest(schema=schema):
                result=self.invoke_standalone(schema,worker_failure=True)
                self.assertIn("UPDATE_INCOMPLETE",result["error"])
                self.assertEqual(result["phases"],["switch" if schema=="0016" else "upgrade","worker"])
                self.assertNotIn("CABINET_READY",result["messages"])

    def test_incompatible_numeric_schema_lineage_is_rejected_before_build_or_switch(self):
        result=self.invoke_standalone("0016",lineage=False)
        self.assertIn("Compatible migration lineage",result["error"])
        self.assertEqual(result["phases"],[])
        self.assertEqual(result["calls"],[])

    def test_main_numeric_schema_collision_is_rejected_before_build_or_switch(self):
        result=self.invoke_standalone("0016",database_lineage=False)
        self.assertIn("Database schema lineage",result["error"])
        self.assertEqual(result["phases"],[])
        self.assertEqual(result["calls"],[])

    def test_internal_defer_is_explicit_and_cannot_claim_full_success(self):
        result=self.invoke_standalone("0014",defer=True)
        self.assertEqual(result["phases"],["upgrade"])
        self.assertIn("CABINET_UPDATED_PROCESSING_PENDING",result["messages"])
        self.assertNotIn("CABINET_READY",result["messages"])

    def test_zero_exit_from_child_does_not_accept_stopped_old_or_unhealthy_worker(self):
        image="bonuspro:"+"b"*12
        for state in ("bonuspro:"+"a"*12+"|true|healthy",image+"|false|healthy",image+"|true|unhealthy"):
            with self.subTest(state=state),patch.object(update,"run",side_effect=[None,state]) as execute:
                with self.assertRaises(update.UpdateFailed):
                    update.start_updated_processing(ROOT,ROOT,image)
                command=execute.call_args_list[0].args[0]
                self.assertNotIn("--tenant",command)
                self.assertEqual(execute.call_args_list[0].kwargs["timeout"],900)

    def test_tenant_selector_is_canonical_and_rejects_invalid_selection(self):
        from uuid import uuid4
        tenant=str(uuid4())
        self.assertEqual(update.tenant_selector(tenant.upper()),tenant)
        with self.assertRaises(update.argparse.ArgumentTypeError):update.tenant_selector("invalid account")

if __name__=="__main__":unittest.main()
