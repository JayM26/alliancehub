# accounts/urls.py
from django.urls import path  # pyright: ignore[reportMissingModuleSource]

from .views import change_main

app_name = "accounts"

urlpatterns = [
    path("change-main/", change_main, name="change_main"),
]
