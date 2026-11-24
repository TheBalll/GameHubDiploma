from django.core.management.base import BaseCommand, CommandError

from games.services.metadata_enrichment import enrich_steam_metadata


class Command(BaseCommand):
    help = "Refresh shared IGDB metadata for the synchronized Steam AppIDs."
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Query IGDB without saving the results.")
        parser.add_argument("--force", action="store_true", help="Recheck mappings and metadata before they expire.")
        parser.add_argument("--limit", type=int, help="Maximum distinct AppIDs to process after skipping fresh records.")

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit is not None and limit <= 0:
            raise CommandError("Limit must be a positive integer.")

        dry_run = options["dry_run"]
        summary = enrich_steam_metadata(limit=limit, force=options["force"], dry_run=dry_run)
        mode = "Dry run" if dry_run else "Enrichment"
        self.stdout.write(
            f"{mode}: {summary.total} unique AppIDs. "
            f"{'Would enrich' if dry_run else 'Enriched'}: {summary.enriched}. "
            f"Skipped: {summary.skipped}. Missing: {summary.missing}. "
            f"Ambiguous: {summary.ambiguous}. Conflicts: {summary.conflict}. "
            f"Failed: {summary.failed}. Deferred: {summary.deferred}."
        )
        if summary.retry_after_seconds is not None:
            self.stderr.write(f"IGDB rate limit reached. Retry after {summary.retry_after_seconds} seconds.")
        if summary.failed:
            raise CommandError("One or more IGDB lookups failed.")
