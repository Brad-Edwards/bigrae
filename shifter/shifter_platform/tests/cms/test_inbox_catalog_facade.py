"""The installation catalog facade delegates to the in-box registration (#2679)."""

from cms.services import register_inbox_catalog


def test_facade_registers_the_inbox_packs_as_the_given_actor(monkeypatch):
    seen = []
    monkeypatch.setattr("cms.scenarios.inbox.register_inbox_packs", lambda *, actor: seen.append(actor) or ["pack"])
    assert register_inbox_catalog(actor="credential") == ["pack"]
    assert seen == ["credential"]
