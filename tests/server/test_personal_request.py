"""Browser benefit requests cannot forge a plan or silently use unpinned settings."""
import unittest
from uuid import uuid4
from pydantic import ValidationError
from bonuspro.api.app import RedemptionRequest


class BenefitRequestTests(unittest.TestCase):
    def body(self,**patch):
        return dict(points=10,request_id=str(uuid4()),source_hash="a"*64,**patch)

    def test_existing_positive_bonus_request_is_unchanged(self):
        value=RedemptionRequest.model_validate(self.body())
        self.assertFalse(value.apply_personal)
        self.assertIsNone(value.expected_policy_version)

    def test_zero_points_requires_explicit_personal_request_and_both_versions(self):
        original={**self.body(),"points":0}
        for patch in ({},{"apply_personal":True},
                      {"apply_personal":True,"expected_policy_version":1}):
            with self.assertRaises(ValidationError):
                RedemptionRequest.model_validate({**original,**patch})
        value=RedemptionRequest.model_validate({**original,"apply_personal":True,
            "expected_policy_version":1,"expected_assignment_version":2})
        self.assertEqual(value.points,0)
        self.assertTrue(value.apply_personal)

    def test_personal_request_types_and_plan_fields_are_strict(self):
        original={**self.body(),"apply_personal":True,
                  "expected_policy_version":1,"expected_assignment_version":1}
        for patch in ({"apply_personal":"true"},{"expected_policy_version":True},
                      {"expected_assignment_version":"1"},{"personal_discount_minor":100},
                      {"wallet_id":str(uuid4())},{"tenant_id":str(uuid4())}):
            with self.subTest(patch=patch),self.assertRaises(ValidationError):
                RedemptionRequest.model_validate({**original,**patch})


if __name__=="__main__":
    unittest.main()
