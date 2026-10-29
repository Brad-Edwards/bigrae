"""Cross-domain named model-access selector composition."""

from __future__ import annotations

from uuid import uuid4

from shared.model_access import (
    SelectorKind,
    SelectorResolution,
    SharingSelector,
    compute_digest,
)


def _atom_resolution(selector, *, authority_owner, members):
    digest = compute_digest(selector)
    authority_ref = {"owner": authority_owner, "reference": f"scope:{selector.kind.value}"}
    return SelectorResolution(
        contract_version="model-access-selector-resolution/v1",
        selector_digest=digest,
        assessment_count=len(members),
        member_refs=tuple({"owner": "deployment", "reference": member} for member in members),
        selector_authority_refs=(authority_ref,),
        subject_authorities=tuple(
            {
                "subject_ref": {"owner": "deployment", "reference": member},
                "authority_ref": authority_ref,
            }
            for member in members
        ),
        publisher_requirements=({"selector_digest": digest, "authority_ref": authority_ref, "scope": "selector"},),
    )


def test_named_collection_unions_members_and_retains_every_atomic_publisher_requirement():
    from config.model_access_sharing import _combine

    event_selector = SharingSelector(kind=SelectorKind.CTF_EVENT, ids=(str(uuid4()),))
    user_selector = SharingSelector(kind=SelectorKind.USER, ids=("7",))
    named = SharingSelector(
        kind=SelectorKind.NAMED_COLLECTION,
        members=(event_selector, user_selector),
    )

    # Pure closed-contract combination does not resolve owner data or publish.
    resolution = _combine(
        named,
        (
            _atom_resolution(event_selector, authority_owner="ctf", members=("range:shared", "range:event")),
            _atom_resolution(user_selector, authority_owner="management", members=("range:shared", "range:user")),
        ),
        preserve_atomic_digests=True,
    )

    assert resolution.selector_digest == compute_digest(named)
    assert {item.reference for item in resolution.member_refs} == {
        "range:shared",
        "range:event",
        "range:user",
    }
    assert {item.selector_digest for item in resolution.publisher_requirements} == {
        compute_digest(event_selector),
        compute_digest(user_selector),
    }
    assert {item.owner for item in resolution.selector_authority_refs} == {"ctf", "management"}
