from django.conf import settings  # pyright: ignore[reportMissingModuleSource]
from django.core.validators import (  # pyright: ignore[reportMissingModuleSource]
    MinValueValidator,
)
from django.db import models  # pyright: ignore[reportMissingModuleSource]
from django.utils import timezone  # pyright: ignore[reportMissingModuleSource]

User = settings.AUTH_USER_MODEL


class ShipPayout(models.Model):
    """Master payout table per ship, by category."""

    ship_name = models.CharField(max_length=100, unique=True)

    strategic = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    peacetime = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    shitstack = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    tnt_special = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )

    hull_contract = models.BooleanField(default=False)  # if True: give hull vs ISK
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ship_name"]
        permissions = [
            ("can_manage_srp_payouts", "Can manage SRP payouts"),
        ]

    def __str__(self):
        return self.ship_name

    def payout_for_category(self, category: str):
        mapping = {
            "STRATEGIC": self.strategic,
            "PEACETIME": self.peacetime,
            "SHITSTACK": self.shitstack,
            "TNT_SPECIAL": self.tnt_special,
        }
        return mapping.get((category or "").strip().upper(), 0)


class EsiTypeCache(models.Model):
    """
    Cache of EVE type_id -> name (modules, ships, ammo, rigs, etc.)
    """

    type_id = models.BigIntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["type_id"]

    def __str__(self):
        return f"{self.type_id} - {self.name}"


class EsiEntityCache(models.Model):
    """
    Cache for corp/alliance IDs -> name (lightweight).
    """

    entity_type = models.CharField(max_length=16)  # "corp" or "alliance"
    entity_id = models.BigIntegerField(db_index=True)
    name = models.CharField(max_length=255)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("entity_type", "entity_id")]
        indexes = [models.Index(fields=["entity_type", "entity_id"])]

    def __str__(self):
        return f"{self.entity_type}:{self.entity_id} - {self.name}"


class SRPConfig(models.Model):
    """One-row configuration for ceilings and behavior."""

    monthly_ceiling_peacetime = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    monthly_ceiling_strategic = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )
    auto_calculate_payouts = models.BooleanField(default=True)
    default_multiplier = models.DecimalField(max_digits=6, decimal_places=2, default=1)
    self_alliance_ids = models.JSONField(default=list, blank=True)
    blue_alliance_ids = models.JSONField(default=list, blank=True)
    blue_corp_ids = models.JSONField(default=list, blank=True)

    def __str__(self):
        return "SRP Configuration"

    @classmethod
    def get(cls):
        return cls.objects.first() or cls.objects.create()


class SRPClaim(models.Model):
    """User-submitted SRP claim (loss)."""

    class Category(models.TextChoices):
        STRATEGIC = "STRATEGIC", "Strategic"
        PEACETIME = "PEACETIME", "Peacetime"
        SHITSTACK = "SHITSTACK", "Shitstack"
        TNT_SPECIAL = "TNT_SPECIAL", "TNT Special"
        MANUAL = "MANUAL", "Manual"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        DENIED = "DENIED", "Denied"
        PAID = "PAID", "Paid"

    # -------------------------
    # Submitter / ownership
    # -------------------------
    submitter = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="srp_claims"
    )
    character_name = models.CharField(
        max_length=100,
        help_text="Character name reported by submitter (may differ from victim)",
    )

    # -------------------------
    # Ship / payout
    # -------------------------
    ship = models.ForeignKey(
        ShipPayout,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="claims",
        help_text="Resolved ShipPayout (manual or ESI)",
    )

    category = models.CharField(max_length=20, choices=Category.choices)

    isk_loss = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    payout_amount = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        blank=True,
        null=True,
    )

    # -------------------------
    # Killmail / ESI input
    # -------------------------
    esi_link = models.URLField(help_text="Killmail / ESI link")

    killmail_id = models.BigIntegerField(null=True, blank=True)
    killmail_hash = models.CharField(max_length=255, null=True, blank=True)
    killmail_raw = models.JSONField(null=True, blank=True)

    # -------------------------
    # Victim (from ESI)
    # -------------------------
    victim_character_id = models.BigIntegerField(null=True, blank=True)
    victim_character_name = models.CharField(max_length=255, null=True, blank=True)

    ship_type_id = models.BigIntegerField(null=True, blank=True)
    ship_name = models.CharField(max_length=255, null=True, blank=True)

    solar_system_id = models.BigIntegerField(null=True, blank=True)
    solar_system_name = models.CharField(max_length=255, null=True, blank=True)

    # -------------------------
    # Location (legacy/manual)
    # -------------------------
    system = models.CharField(max_length=100, blank=True, null=True)
    region = models.CharField(max_length=100, blank=True, null=True)

    # -------------------------
    # Additional claim data
    # -------------------------
    broadcast_text = models.TextField(
        blank=True,
        help_text="Fleet broadcast text (required for some categories)",
    )

    fit_data = models.JSONField(
        blank=True,
        null=True,
        help_text="Optional ship fitting data (future use)",
    )

    # -------------------------
    # Fit checker
    # -------------------------
    fitcheck_status = models.CharField(max_length=30, blank=True, db_index=True)
    fitcheck_best_fit = models.ForeignKey(
        "DoctrineFit",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="best_for_claims",
    )
    fitcheck_selected_fit = models.ForeignKey(
        "DoctrineFit",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="selected_for_claims",
    )
    fitcheck_data = models.JSONField(null=True, blank=True)
    fitcheck_updated_at = models.DateTimeField(null=True, blank=True)
    no_rigs_flag = models.BooleanField(default=False, db_index=True)

    # -------------------------
    # Review / processing
    # -------------------------
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )

    reviewer = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_srp_claims",
    )

    note = models.TextField(
        blank=True,
        help_text="Latest reviewer note (full history in ClaimReview)",
    )

    submitted_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(
        blank=True, null=True
    )  # approve/deny/paid timestamp
    paid_at = models.DateTimeField(null=True, blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)  # reviewer edits timestamp

    @staticmethod
    def canonical_category(value: str | None) -> str:
        return (value or "").strip().upper()

    @classmethod
    def category_label(cls, value: str | None) -> str:
        key = cls.canonical_category(value)
        # choices is list[(value, label)]
        return dict(cls.Category.choices).get(key, key or "—")

    def calculate_payout(self):
        """
        Computes payout from ShipPayout + claim category.
        (Manual is intentionally excluded; manual payout is reviewer-entered.)
        """
        cat = self.canonical_category(self.category)
        if not self.ship:
            return 0
        return self.ship.payout_for_category(cat) or 0

    def set_status(self, new_status: str, reviewer=None, note: str = ""):
        ns = (new_status or "").strip().upper()
        self.status = ns

        if reviewer:
            self.reviewer = reviewer

        # processed_at is only for approve/deny/paid; cleared when returning to pending
        if ns in {self.Status.APPROVED, self.Status.DENIED, self.Status.PAID}:
            self.processed_at = timezone.now()
        elif ns == self.Status.PENDING:
            self.processed_at = None

        if note:
            self.note = (self.note + "\n" if self.note else "") + note

    def save(self, *args, **kwargs):
        # Enforce canonical storage (prevents "Manual" ever living in the DB)
        self.category = self.canonical_category(self.category)
        self.status = (self.status or "").strip().upper()

        cfg = SRPConfig.get()

        # Payout policy: always derived unless Manual
        if cfg.auto_calculate_payouts and self.category != self.Category.MANUAL:
            self.payout_amount = self.calculate_payout()

        super().save(*args, **kwargs)

    def __str__(self):
        return f"SRPClaim #{self.id} - {self.character_name}"

    class Meta:
        indexes = [
            models.Index(fields=["status", "category"]),
            models.Index(fields=["submitted_at"]),
            models.Index(fields=["character_name"]),
        ]
        ordering = ["-submitted_at"]
        permissions = [
            ("can_review_srp", "Can review SRP claims"),
            ("can_view_srp_reports", "Can view SRP reports"),
        ]

    def clean(self):
        from django.core.exceptions import (  # pyright: ignore[reportMissingModuleSource]
            ValidationError,
        )

        cat = self.canonical_category(self.category)

        # Only enforce broadcast requirement for user-submitted categories
        if (
            cat in {self.Category.STRATEGIC, self.Category.PEACETIME}
            and not (self.broadcast_text or "").strip()
        ):
            raise ValidationError(
                "Broadcast/Op Post is required for Strategic or Peacetime claims."
            )
        if self.category == self.Category.MANUAL and not self.reviewer:
            raise ValidationError("Manual category can only be set by SRP reviewers.")


class ClaimReview(models.Model):
    """Optional: keep a trail of actions."""

    claim = models.ForeignKey(
        SRPClaim, on_delete=models.CASCADE, related_name="reviews"
    )
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE)
    action = models.CharField(max_length=50)  # Approved / Denied / Paid / Edited
    comment = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.claim_id} - {self.action} by {self.reviewer}"


class PayoutImportJob(models.Model):
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(default=timezone.now)
    csv_text = models.TextField()
    original_filename = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"PayoutImportJob #{self.id} ({self.created_at:%Y-%m-%d %H:%M})"


# srp/models.py


class DoctrineFit(models.Model):
    """
    One stored doctrine fit for one ship hull (multiple fits per hull allowed).
    Imported from EFT text; only High/Mid/Low/Rigs are used for matching.
    """

    ship_type_id = models.BigIntegerField(db_index=True)
    ship_name = models.CharField(max_length=255, blank=True)

    name = models.CharField(
        max_length=255
    )  # keep full fit name, e.g. "TigersClaw - DPS - V25.1"
    eft_text = models.TextField(help_text="Original EFT text for this fit.")

    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_doctrine_fits",
    )

    class Meta:
        ordering = ["ship_name", "name"]
        indexes = [
            models.Index(fields=["ship_type_id", "active"]),
        ]

    def __str__(self):
        return f"{self.ship_name or self.ship_type_id} — {self.name}"


class DoctrineFitItem(models.Model):
    class SlotGroup(models.TextChoices):
        HIGH = "HIGH", "High"
        MID = "MID", "Mid"
        LOW = "LOW", "Low"
        RIG = "RIG", "Rig"

    doctrine_fit = models.ForeignKey(
        DoctrineFit, on_delete=models.CASCADE, related_name="items"
    )
    slot_group = models.CharField(max_length=10, choices=SlotGroup.choices)
    type_id = models.BigIntegerField(db_index=True)
    type_name = models.CharField(max_length=255, blank=True)
    qty = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["doctrine_fit_id", "slot_group", "type_name", "type_id"]
        indexes = [
            models.Index(fields=["doctrine_fit", "slot_group"]),
            models.Index(fields=["type_id"]),
        ]

    def __str__(self):
        return f"{self.doctrine_fit_id} {self.slot_group}: {self.type_name or self.type_id} x{self.qty}"
