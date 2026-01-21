import base64
import logging
from datetime import timedelta

import requests  # pyright: ignore[reportMissingModuleSource]
from django.conf import settings  # pyright: ignore[reportMissingModuleSource]
from django.utils import timezone  # pyright: ignore[reportMissingModuleSource]

from eve_sso.models import EveCharacter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------
# Centralize timeouts + basic error logging for all EVE SSO / ESI calls.
# This prevents a stalled upstream request from blocking a web worker indefinitely.


def _timeout() -> int:
    return int(getattr(settings, "EVE_HTTP_TIMEOUT", 10) or 10)


def http_get(
    url: str, *, headers: dict | None = None, params: dict | None = None
) -> requests.Response:
    return requests.get(url, headers=headers, params=params, timeout=_timeout())


def http_post(
    url: str, *, headers: dict | None = None, data: dict | None = None
) -> requests.Response:
    return requests.post(url, headers=headers, data=data, timeout=_timeout())


def _esi_url(path: str) -> str:
    base = (getattr(settings, "EVE_ESI_URL", "https://esi.evetech.net") or "").rstrip(
        "/"
    )
    path = path.lstrip("/")
    return f"{base}/{path}"


# ---------------------------------------------------------------------
# ESI data helpers
# ---------------------------------------------------------------------


def get_character_info(character_id: int) -> tuple[int | None, int | None]:
    """
    Fetch ESI character info (corporation_id, alliance_id).

    Returns (corp_id, alliance_id). If ESI fails, returns (None, None).
    """
    url = _esi_url(f"latest/characters/{int(character_id)}/")
    try:
        resp = http_get(url)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("corporation_id"), data.get("alliance_id")
        logger.warning(
            "ESI character fetch failed (%s): %s", resp.status_code, resp.text[:200]
        )
    except requests.RequestException as e:
        logger.warning("ESI character fetch request failed: %s", e)
    except Exception:
        logger.exception("Unexpected error fetching character info")
    return None, None


def get_name(endpoint: str, entity_id: int) -> str | None:
    """
    Fetch the human-readable name for a corporation or alliance via ESI.

    endpoint should be "corporations" or "alliances".
    """
    url = _esi_url(f"latest/{endpoint}/{int(entity_id)}/")
    try:
        resp = http_get(url)
        if resp.status_code == 200:
            return resp.json().get("name")
        logger.warning(
            "Name lookup failed for %s %s (%s)", endpoint, entity_id, resp.status_code
        )
    except requests.RequestException as e:
        logger.warning(
            "Name lookup request failed for %s %s: %s", endpoint, entity_id, e
        )
    except Exception:
        logger.exception(
            "Unexpected error fetching %s name for %s", endpoint, entity_id
        )
    return None


# ---------------------------------------------------------------------
# Token management helpers
# ---------------------------------------------------------------------


def refresh_access_token(character: EveCharacter) -> bool:
    """
    Refresh a character's access token using its refresh token.

    Returns True if refreshed successfully, otherwise False.
    """
    if not character.refresh_token:
        logger.warning(
            "Token refresh skipped for %s: missing refresh_token",
            character.character_name,
        )
        return False

    auth_str = f"{settings.EVE_CLIENT_ID}:{settings.EVE_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()

    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {"grant_type": "refresh_token", "refresh_token": character.refresh_token}

    try:
        response = http_post(settings.EVE_TOKEN_URL, headers=headers, data=data)
    except requests.RequestException as e:
        logger.warning(
            "Token refresh request failed for %s: %s", character.character_name, e
        )
        return False

    if response.status_code != 200:
        logger.warning(
            "Token refresh failed for %s: %s",
            character.character_name,
            response.text[:300],
        )
        return False

    try:
        tokens = response.json()
    except ValueError:
        logger.warning(
            "Token refresh failed for %s: invalid JSON response",
            character.character_name,
        )
        return False

    character.access_token = tokens["access_token"]
    character.refresh_token = tokens.get("refresh_token", character.refresh_token)
    character.token_expiry = timezone.now() + timedelta(
        seconds=int(tokens["expires_in"])
    )
    character.save(
        update_fields=["access_token", "refresh_token", "token_expiry", "updated_at"]
    )

    logger.info("Token refreshed successfully for %s", character.character_name)
    return True


def ensure_valid_access_token(character: EveCharacter) -> EveCharacter:
    """
    Ensure a character has a valid access token.

    - If token_expiry is missing or expired, attempts refresh.
    - Returns the (updated) character.
    - Raises RuntimeError if refresh fails.
    """
    expiry = character.token_expiry
    if not expiry or expiry <= timezone.now():
        logger.info(
            "Access token expired/missing for %s; refreshing.", character.character_name
        )
        ok = refresh_access_token(character)
        if not ok:
            raise RuntimeError("Unable to refresh access token")
    return character
