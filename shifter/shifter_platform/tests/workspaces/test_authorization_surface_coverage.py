"""Explicit production authorization-route coverage after the S8 cutover."""

from shared.authorization import action_definition
from workspaces.api.account_admin_urls import urlpatterns

_COLLECTION_METHODS = {"get", "post"}
_MUTATION_METHODS = {"post"}
_OPERATION_METHODS = {"get", "post"}
_EXPECTED_BY_SUFFIX = {
    "groups/": _COLLECTION_METHODS,
    "groups/<uuid:group_uuid>/memberships/": _MUTATION_METHODS,
    "policies/": _COLLECTION_METHODS,
    "policies/<uuid:policy_uuid>/assignments/": _MUTATION_METHODS,
    "policies/<uuid:policy_uuid>/actions/": _MUTATION_METHODS,
    "direct-assignments/": _MUTATION_METHODS,
    "predefined-assignments/": _MUTATION_METHODS,
    "operations/<uuid:operation_uuid>/": _OPERATION_METHODS,
}


def test_every_authorization_route_and_method_has_explicit_catalog_coverage() -> None:
    scoped = [
        route
        for route in urlpatterns
        if "authorization/" in str(route.pattern) and route.default_args and "target_type" in route.default_args
    ]
    assert len(scoped) == 33, "Authorization route coverage drift: classify every new surface explicitly"
    for route in scoped:
        target_type = route.default_args["target_type"]
        path = str(route.pattern)
        suffix = next((item for item in _EXPECTED_BY_SUFFIX if path.endswith(item)), None)
        assert suffix is not None, f"Unclassified authorization route: {path}"
        methods = {
            method
            for method in ("get", "post", "put", "patch", "delete")
            if callable(getattr(route.callback.cls, method, None))
        }
        assert methods == _EXPECTED_BY_SUFFIX[suffix]
        assert action_definition(f"{target_type}.manage_authorization").target_type == target_type

    catalogs = [route for route in urlpatterns if "authorization/" in str(route.pattern) and not route.default_args]
    assert {str(route.pattern) for route in catalogs} == {
        "workspaces/authorization/catalog/",
        "workspaces/authorization/predefined-catalog/",
    }
