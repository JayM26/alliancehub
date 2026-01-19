# srp/fit_importer.py
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from django.db import transaction  # pyright: ignore[reportMissingModuleSource]

from .models import DoctrineFit, DoctrineFitItem
from .esi import (
    get_type_ids_by_names_cached,
)  # we'll add this helper if you don't already have it


HEADER_RE = re.compile(r"^\[(?P<ship>[^,\]]+)\s*,\s*(?P<name>[^\]]+)\]\s*$")
QTY_RE = re.compile(r"^(?P<name>.+?)\s+x(?P<qty>\d+)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedFit:
    ship_name: str
    fit_name: str
    blocks: list[list[str]]  # blocks of raw lines


def _split_blocks(lines: Iterable[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    cur: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            if cur:
                blocks.append(cur)
                cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def parse_eft_text(eft_text: str) -> ParsedFit:
    lines = [ln.rstrip("\n") for ln in (eft_text or "").splitlines()]
    # find header
    header_idx = None
    ship_name = ""
    fit_name = ""
    for i, ln in enumerate(lines):
        ln = ln.strip()
        if not ln:
            continue
        m = HEADER_RE.match(ln)
        if m:
            header_idx = i
            ship_name = m.group("ship").strip()
            fit_name = m.group("name").strip()
            break
        raise ValueError("EFT header not found. Expected a line like: [Ship, Fit Name]")

    body_lines = lines[header_idx + 1 :]  # type: ignore[arg-type]
    blocks = _split_blocks(body_lines)

    # EFT convention: first four meaningful blocks are Low, Mid, High, Rigs
    if len(blocks) < 4:
        raise ValueError("EFT text didn't contain enough blocks for Low/Mid/High/Rigs.")

    return ParsedFit(ship_name=ship_name, fit_name=fit_name, blocks=blocks)


def _parse_item_line(line: str) -> tuple[str, int]:
    """
    Returns (type_name, qty). Handles 'Item Name x2' lines.
    """
    m = QTY_RE.match(line.strip())
    if m:
        return m.group("name").strip(), int(m.group("qty"))
    return line.strip(), 1


def _block_to_counter(block_lines: list[str]) -> Counter[str]:
    c: Counter[str] = Counter()
    for ln in block_lines:
        name, qty = _parse_item_line(ln)
        if not name:
            continue
        c[name] += qty
    return c


@transaction.atomic
def import_eft_fit(
    *,
    eft_text: str,
    updated_by=None,
    overwrite_fit_id: int | None = None,
) -> DoctrineFit:
    parsed = parse_eft_text(eft_text)
    ship_name_final = parsed.ship_name.strip()

    # Resolve ship type_id automatically
    ship_map = get_type_ids_by_names_cached([ship_name_final], fetch_cap=5)
    ship_type_id = ship_map.get(ship_name_final)
    if not ship_type_id:
        raise ValueError(
            f"Could not resolve ship type_id for ship name: '{ship_name_final}'"
        )

    slot_map = [
        (DoctrineFitItem.SlotGroup.LOW, parsed.blocks[0]),
        (DoctrineFitItem.SlotGroup.MID, parsed.blocks[1]),
        (DoctrineFitItem.SlotGroup.HIGH, parsed.blocks[2]),
        (DoctrineFitItem.SlotGroup.RIG, parsed.blocks[3]),
    ]

    by_slot_names: dict[str, Counter[str]] = {}
    all_names: set[str] = set()
    for slot_group, block in slot_map:
        counter = _block_to_counter(block)
        by_slot_names[slot_group] = counter
        all_names.update(counter.keys())

    name_to_type_id = get_type_ids_by_names_cached(sorted(all_names), fetch_cap=500)

    if overwrite_fit_id:
        fit = DoctrineFit.objects.select_for_update().get(id=overwrite_fit_id)
        fit.ship_type_id = int(ship_type_id)
        fit.ship_name = ship_name_final
        fit.name = parsed.fit_name
        fit.eft_text = eft_text
        fit.active = True
        fit.updated_by = updated_by
        fit.save()
        fit.items.all().delete()
    else:
        fit = DoctrineFit.objects.create(
            ship_type_id=int(ship_type_id),
            ship_name=ship_name_final,
            name=parsed.fit_name,
            eft_text=eft_text,
            active=True,
            updated_by=updated_by,
        )

    bulk: list[DoctrineFitItem] = []
    for slot_group, counter in by_slot_names.items():
        for type_name, qty in counter.items():
            tid = name_to_type_id.get(type_name)
            if not tid:
                # For MVP: skip unknowns rather than failing import
                continue
            bulk.append(
                DoctrineFitItem(
                    doctrine_fit=fit,
                    slot_group=slot_group,
                    type_id=int(tid),
                    type_name=type_name,
                    qty=int(qty),
                )
            )
    DoctrineFitItem.objects.bulk_create(bulk)

    return fit
