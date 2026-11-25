
import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from games.models import Game
from games.services.igdb import get_games, igdb_cover_url, igdb_release_year
from games.services.metadata_enrichment import save_imported_metadata
from games.services.steam_igdb_mapping import _is_positive_id


class Command(BaseCommand):
    help = "Import popular games from IGDB into SQLite"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Number of IGDB results to process (1-200)",
        )

    def handle(self, *args, **options):
        limit = options["limit"]

        if not 1 <= limit <= 200:
            raise CommandError("Limit must be between 1 and 200.")

        created_count = 0
        linked_count = 0
        skipped_count = 0
        processed = 0
        batch_size = 50

        while processed < limit:
            current_limit = min(batch_size, limit - processed)
            started_at = timezone.now()

            try:
                games = get_games(
                    limit=current_limit,
                    offset=processed,
                )
            except (requests.RequestException, KeyError, TypeError, ValueError):
                raise CommandError(
                    f"IGDB request failed at offset {processed}."
                ) from None

            if not games:
                self.stdout.write(
                    self.style.WARNING("No more IGDB results.")
                )
                break

            metadata_rows = []
            for data in games:
                if not isinstance(data, dict):
                    skipped_count += 1
                    continue
                igdb_id = data.get("id")
                title = data.get("name")

                if not _is_positive_id(igdb_id) or not isinstance(title, str) or not title.strip():
                    skipped_count += 1
                    continue

                genres = data.get("genres") or []
                genre = ", ".join(
                    item["name"]
                    for item in genres
                    if item.get("name")
                )[:100]

                companies = data.get("involved_companies") or []
                developers = [
                    item["company"]["name"]
                    for item in companies
                    if item.get("developer")
                    and item.get("company", {}).get("name")
                ]
                developer = ", ".join(developers)[:150]

                release_timestamp = data.get("first_release_date")
                release_year = igdb_release_year(release_timestamp) if release_timestamp else None
                if not release_year:
                    skipped_count += 1
                    continue

                cover = data.get("cover")
                image_id = cover.get("image_id") if isinstance(cover, dict) else None
                image_url = igdb_cover_url(image_id) or ""

                try:
                    with transaction.atomic():
                        game = Game.objects.filter(
                            igdb_id=igdb_id
                        ).first()

                        if game:
                            metadata_rows.append(data)
                            skipped_count += 1
                            continue

                        # Only consider exact titles with no IGDB ID.
                        matches = Game.objects.filter(
                            title__iexact=title,
                            igdb_id__isnull=True,
                        )

                        if matches.exists():
                            # Do not guess which IGDB edition matches.
                            skipped_count += 1
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Needs manual review: {title}"
                                )
                            )
                            continue

                        game = Game.objects.create(
                            igdb_id=igdb_id,
                            title=title[:200],
                            description=data.get("summary") or "",
                            genre=genre,
                            release_year=release_year,
                            developer=developer,
                            background_image=image_url,
                        )
                    metadata_rows.append(data)
                    created_count += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Imported: {game.title}"
                        )
                    )

                except IntegrityError as error:
                    skipped_count += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipped {title}: {error}"
                        )
                    )

            save_imported_metadata(metadata_rows, started_at)
            processed += len(games)

            self.stdout.write(
                f"Processed {processed}/{limit} IGDB results"
            )

            if len(games) < current_limit:
                break

        self.stdout.write(
            self.style.SUCCESS(
                f"Finished. Imported: {created_count}, "
                f"Linked: {linked_count}, "
                f"Skipped: {skipped_count}"
            )
        )
