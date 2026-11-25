from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from games.services.metadata_enrichment import enrich_catalog_metadata, enrich_steam_metadata


class Command(BaseCommand):
    help = "Refresh shared IGDB metadata for synchronized Steam apps or GameHub catalog games."
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument("--catalog", action="store_true", help="Process GameHub catalog IGDB IDs instead of Steam AppIDs.")
        parser.add_argument("--dry-run", action="store_true", help="Query IGDB without saving the results.")
        parser.add_argument("--force", action="store_true", help="Recheck mappings and metadata before they expire.")
        parser.add_argument("--limit", type=int, help="Maximum distinct IDs to process after skipping fresh records.")

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit is not None and limit <= 0:
            raise CommandError("Limit must be a positive integer.")

        dry_run = options["dry_run"]
        catalog = options["catalog"]
        database = str(connection.settings_dict["NAME"])
        if connection.vendor == "sqlite" and database != ":memory:" and not database.startswith("file:"):
            database = str(Path(database).resolve())
        self.stdout.write(f"Database: {database}")
        self.stdout.flush()

        enrich = enrich_catalog_metadata if catalog else enrich_steam_metadata
        summary = enrich(limit=limit, force=options["force"], dry_run=dry_run)
        mode = "Dry run" if dry_run else "Enrichment"
        scope = "catalog IGDB IDs" if catalog else "Steam AppIDs"
        details = (
            f"Without valid IGDB ID: {summary.without_igdb_id}. " if catalog
            else f"Ambiguous: {summary.ambiguous}. Conflicts: {summary.conflict}. "
        )
        self.stdout.write(
            f"{mode}: {summary.total} unique {scope}. "
            f"{'Would enrich' if dry_run else 'Enriched'}: {summary.enriched}. "
            f"Skipped: {summary.skipped}. Missing: {summary.missing}. "
            f"{details}"
            f"Failed: {summary.failed}. Deferred: {summary.deferred}."
        )
        if summary.retry_after_seconds is not None:
            self.stderr.write(f"IGDB rate limit reached. Retry after {summary.retry_after_seconds} seconds.")
        for issue in summary.issues:
            if issue.warning:
                self.stderr.write(f"Warning: {issue.describe()} Metadata can be refreshed later.")
            else:
                retry = "yes" if issue.retryable else "after the underlying error is corrected"
                self.stderr.write(f"Failed: {issue.describe()} Retry: {retry}.")
        if summary.failed:
            raise CommandError("One or more IGDB lookups failed.")
