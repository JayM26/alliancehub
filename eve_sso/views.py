# eve_sso/views.py
"""
EVE Online SSO views.

End-state principles:
- Views orchestrate HTTP flow and persistence; heavy logic lives in utils/services.
- All external network calls must have timeouts (enforced via eve_sso.utils wrappers).
- OAuth "state" is required and time-boxed to bind the callback to the initiating session.
- Environment-specific configuration belongs in settings/.env, not hardcoded in code.
"""

from __future__ import annotations

import base64
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests  # pyright: ignore[reportMissingModuleSource]
from django.conf import settings  # pyright: ignore[reportMissingModuleSource]
from django.db import (  # pyright: ignore[reportMissingModuleSource]
    IntegrityError,
    transaction,
)
from django.contrib.auth import (  # pyright: ignore[reportMissingModuleSource]
    get_user_model,
    login,
)
from django.contrib.auth.decorators import (  # pyright: ignore[reportMissingModuleSource]
    login_required,
)
from django.http import JsonResponse  # pyright: ignore[reportMissingModuleSource]
from django.shortcuts import (  # pyright: ignore[reportMissingModuleSource]
    redirect,
    render,
)
from django.urls import reverse  # pyright: ignore[reportMissingModuleSource]
from django.utils import timezone  # pyright: ignore[reportMissingModuleSource]
from django.utils.dateparse import (  # pyright: ignore[reportMissingModuleSource]
    parse_datetime,
)
from django.utils.text import slugify  # pyright: ignore[reportMissingModuleSource]

from accounts.signals import attach_pending_character
from eve_sso.models import EveCharacter
from eve_sso.utils import (
    ensure_valid_access_token,
    get_character_info,
    get_name,
    http_get,
    http_post,
)

logger = logging.getLogger(__name__)
User = get_user_model()

# Session keys used to store per-login OAuth state.
SSO_STATE_SESSION_KEY = "eve_sso_state"
SSO_STATE_CREATED_SESSION_KEY = "eve_sso_state_created"

# How long an initiated login is allowed to remain valid (defense in depth).
SSO_STATE_MAX_AGE = timedelta(minutes=10)


def _clear_sso_state(request) -> None:
    """Remove any stored OAuth state values from the session."""
    request.session.pop(SSO_STATE_SESSION_KEY, None)
    request.session.pop(SSO_STATE_CREATED_SESSION_KEY, None)


def eve_login(request):
    """
    Redirect the user to CCP's authorization endpoint.

    Generates a per-request OAuth state value and stores it in the session with a timestamp.
    The callback must return the same state within SSO_STATE_MAX_AGE to be accepted.
    """
    state = secrets.token_urlsafe(32)
    request.session[SSO_STATE_SESSION_KEY] = state
    request.session[SSO_STATE_CREATED_SESSION_KEY] = timezone.now().isoformat()

    params = {
        "response_type": "code",
        "redirect_uri": settings.EVE_CALLBACK_URL,
        "client_id": settings.EVE_CLIENT_ID,
        "scope": "",
        "state": state,
    }
    return redirect(f"{settings.EVE_AUTH_URL}?{urlencode(params)}")


def eve_callback(request):
    """
    Handle CCP redirect back to us:
    1) Validate OAuth state (value + time-box)
    2) Exchange code for tokens
    3) Verify character identity
    4) Upsert EveCharacter record
    5) Login and attach pending character if applicable
    """
    logger.info("Received SSO callback")

    # CCP can return error/error_description instead of a code.
    oauth_error = request.GET.get("error")
    if oauth_error:
        details = request.GET.get("error_description") or oauth_error
        _clear_sso_state(request)
        return render(
            request,
            "eve_sso/error.html",
            {"error": "SSO login was not completed.", "details": details},
        )

    code = request.GET.get("code")
    returned_state = request.GET.get("state")
    expected_state = request.session.get(SSO_STATE_SESSION_KEY)
    created_iso = request.session.get(SSO_STATE_CREATED_SESSION_KEY)

    if not code:
        _clear_sso_state(request)
        return render(
            request, "eve_sso/error.html", {"error": "Missing authorization code."}
        )

    # State presence check
    if not expected_state or not returned_state or not created_iso:
        _clear_sso_state(request)
        return render(
            request,
            "eve_sso/error.html",
            {"error": "Invalid login session. Please try again."},
        )

    # Time-box check (use Django's parser for robustness)
    created_at = parse_datetime(created_iso)
    if created_at is None:
        _clear_sso_state(request)
        return render(
            request,
            "eve_sso/error.html",
            {"error": "Invalid login session. Please try again."},
        )

    if timezone.is_naive(created_at):
        created_at = timezone.make_aware(created_at, timezone.get_current_timezone())

    if timezone.now() - created_at > SSO_STATE_MAX_AGE:
        _clear_sso_state(request)
        return render(
            request,
            "eve_sso/error.html",
            {"error": "Login session expired. Please try again."},
        )

    # Constant-time compare is standard practice for secrets.
    if not secrets.compare_digest(str(expected_state), str(returned_state)):
        logger.warning("SSO state mismatch.")
        _clear_sso_state(request)
        return render(
            request,
            "eve_sso/error.html",
            {"error": "Invalid login session (state mismatch). Please try again."},
        )

    # One-time use: clear state after validation.
    _clear_sso_state(request)

    # Token exchange
    auth_str = f"{settings.EVE_CLIENT_ID}:{settings.EVE_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()

    token_headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    token_data = {"grant_type": "authorization_code", "code": code}

    try:
        token_resp = http_post(
            settings.EVE_TOKEN_URL, headers=token_headers, data=token_data
        )
    except requests.RequestException as e:
        logger.warning("Token request failed (request error): %s", e)
        return render(
            request, "eve_sso/error.html", {"error": "Failed to get access token."}
        )

    if token_resp.status_code != 200:
        logger.warning("Token request failed: %s", token_resp.text[:300])
        return render(
            request,
            "eve_sso/error.html",
            {"error": "Failed to get access token."},
        )

    try:
        tokens = token_resp.json()
    except ValueError:
        logger.warning("Token response JSON decode failed.")
        return render(
            request,
            "eve_sso/error.html",
            {"error": "Failed to get access token."},
        )

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = int(tokens.get("expires_in") or 0)

    if not access_token or not refresh_token or expires_in <= 0:
        logger.warning("Token response missing expected fields.")
        return render(
            request,
            "eve_sso/error.html",
            {"error": "SSO token response was missing required fields."},
        )

    # Verify character identity
    verify_headers = {"Authorization": f"Bearer {access_token}"}
    verify_url = getattr(
        settings, "EVE_VERIFY_URL", "https://login.eveonline.com/oauth/verify"
    )

    try:
        verify_resp = http_get(verify_url, headers=verify_headers)
    except requests.RequestException as e:
        logger.warning("Character verification request failed: %s", e)
        return render(
            request, "eve_sso/error.html", {"error": "Failed to verify character."}
        )

    if verify_resp.status_code != 200:
        logger.warning("Character verification failed: %s", verify_resp.text[:300])
        return render(
            request,
            "eve_sso/error.html",
            {"error": "Failed to verify character."},
        )

    try:
        character_data = verify_resp.json()
    except ValueError:
        logger.warning("Verify response JSON decode failed.")
        return render(
            request,
            "eve_sso/error.html",
            {"error": "Failed to verify character."},
        )

    character_id = int(character_data["CharacterID"])
    character_name = character_data["CharacterName"]
    link_mode = request.session.pop("link_mode", None)

    logger.info("Verified EVE character: %s (%s)", character_name, character_id)

    # ESI: corporation/alliance IDs and best-effort names
    corp_id, alliance_id = get_character_info(character_id)
    corp_name = get_name("corporations", corp_id) if corp_id else None
    alliance_name = get_name("alliances", alliance_id) if alliance_id else None

    token_expiry = timezone.now() + timedelta(seconds=expires_in)
    char_defaults = {
        "character_name": character_name,
        "corporation_id": corp_id,
        "corporation_name": corp_name,
        "alliance_id": alliance_id,
        "alliance_name": alliance_name,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expiry": token_expiry,
    }

    with transaction.atomic():
        # Lock only the EveCharacter row (of=("self",)), not the joined user.
        # `select_related("user")` LEFT-OUTER-JOINs the nullable user FK; Django 5.2
        # raises NotSupportedError ("FOR UPDATE cannot be applied to the nullable side
        # of an outer join") if the lock spans that join. Scoping FOR UPDATE to this
        # table keeps both the prefetch and the row lock. (Django 5.2 upgrade fix.)
        existing_char = (
            EveCharacter.objects.select_for_update(of=("self",))
            .filter(character_id=character_id)
            .select_related("user")
            .first()
        )

        if existing_char:
            # HARD GUARD: do not allow linking a character owned by another user
            if existing_char.user and request.user.is_authenticated:
                if existing_char.user_id != request.user.id:
                    logger.warning(
                        "Attempt to link character %s owned by another user",
                        character_id,
                    )
                    return render(
                        request,
                        "eve_sso/error.html",
                        {"error": "This character is already linked to another account. Contact an administrator if you believe this is an error."},
                    )

            for field, value in char_defaults.items():
                setattr(existing_char, field, value)
            existing_char.save(update_fields=[*char_defaults.keys(), "updated_at"])

            login(request, existing_char.user)
            attach_pending_character(sender=None, request=request, user=request.user)

            return render(request, "eve_sso/success.html", {"character": character_name})

        if request.user.is_authenticated or link_mode == "alt":
            target_user = request.user if request.user.is_authenticated else None

            if target_user:
                try:
                    EveCharacter.objects.create(
                        user=target_user,
                        character_id=character_id,
                        **char_defaults,
                    )
                except IntegrityError:
                    return render(
                        request,
                        "eve_sso/error.html",
                        {"error": "This character was just linked by another session. Please try again."},
                    )
                logger.info(
                    "Linked new alt %s to user %s", character_name, target_user.username
                )
                return redirect(settings.LOGIN_REDIRECT_URL or "/")

    # Persist character immediately (user=None until account type is chosen).
    # Tokens never transit through the session.
    try:
        pending_char = EveCharacter.objects.create(
            user=None,
            character_id=character_id,
            **char_defaults,
        )
    except IntegrityError:
        return render(
            request,
            "eve_sso/error.html",
            {"error": "This character was just linked by another session. Please try again."},
        )

    request.session["pending_character"] = {
        "character_id": character_id,
        "character_name": character_name,
    }
    return redirect(reverse("choose_account_type"))


@login_required
def character_info(request, character_id):
    """
    Fetch character info from ESI for a stored EveCharacter.

    Access control:
    - Reviewers/admins can access any character.
    - Regular users can only access characters linked to their account.
    """
    try:
        char = EveCharacter.objects.select_related("user").get(
            character_id=character_id
        )
    except EveCharacter.DoesNotExist:
        return JsonResponse({"error": "Character not found"}, status=404)

    can_review = request.user.has_perm("srp.can_review_srp") or request.user.is_staff
    if not can_review and char.user_id != request.user.id:
        return JsonResponse({"error": "Forbidden"}, status=403)

    try:
        char = ensure_valid_access_token(char)
    except RuntimeError as e:
        logger.warning("Token refresh failed for character %s: %s", character_id, e)
        return JsonResponse({"error": "Token refresh failed"}, status=401)

    headers = {"Authorization": f"Bearer {char.access_token}"}
    esi_url = f"{settings.EVE_ESI_URL.rstrip('/')}/latest/characters/{int(char.character_id)}/"

    try:
        esi_resp = http_get(esi_url, headers=headers)
    except requests.RequestException as e:
        logger.warning("ESI fetch failed for character %s: %s", character_id, e)
        return JsonResponse({"error": "Failed to fetch ESI data"}, status=502)

    if esi_resp.status_code != 200:
        logger.warning("ESI returned %s for character %s", esi_resp.status_code, character_id)
        return JsonResponse({"error": "Failed to fetch ESI data"}, status=esi_resp.status_code)

    return JsonResponse(esi_resp.json())


def choose_account_type(request):
    """
    Ask whether a newly authenticated character should:
    - create a new main account, or
    - begin an alt-link flow (user will log in with an existing main next).
    """
    pending_meta = request.session.get("pending_character")
    if not pending_meta:
        return redirect("eve_login")

    pending_char = EveCharacter.objects.filter(
        character_id=pending_meta["character_id"], user__isnull=True
    ).first()
    if not pending_char:
        request.session.pop("pending_character", None)
        return redirect("eve_login")

    if request.method == "POST":
        choice = request.POST.get("account_type")

        if choice == "main":
            safe_username = slugify(pending_char.character_name)
            if User.objects.filter(username=safe_username).exists():
                safe_username = f"{safe_username}-{pending_char.character_id}"

            new_user = User.objects.create_user(username=safe_username, password=None)

            pending_char.user = new_user
            pending_char.save(update_fields=["user", "updated_at"])

            new_user.main_character = pending_char
            new_user.save(update_fields=["main_character"])

            login(request, new_user)
            request.session.pop("pending_character", None)

            return redirect(settings.LOGIN_REDIRECT_URL or "/")

        if choice == "alt":
            logger.info("Alt linking initiated — keeping pending_character in session")
            return redirect("eve_login")

    return render(
        request,
        "eve_sso/choose_account_type.html",
        {"character": {"character_name": pending_char.character_name, "character_id": pending_char.character_id}},
    )
