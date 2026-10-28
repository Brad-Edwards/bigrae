---
id: GEN-004
title: "Authenticated In-App Documentation Portal"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-05-09T05:11:30.106745Z
updated_at: 2026-10-28T00:00:00Z
---

# GEN-004: Authenticated In-App Documentation Portal

## Statement

The platform shall serve authenticated in-app documentation from versioned repository content, with structured navigation, sanitized Markdown rendering, and access controls that prevent exposing deprecated or internal-only paths unless explicitly allowed.

## Rationale

The codebase ships a documentation Django app and a versioned documentation tree. The feature is broader than the existing requirement that major features must be documented.

## Traceability

- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#60` (Portal: Add documentation section)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#463` (Any authenticated user can access internal technical documentation and infrastructure runbooks)
- IMPLEMENTS → CONFIG `shifter/shifter_platform/config/urls.py` (docs/ route mounted in the platform)
- DOCUMENTS → DOCUMENTATION `docs/product-overview.md` (Documentation portal index)
