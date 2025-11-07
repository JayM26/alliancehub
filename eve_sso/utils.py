import base64
import logging
import requests
from django.utils import timezone
from datetime import datetime, timedelta
from django.conf import settings

logger = logging.getLogger(__name__)


### Access token is only good for 20 minuites. Use refresh token to get a new one. ###
def refresh_access_token(eve_character):
    """
    Use the character's refresh token to get a new access token.
    Updates the EveCharacter record in the database.
    """
    auth_str = f"{settings.EVE_CLIENT_ID}:{settings.EVE_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()

    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {
        "grant_type": "refresh_token",
        "refresh_token": eve_character.refresh_token,
    }

    try:
        response = requests.post(settings.EVE_TOKEN_URL, headers=headers, data=data)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"[SSO Refresh Error] {eve_character.character_name}: {str(e)}")
        return None

    tokens = response.json()
    access_token = tokens.get("access_token")
    expires_in = tokens.get("expires_in")

    if not access_token:
        logger.error(
            f"[SSO Refresh Error] {eve_character.character_name}: No access_token in response {tokens}"
        )
        return None

    eve_character.access_token = access_token
    eve_character.token_expiry = datetime.utcnow() + timedelta(seconds=expires_in)
    eve_character.save()

    logger.info(f"[SSO Refresh OK] {eve_character.character_name} token refreshed.")
    return eve_character


def ensure_valid_access_token(eve_character):
    """
    Checks whether a character's access token is still valid.
    If expired or about to expire (within 2 minutes), it refreshes automatically.
    Returns the character with a guaranteed-valid access token.
    """
    # Safety margin
    threshold = timezone.now() + timedelta(minutes=2)

    if eve_character.token_expiry is None or eve_character.token_expiry <= threshold:
        from .utils import refresh_access_token  # avoid circular import

        refreshed = refresh_access_token(eve_character)
        return refreshed or eve_character

    return eve_character
