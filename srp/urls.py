from django.urls import path
from . import views

app_name = "srp"

urlpatterns = [
    path("payouts/", views.payout_table, name="payout_table"),
    path("submit/", views.submit_claim, name="submit_claim"),
    path("my-claims/", views.my_claims, name="my_claims"),
]
