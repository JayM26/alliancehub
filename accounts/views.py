# accounts/views.py
from __future__ import annotations

from django.contrib.auth.decorators import (  # pyright: ignore[reportMissingModuleSource]
    login_required,
)
from django.shortcuts import (  # pyright: ignore[reportMissingModuleSource]
    get_object_or_404,
    redirect,
    render,
)

from eve_sso.models import EveCharacter


@login_required
def change_main(request):
    """
    Allow a logged-in user to choose which linked EveCharacter is their main.

    This updates accounts.User.main_character and redirects back to the dashboard.
    """
    user = request.user
    characters = list(EveCharacter.objects.filter(user=user).order_by("character_name"))

    current_main_id = getattr(getattr(user, "main_character", None), "id", None)

    if request.method == "POST":
        char_id = (request.POST.get("character_id") or "").strip()
        if not char_id:
            return redirect("accounts:change_main")

        # Only allow selecting a character already linked to this user.
        new_main = get_object_or_404(EveCharacter, user=user, character_id=int(char_id))
        user.main_character = new_main
        user.save(update_fields=["main_character"])

        return redirect("dashboard")

    return render(
        request,
        "accounts/change_main.html",
        {
            "characters": characters,
            "current_main_id": current_main_id,
        },
    )
