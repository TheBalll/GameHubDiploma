
from datetime import datetime, timezone

from django.db import IntegrityError, transaction

from games.models import Game
from games.services.igdb import get_game_by_id
from games.services.steam import get_steam_game_by_id


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


def import_game_from_steam(steam_id):
    if not isinstance(steam_id, int) or steam_id <= 0:
        raise ValueError("Invalid Steam game ID.")

    # Check if this Steam game already exists.
    existing_game = Game.objects.filter(
        steam_id=steam_id
    ).first()

    if existing_game:
        return existing_game, False

    # Get game details from Steam.
    data = get_steam_game_by_id(steam_id)

    if data is None:
        return None, False

    title = (data.get("name") or "Unknown")[:200]

    # Extract genres.
    genres = [
        genre.get("description")
        for genre in data.get("genres", [])
        if genre.get("description")
    ]
    genre_text = ", ".join(genres)[:100] or "Unknown"

    # Extract developer.
    developers = data.get("developers") or []
    developer = (
        developers[0][:150]
        if developers
        else "Unknown"
    )

    # Extract release year.
    release_year = 0
    release_data = data.get("release_date") or {}
    release_text = release_data.get("date") or ""

    if not release_data.get("coming_soon"):
        import re
        match = re.search(r"\b(?:19|20)\d{2}\b", release_text)
        if match:
            release_year = int(match.group())

    description = (
        data.get("short_description")
        or "No description available."
    )

    image = data.get("header_image") or ""

    # Check for an existing game with matching metadata.
    # Only link records when title, release year and
    # developer all match.
    if release_year and developer != "Unknown":
        matching_games = Game.objects.filter(
            title__iexact=title,
            release_year=release_year,
            developer__iexact=developer,
            steam_id__isnull=True,
        )

        if matching_games.count() == 1:
            game = matching_games.first()

            game.steam_id = steam_id
            game.save(update_fields=["steam_id"])

            return game, False

    # Create a new game if no reliable match exists.
    try:
        with transaction.atomic():
            game, created = Game.objects.get_or_create(
                steam_id=steam_id,
                defaults={
                    "title": title,
                    "description": description,
                    "genre": genre_text,
                    "release_year": release_year,
                    "developer": developer,
                    "background_image": image[:500],
                }
            )
    except IntegrityError:
        game = Game.objects.get(steam_id=steam_id)
        created = False

    return game, created
