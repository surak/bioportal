"""Store a Blablador API key for a user from the command line.

    manage.py setkey --user admin --key-file /path/to/key
    BLABLADOR_API_KEY=... manage.py setkey --user admin --from-env

Reads the key from a file or the environment rather than argv, so it does not
land in shell history or process listings.
"""

from __future__ import annotations

import os
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from jobs.models import ApiCredential


class Command(BaseCommand):
    help = "Store a user's Blablador API key (encrypted)."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True)
        parser.add_argument("--key-file")
        parser.add_argument("--from-env", action="store_true")
        parser.add_argument("--verify", action="store_true",
                            help="Check the key against the service before storing.")

    def handle(self, *args, **opts):
        if opts["key_file"]:
            key = Path(opts["key_file"]).read_text().strip()
        elif opts["from_env"]:
            key = os.environ.get("BLABLADOR_API_KEY", "").strip()
        else:
            raise CommandError("pass --key-file or --from-env")
        if not key:
            raise CommandError("no key found")

        User = get_user_model()
        try:
            user = User.objects.get(username=opts["user"])
        except User.DoesNotExist:
            raise CommandError(f"no such user: {opts['user']}")

        if opts["verify"]:
            from jobs import blablador, registry

            blablador.queue(key, registry.all_specs()[0].alias)
            self.stdout.write("key verified against the service")

        cred = ApiCredential.store(user, key)
        self.stdout.write(
            self.style.SUCCESS(f"stored key {cred.fingerprint} for {user.username}")
        )
