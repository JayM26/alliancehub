# accounts/utils.py
from __future__ import annotations

from typing import Any

from eve_sso.models import EveCharacter


def get_user_identity_bundle(user) -> dict[str, Any]:
    """
    Returns a consistent identity bundle for UI use:
      - main_char: EveCharacter | None  (display main; falls back to first linked)
      - alts: list[EveCharacter]
      - all_characters: list[EveCharacter]
    """
    characters = list(EveCharacter.objects.filter(user=user).order_by("character_name"))

    main_char = getattr(user, "main_character", None)
    if main_char and getattr(main_char, "user_id", None) != user.id:
        main_char = None

    display_main = main_char or (characters[0] if characters else None)

    alts = [c for c in characters if not display_main or c.id != display_main.id]

    return {
        "main_char": display_main,
        "alts": alts,
        "all_characters": characters,
    }
