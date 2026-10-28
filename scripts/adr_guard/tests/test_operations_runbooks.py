"""Structural and command-safety contract for the operations runbook catalog."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNBOOK_ROOT = REPO_ROOT / "docs" / "runbooks"
REQUIRED_SECTIONS = ("Symptom", "Investigation", "Resolution", "Prevention")
RUNBOOK_RE = re.compile(r"rb(?P<number>\d{3})-[a-z0-9-]+\.md")
SHELL_FENCE_RE = re.compile(
    r"(?ms)^[ \t]{0,3}```(?:bash|sh)\n(?P<body>.*?)^[ \t]{0,3}```$"
)
PROHIBITED_SHELL = (
    re.compile(r"\bkubectl\s+exec\b"),
    re.compile(r"\bkubectl\s+.*\bsecret(?:s)?\b.*(?:-o|--output)\b"),
    re.compile(
        r"\bkubectl\b[^\n]*\b(?:apply|create|delete|drain|edit|patch|replace|scale|set|taint|cordon|uncordon)\b"
    ),
    re.compile(r"\bkubectl\b[^\n]*\brollout\s+restart\b"),
    re.compile(r"\bgcloud\s+secrets\s+versions\s+access\b"),
    re.compile(
        r"\bgcloud\b[^\n]*\b(?:add-iam-policy-binding|create|delete|patch|remove-iam-policy-binding|reset|rm|set-iam-policy|start|stop|update)\b"
    ),
    re.compile(r"\baws\b[^\n]*\b(?:create|delete|modify|put|reboot|run|start|stop|terminate|update)\b"),
    re.compile(r"\bterraform\s+(?:apply|destroy|force-unlock)\b"),
    re.compile(r"\bhelm\s+(?:install|upgrade|uninstall)\b"),
)


class OperationsRunbookContractTests(unittest.TestCase):
    """Keep the stable incident catalog complete, discoverable, and safe."""

    def setUp(self) -> None:
        self.index = (RUNBOOK_ROOT / "index.md").read_text(encoding="utf-8")
        self.runbooks = sorted(
            path for path in RUNBOOK_ROOT.glob("rb*.md") if RUNBOOK_RE.fullmatch(path.name)
        )

    def test_catalog_has_all_stable_ids_once(self) -> None:
        expected = [f"{number:03d}" for number in range(1, 24)]
        actual = [RUNBOOK_RE.fullmatch(path.name)["number"] for path in self.runbooks]
        self.assertEqual(expected, actual)

    def test_every_runbook_is_indexed_and_has_required_sections(self) -> None:
        for path in self.runbooks:
            with self.subTest(runbook=path.name):
                text = path.read_text(encoding="utf-8")
                number = RUNBOOK_RE.fullmatch(path.name)["number"]
                self.assertTrue(text.startswith(f"# RB{number}: "))
                self.assertIn(f"({path.name})", self.index)
                positions = [text.find(f"## {section}\n") for section in REQUIRED_SECTIONS]
                self.assertTrue(all(position >= 0 for position in positions), positions)
                self.assertEqual(sorted(positions), positions)
                self.assertIn("Escalate", text)

    def test_shell_examples_are_explicitly_read_only(self) -> None:
        for path in self.runbooks:
            text = path.read_text(encoding="utf-8")
            for match in SHELL_FENCE_RE.finditer(text):
                with self.subTest(runbook=path.name, command=match.group("body")):
                    preceding = text[: match.start()].rstrip().splitlines()
                    self.assertTrue(preceding and preceding[-1].strip() == "Read-only command:")
                    for prohibited in PROHIBITED_SHELL:
                        self.assertIsNone(prohibited.search(match.group("body")))

    def test_mutating_command_classes_are_rejected(self) -> None:
        examples = (
            'kubectl -n "$NAMESPACE" apply -f workload.yaml',
            'kubectl -n "$NAMESPACE" rollout restart deploy/portal-web',
            'gcloud projects add-iam-policy-binding "$PROJECT_ID"',
            'aws ec2 stop-instances --instance-ids "$INSTANCE_ID"',
            'terraform apply saved.tfplan',
            'helm upgrade shifter ./chart',
        )
        for command in examples:
            with self.subTest(command=command):
                self.assertTrue(any(pattern.search(command) for pattern in PROHIBITED_SHELL))


if __name__ == "__main__":
    unittest.main()
