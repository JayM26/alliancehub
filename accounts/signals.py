from django.contrib.auth.signals import (
    user_logged_in,
)  # pyright: ignore[reportMissingModuleSource]
from django.dispatch import receiver  # pyright: ignore[reportMissingModuleSource]
from django.utils import timezone  # pyright: ignore[reportMissingModuleSource]
from datetime import timedelta
from eve_sso.models import EveCharacter


@receiver(user_logged_in)
def attach_pending_character(sender, request, user, **kwargs):
    """
    If there's a pending character in the session (from SSO flow),
    attach it as an alt to the logged-in user's account.
    """
    pending_char = request.session.get("pending_character")
    if not pending_char:
        return  # Nothing to do

    # Check if this character already exists (safety guard)
    existing = EveCharacter.objects.filter(
        character_id=pending_char["character_id"]
    ).first()

    if existing:
        existing.user = user
        existing.save()
    else:
        EveCharacter.objects.create(
            user=user,
            character_id=pending_char["character_id"],
            character_name=pending_char["character_name"],
            corporation_id=pending_char["corp_id"],
            corporation_name=pending_char["corp_name"],
            alliance_id=pending_char["alliance_id"],
            alliance_name=pending_char["alliance_name"],
            access_token=pending_char["access_token"],
            refresh_token=pending_char["refresh_token"],
            token_expiry=timezone.now() + timedelta(seconds=pending_char["expires_in"]),
        )

    # Clear from session
    del request.session["pending_character"]
