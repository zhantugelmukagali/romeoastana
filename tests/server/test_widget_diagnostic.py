"""Run the diagnostic's container code against the real REMAP adapter and synthetic HTTP."""
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock,patch
from contextlib import ExitStack,redirect_stdout
from uuid import uuid4

import httpx
import psycopg
from bonuspro.adapters.moysklad import remap
from bonuspro.settings import Settings
from bonuspro.adapters.postgres.installations import Installations
from bonuspro.adapters.postgres.redemptions import Redemptions


ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"deploy"))
spec=importlib.util.spec_from_file_location("widget_diagnostic",ROOT/"deploy/diagnose_widget.py")
diagnostic=importlib.util.module_from_spec(spec);spec.loader.exec_module(diagnostic)

class WidgetDiagnosticTests(unittest.TestCase):
    def child(self,*,prepare=True,existing=False,status=412,body=None,locked=True,
              binding_mode=None,adapter_contract="installed",proof=None):
        tenant,marker=str(uuid4()),str(uuid4());requests=[];sql=[];closed=[]
        row={"id":marker,"name":"БонусПро: операция списания","type":"string","required":False,
             "accountId":tenant,"meta":{"type":"attributemetadata",
                 "href":remap.BASE+"/entity/customerorder/metadata/attributes/"+marker}}
        def respond(request):
            requests.append(request)
            self.assertEqual(request.headers["Authorization"],"Bearer synthetic-private-token")
            if request.method=="GET":
                return httpx.Response(200,json={"meta":{},"rows":[row] if existing else []})
            self.assertEqual(json.loads(request.content),[
                {"name":"БонусПро: операция списания","type":"string","required":False}])
            if body is not None:
                return httpx.Response(status,text=body,headers={"Content-Type":"application/json"})
            return httpx.Response(status,json={"errors":[
                {"code":3000,"error":"synthetic-private-token","moreInfo":"https://private.test/token"},
                {"code":3000},{"code":True},{"code":"synthetic-private-token"},{"code":1000000}]})
        real_client=remap.RemapClient
        class PermanentMetadataError(remap.RemapError):pass
        class Adapter(real_client):
            def marker_attribute(self,**kwargs):
                try:return super().marker_attribute(**kwargs)
                except remap.RemapError:
                    if requests and requests[-1].method=="POST" and status==412:
                        if adapter_contract=="legacy":raise remap.RetryLater(30,"conflict") from None
                        if adapter_contract=="permanent":raise PermanentMetadataError("synthetic-private-token") from None
                    raise
        def factory(token,account,**kwargs):
            self.assertEqual((token,account),("synthetic-private-token",tenant))
            return Adapter(token,account,transport=httpx.MockTransport(respond),**kwargs)
        def execute(statement,params=None):
            sql.append((statement,params));return SimpleNamespace(fetchone=lambda:(locked,))
        guard=SimpleNamespace(execute=execute,close=lambda:closed.append(True))
        config=SimpleNamespace(database_url="synthetic-private-dsn",
            lifecycle_database_url="synthetic-private-lifecycle",credentials=object())
        repo=SimpleNamespace(remap_token=lambda account:"synthetic-private-token",ledger=object())
        bound=marker if binding_mode=="same" else str(uuid4()) if binding_mode=="different" else None
        def binding(account):
            self.assertEqual(account,tenant);return bound
        bindings=SimpleNamespace(binding=binding)
        original_send=httpx.Client.send
        real_response=httpx.Response;self.reconstructed=[]
        def response(*args,**kwargs):
            if "request" in kwargs and "json" in kwargs:
                self.reconstructed.append(kwargs["json"])
            return real_response(*args,**kwargs)
        original_codes=remap._access_error_codes
        original_details=getattr(remap,"_provider_error_details",None)
        def details(response,deadline=None):
            codes=(original_details(response,deadline)[0] if callable(original_details)
                   else original_codes(response,deadline))
            return codes,proof
        output=io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys,"argv",["-",tenant,"prepare" if prepare else "read_only"]))
            stack.enter_context(patch.object(Settings,"from_env",return_value=config))
            stack.enter_context(patch.object(sys.modules[Installations.__module__],"Installations",return_value=repo))
            stack.enter_context(patch.object(sys.modules[Redemptions.__module__],"Redemptions",return_value=bindings))
            stack.enter_context(patch.object(remap,"RemapClient",side_effect=factory))
            if proof is not None:
                stack.enter_context(patch.object(remap,"_provider_error_details",side_effect=details,create=True))
            stack.enter_context(patch.object(httpx,"Response",side_effect=response))
            stack.enter_context(patch.object(psycopg,"connect",return_value=guard))
            stack.enter_context(redirect_stdout(output))
            with self.assertRaises(SystemExit) as completed:
                exec(compile(diagnostic.CHILD_CODE,"<widget-diagnostic>","exec"),{"__name__":"__main__"})
        self.assertIs(httpx.Client.send,original_send)
        self.assertEqual(closed,[True])
        self.assertEqual(sql,[("SET statement_timeout='5s'",None),
            ("SELECT pg_try_advisory_lock(hashtextextended(%s,0))",("widget/provision/"+tenant,))])
        text=output.getvalue()
        for private in (tenant,marker,"synthetic-private-token","synthetic-private-dsn",
                        "synthetic-private-lifecycle","private.test"):
            self.assertNotIn(private,text)
        return json.loads(text),requests,completed.exception.code

    def test_legacy_prepare_records_real_get_and_post_412_numeric_codes_without_secrets_or_binding(self):
        report,requests,code=self.child(adapter_contract="legacy")
        self.assertEqual(report,{"read_only":False,"requests":[
            {"phase":"metadata_read","http":200,"codes":[]},
            {"phase":"metadata_create","http":412,"codes":[3000]}],"result":"transient_error"})
        self.assertEqual(code,1)
        self.assertEqual([r.method for r in requests],["GET","POST"])

    def test_existing_field_is_validated_without_post_even_in_prepare_mode(self):
        report,requests,code=self.child(existing=True)
        self.assertEqual(report["result"],"marker_valid")
        self.assertEqual(code,0)
        self.assertEqual([r.method for r in requests],["GET"])
        self.assertEqual(report["requests"],[{"phase":"metadata_read","http":200,"codes":[]}])

    def test_default_read_only_does_not_create_a_missing_field(self):
        report,requests,code=self.child(prepare=False)
        self.assertTrue(report["read_only"])
        self.assertEqual(report["result"],"metadata_error")
        self.assertEqual(code,1)
        self.assertEqual([r.method for r in requests],["GET"])

    def test_oversized_denial_body_keeps_http_status_but_no_codes_or_payload(self):
        body='{"errors":[{"code":3000}],"private":"'+("x"*65536)+'"}'
        report,requests,code=self.child(body=body,adapter_contract="legacy")
        self.assertEqual(code,1)
        self.assertEqual(report["requests"][-1],{"phase":"metadata_create","http":412,"codes":[]})
        self.assertEqual(report["result"],"transient_error")
        self.assertEqual(len(requests),2)

    def test_permanent_metadata_exception_in_updated_image_preserves_actual_http_codes(self):
        report,requests,code=self.child(adapter_contract="permanent")
        self.assertEqual(report["result"],"metadata_error")
        self.assertEqual(report["requests"][-1],{"phase":"metadata_create","http":412,"codes":[3000]})
        self.assertEqual((len(requests),code),(2,1))

    def test_prepare_never_recreates_a_deleted_previously_bound_field(self):
        report,requests,code=self.child(binding_mode="same")
        self.assertEqual(report["result"],"metadata_error")
        self.assertEqual([r.method for r in requests],["GET"])
        self.assertEqual(code,1)

    def test_existing_different_marker_cannot_replace_the_immutable_binding(self):
        report,requests,code=self.child(existing=True,binding_mode="different")
        self.assertEqual(report["result"],"binding_mismatch")
        self.assertEqual([r.method for r in requests],["GET"])
        self.assertEqual(code,1)

    def test_reconstructed_mixed_1065_keeps_unknown_error_evidence(self):
        body='{"errors":[{"code":1065},{}]}'
        report,_,code=self.child(body=body)
        self.assertEqual(report["requests"][-1],{"phase":"metadata_create","http":412,"codes":[1065]})
        self.assertEqual(self.reconstructed,[{"errors":[{"code":1065},{}]}])
        self.assertEqual(code,1)

    def test_available_exclusive_1065_proof_survives_safe_reconstruction(self):
        body='{"errors":[{"code":1065}]}'
        report,_,code=self.child(body=body,proof=True)
        self.assertEqual(report["requests"][-1]["codes"],[1065])
        self.assertEqual(self.reconstructed,[{"errors":[{"code":1065}]}])
        self.assertEqual(code,1)

    def test_busy_provisioning_lock_never_sends_a_provider_request(self):
        report,requests,code=self.child(locked=False)
        self.assertEqual((report["result"],report["requests"],requests,code),("setup_in_progress",[],[],1))

    def test_ambiguous_account_selection_stops_before_container_execution(self):
        rows=[{"tenant_id":str(uuid4()),"state":state} for state in ("Activated","SettingsRequired")]
        runner=Mock()
        with patch.object(diagnostic,"installation_rows",return_value=rows):
            with self.assertRaises(diagnostic.ProcessingFailed):diagnostic.diagnose(runner=runner)
        runner.assert_not_called()

    def test_host_sends_complete_code_as_stdin_without_shell_or_user_heredoc(self):
        tenant=str(uuid4())
        report={"read_only":False,"requests":[{"phase":"metadata_create","http":412,"codes":[3000]}],
                "result":"transient_error"}
        runner=Mock(return_value=SimpleNamespace(returncode=1,stdout=json.dumps(report),
                                                stderr="synthetic-private-token"))
        with patch.object(diagnostic,"installation_rows",return_value=[
                {"tenant_id":tenant,"state":"SettingsRequired"}]):
            result=diagnostic.diagnose(prepare=True,runner=runner)
        self.assertEqual(result,report)
        args,options=runner.call_args
        self.assertEqual(args[0],["docker","exec","-i","bonuspro-release-app-1","python","-",tenant,"prepare"])
        self.assertEqual(options["input"],diagnostic.CHILD_CODE)
        self.assertTrue(options["text"]);self.assertTrue(options["capture_output"])
        self.assertEqual(options["timeout"],75)
        self.assertNotIn("shell",options)

    def test_host_reconstructs_only_finite_allowed_output_and_rejects_writes_in_read_only_mode(self):
        report={"read_only":True,"requests":[{"phase":"metadata_read","http":403,"codes":[1070,1070],
                                             "token":"synthetic-private-token"}],
                "result":"access_denied","body":"synthetic-private-token"}
        safe=diagnostic.safe_report(json.dumps(report),prepare=False)
        self.assertEqual(safe["requests"],[{"phase":"metadata_read","http":403,"codes":[1070]}])
        self.assertNotIn("synthetic-private-token",json.dumps(safe))
        report["requests"][0]["phase"]="metadata_create"
        with self.assertRaises(diagnostic.DiagnosticFailed):
            diagnostic.safe_report(json.dumps(report),prepare=False)

if __name__=="__main__":unittest.main()
