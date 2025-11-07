from django.urls import path
from . import views

urlpatterns = [
    path("login/", views.eve_login, name="eve_login"),
    path("callback/", views.eve_callback, name="eve_callback"),
    path("character/<int:character_id>/", views.character_info, name="character_info"),
    path("choose-account-type/", views.choose_account_type, name="choose_account_type"),
]
