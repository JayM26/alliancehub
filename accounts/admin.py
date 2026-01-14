from django.contrib import admin  # pyright: ignore[reportMissingModuleSource]
from django.contrib.auth.admin import (  # pyright: ignore[reportMissingModuleSource]
    UserAdmin as DjangoUserAdmin,
)
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
    filter_horizontal = ("groups", "user_permissions")
    search_fields = ("username", "email")
    list_filter = ("is_staff", "is_superuser", "is_active")
    inlines = [EveCharacterInline]
