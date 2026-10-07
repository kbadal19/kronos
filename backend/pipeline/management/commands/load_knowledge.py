"""
Load a structured knowledge file: entities, how they connect, and who the
speaker is. The richer sibling of seed_entities.

    python manage.py load_knowledge knowledge/primary.json --user badal
    ... | python manage.py load_knowledge - --user badal     # from stdin

The file is personal (people, clients, opinions), so the deploy never ships
it; on the server it's piped in over stdin and never touches disk.

Shape:
    speaker_notes  str           -> TranscriptionProfile.speaker_notes
    entities       [{kind, title, aliases, misheard_as, summary, body, weight}]
    relations      [[from_title, to_title], ...]   -> part_of edges
    clients        [title, ...]                     -> metadata.client_of
    clients_of     title of the company they're clients of

`summary` goes into every transcription prompt — keep it to a few factual
words. `body` is for later retrieval and never sent per note. `weight`
seeds mention_count, which orders the capped Deepgram keyterm list.

Safe to re-run: entities are matched by kind + title (case-insensitive),
the file wins for summary/body, aliases are merged, and mention_count only
ever goes up.
"""
import json
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from graph.models import Edge, Node
from pipeline.keyterms import invalidate_keyterms
from pipeline.models import CorrectionPair, TranscriptionProfile

CLIENT_KEY = "client_of"


class Command(BaseCommand):
    help = "Load entities, part_of relations and speaker notes from a JSON knowledge file."

    def add_arguments(self, parser):
        parser.add_argument("path", help="JSON file, or - for stdin")
        parser.add_argument("--user", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, path, user, dry_run, **options):
        try:
            owner = get_user_model().objects.get(username=user)
        except get_user_model().DoesNotExist:
            raise CommandError(f"No user {user!r}")

        try:
            data = json.load(sys.stdin if path == "-" else open(path))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Can't read {path}: {exc}")

        valid_kinds = {k.value for k in Node.Kind if k in Node.SEMANTIC_KINDS}

        with transaction.atomic():
            nodes, created, updated = {}, 0, 0
            for item in data.get("entities", []):
                kind, title = item.get("kind"), (item.get("title") or "").strip()
                if kind not in valid_kinds or not title:
                    raise CommandError(f"Bad entity {item!r}: kind must be one of {sorted(valid_kinds)}")

                node = Node.objects.filter(
                    user=owner, kind=kind, title__iexact=title, merged_into__isnull=True
                ).first()
                aliases = [a.strip() for a in item.get("aliases", []) if a.strip()]
                weight = int(item.get("weight", 1))

                if node is None:
                    node = Node.objects.create(
                        user=owner, kind=kind, title=title, aliases=aliases,
                        summary=item.get("summary", ""), body=item.get("body", ""),
                        provenance=Node.Provenance.MANUAL, pinned_by_user=True,
                        mention_count=max(weight, 1),
                    )
                    created += 1
                else:
                    node.aliases = list(dict.fromkeys([*node.aliases, *aliases]))
                    node.summary = item.get("summary", node.summary)
                    node.body = item.get("body", node.body)
                    node.mention_count = max(node.mention_count, weight)
                    node.state = Node.State.ACTIVE
                    node.save(update_fields=["aliases", "summary", "body", "mention_count", "state"])
                    updated += 1
                nodes[title.lower()] = node

                for wrong in item.get("misheard_as", []):
                    CorrectionPair.objects.get_or_create(user=owner, wrong=wrong.strip(), right=title)

            def find(title):
                node = nodes.get(title.lower()) or Node.objects.filter(
                    user=owner, title__iexact=title, kind__in=Node.SEMANTIC_KINDS,
                    merged_into__isnull=True,
                ).first()
                if node is None:
                    raise CommandError(f"Relation refers to unknown entity {title!r}")
                return node

            edges = 0
            for pair in data.get("relations", []):
                source, target = find(pair[0]), find(pair[1])
                _, made = Edge.objects.get_or_create(
                    from_node=source, relation=Edge.Relation.PART_OF, to_node=target,
                    defaults={"provenance": Node.Provenance.MANUAL, "pinned_by_user": True},
                )
                edges += made

            clients = data.get("clients", [])
            if clients and not data.get("clients_of"):
                raise CommandError("'clients' needs 'clients_of': whose clients they are")
            agency = find(data["clients_of"]) if clients else None
            for title in clients:
                node = find(title)
                node.metadata = {**node.metadata, CLIENT_KEY: agency.title}
                node.save(update_fields=["metadata"])

            notes = data.get("speaker_notes")
            if notes:
                profile, _ = TranscriptionProfile.objects.get_or_create(user=owner)
                profile.speaker_notes = notes
                profile.save(update_fields=["speaker_notes", "updated_at"])

            if dry_run:
                transaction.set_rollback(True)

        if not dry_run:
            invalidate_keyterms(owner.id)
            from pipeline.tasks import embed_user_entities

            embed_user_entities.delay(owner.id)

        verb = "Would load" if dry_run else "Loaded"
        self.stdout.write(self.style.SUCCESS(
            f"{verb}: {created} new entities, {updated} updated, {edges} new part_of edges, "
            f"{len(data.get('clients', []))} clients tagged"
            + (", speaker notes set" if notes else "")
        ))
