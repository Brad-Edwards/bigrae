"""S8 account administration HTTP adapter mounted in production routing."""

from __future__ import annotations

from uuid import UUID

from drf_spectacular.utils import extend_schema
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from shared.api.errors import api_error_response
from shared.api.permissions import IsAuthenticatedSessionOrApiToken
from shared.api.principals import authenticated_credential
from shared.api.strict_json import ClosedJSONParser
from shared.audit import request_audit
from shared.authorization import AuthorizationProviderBindingError, configured_authorization_provider
from shared.identity_scope import PrincipalRef
from shared.principal_port import PrincipalResolutionError
from workspaces import services
from workspaces.api.serializers import WorkspaceQuotaResourceSerializer, WorkspaceSerializer

from .account_admin_serializers import (
    AccountCommandSerializer,
    AccountMemberCommandSerializer,
    AccountMemberPageSerializer,
    AccountMemberViewSerializer,
    AccountPageQuerySerializer,
    AccountPageSerializer,
    AccountSearchQuerySerializer,
    AccountViewSerializer,
    EmptyAccountCommandSerializer,
    HierarchyPendingReceiptSerializer,
    IntegrationOrganizationQuerySerializer,
    OrganizationPageSerializer,
    OrganizationViewSerializer,
    SubdivisionCommandSerializer,
    SubdivisionCreationCommandSerializer,
    WorkspaceEgressPolicyCommandSerializer,
    WorkspacePageQuerySerializer,
    WorkspacePageSerializer,
    WorkspaceQuotaPolicyCommandSerializer,
    WorkspaceViewSerializer,
)

__all__ = [
    "AccountCollectionView",
    "AccountCommandSerializer",
    "AccountCurrentView",
    "AccountDetailView",
    "AccountMemberCollectionView",
    "AccountMemberCommandSerializer",
    "AccountMemberDetailView",
    "AccountMemberPageSerializer",
    "AccountMemberViewSerializer",
    "AccountOrganizationCollectionView",
    "AccountOrganizationDetailView",
    "AccountPageQuerySerializer",
    "AccountPageSerializer",
    "AccountSearchQuerySerializer",
    "AccountViewSerializer",
    "AccountWorkspaceArchiveView",
    "AccountWorkspaceCollectionView",
    "AccountWorkspaceDetailView",
    "AccountWorkspaceEgressPolicyView",
    "AccountWorkspaceQuotaPolicyView",
    "AccountWorkspaceRenameView",
    "AccountWorkspaceRestoreView",
    "EmptyAccountCommandSerializer",
    "HierarchyPendingReceiptSerializer",
    "IntegrationOrganizationCollectionView",
    "IntegrationOrganizationQuerySerializer",
    "OrganizationPageSerializer",
    "OrganizationViewSerializer",
    "SubdivisionCommandSerializer",
    "SubdivisionCreationCommandSerializer",
    "WorkspaceEgressPolicyCommandSerializer",
    "WorkspacePageQuerySerializer",
    "WorkspacePageSerializer",
    "WorkspaceQuotaPolicyCommandSerializer",
    "WorkspaceViewSerializer",
]


class _AccountAPIView(APIView):
    """Apply canonical credential admission, closed parsing, and safe errors."""

    permission_classes = [IsAuthenticatedSessionOrApiToken]
    parser_classes = [ClosedJSONParser, FormParser, MultiPartParser]

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, services.HierarchyPublicationPending):
            return Response(
                HierarchyPendingReceiptSerializer(
                    {
                        "uuid": exc.object_uuid,
                        "publication_receipt": exc.publication_receipt,
                        "publication_state": "pending",
                    }
                ).data,
                status=202,
            )
        if isinstance(exc, (services.AccountScopeError, PrincipalResolutionError, ValueError)):
            code, message, status_code = "account_access_denied", "Account access denied", 403
        elif isinstance(exc, AuthorizationProviderBindingError):
            code, message, status_code = "authorization_unavailable", "Authorization unavailable", 503
        elif isinstance(
            exc,
            (
                services.WorkspaceQuotaError,
                services.WorkspaceLifecycleError,
                services.WorkspaceMembershipError,
                services.WorkspaceInvitationError,
            ),
        ):
            code, message = exc.code, exc.message
            status_code = 400 if exc.code.endswith("_invalid") else 403
        elif isinstance(exc, services.OrganizationValidationError):
            code, message, status_code = "invalid_profile", "Invalid organization profile", 400
        else:
            return super().handle_exception(exc)
        return api_error_response(code=code, message=message, status_code=status_code, request=self.request)


class IntegrationOrganizationCollectionView(_AccountAPIView):
    """Discover organizations authorized for one integration administration action."""

    @extend_schema(
        parameters=[IntegrationOrganizationQuerySerializer],
        responses=OrganizationPageSerializer,
        operation_id="prepared_integration_organizations_list",
    )
    def get(self, request: Request) -> Response:
        query = IntegrationOrganizationQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        page = services.admin_list_integration_organizations(
            authenticated_credential(request), configured_authorization_provider(), **query.validated_data
        )
        return Response(OrganizationPageSerializer(page).data)


class AccountCollectionView(_AccountAPIView):
    """List authorized accounts or create one under installation authority."""

    @extend_schema(
        parameters=[AccountSearchQuerySerializer],
        responses=AccountPageSerializer,
        operation_id="prepared_accounts_list",
    )
    def get(self, request: Request) -> Response:
        query = AccountSearchQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        page = services.admin_list_accounts(
            authenticated_credential(request), configured_authorization_provider(), **query.validated_data
        )
        return Response(AccountPageSerializer(page).data)

    @extend_schema(
        request=AccountCommandSerializer, responses={201: AccountViewSerializer, 202: HierarchyPendingReceiptSerializer}
    )
    def post(self, request: Request) -> Response:
        command = AccountCommandSerializer(data=request.data)
        command.is_valid(raise_exception=True)
        values = command.validated_data
        owner_uuid: UUID | None = values.get("individual_principal_uuid")
        actor = authenticated_credential(request)
        account = services.admin_create_account(
            actor,
            configured_authorization_provider(),
            kind=values["kind"],
            name=values["name"],
            owner=PrincipalRef(owner_uuid, "human") if owner_uuid else None,
            publication_receipt=values.get("publication_receipt"),
            audit=request_audit(request, credential=actor),
        )
        return Response(
            AccountViewSerializer({"uuid": account.uuid, "kind": account.kind, "name": values["name"]}).data,
            status=201,
        )


class AccountCurrentView(_AccountAPIView):
    """Resolve an individual's own account with no subdivision selector."""

    @extend_schema(responses=AccountViewSerializer)
    def get(self, request: Request) -> Response:
        account = services.admin_individual_account(
            authenticated_credential(request), configured_authorization_provider()
        )
        return Response(AccountViewSerializer(account).data)


class AccountDetailView(_AccountAPIView):
    """Read an exact account after its own policy decision."""

    @extend_schema(responses=AccountViewSerializer)
    def get(self, request: Request, account_uuid: UUID) -> Response:
        item = services.admin_get_account(
            authenticated_credential(request), configured_authorization_provider(), account_uuid
        )
        return Response(AccountViewSerializer(item).data)


class AccountOrganizationCollectionView(_AccountAPIView):
    """Create a real organization under an authorized business account."""

    @extend_schema(
        parameters=[AccountPageQuerySerializer],
        responses=OrganizationPageSerializer,
        operation_id="prepared_organizations_list",
    )
    def get(self, request: Request, account_uuid: UUID) -> Response:
        query = AccountPageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        page = services.admin_list_organizations(
            authenticated_credential(request), configured_authorization_provider(), account_uuid, **query.validated_data
        )
        return Response(OrganizationPageSerializer(page).data)

    @extend_schema(
        request=SubdivisionCreationCommandSerializer,
        responses={201: OrganizationViewSerializer, 202: HierarchyPendingReceiptSerializer},
    )
    def post(self, request: Request, account_uuid: UUID) -> Response:
        command = SubdivisionCreationCommandSerializer(data=request.data)
        command.is_valid(raise_exception=True)
        name = command.validated_data["name"]
        actor = authenticated_credential(request)
        organization = services.admin_create_organization(
            actor,
            configured_authorization_provider(),
            account_uuid,
            name=name,
            publication_receipt=command.validated_data.get("publication_receipt"),
            audit=request_audit(request, credential=actor),
        )
        return Response(
            OrganizationViewSerializer(
                {"uuid": organization.uuid, "name": name, "is_default": organization.is_default}
            ).data,
            status=201,
        )


class AccountWorkspaceCollectionView(_AccountAPIView):
    """Create a workspace only beneath its authorized account and organization."""

    @extend_schema(
        parameters=[WorkspacePageQuerySerializer],
        responses=WorkspacePageSerializer,
        operation_id="prepared_workspaces_list",
    )
    def get(self, request: Request, account_uuid: UUID, organization_uuid: UUID) -> Response:
        query = WorkspacePageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        page = services.admin_list_workspaces(
            authenticated_credential(request),
            configured_authorization_provider(),
            account_uuid,
            organization_uuid,
            offset=query.validated_data["offset"],
            limit=query.validated_data["limit"],
            filters=services.WorkspaceAdminFilters(
                include_archived=query.validated_data["include_archived"],
                search=query.validated_data["search"],
            ),
        )
        return Response(WorkspacePageSerializer(page).data)

    @extend_schema(
        request=SubdivisionCreationCommandSerializer,
        responses={201: WorkspaceViewSerializer, 202: HierarchyPendingReceiptSerializer},
    )
    def post(self, request: Request, account_uuid: UUID, organization_uuid: UUID) -> Response:
        command = SubdivisionCreationCommandSerializer(data=request.data)
        command.is_valid(raise_exception=True)
        name = command.validated_data["name"]
        actor = authenticated_credential(request)
        workspace = services.admin_create_workspace(
            actor,
            configured_authorization_provider(),
            account_uuid,
            organization_uuid,
            name=name,
            publication_receipt=command.validated_data.get("publication_receipt"),
            audit=request_audit(request, credential=actor),
        )
        return Response(
            WorkspaceViewSerializer(
                {
                    "uuid": workspace.uuid,
                    "name": name,
                    "is_default": workspace.is_default,
                    "archived_at": None,
                    "owner_principal_uuids": [],
                    "egress_policy": "status-quo",
                }
            ).data,
            status=201,
        )


class AccountOrganizationDetailView(_AccountAPIView):
    """Read an exact organization beneath its persisted account."""

    @extend_schema(responses=OrganizationViewSerializer)
    def get(self, request: Request, account_uuid: UUID, organization_uuid: UUID) -> Response:
        item = services.admin_get_organization(
            authenticated_credential(request), configured_authorization_provider(), account_uuid, organization_uuid
        )
        return Response(OrganizationViewSerializer(item).data)


class AccountWorkspaceDetailView(_AccountAPIView):
    """Read an exact workspace beneath its persisted organization and account."""

    @extend_schema(parameters=[WorkspacePageQuerySerializer], responses=WorkspaceViewSerializer)
    def get(self, request: Request, account_uuid: UUID, organization_uuid: UUID, workspace_uuid: UUID) -> Response:
        query = WorkspacePageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        item = services.admin_get_workspace(
            authenticated_credential(request),
            configured_authorization_provider(),
            account_uuid,
            organization_uuid,
            workspace_uuid,
            include_archived=query.validated_data["include_archived"],
        )
        return Response(WorkspaceViewSerializer(item).data)


class AccountMemberCollectionView(_AccountAPIView):
    """Record an explicit principal membership after exact account authority."""

    @extend_schema(parameters=[AccountPageQuerySerializer], responses=AccountMemberPageSerializer)
    def get(self, request: Request, account_uuid: UUID) -> Response:
        query = AccountPageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        page = services.admin_list_account_members(
            authenticated_credential(request), configured_authorization_provider(), account_uuid, **query.validated_data
        )
        return Response(AccountMemberPageSerializer(page).data)

    @extend_schema(request=AccountMemberCommandSerializer, responses={204: None})
    def post(self, request: Request, account_uuid: UUID) -> Response:
        command = AccountMemberCommandSerializer(data=request.data)
        command.is_valid(raise_exception=True)
        actor = authenticated_credential(request)
        services.admin_add_account_member(
            actor,
            configured_authorization_provider(),
            account_uuid,
            PrincipalRef(command.validated_data["principal_uuid"], command.validated_data["principal_kind"]),
            audit=request_audit(request, credential=actor),
        )
        return Response(status=204)


class AccountMemberDetailView(_AccountAPIView):
    """Remove one explicit account membership fact."""

    @extend_schema(responses={204: None})
    def delete(self, request: Request, account_uuid: UUID, principal_uuid: UUID) -> Response:
        actor = authenticated_credential(request)
        services.admin_remove_account_member(
            actor,
            configured_authorization_provider(),
            account_uuid,
            principal_uuid,
            audit=request_audit(request, credential=actor),
        )
        return Response(status=204)


class AccountWorkspaceQuotaPolicyView(_AccountAPIView):
    """Policy-authorized quota authoring for human and service administrators."""

    @extend_schema(request=WorkspaceQuotaPolicyCommandSerializer, responses=WorkspaceQuotaResourceSerializer)
    def put(self, request: Request, workspace_uuid: UUID) -> Response:
        command = WorkspaceQuotaPolicyCommandSerializer(data=request.data)
        command.is_valid(raise_exception=True)
        actor = authenticated_credential(request)
        item = services.admin_set_workspace_quota_policy(
            actor,
            configured_authorization_provider(),
            workspace_uuid,
            **command.validated_data,
            audit=request_audit(request, credential=actor),
        )
        return Response(WorkspaceQuotaResourceSerializer(item).data)


class AccountWorkspaceEgressPolicyView(_AccountAPIView):
    """Policy-authorized egress authoring for human and service administrators."""

    @extend_schema(
        request=WorkspaceEgressPolicyCommandSerializer,
        responses=WorkspaceSerializer,
        operation_id="api_v1_workspace_set_egress_policy",
    )
    def put(self, request: Request, workspace_uuid: UUID) -> Response:
        command = WorkspaceEgressPolicyCommandSerializer(data=request.data)
        command.is_valid(raise_exception=True)
        actor = authenticated_credential(request)
        item = services.admin_set_workspace_egress_policy(
            actor,
            configured_authorization_provider(),
            workspace_uuid,
            command.validated_data["egress_policy"],
            audit=request_audit(request, credential=actor),
        )
        return Response(WorkspaceSerializer(item).data)


class AccountWorkspaceRenameView(_AccountAPIView):
    """Rename one workspace through its policy-authorized locked writer."""

    @extend_schema(request=SubdivisionCommandSerializer, responses=WorkspaceSerializer)
    def post(self, request: Request, workspace_uuid: UUID) -> Response:
        command = SubdivisionCommandSerializer(data=request.data)
        command.is_valid(raise_exception=True)
        actor = authenticated_credential(request)
        item = services.admin_rename_workspace(
            actor,
            configured_authorization_provider(),
            workspace_uuid,
            command.validated_data["name"],
            audit=request_audit(request, credential=actor),
        )
        return Response(WorkspaceSerializer(item).data)


class AccountWorkspaceArchiveView(_AccountAPIView):
    """Archive one non-default workspace after exact policy checks."""

    @extend_schema(
        request=EmptyAccountCommandSerializer,
        responses=WorkspaceSerializer,
        operation_id="api_v1_workspace_archive",
    )
    def post(self, request: Request, workspace_uuid: UUID) -> Response:
        command = EmptyAccountCommandSerializer(data=request.data)
        command.is_valid(raise_exception=True)
        actor = authenticated_credential(request)
        item = services.admin_archive_workspace(
            actor, configured_authorization_provider(), workspace_uuid, audit=request_audit(request, credential=actor)
        )
        return Response(WorkspaceSerializer(item).data)


class AccountWorkspaceRestoreView(_AccountAPIView):
    """Restore one workspace under its persisted account ancestry."""

    @extend_schema(
        request=EmptyAccountCommandSerializer,
        responses=WorkspaceSerializer,
        operation_id="api_v1_workspace_restore",
    )
    def post(self, request: Request, workspace_uuid: UUID) -> Response:
        command = EmptyAccountCommandSerializer(data=request.data)
        command.is_valid(raise_exception=True)
        actor = authenticated_credential(request)
        item = services.admin_restore_workspace(
            actor, configured_authorization_provider(), workspace_uuid, audit=request_audit(request, credential=actor)
        )
        return Response(WorkspaceSerializer(item).data)
