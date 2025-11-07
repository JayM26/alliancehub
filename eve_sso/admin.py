# eve_sso/admin.py
from django.contrib import admin
from .models import EveCharacter


@admin.register(EveCharacter)
class EveCharacterAdmin(admin.ModelAdmin):
    list_display = (
        "character_name",
        "character_id",
        "user",
        "corporation_name",
        "alliance_name",
        "token_expiry",
    )
    search_fields = (
        "character_name",
        "character_id",
        "corporation_name",
        "alliance_name",
    )
    list_filter = ("corporation_name", "alliance_name")
