# Platform API Development

BigRAE's non-public HTTP/JSON API is a Django REST Framework surface mounted
under `/api/v1/`. New API endpoints should join that surface instead of adding
ad-hoc `JsonResponse` function views.

## Endpoint Shape

- Put request and response validation in DRF serializers.
- Put application behavior in the existing service layer (`cms.services`,
  `ctf.services`, `engine.services`, or the owning app's public facade).
- Use `APIView` or `ViewSet` for HTTP concerns only: authentication,
  permissions, serializer selection, response status, pagination, filtering, and
  OpenAPI metadata.
- Mount app routers through `config/api_urls.py` so the public path remains
  `/api/v1/...` and the OpenAPI schema sees every migrated route.

## Authentication And Authority

DRF attempts `ApiTokenAuthentication` before `SessionAuthentication`. A supplied
invalid bearer fails closed; it cannot fall through to a browser session.
The bearer adapter verifies personal tokens or configured service credentials.
Browser requests require a live native session reference.

Resolve the request through `shared.api.principals.authenticated_credential`
and pass its current `CredentialContext` to the owning service. A Django user,
a legacy group flag, or a service principal's responsible human is not authority.
Use the native principal for policy, resource custody and audit attribution.

Credential scopes and action ceilings narrow access. They do not grant an
operation by themselves. The owning service checks current policy for the exact
action and resource, lifecycle state, and account, organization or workspace.
Use `shared.authorization.operations` and `shared.api_tokens.scopes` for the
published action and compatibility-scope vocabulary. Do not copy those catalogs
into a view or infer permission from a scope name.

The mounted audit API is `PolicyAuditLogViewSet`, registered by
`management.api.account_admin_urls`. It resolves the current credential and
uses `authorized_audit_events` to enforce native audit policy. Legacy staff
flags do not authorize audit reads.

## CMS Boundaries

Register CMS endpoints through `config/api_urls.py`. Views own HTTP parsing,
serializers, response status and schema metadata. Content and tenant-pack
services own admission, validation, persistence and cleanup.

Use `cms.api.permissions.cms_actor_credential` to obtain current proof. Pass
that proof to the existing content service; do not substitute `ApiToken.created_by`
or grant access from staff or legacy group membership. Service policy checks
remain required after HTTP admission. Private content images require current
policy or eligible participation and visible content. Policy outages fail closed.

Keep YAML parsing, scenario validation, audit and persistence in their owning
services. Do not put bearer tokens, CSRF tokens, private YAML, provider responses
or process arguments in logs or documentation examples.

## Mission Control Boundaries

Register Mission Control JSON endpoints under `/api/v1/`. Use
`MissionControlAPIView.actor_credential` and the canonical CMS and Engine
service facades. Each operation retains current proof, exact resource custody,
policy, lifecycle and retained-request checks. Temporary participant credentials
remain limited to their admitted event and allowed lifecycle operations.

The current-range response contains the server-owned cleanup deadline, maximum
deadline, extension increment and current extension eligibility. Extension is a
bodyless `POST`; callers cannot choose a deadline or increment. The owning
service authorizes the current credential before changing the lease.

VPN profile delivery is a separate bodyless `POST` with its own native action
check. It returns a bounded `application/x-openvpn-profile` attachment with
private, no-store caching. Do not place profile bytes in JSON or query state.

Reuse the existing typed domain and RAES contracts rather than copying their
validation rules. Presigned upload URLs, upload tokens and Guacamole URLs are
sensitive. Keep them out of logs and OpenAPI examples. The Guacamole API uses
`mission_control.guacamole_authority` and the existing bootstrap/session services.
Polling and one-time URL consumption retain current proof and custody checks,
expiry, `Retry-After` and worker-capacity handling.

## Errors

DRF exceptions use the platform envelope:

```json
{
  "error": {
    "code": "invalid",
    "message": "Invalid request",
    "details": {
      "field": ["This field is required."]
    },
    "request_id": "req-123"
  }
}
```

Use `shared.api.errors.api_error_response(...)` for explicit DRF error
responses. Do not return raw exception text, provider payloads, bearer tokens,
cookies, CSRF tokens, presigned URLs, or signed Guacamole URLs.

The explicit helper accepts authored public messages. Map caught exceptions to
fixed public messages before calling it; stripping line breaks does not remove
confidential exception content. For validation details, pass the actual
`detail_serializer` so only its declared field names enter the response. The
shared exception handler uses the view's declared serializer. Nested serializers
and list indices retain their error structure; unknown keys become
`non_field_errors`, and unrecognized error text becomes `Invalid value.`.
Structured domain details, such as pack-removal blockers, belong in the owning
adapter's fixed-field projection with enumerated states and no private labels.

## Schema And Docs

OpenAPI is served from `/api/v1/schema/`; Swagger UI is served from
`/api/v1/docs/`. These endpoints use the same platform authentication posture
as the non-public API: a current native session or verified bearer credential
can read the contract. Swagger UI assets are served from the pinned local
`drf-spectacular-sidecar` package, not from a third-party CDN.

Keep examples secret-free. Show header names and placeholder values, not real
tokens or signed URLs.

## Pagination And Filtering

The default list behavior is page-number pagination with page size 50. The
platform enables DRF's search and ordering filter backends globally, but each
endpoint must declare its supported `search_fields` and `ordering_fields`.
Prefer serializer- or filterset-shaped query validation for new filters instead
of parsing unbounded query parameters in `get_queryset()`.
