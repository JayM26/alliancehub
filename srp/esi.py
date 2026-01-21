from __future__ import annotations

import re
from typing import (
    Optional,
    Tuple,
    Iterable,
)
import requests  # pyright: ignore[reportMissingModuleSource]

from django.conf import settings  # pyright: ignore[reportMissingModuleSource]

from .models import EsiTypeCache, EsiEntityCache


UA = "AllianceHub-SRP/1.0"


def _esi_base() -> str:
    """
    Base ESI URL (no trailing slash), environment-controlled via settings.EVE_ESI_URL.
    """
    base = (getattr(settings, "EVE_ESI_URL", "https://esi.evetech.net") or "").rstrip(
        "/"
    )
    return f"{base}/latest"


def _timeout() -> int:
    """
    Network timeout for ESI calls, environment-controlled via settings.EVE_HTTP_TIMEOUT.
    """
    return int(getattr(settings, "EVE_HTTP_TIMEOUT", 15) or 15)


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
    """
    Safe ESI GET helper.
    Returns {} on failure instead of raising, to avoid breaking page loads.
    """
    base = _esi_base()
    path = path.lstrip("/")
    url = f"{base}/{path}"
    joiner = "&" if "?" in url else "?"
    url = f"{url}{joiner}datasource=tranquility"

    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except requests.RequestException:
        return {}


def fetch_type_ids_by_names(type_names: list[str]) -> dict[str, int]:
    """
    Uses ESI /universe/ids/ to resolve type names -> type_id.
    This endpoint accepts a JSON array of strings via POST.
    Returns mapping for types only.
    """
    if not type_names:
        return {}

    url = f"{_esi_base()}/universe/ids/?datasource=tranquility"

    try:
        r = requests.post(
            url,
            json=type_names,
            headers={"User-Agent": UA},
            timeout=_timeout(),
        )
        r.raise_for_status()
        data = r.json() or {}
    except (requests.RequestException, ValueError):
        # Best-effort resolver: callers already treat failures as non-fatal.
        return {}

    out: dict[str, int] = {}
    for row in data.get("inventory_types") or []:
        name = (row.get("name") or "").strip()
        tid = row.get("id")
        if name and tid:
            out[name] = int(tid)
    return out


def get_type_ids_by_names_cached(
    type_names: Iterable[str], fetch_cap: int = 200
) -> dict[str, int]:
    """
    Resolve item names -> type_id using DB cache first.
    Fetches missing names from ESI /universe/ids/ in batches and stores them.
    """
    # Normalize + de-dupe, preserve original casing for exact-match mapping
    names = []
    seen = set()
    for n in type_names:
        nn = (n or "").strip()
        if not nn:
            continue
        if nn in seen:
            continue
        seen.add(nn)
        names.append(nn)

    if not names:
        return {}

    # 1) Cache lookup
    cached_qs = EsiTypeCache.objects.filter(name__in=names).values_list(
        "name", "type_id"
    )
    result: dict[str, int] = {name: int(type_id) for (name, type_id) in cached_qs}

    missing = [n for n in names if n not in result]
    if not missing or fetch_cap <= 0:
        return result

    # 2) Fetch missing (bounded) from ESI in chunks (ESI accepts a list; keep chunk size reasonable)
    to_fetch = missing[: int(fetch_cap)]
    chunk_size = 100
    new_rows: list[EsiTypeCache] = []

    for i in range(0, len(to_fetch), chunk_size):
        chunk = to_fetch[i : i + chunk_size]
        try:
            fetched = fetch_type_ids_by_names(chunk)
        except Exception:
            fetched = {}

        for name, tid in fetched.items():
            # Only store if it was actually requested (defensive)
            if name in missing:
                result[name] = int(tid)
                new_rows.append(EsiTypeCache(type_id=int(tid), name=name))

    if new_rows:
        try:
            EsiTypeCache.objects.bulk_create(new_rows, ignore_conflicts=True)
        except Exception:
            pass

    return result


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


def fetch_corp_name(corp_id: int) -> str:
    data = esi_get_json(f"/corporations/{corp_id}/")
    return data.get("name") or ""


def fetch_alliance_name(alliance_id: int) -> str:
    data = esi_get_json(f"/alliances/{alliance_id}/")
    # alliances endpoint is a list in some contexts, but /alliances/{id}/ is an object with "name"
    return data.get("name") or ""


def get_entity_names_cached(
    entity_type: str, ids: Iterable[int], fetch_cap: int = 40
) -> dict[int, str]:
    """
    Cache for corp/alliance IDs -> names.
    entity_type: "corp" or "alliance"
    """
    et = (entity_type or "").strip().lower()
    ids2 = [int(x) for x in set(ids) if x]
    if not ids2:
        return {}

    cached = EsiEntityCache.objects.filter(entity_type=et, entity_id__in=ids2)
    result = {int(row.entity_id): row.name for row in cached}

    missing = [i for i in ids2 if i not in result]
    if not missing:
        return result

    to_fetch = missing[: max(0, int(fetch_cap))]
    new_rows = []

    for eid in to_fetch:
        try:
            if et == "corp":
                name = fetch_corp_name(int(eid))
            elif et == "alliance":
                name = fetch_alliance_name(int(eid))
            else:
                continue

            if name:
                result[int(eid)] = name
                new_rows.append(
                    EsiEntityCache(entity_type=et, entity_id=int(eid), name=name)
                )
        except Exception:
            pass

    if new_rows:
        try:
            EsiEntityCache.objects.bulk_create(new_rows, ignore_conflicts=True)
        except Exception:
            pass

    return result
