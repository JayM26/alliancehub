from django.contrib import admin
from .models import User
from eve_sso.models import EveCharacter


class EveCharacterInline(admin.TabularInline):
    model = EveCharacter
    extra = 0
    fields = ("character_name", "corporation_name", "alliance_name", "token_expiry")
    readonly_fields = fields


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = (
        "username",
        "email",
        "main_character",
        "is_staff",
        "is_superuser",
        "is_active",
    )
    search_fields = ("username", "email")
    list_filter = ("is_staff", "is_superuser", "is_active")
    inlines = [EveCharacterInline]
