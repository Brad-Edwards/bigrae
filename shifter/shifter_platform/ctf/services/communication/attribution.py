"""Non-authoritative attribution derived only from an admitted native actor."""

from dataclasses import dataclass
from uuid import UUID

from ctf.services.actors import event_actor_user_id
from ctf.services.communication.admission import AdmissionActor
from shared.api_tokens.models import ApiToken


@dataclass(frozen=True, slots=True)
class CommunicationAttribution:
    """Stable principal identity with optional retained human/token projections."""

    principal_uuid: UUID | None
    user_id: int | None
    token_id: int | None


def actor_attribution(actor: AdmissionActor) -> CommunicationAttribution:
    """Never interpret a service's responsible contact as the acting human."""
    if actor.credential is None:
        return CommunicationAttribution(None, actor.user_id, actor.token_id)
    credential = actor.credential
    token_id = actor.token_id
    if credential.kind == "personal":
        token_id = ApiToken.objects.values_list("pk", flat=True).get(credential_uuid=credential.credential_uuid)
    return CommunicationAttribution(credential.principal.uuid, event_actor_user_id(credential), token_id)
