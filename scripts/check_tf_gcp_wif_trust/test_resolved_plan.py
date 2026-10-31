"""Behavioral policy checks on the resolved Terraform apply surface."""

import copy
import unittest

from .check_tf_gcp_wif_trust import check_resolved_plan


def fixture():
    subject = "repo:example/product:environment:new-deployment"
    condition = (
        "assertion.repository == 'example/product' && assertion.repository_id == '123' && "
        "assertion.repository_owner_id == '456' && assertion.event_name == 'workflow_dispatch' && "
        "((assertion.sub == 'repo:example/product:environment:new-deployment' && "
        "assertion.ref == 'refs/heads/main' && "
        "assertion.workflow_ref == 'example/product/.github/workflows/deploy.yml@refs/heads/main'))"
    )
    account = "projects/example-foundation/serviceAccounts/new-deploy@example-foundation.iam.gserviceaccount.com"
    expected = {
        "attribute_condition": condition,
        "project_id": "example-foundation",
        "project_number": "789",
        "name_prefix": "new",
        "purpose_subjects": {"deploy": [subject]},
        "service_accounts": {"deploy": account},
    }
    resources = [
        ("google_iam_workload_identity_pool_provider", "github", {
            "project": "example-foundation", "workload_identity_pool_id": "new-github",
            "workload_identity_pool_provider_id": "github", "attribute_condition": condition,
            "attribute_mapping": {"google.subject": "assertion.sub"},
            "oidc": [{"issuer_uri": "https://token.actions.githubusercontent.com", "allowed_audiences": []}],
        }),
        ("google_service_account_iam_member", "deploy_wif", {
            "service_account_id": account, "role": "roles/iam.workloadIdentityUser",
            "member": f"principal://iam.googleapis.com/projects/789/locations/global/workloadIdentityPools/new-github/subject/{subject}",
        }),
    ]
    return {"resource_changes": [
        {"address": f"module.cicd_oidc_identity.{kind}.{name}", "type": kind,
         "change": {"actions": ["create"], "after": after, "after_unknown": {}}}
        for kind, name, after in resources
    ]}, expected


class ResolvedPlanTests(unittest.TestCase):
    def test_empty_nested_unknown_masks_are_known(self):
        plan, expected = fixture()
        plan["resource_changes"][0]["change"]["after_unknown"] = {"oidc": [{}], "attribute_mapping": {}}
        self.assertEqual(check_resolved_plan(plan, expected), [])

    def test_exact_plan_accepts_unseen_environment(self):
        plan, expected = fixture()
        self.assertEqual(check_resolved_plan(plan, expected), [])

    def test_condition_bypass_is_rejected(self):
        plan, expected = fixture()
        plan["resource_changes"][0]["change"]["after"]["attribute_condition"] = "true || " + expected["attribute_condition"]
        self.assertTrue(check_resolved_plan(plan, expected))

    def test_wrong_repository_ref_workflow_subject_or_issuer_is_rejected(self):
        for before, after in [
            ("example/product", "attacker/product"), ("refs/heads/main", "refs/heads/feature"),
            ("deploy.yml", "evil.yml"), ("environment:new-deployment", "environment:another"),
            ("repository_id == '123'", "repository_id == '999'"),
        ]:
            with self.subTest(after=after):
                plan, expected = fixture()
                plan["resource_changes"][0]["change"]["after"]["attribute_condition"] = expected["attribute_condition"].replace(before, after)
                self.assertTrue(check_resolved_plan(plan, expected))
        plan, expected = fixture()
        plan["resource_changes"][0]["change"]["after"]["oidc"][0]["issuer_uri"] = "https://attacker.example"
        self.assertTrue(check_resolved_plan(plan, expected))

    def test_cross_purpose_or_repository_wide_impersonation_is_rejected(self):
        for key, value in [
            ("member", "principalSet://iam.googleapis.com/pool/attribute.repository/example/product"),
            ("service_account_id", "projects/example/serviceAccounts/another@example.iam.gserviceaccount.com"),
            ("role", "roles/iam.serviceAccountTokenCreator"),
        ]:
            plan, expected = fixture()
            plan["resource_changes"][1]["change"]["after"][key] = value
            self.assertTrue(check_resolved_plan(plan, expected))

    def test_missing_duplicate_unknown_and_destructive_plans_fail_closed(self):
        for mutation in ("missing", "duplicate", "unknown", "replace"):
            plan, expected = fixture()
            if mutation == "missing":
                plan["resource_changes"].pop()
            elif mutation == "duplicate":
                plan["resource_changes"].append(copy.deepcopy(plan["resource_changes"][0]))
            elif mutation == "unknown":
                plan["resource_changes"][1]["change"]["after_unknown"]["member"] = True
            else:
                plan["resource_changes"][0]["change"]["actions"] = ["delete", "create"]
            with self.subTest(mutation=mutation):
                self.assertTrue(check_resolved_plan(plan, expected))

    def test_only_an_unauthorized_service_account_may_be_deleted(self):
        """#2679: a foundation that builds no images removes its builder identity."""
        plan, expected = fixture()

        def account_change(email, actions):
            return {
                "address": "module.cicd_oidc_identity.google_service_account.x",
                "type": "google_service_account",
                "change": {"actions": actions, "before": {
                    "name": f"projects/example-foundation/serviceAccounts/{email}",
                    "email": email, "project": "example-foundation",
                }, "after": None if actions == ["delete"] else {}, "after_unknown": {}},
            }

        builder = "new-packer@example-foundation.iam.gserviceaccount.com"
        deploy = "new-deploy@example-foundation.iam.gserviceaccount.com"
        removal = copy.deepcopy(plan)
        removal["resource_changes"].append(account_change(builder, ["delete"]))
        self.assertEqual(check_resolved_plan(removal, expected), [])
        for email, actions in ((deploy, ["delete"]), (builder, ["delete", "create"])):
            with self.subTest(email=email, actions=actions):
                changed = copy.deepcopy(plan)
                changed["resource_changes"].append(account_change(email, actions))
                self.assertIn("Migration would delete or replace an identity foundation", check_resolved_plan(changed, expected))


def impersonation_fixture():
    """A deployment without GitHub federation: named operators only (ADR-070)."""
    account = "projects/example-foundation/serviceAccounts/new-deploy@example-foundation.iam.gserviceaccount.com"
    expected = {
        "attribute_condition": "",
        "project_id": "example-foundation",
        "project_number": "789",
        "name_prefix": "new",
        "purpose_subjects": {},
        "service_accounts": {"deploy": account},
        "impersonation": {"deploy": ["group:operators@example.com"]},
    }
    plan = {"resource_changes": [{
        "address": "module.cicd_oidc_identity.google_service_account_iam_member.impersonation",
        "type": "google_service_account_iam_member",
        "change": {"actions": ["create"], "after_unknown": {}, "after": {
            "service_account_id": account, "role": "roles/iam.serviceAccountTokenCreator",
            "member": "group:operators@example.com",
        }},
    }]}
    return plan, expected


class ImpersonationPlanTests(unittest.TestCase):
    def test_named_principals_without_federation_are_accepted(self):
        plan, expected = impersonation_fixture()
        self.assertEqual(check_resolved_plan(plan, expected), [])

    def test_unnamed_or_cross_purpose_grants_are_rejected(self):
        for key, value in [
            ("member", "group:everyone@example.com"),
            ("member", "allUsers"),
            ("service_account_id", "projects/example/serviceAccounts/new-destroy@example.iam.gserviceaccount.com"),
            ("role", "roles/iam.serviceAccountUser"),
        ]:
            with self.subTest(key=key, value=value):
                plan, expected = impersonation_fixture()
                plan["resource_changes"][0]["change"]["after"][key] = value
                self.assertTrue(check_resolved_plan(plan, expected))

    def test_missing_grant_and_unexpected_provider_are_rejected(self):
        plan, expected = impersonation_fixture()
        plan["resource_changes"].append(copy.deepcopy(plan["resource_changes"][0]))
        plan["resource_changes"][1]["change"]["after"]["member"] = "user:owner@example.com"
        self.assertTrue(check_resolved_plan(plan, expected))
        plan, expected = impersonation_fixture()
        expected["impersonation"]["deploy"].append("user:owner@example.com")
        self.assertTrue(check_resolved_plan(plan, expected))
        federated_plan, _ = fixture()
        plan, expected = impersonation_fixture()
        plan["resource_changes"].append(federated_plan["resource_changes"][0])
        self.assertIn(
            "A deployment without federated subjects must contain no WIF provider",
            check_resolved_plan(plan, expected),
        )


if __name__ == "__main__":
    unittest.main()
