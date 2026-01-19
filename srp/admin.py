from django.contrib import admin  # pyright: ignore[reportMissingModuleSource]
from .models import (
    ShipPayout,
    SRPClaim,
    SRPConfig,
    ClaimReview,
    DoctrineFit,
    DoctrineFitItem,
)


@admin.register(ShipPayout)
class ShipPayoutAdmin(admin.ModelAdmin):
    list_display = (
        "ship_name",
        "strategic",
        "peacetime",
        "shitstack",
        "tnt_special",
        "hull_contract",
        "last_updated",
    )
    search_fields = ("ship_name",)
    list_filter = ("hull_contract",)
    ordering = ("ship_name",)


class ClaimReviewInline(admin.TabularInline):
    model = ClaimReview
    extra = 0
    readonly_fields = ("reviewer", "action", "comment", "timestamp")


@admin.action(description="Mark selected claims as Approved")
def approve_claims(modeladmin, request, queryset):
    for claim in queryset:
        claim.set_status(
            "APPROVED", reviewer=request.user, note="Approved via admin action."
        )
        claim.save()
        ClaimReview.objects.create(
            claim=claim,
            reviewer=request.user,
            action="Approved",
            comment="Admin bulk action",
        )


@admin.action(description="Mark selected claims as Denied")
def deny_claims(modeladmin, request, queryset):
    for claim in queryset:
        claim.set_status(
            "DENIED", reviewer=request.user, note="Denied via admin action."
        )
        claim.save()
        ClaimReview.objects.create(
            claim=claim,
            reviewer=request.user,
            action="Denied",
            comment="Admin bulk action",
        )


@admin.action(description="Mark selected claims as Paid")
def pay_claims(modeladmin, request, queryset):
    for claim in queryset:
        claim.set_status("PAID", reviewer=request.user, note="Paid via admin action.")
        claim.save()
        ClaimReview.objects.create(
            claim=claim,
            reviewer=request.user,
            action="Paid",
            comment="Admin bulk action",
        )


@admin.register(SRPClaim)
class SRPClaimAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "character_name",
        "ship",
        "category",
        "status",
        "payout_amount",
        "submitted_at",
        "processed_at",
        "reviewer",
    )
    list_filter = ("status", "category", "ship__hull_contract", "submitted_at")
    search_fields = (
        "character_name",
        "ship__ship_name",
        "esi_link",
        "system",
        "region",
    )
    date_hierarchy = "submitted_at"
    inlines = [ClaimReviewInline]
    actions = [approve_claims, deny_claims, pay_claims]
    readonly_fields = ("submitted_at", "processed_at")


@admin.register(SRPConfig)
class SRPConfigAdmin(admin.ModelAdmin):
    list_display = (
        "monthly_ceiling_peacetime",
        "monthly_ceiling_strategic",
        "auto_calculate_payouts",
        "default_multiplier",
    )


class DoctrineFitItemInline(admin.TabularInline):
    model = DoctrineFitItem
    extra = 0
    fields = ("slot_group", "type_id", "type_name", "qty")
    readonly_fields = ("type_name",)


@admin.register(DoctrineFit)
class DoctrineFitAdmin(admin.ModelAdmin):
    list_display = (
        "ship_name",
        "ship_type_id",
        "name",
        "active",
        "updated_at",
        "updated_by",
    )
    list_filter = ("active",)
    search_fields = ("ship_name", "name", "ship_type_id")
    inlines = [DoctrineFitItemInline]
    readonly_fields = ("created_at", "updated_at")
