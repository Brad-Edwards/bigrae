---
id: GEN-001
title: "Documentation Required for Major Features"
status: ACTIVE
type: NON_FUNCTIONAL
priority: MUST
created_at: 2026-03-20T21:18:27.032056Z
updated_at: 2026-05-09T05:11:30.348297Z
---

# GEN-001: Documentation Required for Major Features

## Statement

All major platform features shall have accompanying user documentation and technical documentation.

## Rationale

As the platform grows, undocumented features become a liability for onboarding, support, and maintenance. Documentation should be treated as a deliverable alongside code.

## Traceability

- IMPLEMENTS → DOCUMENTATION `docs/ops/artifact-preparation.md` (Operator guide for private packs, adapters, preparation and lifecycle)
- IMPLEMENTS → DOCUMENTATION `docs/architecture/raes-in-tenant-artifact-preparation-design-1583.md` (Preparation architecture, contracts, authority and verification)
- DOCUMENTS → DOCUMENTATION `docs/index.md` (Top-level public documentation index)
- DOCUMENTS → DOCUMENTATION `docs/features/index.md` (Feature documentation index)
- DOCUMENTS → DOCUMENTATION `docs/technical/index.md` (Technical documentation index)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#670` (GEN-001: Documentation Required for Major Features)
- IMPLEMENTS → CONFIG `docs/adr/documentation-coverage.yaml` (Documentation coverage manifest (major feature -> user+technical docs))
- TESTS → TEST `scripts/adr_guard/tests/test_adr_guard.py` (DocumentationCoverageTests (manifest existence/linkage/coverage invariants))
- DOCUMENTS → DOCUMENTATION `docs/features/ctf.md` (CTF feature user documentation)
- DOCUMENTS → DOCUMENTATION `docs/features/ctf-organizer-guide.md` (CTF organizer guide)
- DOCUMENTS → DOCUMENTATION `docs/technical/shifter_platform/ctf.md` (CTF technical documentation)
- IMPLEMENTS → CODE_FILE `scripts/adr_guard/_guard/checks/documentation.py` (documentation-coverage adr_guard check (ADR-022-R1), check_documentation_coverage)
- IMPLEMENTS → DOCUMENTATION `docs/technical/architecture.md` (Canonical current-state architecture and ownership handbook)
- IMPLEMENTS → DOCUMENTATION `docs/technical/handbook/gcp-first-run.md` (GCP installation and synthetic scenario path)
- IMPLEMENTS → DOCUMENTATION `docs/technical/handbook/scenario-contract-transition.md` (Current, transition and target scenario contracts)
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#1531` (Current-state handbook and architecture documentation drift review)
- IMPLEMENTS → CODE_FILE `scripts/quality_ownership/classify_paths.py` (Always-run CI documentation pairing before path routing)
- TESTS → TEST `scripts/adr_guard/tests/test_quality_path_ownership.py` (Source-only handbook enforcement and deletion-aware diff regression)
- IMPLEMENTS → CODE_FILE `scripts/adr_guard/_guard/checks/_documentation_drift.py` (Focused handbook pairing, base associations and manifest validation helpers)
