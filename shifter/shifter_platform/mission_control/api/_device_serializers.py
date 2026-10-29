"""Mission Control native device and credential wire serializers."""

from rest_framework import serializers

from mission_control.api.scope import ResourcePlacementSerializer


class NGFWCreateSerializer(ResourcePlacementSerializer):
    """Validate NGFW creation requests."""

    name = serializers.CharField(allow_blank=True, required=False, default="", trim_whitespace=True)
    deployment_profile_id = serializers.IntegerField(required=False, allow_null=True)
    registration_method = serializers.CharField(required=False, allow_blank=True, default="", trim_whitespace=True)
    scm_credential_id = serializers.IntegerField(required=False, allow_null=True)
    otp_value = serializers.CharField(required=False, allow_blank=True, allow_null=True, trim_whitespace=True)
    otp_folder = serializers.CharField(required=False, allow_blank=True, allow_null=True, trim_whitespace=True)


class NGFWDestroySerializer(serializers.Serializer):
    """Validate NGFW destroy requests."""

    confirm_name = serializers.CharField(allow_blank=True, required=False, default="", trim_whitespace=True)


class CredentialCreateSerializer(ResourcePlacementSerializer):
    """Validate credential creation requests before schema-specific validation."""

    credential_type = serializers.CharField(trim_whitespace=True)
    name = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True)
    expires_at = serializers.CharField(required=False, allow_blank=True, allow_null=True, trim_whitespace=True)
    scm_folder_name = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True)
    scm_pin_id = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True)
    scm_pin_value = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    sls_region = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True)
    authcode = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)

    def validate_credential_type(self, value: str) -> str:
        if value not in ("scm", "deployment_profile"):
            raise serializers.ValidationError("Invalid credential type.")
        return value


class NGFWCreateResponseSerializer(serializers.Serializer):
    """Response body for ``NGFWCreateView.post`` (HTTP 201)."""

    id = serializers.CharField()
    name = serializers.CharField()
    status = serializers.CharField()


class NGFWListItemSerializer(serializers.Serializer):
    """One entry from ``NGFWListView.get``."""

    id = serializers.CharField()
    name = serializers.CharField()
    status = serializers.CharField()
    created_at = serializers.DateTimeField()
    serial_number = serializers.CharField(allow_null=True, allow_blank=True)


class NGFWListResponseSerializer(serializers.Serializer):
    """Response body for ``NGFWListView.get``."""

    ngfws = NGFWListItemSerializer(many=True)


class NGFWDestroyResponseSerializer(serializers.Serializer):
    """Response body for ``NGFWDestroyView.post``."""

    status = serializers.CharField()


class CredentialCreateResponseSerializer(serializers.Serializer):
    """Response body for ``CredentialCreateView.post`` (HTTP 201)."""

    id = serializers.IntegerField()
    name = serializers.CharField()
    credential_type = serializers.CharField()
