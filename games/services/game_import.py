
from django.db import IntegrityError, transaction
from django.utils import timezone

from games.models import Game
from games.services.igdb import get_game_by_id, igdb_cover_url, igdb_release_year
from games.services.metadata_enrichment import save_imported_metadata
from games.services.steam import get_steam_game_by_id


def fetch_igdb_game_defaults(igdb_id):
    """Fetch catalog values and retain recommendation metadata from the same response."""
    started_at = timezone.now()
    data = get_game_by_id(igdb_id)
    if data is None:
        return None
    if not isinstance(data, dict) or type(data.get("id")) is not int or data["id"] != igdb_id:
        raise ValueError("IGDB returned an unexpected game identity.")
    title = data.get("name")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("IGDB returned a game without a name.")

    raw_genres = data.get("genres")
    raw_genres = raw_genres if isinstance(raw_genres, list) else []
    genres = [
        genre["name"].strip() for genre in raw_genres
        if isinstance(genre, dict) and isinstance(genre.get("name"), str) and genre["name"].strip()
    ]
    genre_text = ", ".join(genres) or "Unknown"

    developer = "Unknown"
    companies = data.get("involved_companies")
    if isinstance(companies, list):
        for involvement in companies:
            if isinstance(involvement, dict) and involvement.get("developer"):
                company = involvement.get("company")
                name = company.get("name") if isinstance(company, dict) else None
                if isinstance(name, str) and name.strip():
                    developer = name.strip()
                    break

    cover = data.get("cover")
    image_id = cover.get("image_id") if isinstance(cover, dict) else None
    description = data.get("summary")
    save_imported_metadata([data], started_at)
    return {
        "title": title.strip()[:200],
        "description": description if isinstance(description, str) and description.strip()
        else "No description available.",
        "genre": genre_text[:100],
        "release_year": igdb_release_year(data.get("first_release_date")) or 0,
        "developer": developer[:150],
        "background_image": igdb_cover_url(image_id) or "",
    }


def import_game_from_igdb(igdb_id):
    existing_game = Game.objects.filter(igdb_id=igdb_id).first()
    if existing_game:
        return existing_game, False

    defaults = fetch_igdb_game_defaults(igdb_id)
    if defaults is None:
        return None, False
    return Game.objects.get_or_create(igdb_id=igdb_id, defaults=defaults)


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
