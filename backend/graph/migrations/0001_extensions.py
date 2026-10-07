"""Postgres extensions the graph depends on.

Split into its own migration, ahead of any table, because both are
server-side capabilities rather than schema: `vector` backs the embedding
column and its HNSW index, `pg_trgm` backs the fuzzy-match rung of the
entity resolution ladder (exact -> alias -> fuzzy -> vector -> LLM).

Needs a superuser connection the first time. On the managed-nothing setup
in docker-compose.yml the default role is superuser, so this just works;
on a hosted Postgres you may have to `CREATE EXTENSION` by hand and fake
this migration.
"""
from django.contrib.postgres.operations import CreateExtension, TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        # Django's CreateExtension rather than pgvector's VectorExtension
        # wrapper: as of pgvector 0.4.1 that subclass overrides __init__
        # without calling super(), so it never sets self.hints, and
        # Django 6.0's database_forwards reads self.hints on every
        # operation — AttributeError before a single table is created.
        # This does exactly the same CREATE EXTENSION without the bug.
        CreateExtension("vector"),
        TrigramExtension(),
    ]
