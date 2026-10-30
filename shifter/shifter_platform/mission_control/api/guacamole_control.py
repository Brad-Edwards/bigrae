"""Independent gateway control, carrying only opaque bootstrap bindings."""

from dataclasses import dataclass
from functools import partial
from uuid import UUID

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.parsers import JSONParser
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from mission_control.guacamole_authority import renew_gateway_authority
from shared.aws_web_identity import verify_aws_web_identity_token
from shared.model_access import ContractError
from shared.model_access.control_identity import verify_google_control_assertion

_UNAUTHORIZED = "control.unauthorized"


@dataclass(frozen=True)
class GatewayIdentity:
    """Independent workload, without application scopes or administrator rights."""

    is_authenticated: bool = True


class GatewayAuthentication(BaseAuthentication):
    """Exactly one configured native workload identity must match.

    GCP requires the Google workload email AND immutable subject. AWS requires
    an STS web identity token whose role ARN equals the configured gateway role.
    Configuring both modes, or neither, refuses every request.
    """

    def authenticate(self, request: Request) -> tuple[GatewayIdentity, None]:
        """Admit only the configured gateway identity; refuse every other request."""
        header = get_authorization_header(request)
        audience = settings.GUACAMOLE_CONTROL_AUDIENCE
        email = settings.GUACAMOLE_CONTROLLER_SERVICE_ACCOUNT_EMAIL
        subject = settings.GUACAMOLE_CONTROLLER_SERVICE_ACCOUNT_ID
        role = getattr(settings, "GUACAMOLE_CONTROLLER_ROLE_ARN", "")
        google = bool(email or subject)
        if not header or len(header) > 16_384 or not audience or google == bool(role):
            raise AuthenticationFailed("Gateway authentication failed")
        try:
            assertion = header.decode("ascii")
            if role:
                self._verify_aws(assertion, audience, role)
            elif email and subject:
                self._verify_google(assertion, audience, email, subject)
            else:
                raise ContractError(_UNAUTHORIZED)
        except (UnicodeError, ContractError):
            raise AuthenticationFailed("Gateway authentication failed") from None
        return GatewayIdentity(), None

    @staticmethod
    def _verify_google(assertion: str, audience: str, email: str, subject: str) -> None:
        from google.auth.transport.requests import Request as GoogleTransport

        verify_google_control_assertion(
            assertion,
            audience=audience,
            expected_subject=email,
            expected_subject_id=subject,
            request=partial(GoogleTransport(), timeout=5),
        )

    @staticmethod
    def _verify_aws(assertion: str, audience: str, role: str) -> None:
        if not assertion.startswith("Bearer "):
            raise ContractError(_UNAUTHORIZED)
        try:
            verified = verify_aws_web_identity_token(
                assertion[7:],
                issuer=str(getattr(settings, "AWS_SERVICE_TOKEN_ISSUER", "") or ""),
                audience=audience,
                account_id=str(getattr(settings, "AWS_ACCOUNT_ID", "") or ""),
            )
        except ValueError:
            raise ContractError(_UNAUTHORIZED) from None
        if verified != role:
            raise ContractError(_UNAUTHORIZED)

    def authenticate_header(self, request: Request) -> str:
        return 'Bearer realm="guacamole-control"'


class IsGateway(BasePermission):
    """A platform login or administrator cannot stand in for the gateway."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        return isinstance(request.user, GatewayIdentity)


class GuacamoleAuthorityView(APIView):
    """Renew an immutable binding; expose neither secrets nor scope details."""

    authentication_classes = [GatewayAuthentication]
    permission_classes = [IsGateway]
    parser_classes = [JSONParser]

    @extend_schema(exclude=True)
    def post(self, request: Request) -> Response:
        body = request.data
        try:
            if not isinstance(body, dict) or set(body) != {"bootstrap"}:
                raise ValueError
            value = body["bootstrap"]
            if not isinstance(value, str) or len(value) != 36 or str(UUID(value)) != value:
                raise ValueError
        except ValueError:
            return Response({"error": "gateway.invalid_request"}, status=400)
        return Response({"allowed": renew_gateway_authority(value)})
