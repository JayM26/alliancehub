import requests
import base64
import logging
from datetime import datetime, timedelta
from django.conf import settings
from django.shortcuts import redirect, render
from django.contrib.auth import login
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.utils import timezone
from django.urls import reverse
from eve_sso.utils import ensure_valid_access_token
from .models import EveCharacter
from urllib.parse import urlencode
from accounts.signals import attach_pending_character

User = get_user_model()
logger = logging.getLogger(__name__)


def eve_login(request):
    params = {
        "response_type": "code",
        "redirect_uri": settings.EVE_CALLBACK_URL,
        "client_id": settings.EVE_CLIENT_ID,
        "scope": "",
        "state": "randomstring123",
    }
    url = f"{settings.EVE_AUTH_URL}?{urlencode(params)}"
    return redirect(url)


def eve_callback(request):
    logger.info("Received SSO callback")
    code = request.GET.get("code")
    if not code:
        return render(
            request, "eve_sso/error.html", {"error": "Missing authorization code."}
        )

    # Token exchange
    auth_str = f"{settings.EVE_CLIENT_ID}:{settings.EVE_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()

    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {"grant_type": "authorization_code", "code": code}
    response = requests.post(settings.EVE_TOKEN_URL, headers=headers, data=data)

    if response.status_code != 200:
        logger.warning("Token request failed: %s", response.text)  # <-- Debug line
        return render(
            request,
            "eve_sso/error.html",
            {"error": "Failed to get access token.", "details": response.text},
        )

    tokens = response.json()
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in")

    logger.info("Access token received successfully")  # <-- Debug line

    # Use access token to verify character identity
    verify_headers = {"Authorization": f"Bearer {access_token}"}
    verify_response = requests.get(
        "https://login.eveonline.com/oauth/verify", headers=verify_headers
    )

    if verify_response.status_code != 200:
        logger.warning(
            "Character verification failed: %s", verify_response.text
        )  # <-- Debug line
        return render(
            request,
            "eve_sso/error.html",
            {"error": "Failed to verify character.", "details": verify_response.text},
        )

    character_data = verify_response.json()
    character_id = character_data["CharacterID"]
    character_name = character_data["CharacterName"]

    logger.info(f"Verified EVE character: {character_name} ({character_id})")

    # Fetch extra info from ESI
    esi_response = requests.get(
        f"https://esi.evetech.net/latest/characters/{character_id}/"
    )
    if esi_response.status_code == 200:
        esi_data = esi_response.json()
        corp_id = esi_data.get("corporation_id")
        alliance_id = esi_data.get("alliance_id")
    else:
        corp_id = alliance_id = None
        logger.warning("Failed to fetch ESI character details")  # <-- Debug line

    # Fetch corp and alliance names
    corp_name = None
    alliance_name = None

    # Corporation lookup
    if corp_id:
        corp_resp = requests.get(
            f"https://esi.evetech.net/latest/corporations/{corp_id}/"
        )
        if corp_resp.status_code == 200:
            corp_name = corp_resp.json().get("name")

    # Alliance lookup (only if alliance_id exists)
    if alliance_id:
        alli_resp = requests.get(
            f"https://esi.evetech.net/latest/alliances/{alliance_id}/"
        )
        if alli_resp.status_code == 200:
            alliance_name = alli_resp.json().get("name")

    # --- Save or handle new/existing character ---
    existing_char = EveCharacter.objects.filter(character_id=character_id).first()

    if existing_char:
        # Update existing record
        existing_char.access_token = access_token
        existing_char.refresh_token = refresh_token
        existing_char.token_expiry = timezone.now() + timedelta(seconds=expires_in)
        existing_char.corporation_id = corp_id
        existing_char.corporation_name = corp_name
        existing_char.alliance_id = alliance_id
        existing_char.alliance_name = alliance_name
        existing_char.save()
        logger.info(f"Updated existing character {character_name}")

        # ✅ Log the user in if not already
        login(request, existing_char.user)

        # ✅ Attach any pending alt that was waiting in session
        attach_pending_character(sender=None, request=request, user=request.user)

        return render(request, "eve_sso/success.html", {"character": character_name})

    else:
        # This is a new character
        if request.user.is_authenticated:
            # User is logged in — link as alt
            EveCharacter.objects.create(
                user=request.user,
                character_id=character_id,
                character_name=character_name,
                corporation_id=corp_id,
                corporation_name=corp_name,
                alliance_id=alliance_id,
                alliance_name=alliance_name,
                access_token=access_token,
                refresh_token=refresh_token,
                token_expiry=timezone.now() + timedelta(seconds=expires_in),
            )
            logger.info(
                f"Linked new alt {character_name} to user {request.user.username}"
            )
            return render(
                request, "eve_sso/success.html", {"character": character_name}
            )
        else:
            # User not logged in — ask if this is a main or alt
            request.session["pending_character"] = {
                "character_id": character_id,
                "character_name": character_name,
                "corp_id": corp_id,
                "corp_name": corp_name,
                "alliance_id": alliance_id,
                "alliance_name": alliance_name,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": expires_in,
            }
            return redirect(reverse("choose_account_type"))


def character_info(request, character_id):
    """
    Example view that ensures a valid access token,
    then fetches character info from ESI.
    """
    from eve_sso.models import EveCharacter

    try:
        char = EveCharacter.objects.get(character_id=character_id)
    except EveCharacter.DoesNotExist:
        return JsonResponse({"error": "Character not found"}, status=404)

    # ✅ Ensure the access token is valid (auto-refresh if needed)
    char = ensure_valid_access_token(char)

    headers = {"Authorization": f"Bearer {char.access_token}"}
    esi_url = f"https://esi.evetech.net/latest/characters/{char.character_id}/"

    esi_response = requests.get(esi_url, headers=headers)

    if esi_response.status_code != 200:
        return JsonResponse(
            {"error": "Failed to fetch ESI data", "details": esi_response.text},
            status=esi_response.status_code,
        )

    return JsonResponse(esi_response.json())


def choose_account_type(request):
    """
    Page that asks if the newly logged-in character is a Main or Alt.
    """
    pending_char = request.session.get("pending_character")

    if not pending_char:
        # No pending character data in session (user refreshed or invalid flow)
        return redirect("eve_login")

    if request.method == "POST":
        choice = request.POST.get("account_type")

        # User chose to register a new main account
        if choice == "main":
            from django.contrib.auth import get_user_model

            User = get_user_model()

            new_user = User.objects.create_user(
                username=pending_char["character_name"],
                password=None,  # we'll handle proper login after
            )

            # Create the EveCharacter and link it as their main
            new_char = EveCharacter.objects.create(
                user=new_user,
                character_id=pending_char["character_id"],
                character_name=pending_char["character_name"],
                corporation_id=pending_char["corp_id"],
                corporation_name=pending_char["corp_name"],
                alliance_id=pending_char["alliance_id"],
                alliance_name=pending_char["alliance_name"],
                access_token=pending_char["access_token"],
                refresh_token=pending_char["refresh_token"],
                token_expiry=timezone.now()
                + timedelta(seconds=pending_char["expires_in"]),
            )

            # Set as main character and log them in
            new_user.main_character = new_char
            new_user.save()

            login(request, new_user)
            request.session.pop("pending_character", None)

            return redirect(
                "/"
            )  # or reverse("admin:index") if you prefer to go to /admin

        # User chose to link as an alt
        elif choice == "alt":
            # Keep the pending character in session so we can attach it after SSO login
            return redirect("eve_login")

    # GET request → render the choice page
    return render(
        request, "eve_sso/choose_account_type.html", {"character": pending_char}
    )
