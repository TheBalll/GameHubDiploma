
from datetime import datetime, timezone

from django.db import IntegrityError, transaction

from games.models import Game
from games.services.igdb import get_game_by_id


def import_game_from_igdb(igdb_id):
    # Check if the game already exists in SQLite.
    existing_game = Game.objects.filter(
        igdb_id=igdb_id
    ).first()

    if existing_game:
        return existing_game, False

    # Get the game information from IGDB.
    data = get_game_by_id(igdb_id)

    if data is None:
        return None, False

    # Extract game genres.
    genres = [
        genre["name"]
        for genre in data.get("genres", [])
        if genre.get("name")
    ]

    genre_text = ", ".join(genres) or "Unknown"

    # Find the developer.
    developer = "Unknown"

    for company in data.get("involved_companies", []):
        if company.get("developer"):
            developer = company.get(
                "company", {}
            ).get("name", "Unknown")
            break

    # Extract release year.
    release_timestamp = data.get("first_release_date")

    if release_timestamp:
        release_year = datetime.fromtimestamp(
            release_timestamp,
            tz=timezone.utc
        ).year
    else:
        release_year = 0

    # Build the IGDB image URL.
    cover = data.get("cover") or {}
    image_id = cover.get("image_id")

    background_image = ""

    if image_id:
        background_image = (
            "https://images.igdb.com/igdb/image/upload/"
            f"t_cover_big/{image_id}.jpg"
        )

    # Save the game only if its IGDB ID is not present.
    try:
        with transaction.atomic():
            game, created = Game.objects.get_or_create(
                igdb_id=igdb_id,
                defaults={
                    "title": data.get("name", "Unknown")[:200],
                    "description": data.get("summary") or "No description available.",
                    "genre": genre_text[:100],
                    "release_year": release_year,
                    "developer": developer[:150],
                    "background_image": background_image,
                }
            )
    except IntegrityError:
        game = Game.objects.get(igdb_id=igdb_id)
        created = False

    return game, created
