import re  # pyright: ignore[reportMissingModuleSource]
from typing import (
    Optional,
    Tuple,
    Iterable,
)  # pyright: ignore[reportMissingModuleSource]
from .models import EsiTypeCache  # pyright: ignore[reportMissingModuleSource]
import requests  # pyright: ignore[reportMissingModuleSource]

ESI_BASE = "https://esi.evetech.net/latest"
UA = "AllianceHub-SRP/1.0"


def parse_killmail_from_link(link: str) -> Optional[Tuple[int, str]]:
    """
    Accepts links like:
      https://esi.evetech.net/latest/killmails/12345678/abcdef.../?datasource=tranquility
    Returns (killmail_id, hash) or None.
    """
    if not link:
        return None

    m = re.search(r"/killmails/(\d+)/([0-9a-fA-F]+)", link)
    if not m:
        return None

    return int(m.group(1)), m.group(2)


def esi_get_json(path: str) -> dict:
    # Always pin datasource; avoids surprises
    url = f"{ESI_BASE}{path}"
    joiner = "&" if "?" in url else "?"
    url = f"{url}{joiner}datasource=tranquility"

    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_killmail(killmail_id: int, killmail_hash: str) -> dict:
    return esi_get_json(f"/killmails/{killmail_id}/{killmail_hash}/")


def fetch_type_name(type_id: int) -> str:
    data = esi_get_json(f"/universe/types/{type_id}/")
    return data.get("name") or ""


def fetch_system_name(system_id: int) -> str:
    data = esi_get_json(f"/universe/systems/{system_id}/")
    return data.get("name") or ""


def populate_claim_from_esi(claim) -> bool:
    """
    Mutates claim in-memory; caller should save().
    Returns True if killmail was successfully fetched, else False.
    """
    parsed = parse_killmail_from_link(claim.esi_link or "")
    if not parsed:
        return False

    km_id, km_hash = parsed
    claim.killmail_id = km_id
    claim.killmail_hash = km_hash

    km = fetch_killmail(km_id, km_hash)
    claim.killmail_raw = km

    victim = km.get("victim") or {}
    claim.victim_character_id = victim.get("character_id")
    if claim.victim_character_id and not claim.victim_character_name:
        try:
            claim.victim_character_name = fetch_character_name(
                int(claim.victim_character_id)
            )
        except Exception:
            pass

    ship_type_id = victim.get("ship_type_id")
    if ship_type_id:
        claim.ship_type_id = ship_type_id
        try:
            claim.ship_name = fetch_type_name(int(ship_type_id)) or claim.ship_name
        except Exception:
            # Don't let a type lookup failure kill the whole ESI pull
            pass

    system_id = km.get("solar_system_id")
    if system_id:
        claim.solar_system_id = system_id
        try:
            claim.solar_system_name = (
                fetch_system_name(int(system_id)) or claim.solar_system_name
            )
        except Exception:
            pass

    return True


def get_type_names_cached(
    type_ids: Iterable[int], fetch_cap: int = 40
) -> dict[int, str]:
    """
    Returns {type_id: name} using DB cache first.
    Fetches up to fetch_cap missing IDs from ESI and stores them.
    """
    ids = [int(x) for x in set(type_ids) if x]
    if not ids:
        return {}

    # 1) Read what we already have
    cached = EsiTypeCache.objects.filter(type_id__in=ids)
    result = {int(row.type_id): row.name for row in cached}

    missing = [tid for tid in ids if tid not in result]
    if not missing:
        return result

    # 2) Fetch a capped number of missing type IDs from ESI
    to_fetch = missing[: max(0, int(fetch_cap))]
    new_rows = []

    for tid in to_fetch:
        try:
            name = fetch_type_name(int(tid))
            if name:
                result[int(tid)] = name
                new_rows.append(EsiTypeCache(type_id=int(tid), name=name))
        except Exception:
            # Don't break page load for a lookup failure
            pass

    # 3) Insert new cache rows (ignore conflicts if multiple requests race)
    if new_rows:
        try:
            EsiTypeCache.objects.bulk_create(new_rows, ignore_conflicts=True)
        except Exception:
            pass

    return result


def fetch_character_name(character_id: int) -> str:
    data = esi_get_json(f"/characters/{character_id}/")
    return data.get("name") or ""
