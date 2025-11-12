import base64
import logging
import requests  # pyright: ignore[reportMissingModuleSource]
from django.utils import timezone  # pyright: ignore[reportMissingModuleSource]
from datetime import datetime, timedelta
from django.conf import settings  # pyright: ignore[reportMissingModuleSource]
from eve_sso.models import EveCharacter

logger = logging.getLogger(__name__)
ESI_BASE = "https://esi.evetech.net/latest"

# ------------------------
# 🔹 ESI Data Helpers
# ------------------------


def get_character_info(character_id: int):
    """Fetch ESI character info (corporation_id, alliance_id)."""
    try:
        resp = requests.get(f"{ESI_BASE}/characters/{character_id}/")
        if resp.status_code == 200:
            data = resp.json()
            return data.get("corporation_id"), data.get("alliance_id")
        logger.warning(f"ESI character fetch failed: {resp.status_code}")
    except Exception as e:
        logger.exception(f"Error fetching character info: {e}")
    return None, None


def get_name(endpoint: str, entity_id: int):
    """Fetch the human-readable name for a corp or alliance."""
    try:
        resp = requests.get(f"{ESI_BASE}/{endpoint}/{entity_id}/")
        if resp.status_code == 200:
            return resp.json().get("name")
        logger.warning(f"Name lookup failed for {endpoint} {entity_id}")
    except Exception as e:
        logger.exception(f"Error fetching {endpoint} name: {e}")
    return None


# ------------------------
# 🔹 Token Management Helpers
# ------------------------


### Access token is only good for 20 minuites. Use refresh token to get a new one. ###
def refresh_access_token(character: EveCharacter) -> bool:
    """Refresh a character's access token using its refresh token."""
    logger.info(f"Refreshing token for {character.character_name}")

    # Build authorization header
    auth_str = f"{settings.EVE_CLIENT_ID}:{settings.EVE_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()

    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {"grant_type": "refresh_token", "refresh_token": character.refresh_token}

    response = requests.post(settings.EVE_TOKEN_URL, headers=headers, data=data)

    if response.status_code != 200:
        logger.warning(
            f"Token refresh failed for {character.character_name}: {response.text}"
        )
        return False

    tokens = response.json()
    character.access_token = tokens["access_token"]
    character.refresh_token = tokens.get("refresh_token", character.refresh_token)
    character.token_expiry = timezone.now() + timedelta(seconds=tokens["expires_in"])
    character.save()

    logger.info(f"Token refreshed successfully for {character.character_name}")
    return True


def ensure_valid_access_token(character: EveCharacter) -> bool:
    """Ensure a character's token is valid; refresh if expired."""
    if character.token_expiry <= timezone.now():
        logger.info(f"Access token expired for {character.character_name}; refreshing.")
        return refresh_access_token(character)
    return True
