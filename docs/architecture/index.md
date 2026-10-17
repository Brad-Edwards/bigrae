# Architecture records

Read the [current-state architecture handbook](../technical/architecture.md)
for the runtime, ownership and support model. It is published through the
single MkDocs site and linked from the root README.

This directory contains issue-specific design/review evidence and historical
implementation records. Files named `*preflight*.md` are historical preflights;
their visible banners apply even when reached through a direct link. They record
constraints at the time of an issue, not proof of current implementation,
activation, support or live qualification. Original evidence and paths remain
available for traceability.

Accepted ADRs and durable design notes can also live here. They retain their
stated authority unless superseded; the historical preflight classification
does not retire every document in this directory. Consult the
[ADR registry](../adr/index.yaml), implementation and current handbook together.

## Find implementation history

Use the issue number or subject to locate a record without treating it as
installation guidance:

```shell
rg --files docs/architecture | rg 'preflight|review'
```

REV1 and later adoption reviews are dated assessments. Their evidence limits and
superseding decisions matter. Do not infer a live release claim from a proposed
control or an earlier passing test. This directory remains excluded from the
public MkDocs site; exclusion is not a confidentiality boundary.

## Durable design notes

These retain their stated design authority and are not historical preflights:

- [Artifact preparation](raes-in-tenant-artifact-preparation-design-1583.md)
- [RAES hard cut](raes-migration-adr.md)
- [External runtime boundary](external-scenario-runtime-design.md)
