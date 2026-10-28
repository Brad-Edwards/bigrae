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
from shared.model_access import ContractError
from shared.model_access.control_identity import verify_google_control_assertion


@dataclass(frozen=True)
class GatewayIdentity:
    """Independent workload, without application scopes or administrator rights."""

    is_authenticated: bool = True


class GatewayAuthentication(BaseAuthentication):
    """The configured Google workload email AND immutable subject must match."""

    def authenticate(self, request: Request) -> tuple[GatewayIdentity, None]:
        from google.auth.transport.requests import Request as GoogleTransport

        header = get_authorization_header(request)
        audience = settings.GUACAMOLE_CONTROL_AUDIENCE
        email = settings.GUACAMOLE_CONTROLLER_SERVICE_ACCOUNT_EMAIL
        subject = settings.GUACAMOLE_CONTROLLER_SERVICE_ACCOUNT_ID
        if not header or len(header) > 16_384 or not (audience and email and subject):
            raise AuthenticationFailed("Gateway authentication failed")
        try:
            verify_google_control_assertion(
                header.decode("ascii"),
                audience=audience,
                expected_subject=email,
                expected_subject_id=subject,
                request=partial(GoogleTransport(), timeout=5),
            )
        except (UnicodeError, ContractError):
            raise AuthenticationFailed("Gateway authentication failed") from None
        return GatewayIdentity(), None

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
