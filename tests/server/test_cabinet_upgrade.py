"""Deployment orchestration checks; all services and SQL are isolated fakes."""
import importlib.util,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location("guarded_upgrade",ROOT/"deploy/update_cabinet.py")
update=importlib.util.module_from_spec(spec);spec.loader.exec_module(update)

class SchemaUpgradeTests(unittest.TestCase):
    def exercise(self,*,local_failure=False,public_failure=False,preflight_failure=False,
                 rollback_blocked=False,ambiguous_commit=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);env=root/".env.release"
            old="BONUSPRO_IMAGE=bonuspro:"+"a"*12+"\n";env.write_text(old);env.chmod(0o600)
            backup=root/"protected";backup.mkdir(mode=0o700)
            schema={"value":"0013"};audit={"rows":0};events=[]
            initial={name:True for name in update.QUIET_SERVICES}
            current=initial.copy()
            def execute(args,**kwargs):
                if "up" in args:
                    events.append("new_app");current["bonuspro-release-app-1"]=True
                elif args[:2]==["docker","stop"]:
                    events.append("stop_app");current[args[-1]]=False
                elif args[:2]==["docker","start"]:
                    events.append("web_exposure");current[args[-1]]=True
            def stop(states):
                events.append("stop_all")
                for name in current:current[name]=False
            def dumped(path):
                events.append(path.name);path.write_bytes(b"protected synthetic dump")
            def preflight(*args):
                events.append("isolated_restore")
                if preflight_failure:raise RuntimeError("isolated migration failed")
            def apply(database,source):
                events.append("migration");schema["value"]="0014"
                if ambiguous_commit:raise RuntimeError("connection lost after commit")
            def local(source):
                events.append("local_verify")
                if local_failure:
                    if rollback_blocked:audit["rows"]=1
                    raise RuntimeError("new application is unhealthy")
            def rollback():
                events.append("ddl_rollback")
                if audit["rows"]:raise update.UpdateFailed("history prevents rollback")
                schema["value"]="0013"
            def public(source):
                events.append("public_verify")
                if public_failure:raise RuntimeError("public verification failed")
            def restore(*args):
                events.append("restore_previous")
                current.update(initial)
            with ExitStack() as stack:
                replacements={"run":execute,"service_states":lambda:current.copy(),
                    "protected_backup":lambda _:backup,"dump_database":dumped,
                    "verify_restore":preflight,"stop_for_upgrade":stop,"database_schema":lambda:schema["value"],
                    "apply_rebinding_migration":apply,"verify_local":local,
                    "rollback_rebinding_migration":rollback,"restore_previous_services":restore}
                for name,value in replacements.items():stack.enter_context(patch.object(update,name,value))
                error=None
                try:update.upgrade_schema_and_switch(root,root,"bonuspro:"+"b"*12,verify=public)
                except update.UpdateFailed as failure:error=str(failure)
            return dict(events=events,error=error,schema=schema["value"],env=env.read_text(),old=old,
                        states=current,backups=[p.name for p in backup.iterdir()])

    def test_isolated_restore_precedes_downtime_and_new_app_is_checked_before_https(self):
        result=self.exercise();events=result["events"]
        self.assertIsNone(result["error"])
        self.assertLess(events.index("isolated_restore"),events.index("stop_all"))
        self.assertLess(events.index("stop_all"),events.index("database.pre-upgrade.dump"))
        self.assertLess(events.index("database.pre-upgrade.dump"),events.index("migration"))
        self.assertLess(events.index("migration"),events.index("new_app"))
        self.assertLess(events.index("local_verify"),events.index("web_exposure"))
        self.assertEqual(result["schema"],"0014")
        self.assertIn("database.dump",result["backups"])
        self.assertIn("database.pre-upgrade.dump",result["backups"])
        # The old worker cannot run against a new schema before start_processing replaces it.
        self.assertFalse(result["states"]["bonuspro-release-worker-1"])

    def test_failed_preflight_leaves_live_services_and_configuration_untouched(self):
        result=self.exercise(preflight_failure=True)
        self.assertIn("UPDATE_PRECHECK_FAILED",result["error"])
        self.assertEqual(result["schema"],"0013");self.assertEqual(result["env"],result["old"])
        self.assertNotIn("stop_all",result["events"])
        self.assertNotIn("migration",result["events"])

    def test_pretraffic_failure_restores_old_schema_configuration_and_services(self):
        for ambiguous in (False,True):
            result=self.exercise(local_failure=not ambiguous,ambiguous_commit=ambiguous)
            self.assertIn("UPDATE_FAILED:",result["error"])
            self.assertEqual(result["schema"],"0013");self.assertEqual(result["env"],result["old"])
            self.assertIn("ddl_rollback",result["events"])
            self.assertIn("restore_previous",result["events"])
            self.assertNotIn("web_exposure",result["events"])

    def test_audit_history_blocks_ddl_rollback_and_old_service_restart(self):
        result=self.exercise(local_failure=True,rollback_blocked=True)
        self.assertIn("UPDATE_ROLLBACK_BLOCKED",result["error"])
        self.assertEqual(result["schema"],"0014");self.assertNotEqual(result["env"],result["old"])
        self.assertNotIn("restore_previous",result["events"])
        self.assertFalse(any(result["states"].values()))

    def test_any_public_exposure_forbids_automatic_schema_downgrade(self):
        result=self.exercise(public_failure=True)
        self.assertIn("UPDATE_NOT_CONFIRMED",result["error"])
        self.assertEqual(result["schema"],"0014");self.assertNotEqual(result["env"],result["old"])
        self.assertNotIn("ddl_rollback",result["events"])
        self.assertNotIn("restore_previous",result["events"])

    def test_rollback_audit_check_runs_before_force_rls_owner_role_and_drops_no_history_cascade(self):
        captured=[]
        with patch.object(update,"migration_owner",return_value='"bonuspro_migrator"'),\
             patch.object(update,"database_sql",side_effect=lambda database,sql,**kw:captured.append(sql)):
            update.rollback_rebinding_migration()
        sql=captured[0]
        self.assertLess(sql.index("IF EXISTS(SELECT 1 FROM bonuspro.order_customer_rebindings)"),
                        sql.index("SET LOCAL ROLE"))
        self.assertLess(sql.index("LOCK TABLE bonuspro.order_customer_rebindings"),sql.index("IF EXISTS"))
        self.assertIn("EXECUTE FUNCTION bonuspro.reject_mutation()",sql)
        self.assertIn("DROP TRIGGER reservation_order_binding",sql)
        self.assertIn("DROP COLUMN binding_revision",sql)
        self.assertNotIn("CASCADE",sql)
        self.assertIn("version_num='0013'",sql)
        self.assertTrue(sql.startswith("BEGIN;"));self.assertTrue(sql.endswith("COMMIT;\n"))

    def test_migration_owner_must_be_nonprivileged_and_exact_sql_is_transactional(self):
        with patch.object(update,"database_sql",return_value='{"role":"postgres","safe":false}'):
            with self.assertRaises(update.UpdateFailed):update.migration_owner("bonuspro")
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory);path=source/"migrations/versions/0014_order_rebinding.sql"
            path.parent.mkdir(parents=True);path.write_text("SELECT 'exact static migration';")
            captured=[]
            with patch.object(update,"migration_owner",return_value='"bonuspro_migrator"'),\
                 patch.object(update,"database_sql",side_effect=lambda database,sql,**kw:captured.append(sql)):
                update.apply_rebinding_migration("bonuspro",source)
            sql=captured[0]
            self.assertIn(path.read_text(),sql)
            self.assertLess(sql.index("SET LOCAL ROLE"),sql.index(path.read_text()))
            self.assertIn("version_num='0014'",sql)
            self.assertIn("Unexpected schema",sql)
            self.assertTrue(sql.startswith("BEGIN;"));self.assertTrue(sql.endswith("COMMIT;\n"))
