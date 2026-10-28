---
id: CTF-1401
title: "Plugin System"
status: ACTIVE
type: NON_FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T05:28:24.343589Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-1401: Plugin System

## Statement

The platform could support extensibility for CTF functionality via Django's app architecture. Custom challenge types, flag validators, and scoring models shall be implementable as Django apps that register with the CTF layer's extension points. The CTF layer shall not build a standalone plugin lifecycle (install/enable/disable/uninstall), Django app registration and settings are sufficient.

## Rationale

Extensibility allows the platform to support custom challenge types (for example, dynamic challenges, container-based challenges) and custom scoring logic without modifying core code. Django's app architecture already provides the registration and discovery patterns needed. A CTFd-style plugin system would be redundant given Django's existing capabilities.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/extensions.py` (Extension points: register_flag_validator and register_scoring_strategy for external Django apps)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#638` (CTF-1401: Plugin System)
