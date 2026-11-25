from rest_framework import serializers

from .models import Deposition, Depositor


class DepositorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Depositor
        fields = ["name", "orcid"]


class DepositionSerializer(serializers.ModelSerializer):
    depositor = DepositorSerializer(read_only=True)
    embargoed = serializers.SerializerMethodField()
    coordinates_url = serializers.SerializerMethodField()

    class Meta:
        model = Deposition
        fields = ["accession", "title", "method", "resolution", "depositor",
                  "deposited_on", "release_on", "embargoed", "coordinates_url"]

    def get_embargoed(self, obj):
        return obj.is_embargoed()

    def get_coordinates_url(self, obj):
        if not obj.coordinates:
            return None
        # The API path, not the file's location on disk. Access is re-checked on that request.
        return f"/api/depositions/{obj.accession}/coordinates/"


class SessionSerializer(serializers.Serializer):
    """What the presented token is good for. Shown to the reviewer, not to the public."""

    accession = serializers.CharField()
    issued_for = serializers.CharField()
    expires_at = serializers.DateTimeField()
    accesses_remaining = serializers.IntegerField(allow_null=True)
