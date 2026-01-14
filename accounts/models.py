# accounts/models.py
from django.db import models  # pyright: ignore[reportMissingModuleSource]
from django.contrib.auth.models import (
    AbstractUser,
)  # pyright: ignore[reportMissingModuleSource]


class User(AbstractUser):
    main_character = models.ForeignKey(
        "eve_sso.EveCharacter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="main_for_users",
    )

    def get_main_character(self):
        if self.main_character:
            return self.main_character
        return self.eve_characters.first()

    def get_corp_name(self) -> str:
        ch = self.get_main_character()
        return (getattr(ch, "corporation_name", None) or "Unknown").strip()

    def get_alliance_name(self) -> str:
        ch = self.get_main_character()
        return (getattr(ch, "alliance_name", None) or "Unknown").strip()
