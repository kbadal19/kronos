"""
Seed the semantic node table before your first capture.

The keyterm flywheel has a cold-start problem: biasing is built from
entities you already have, so on day one there are none and the recognizer
gets no help at all. That's backwards, because the transcripts from your
first weeks are precisely the ones that become your eval set and the
corpus you tune extraction against — the moment accuracy matters most is
the moment the system is least able to help itself.

Ten minutes writing down the proper nouns you already know you'll say
fixes it. Run before Phase 0 capture:

    python manage.py seed_entities --user badal entities.txt

Format, one per line, blank lines and `#` comments ignored:

    project: Project CB | CB | the observability project
    company: Credence Marketings | Credence
    technology: masterflow
    concept: RCA | root cause analysis
    habit: Gym

The first name is canonical, the rest are aliases. Re-running is safe: an
existing entity gains any new aliases rather than being duplicated.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from graph.models import Node
from pipeline.keyterms import invalidate_keyterms


class Command(BaseCommand):
    help = "Seed semantic nodes (entities) so keyterm biasing works from the first capture."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Text file of entities, one per line")
        parser.add_argument("--user", required=True, help="Username to attach them to")
        parser.add_argument(
            "--dry-run", action="store_true", help="Show what would happen, change nothing"
        )

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(username=options["user"])
        except User.DoesNotExist:
            raise CommandError(f"No user named {options['user']!r}")

        verbosity = options.get("verbosity", 1)

        def say(message):
            if verbosity:
                self.stdout.write(message)

        valid_kinds = {k.value for k in Node.Kind if k in Node.SEMANTIC_KINDS}

        created = updated = 0
        with open(options["path"]) as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                if ":" not in line:
                    raise CommandError(
                        f"{options['path']}:{lineno}: expected 'kind: Name | alias', got {line!r}"
                    )

                kind, _, rest = line.partition(":")
                kind = kind.strip().lower()
                if kind not in valid_kinds:
                    raise CommandError(
                        f"{options['path']}:{lineno}: {kind!r} is not a semantic kind. "
                        f"Valid: {', '.join(sorted(valid_kinds))}"
                    )

                names = [p.strip() for p in rest.split("|") if p.strip()]
                if not names:
                    raise CommandError(f"{options['path']}:{lineno}: no name given")

                title, aliases = names[0], names[1:]

                existing = Node.objects.filter(
                    user=user, kind=kind, title__iexact=title, merged_into__isnull=True
                ).first()

                if existing:
                    merged = list(dict.fromkeys([*existing.aliases, *aliases]))
                    if merged != existing.aliases:
                        added = len(merged) - len(existing.aliases)
                        if not options["dry_run"]:
                            existing.aliases = merged
                            existing.save(update_fields=["aliases"])
                        updated += 1
                        say(f"  update  {title} (+{added} alias{'es' if added != 1 else ''})")
                    continue

                if not options["dry_run"]:
                    Node.objects.create(
                        user=user,
                        kind=kind,
                        title=title,
                        aliases=aliases,
                        provenance=Node.Provenance.MANUAL,
                        # Seeded by hand, so it survives any future
                        # reprocessing run untouched.
                        pinned_by_user=True,
                        # Deliberately 1, not 0: mention_count orders the
                        # keyterm list, and a seed you have never actually
                        # said should still outrank nothing at all.
                        mention_count=1,
                    )
                created += 1
                say(f"  create  {kind}: {title}" + (f" ({', '.join(aliases)})" if aliases else ""))

        if not options["dry_run"]:
            invalidate_keyterms(user.id)

        verb = "Would seed" if options["dry_run"] else "Seeded"
        say(self.style.SUCCESS(f"{verb} {created} new, {updated} updated."))
