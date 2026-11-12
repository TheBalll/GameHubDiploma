
from datetime import datetime, timezone

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from games.models import Game
from games.services.igdb import get_games


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

            try:
                games = get_games(
                    limit=current_limit,
                    offset=processed,
                )
            except (requests.RequestException, KeyError, ValueError) as error:
                raise CommandError(
                    f"IGDB request failed at offset {processed}: {error}"
                ) from error

            if not games:
                self.stdout.write(
                    self.style.WARNING("No more IGDB results.")
                )
                break

            for data in games:
                igdb_id = data.get("id")
                title = data.get("name")

                if not igdb_id or not title:
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
                if not release_timestamp:
                    skipped_count += 1
                    continue

                release_year = datetime.fromtimestamp(
                    release_timestamp,
                    tz=timezone.utc,
                ).year

                image_id = (data.get("cover") or {}).get("image_id")
                image_url = (
                    "https://images.igdb.com/igdb/image/upload/"
                    f"t_cover_big/{image_id}.jpg"
                    if image_id
                    else ""
                )

                try:
                    with transaction.atomic():
                        game = Game.objects.filter(
                            igdb_id=igdb_id
                        ).first()

                        if game:
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
