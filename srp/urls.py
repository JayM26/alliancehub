from django.urls import path  # pyright: ignore[reportMissingModuleSource]
from . import views

app_name = "srp"

urlpatterns = [
    path("payouts/", views.payout_table, name="payout_table"),
    path("submit/", views.submit_claim, name="submit_claim"),
    path("my-claims/", views.my_claims, name="my_claims"),
    # reviewer queue + actions
    path("queue/", views.review_queue, name="review_queue"),
    path("claim/<int:claim_id>/", views.claim_detail, name="claim_detail"),
    path("queue/<int:claim_id>/approve/", views.approve_claim, name="approve_claim"),
    path("queue/<int:claim_id>/deny/", views.deny_claim, name="deny_claim"),
    path("queue/<int:claim_id>/pay/", views.pay_claim, name="pay_claim"),
    # --- admin dashboards
    path("admin/overview/", views.admin_overview, name="admin_overview"),
    path("admin/payouts/", views.admin_payouts, name="admin_payouts"),
    path("admin/payouts/new/", views.admin_payout_new, name="admin_payout_new"),
    path(
        "admin/payouts/<int:ship_id>/",
        views.admin_payout_edit,
        name="admin_payout_edit",
    ),
    path("admin/payouts/bulk/", views.admin_payouts_bulk, name="admin_payouts_bulk"),
    path(
        "admin/payouts/bulk/apply/",
        views.admin_payouts_bulk_apply,
        name="admin_payouts_bulk_apply",
    ),
]
