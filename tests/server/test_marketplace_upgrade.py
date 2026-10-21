"""Guarded main deployment and pool readiness; no production commands are run."""
from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timezone, timedelta
import io,json,ssl,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.error import HTTPError, URLError

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"deploy"))
import finish_marketplace as upgrade

class MarketplaceUpgradeTests(unittest.TestCase):
    def test_schema_proof_strips_only_matching_dump_framing_and_keeps_function_comments(self):
        body=b"-- PostgreSQL database dump\n\nSET search_path = '';\nCREATE FUNCTION f() RETURNS text AS $$\n-- do not remove this\nSELECT '\\restrict inner';\n$$ LANGUAGE sql;\n"
        framed=b"-- PostgreSQL database dump\n\n\\restrict token123\n"+body.split(b"\n\n",1)[1]+b"\n\\unrestrict token123\n"
        self.assertEqual(upgrade.canonical_schema(framed),body+b"\n")
        self.assertIn(b"-- do not remove this",upgrade.canonical_schema(framed))
        self.assertIn(b"\\restrict inner",upgrade.canonical_schema(framed))
        with self.assertRaises(upgrade.MarketplaceFailed):
            upgrade.canonical_schema(b"\\restrict one\nSELECT 1;\n\\unrestrict two\n")

    def test_source_fingerprint_rejects_changed_bridge_sql_before_any_database_command(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory);(source/"migrations/versions").mkdir(parents=True)
            for name in upgrade.SOURCE_BLOBS:
                (source/"migrations/versions"/name).write_bytes((ROOT/"migrations/versions"/name).read_bytes())
            path=source/"migrations/versions/0014_event_sync.sql"
            path.write_bytes(path.read_bytes()+b"\n-- unreviewed change\n")
            with self.assertRaisesRegex(upgrade.MarketplaceFailed,"fingerprint"):
                upgrade.verify_source(source)

    def test_pool_requires_matching_image_and_a_full_startup_allowance(self):
        image="bonuspro:"+"a"*12
        config={"services":{"app":{"image":image},"worker-pool":{"image":image,
            "command":["python","-m","bonuspro.pool"],"healthcheck":{
                "test":["CMD","python","-m","bonuspro.pool","--health"],"start_period":"10m0s"}}}}
        upgrade.check_pool_config(config,image)
        config["services"]["worker-pool"]["healthcheck"]["start_period"]="30s"
        with self.assertRaises(upgrade.MarketplaceFailed):upgrade.check_pool_config(config,image)
        config["services"]["worker-pool"]["healthcheck"]["start_period"]="600s"
        config["services"]["worker-pool"]["image"]="bonuspro:"+"b"*12
        with self.assertRaises(upgrade.MarketplaceFailed):upgrade.check_pool_config(config,image)

    def test_pool_health_cannot_replace_fresh_readiness_for_every_account(self):
        baseline=datetime(2026,10,21,tzinfo=timezone.utc)
        row={"ready":True,"last_processing_at":(baseline+timedelta(seconds=1)).isoformat()}
        report={"accounts":{"synthetic-a":dict(row),"synthetic-b":dict(row)}}
        self.assertTrue(upgrade.pool_confirmed(report,baseline.isoformat()))
        report["accounts"]["synthetic-b"]["last_processing_at"]=baseline.isoformat()
        self.assertFalse(upgrade.pool_confirmed(report,baseline.isoformat()))
        report["accounts"]["synthetic-b"]=dict(row,ready=False)
        self.assertFalse(upgrade.pool_confirmed(report,baseline.isoformat()))
        self.assertFalse(upgrade.pool_confirmed({"accounts":{}},baseline.isoformat()))

    def fixture(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        base=Path(temporary.name);root=base/"release";source=base/"source";backup=base/"backup"
        root.mkdir();source.mkdir();backup.mkdir()
        (root/".env.release").write_text("BONUSPRO_IMAGE=bonuspro:"+"b"*12+"\n")
        (root/"compose.release.yaml").write_text("old managed compose\n")
        (root/"compose.https.yaml").write_text("existing HTTPS configuration\n")
        (source/"compose.release.yaml").write_text("new managed main compose\n")
        state={"schema":"0015","shape":"legacy"}
        db=Mock();db.schema.side_effect=lambda _:state["schema"]
        db.schema_hash.side_effect=lambda _:state["shape"]
        db.owner.return_value="migration_owner"
        image="bonuspro:"+"a"*12
        config={"services":{"app":{"image":image},"worker-pool":{"image":image,
            "command":["python","-m","bonuspro.pool"],"healthcheck":{
                "test":["CMD","python","-m","bonuspro.pool","--health"],"start_period":"600s"}}}}
        def checked(command,**kwargs):
            if "show" in command:return b"old managed compose\n"
            if "config" in command:return json.dumps(config).encode()
            return b""
        patches={
            "Database":Mock(return_value=db),"verify_source":Mock(return_value="0018"),
            "source_revision":Mock(return_value="a"*40),"managed_services":Mock(return_value={
                "app":{"name":"bonuspro-release-app-1","running":True},
                "web":{"name":"bonuspro-release-web-1","running":True},
                "worker":{"name":"bonuspro-release-worker-1","running":True}}),
            "backup_config":Mock(return_value=backup),"checked":Mock(side_effect=checked),
            "expected_schemas":Mock(return_value=nullcontext({"0015":"legacy","0017":"main17","0018":"main18"})),
            "verify_restore":Mock(),"stop_services":Mock(),"restore_old_services":Mock(),
            "advance":Mock(),"verify_local":Mock(),"verify_public":Mock(),"wait_public_ready":Mock(),"start_pool":Mock()
        }
        for name,value in patches.items():
            active=patch.object(upgrade,name,value);active.start();self.addCleanup(active.stop)
        return root,source,backup,db,state,patches

    def test_failed_restore_preflight_never_stops_a_worker_or_changes_configuration(self):
        root,source,backup,db,state,mocks=self.fixture()
        mocks["verify_restore"].side_effect=RuntimeError("synthetic restore failure")
        with self.assertRaisesRegex(upgrade.MarketplaceFailed,"PRECHECK_FAILED"):
            upgrade.finish(root,source)
        mocks["stop_services"].assert_not_called();mocks["advance"].assert_not_called()
        self.assertEqual((root/"compose.release.yaml").read_text(),"old managed compose\n")
        self.assertIn("b"*12,(root/".env.release").read_text())

    def test_failed_final_checkpoint_restores_previous_services_without_schema_downgrade(self):
        root,source,backup,db,state,mocks=self.fixture()
        mocks["verify_restore"].side_effect=[{},RuntimeError("synthetic final restore failure")]
        with self.assertRaisesRegex(upgrade.MarketplaceFailed,"PRECHECK_FAILED"):
            upgrade.finish(root,source)
        mocks["stop_services"].assert_called_once()
        mocks["restore_old_services"].assert_called_once()
        mocks["advance"].assert_not_called()
        self.assertEqual(state["schema"],"0015")

    def test_failure_after_bridge_commit_keeps_main_configuration_and_never_restarts_old_worker(self):
        root,source,backup,db,state,mocks=self.fixture()
        def committed(*args):
            state.update(schema="0017",shape="main17")
            raise RuntimeError("synthetic next migration failure")
        mocks["advance"].side_effect=committed
        with self.assertRaisesRegex(upgrade.MarketplaceFailed,"MARKETPLACE_NOT_CONFIRMED"):
            upgrade.finish(root,source)
        mocks["restore_old_services"].assert_not_called();mocks["start_pool"].assert_not_called()
        self.assertEqual((root/"compose.release.yaml").read_text(),"new managed main compose\n")
        self.assertIn("a"*12,(root/".env.release").read_text())
        receipt=json.loads((root/".marketplace-deployment.json").read_text())
        self.assertEqual(receipt["schema"],"0017")
        self.assertFalse(receipt["business_acceptance_verified"])

    def test_failure_after_possible_exposure_never_rolls_back_an_unchanged_schema_number(self):
        root,source,backup,db,state,mocks=self.fixture()
        state.update(schema="0018",shape="main18")
        mocks["advance"].return_value={"schema":"0018"}
        mocks["verify_public"].side_effect=RuntimeError("sensitive provider body must stay private")
        output=io.StringIO()
        with redirect_stdout(output),self.assertRaisesRegex(upgrade.MarketplaceFailed,"MARKETPLACE_NOT_CONFIRMED"):
            upgrade.finish(root,source)
        diagnostic=next(line for line in output.getvalue().splitlines() if line.startswith("MARKETPLACE_STAGE_FAILED: "))
        self.assertEqual(json.loads(diagnostic.split(": ",1)[1]),{"stage":"public_assets","kind":"unexpected_error"})
        self.assertNotIn("sensitive provider",output.getvalue())
        mocks["restore_old_services"].assert_not_called()
        self.assertEqual((root/"compose.release.yaml").read_text(),"new managed main compose\n")
        self.assertEqual(state["schema"],"0018")


    def test_concurrent_entry_cannot_capture_configuration_or_consult_the_database(self):
        root,source,backup,db,state,mocks=self.fixture()
        with upgrade.deployment_lock(root):
            with self.assertRaisesRegex(upgrade.MarketplaceFailed,"Another marketplace deployment"):
                upgrade.finish(root,source)
        mocks["verify_source"].assert_not_called()
        mocks["Database"].assert_not_called()
        mocks["stop_services"].assert_not_called()
        self.assertIn("b"*12,(root/".env.release").read_text())

    def test_same_schema_local_failure_after_app_replacement_never_reopens_unverified_image(self):
        root,source,backup,db,state,mocks=self.fixture()
        state.update(schema="0018",shape="main18")
        mocks["advance"].return_value={"schema":"0018"}
        mocks["verify_local"].side_effect=RuntimeError("synthetic replacement health failure")
        with self.assertRaisesRegex(upgrade.MarketplaceFailed,"MARKETPLACE_NOT_CONFIRMED"):
            upgrade.finish(root,source)
        mocks["restore_old_services"].assert_not_called()
        mocks["verify_public"].assert_not_called()
        self.assertEqual((root/"compose.release.yaml").read_text(),"new managed main compose\n")
        self.assertIn("a"*12,(root/".env.release").read_text())
        self.assertEqual(json.loads((root/".marketplace-deployment.json").read_text())["schema"],"0018")


    def test_safe_failure_reports_http_status_without_url_or_response_body(self):
        error=HTTPError("https://example.invalid/?token=private",503,"private provider message",{},None)
        details=upgrade.failure_details(error)
        self.assertEqual(details,{"kind":"http_error","http_status":503})
        self.assertNotIn("private",json.dumps(details))


    def test_https_startup_retries_only_temporary_get_failures_before_readiness(self):
        instant=[0.0];requested=[];delays=[]
        temporary_response=io.BytesIO(b"private response body")
        outcomes=[HTTPError("https://example.invalid",503,"private",{},temporary_response),
                  URLError(ConnectionRefusedError("private")),
                  nullcontext(Mock(status=200,read=Mock(return_value=b'{"status":"ready"}')))]
        def open_url(url,**options):
            requested.append((url,options))
            result=outcomes.pop(0)
            if isinstance(result,BaseException):raise result
            return result
        def pause(seconds):delays.append(seconds);instant[0]+=seconds
        with redirect_stdout(io.StringIO()):
            upgrade.wait_public_ready(open_url=open_url,clock=lambda:instant[0],pause=pause)
        self.assertEqual(len(requested),3)
        self.assertTrue(all(url=="https://loyalty.addwise.kz/health/ready" for url,_ in requested))
        self.assertTrue(all(set(options)=={"timeout"} and 0<options["timeout"]<=5 for _,options in requested))
        self.assertEqual(delays,[1,1])
        self.assertTrue(temporary_response.closed)

    def test_https_startup_never_retries_permanent_http_or_certificate_failures(self):
        for error in (HTTPError("https://example.invalid",401,"private",{},None),
                      HTTPError("https://example.invalid",429,"private",{},None),
                      HTTPError("https://example.invalid",500,"private",{},None),
                      URLError(ssl.SSLCertVerificationError("private certificate failure")),
                      URLError("unclassified private network failure")):
            with self.subTest(kind=type(error).__name__,status=getattr(error,"code",None)):
                open_url=Mock(side_effect=error);pause=Mock()
                with self.assertRaises(type(error)) as failure:
                    upgrade.wait_public_ready(open_url=open_url,pause=pause)
                self.assertIs(failure.exception,error)
                self.assertEqual(open_url.call_count,1);pause.assert_not_called()
                if isinstance(error,HTTPError):
                    self.assertFalse(error.fp.closed)
                    error.close()

    def test_https_startup_refuses_unexpected_success_payload_without_retry(self):
        open_url=Mock(return_value=nullcontext(Mock(status=200,read=Mock(return_value=b'{"status":"starting"}'))))
        pause=Mock()
        with self.assertRaises(upgrade.MarketplaceFailed):
            upgrade.wait_public_ready(open_url=open_url,pause=pause)
        self.assertEqual(open_url.call_count,1);pause.assert_not_called()

    def test_https_startup_timeout_is_bounded_and_reports_no_provider_error_text(self):
        instant=[0.0];delays=[]
        open_url=Mock(side_effect=HTTPError("https://example.invalid/?private",502,"private body",{},None))
        def pause(seconds):delays.append(seconds);instant[0]+=seconds
        output=io.StringIO()
        with redirect_stdout(output),self.assertRaises(upgrade.PublicStartupPending) as failure:
            upgrade.wait_public_ready(timeout=3,open_url=open_url,clock=lambda:instant[0],pause=pause)
        self.assertEqual(sum(delays),3)
        self.assertEqual(open_url.call_count,3)
        self.assertNotIn("private",str(failure.exception)+output.getvalue())

    def test_exact_asset_mismatch_is_checked_once_and_keeps_forward_recovery(self):
        root,source,backup,db,state,mocks=self.fixture()
        state.update(schema="0018",shape="main18")
        mocks["advance"].return_value={"schema":"0018"}
        from update_cabinet import UpdateFailed
        mocks["verify_public"].side_effect=UpdateFailed("public assets do not match the image source")
        with redirect_stdout(io.StringIO()),self.assertRaisesRegex(upgrade.MarketplaceFailed,"MARKETPLACE_NOT_CONFIRMED"):
            upgrade.finish(root,source)
        mocks["wait_public_ready"].assert_called_once_with()
        mocks["verify_public"].assert_called_once_with(source)
        mocks["restore_old_services"].assert_not_called()
        self.assertIn("a"*12,(root/".env.release").read_text())

if __name__=="__main__":unittest.main()
