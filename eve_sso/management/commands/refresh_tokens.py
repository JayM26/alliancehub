from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from eve_sso.models import EveCharacter
from eve_sso.utils import refresh_access_token


class Command(BaseCommand):
    help = "Refresh EVE Online access tokens for characters nearing expiration."

    def handle(self, *args, **options):
        now = timezone.now()
        threshold = now + timedelta(minutes=5)
        characters = EveCharacter.objects.filter(token_expiry__lte=threshold)

        if not characters.exists():
            self.stdout.write(self.style.SUCCESS("✅ No tokens need refreshing."))
            return

        self.stdout.write(f"Refreshing tokens for {characters.count()} character(s)...")

        for char in characters:
            self.stdout.write(f" → {char.character_name}")
            refreshed = refresh_access_token(char)
            if refreshed:
                self.stdout.write(
                    self.style.SUCCESS(f"    Token refreshed successfully.")
                )
            else:
                self.stdout.write(self.style.ERROR(f"    Failed to refresh token."))

        self.stdout.write(self.style.SUCCESS("✅ Token refresh process completed."))
