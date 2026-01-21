from __future__ import annotations


def slot_group_from_flag(flag: int, *, extended: bool = False) -> str | None:
    """
    Map EVE killmail item flags to slot groups.

    If extended=False (default):
      - returns only High/Mid/Low/Rigs, otherwise None

    If extended=True:
      - includes Cargo, Drone Bay, and returns "Other" for anything else
    """
    if 27 <= flag <= 34:
        return "High Slots"
    if 19 <= flag <= 26:
        return "Mid Slots"
    if 11 <= flag <= 18:
        return "Low Slots"
    if 92 <= flag <= 94:
        return "Rigs"

    if not extended:
        return None

    if flag == 5:
        return "Cargo"
    if flag == 87:
        return "Drone Bay"
    return "Other"
