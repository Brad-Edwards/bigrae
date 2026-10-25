"""Security invariants for the checked-in AWS connector templates."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_ROOT = REPO_ROOT / "platform" / "cloudformation"
RESOURCE_START = re.compile(r"(?m)^  [A-Za-z][A-Za-z0-9]+:\n")


def _resource(text: str, logical_id: str) -> str:
    marker = f"  {logical_id}:\n"
    start = text.index(marker)
    match = RESOURCE_START.search(text, start + len(marker))
    return text[start : match.start() if match else len(text)]


class CloudFormationConnectorTests(unittest.TestCase):
    """Keep dev and prod audit-delivery hardening structurally equivalent."""

    def setUp(self) -> None:
        self.templates = sorted(TEMPLATE_ROOT.glob("*/connectors_aws_cf-*.yml"))
        self.assertEqual(len(self.templates), 2)

    def test_cloudtrail_sns_uses_service_compatible_customer_key(self) -> None:
        for path in self.templates:
            with self.subTest(template=path.name):
                text = path.read_text(encoding="utf-8")
                key = _resource(text, "CloudTrailSNSTopicKMSKey")
                topic = _resource(text, "CloudTrailSNSTopic")
                policy = _resource(text, "CloudTrailSNSTopicPolicy")

                self.assertIn("EnableKeyRotation: true", key)
                self.assertIn("Service: cloudtrail.amazonaws.com", key)
                self.assertIn("- kms:GenerateDataKey*", key)
                self.assertIn("- kms:Decrypt", key)
                self.assertIn("aws:SourceArn:", key)
                self.assertIn("KmsMasterKeyId: !Ref 'CloudTrailSNSTopicKMSKey'", topic)
                self.assertNotIn("alias/aws/sns", topic)
                self.assertIn("aws:SourceArn:", policy)

    def test_versioned_log_bucket_cleanup_removes_every_version(self) -> None:
        for path in self.templates:
            with self.subTest(template=path.name):
                text = path.read_text(encoding="utf-8")
                bucket = _resource(text, "CloudTrailLogsBucket")
                cleanup = _resource(text, "EmptyBucketLambda")

                self.assertIn("VersioningConfiguration:", bucket)
                self.assertIn("NoncurrentVersionExpiration:", bucket)
                self.assertIn("bucket.object_versions.delete()", cleanup)
                self.assertNotIn("bucket.objects.delete()", cleanup)


if __name__ == "__main__":
    unittest.main()
