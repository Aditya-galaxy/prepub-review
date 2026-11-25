from django.contrib import admin, messages

from .models import AccessRecord, Deposition, Depositor, ReviewToken


@admin.register(Depositor)
class DepositorAdmin(admin.ModelAdmin):
    list_display = ["name", "orcid"]
    search_fields = ["name", "orcid"]


@admin.register(Deposition)
class DepositionAdmin(admin.ModelAdmin):
    list_display = ["accession", "title", "method", "release_on", "embargoed", "embargo_simulated"]
    list_filter = ["method", "embargo_simulated"]
    search_fields = ["accession", "title"]
    actions = ["issue_review_token"]

    @admin.display(boolean=True, description="Embargoed")
    def embargoed(self, obj):
        return obj.is_embargoed()

    @admin.action(description="Issue a 30-day review token")
    def issue_review_token(self, request, queryset):
        """The token is shown once, here, and never stored in a readable form."""
        for deposition in queryset:
            token, plaintext = ReviewToken.issue(
                deposition, issued_for="issued from admin", issued_by=request.user)
            self.message_user(
                request,
                f"{deposition.accession}: {plaintext} (expires {token.expires_at:%Y-%m-%d}). "
                f"Copy it now, it cannot be shown again.",
                level=messages.WARNING)


@admin.register(ReviewToken)
class ReviewTokenAdmin(admin.ModelAdmin):
    list_display = ["prefix", "deposition", "issued_for", "expires_at", "revoked_at",
                    "access_count", "max_accesses"]
    list_filter = ["revoked_at"]
    search_fields = ["prefix", "issued_for", "deposition__accession"]
    readonly_fields = ["token_hash", "prefix", "access_count", "created_at"]
    actions = ["revoke_tokens"]

    @admin.action(description="Revoke selected tokens")
    def revoke_tokens(self, request, queryset):
        for token in queryset:
            token.revoke()
        self.message_user(request, f"Revoked {queryset.count()} token(s).")


@admin.register(AccessRecord)
class AccessRecordAdmin(admin.ModelAdmin):
    """Read-only in the admin too. The trail is append-only at the model level as well."""

    list_display = ["occurred_at", "outcome", "accession", "token_prefix", "status_code", "path"]
    list_filter = ["outcome", "status_code"]
    search_fields = ["accession", "token_prefix"]
    date_hierarchy = "occurred_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
