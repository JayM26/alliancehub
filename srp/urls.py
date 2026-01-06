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
]
