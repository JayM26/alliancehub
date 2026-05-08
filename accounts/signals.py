from django.contrib.auth.signals import (
    user_logged_in,
)  # pyright: ignore[reportMissingModuleSource]
from django.dispatch import receiver  # pyright: ignore[reportMissingModuleSource]

from eve_sso.models import EveCharacter


@receiver(user_logged_in)
def attach_pending_character(sender, request, user, **kwargs):
    """
    If there's a pending character in the session (from SSO flow),
    assign it to the logged-in user. The EveCharacter row already exists
    with user=None; we just claim it.
    """
    pending_meta = request.session.get("pending_character")
    if not pending_meta:
        return

    EveCharacter.objects.filter(
        character_id=pending_meta["character_id"], user__isnull=True
    ).update(user=user)

    del request.session["pending_character"]
