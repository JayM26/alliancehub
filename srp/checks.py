"""
Auto-check computation for SRP claims.

Central place for the reviewer "auto-checks" (blue-on-blue, corp mismatch,
non-alliance victim, NPC involvement) so the review queue and the claim detail
page compute them the same way — views orchestrate, this module computes.

Tri-state model (Cluster A, "absent != zero/clean"): every check reports one of
  - WARN  : computed and it looks wrong (renders red/amber)
  - CLEAN : computed and it looks fine (renders green)
  - INFO  : computed, informational, not alarming (renders neutral)
  - NA    : could NOT be computed — config missing or data absent (renders
            neutral). NEVER a green check and NEVER a silent pass.

The whole point is that an empty ``SRPConfig`` (no blue lists, no self-alliance)
must show a *neutral* "not configured" badge, not a green "all clear" that a
reviewer mistakes for "checked & safe".
"""

from __future__ import annotations


class CheckResult:
    """One auto-check outcome: a tri-state (plus INFO) + a human label."""

    WARN = "WARN"
    CLEAN = "CLEAN"
    INFO = "INFO"
    NA = "NA"

    def __init__(self, state: str, label: str, *, warn_class: str = "warning"):
        self.state = state
        self.label = label
        # Bootstrap contextual suffix used when state == WARN (some checks are
        # amber "warning", the more serious ones — e.g. non-alliance victim —
        # are red "danger").
        self.warn_class = warn_class

    @property
    def badge_class(self) -> str:
        """Bootstrap ``text-bg-*`` suffix for this state (display metadata)."""
        if self.state == self.WARN:
            return self.warn_class
        if self.state == self.CLEAN:
            return "success"
        # INFO / NA both render neutral.
        return "secondary"

    @property
    def is_warn(self) -> bool:
        return self.state == self.WARN

    @property
    def is_clean(self) -> bool:
        return self.state == self.CLEAN

    @property
    def is_neutral(self) -> bool:
        return self.state in (self.INFO, self.NA)

    @property
    def is_na(self) -> bool:
        return self.state == self.NA

    def __repr__(self):  # pragma: no cover - debug aid
        return f"CheckResult({self.state}, {self.label!r})"


def _int_set(values) -> set[int]:
    """Coerce a JSON list of ids into a set of ints, dropping non-numerics."""
    return set(int(x) for x in (values or []) if str(x).isdigit())


def blue_check(claim, cfg) -> CheckResult:
    """
    Blue-on-blue detection.

    NA when no blue alliance/corp list is configured (can't check) OR when the
    killmail has no attacker data. CLEAN only when we actually checked real
    attackers against a real blue list and found none.
    """
    blue_alliance_ids = _int_set(getattr(cfg, "blue_alliance_ids", None)) if cfg else set()
    blue_corp_ids = _int_set(getattr(cfg, "blue_corp_ids", None)) if cfg else set()

    if not blue_alliance_ids and not blue_corp_ids:
        return CheckResult(CheckResult.NA, "Blue check: not configured")

    km = claim.killmail_raw or {}
    attackers = km.get("attackers") or []
    if not attackers:
        return CheckResult(CheckResult.NA, "Blue check: no attacker data")

    for a in attackers:
        if not a.get("character_id"):
            continue  # NPC — handled by npc_check
        alliance_id = a.get("alliance_id")
        corp_id = a.get("corporation_id")
        if (alliance_id and int(alliance_id) in blue_alliance_ids) or (
            corp_id and int(corp_id) in blue_corp_ids
        ):
            return CheckResult(CheckResult.WARN, "Blue involved")

    return CheckResult(CheckResult.CLEAN, "No blues detected")


def corp_mismatch_check(claim) -> CheckResult:
    """
    Submitter corp vs victim corp.

    NA when either side's corp id is unknown (no linked main character, or ESI
    victim corp missing) — the check simply can't run, which must NOT read as a
    green "corps match".
    """
    km = claim.killmail_raw or {}
    victim = km.get("victim") or {}
    victim_corp_id = victim.get("corporation_id")

    submitter_corp_id = None
    mc = getattr(claim.submitter, "main_character", None)
    if mc:
        submitter_corp_id = getattr(mc, "corporation_id", None)

    if not victim_corp_id or not submitter_corp_id:
        return CheckResult(CheckResult.NA, "Corp check: not available")

    if int(victim_corp_id) != int(submitter_corp_id):
        return CheckResult(CheckResult.WARN, "Corp mismatch (submitter vs victim)")
    return CheckResult(CheckResult.CLEAN, "Corps match")


def non_tnt_check(claim, cfg) -> CheckResult:
    """
    Victim-not-in-our-alliance detection.

    NA when our own alliance id(s) aren't configured, or the victim has no
    alliance id — never a green "victim is in TNT" on an unconfigured install.
    """
    self_alliance_ids = _int_set(getattr(cfg, "self_alliance_ids", None)) if cfg else set()

    if not self_alliance_ids:
        return CheckResult(
            CheckResult.NA, "TNT check: not configured", warn_class="danger"
        )

    km = claim.killmail_raw or {}
    victim = km.get("victim") or {}
    victim_alliance_id = victim.get("alliance_id")

    if not victim_alliance_id:
        return CheckResult(
            CheckResult.NA, "TNT check: victim has no alliance", warn_class="danger"
        )

    if int(victim_alliance_id) not in self_alliance_ids:
        return CheckResult(CheckResult.WARN, "Victim not in TNT", warn_class="danger")
    return CheckResult(CheckResult.CLEAN, "Victim is in TNT", warn_class="danger")


def claim_auto_checks(claim, cfg) -> dict[str, CheckResult]:
    """Bundle the config/data-dependent auto-checks for a claim."""
    return {
        "blue": blue_check(claim, cfg),
        "corp": corp_mismatch_check(claim),
        "non_tnt": non_tnt_check(claim, cfg),
    }
