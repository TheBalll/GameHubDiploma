import re
import time
from datetime import UTC, datetime
from threading import Lock

import requests
from django.conf import settings


IGDB_API_URL = "https://api.igdb.com/v4"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
ID_LIST_FIELDS = ("genres", "themes", "game_modes", "keywords", "similar_games")
RATING_FIELDS = ("rating", "aggregated_rating", "total_rating")
METADATA_FIELDS = (
    *ID_LIST_FIELDS, *RATING_FIELDS, *(f"{field}_count" for field in RATING_FIELDS),
    "cover.image_id", "updated_at",
)
CATALOG_GAME_FIELDS = ", ".join((
    "id", "name", "summary", "first_release_date", "involved_companies.company.name",
    "involved_companies.developer", *("genres.id, genres.name" if field == "genres" else field for field in METADATA_FIELDS),
))

_access_token = None
_token_expires_at = 0
_query_lock = Lock()
_next_query_at = 0.0
_query_interval = 0.26


def igdb_cover_url(image_id):
    if isinstance(image_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", image_id):
        return f"https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.jpg"
    return None


def igdb_release_year(timestamp):
    if type(timestamp) is int:
        try:
            return datetime.fromtimestamp(timestamp, UTC).year
        except (ValueError, OverflowError, OSError):
            pass
    return None


def get_access_token():
    global _access_token, _token_expires_at

    if _access_token and time.time() < _token_expires_at:
        return _access_token

    response = requests.post(
        TWITCH_TOKEN_URL,
        data={
            "client_id": settings.IGDB_CLIENT_ID,
            "client_secret": settings.IGDB_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )

    response.raise_for_status()
    token_data = response.json()

    _access_token = token_data["access_token"]
    _token_expires_at = (
        time.time() + token_data["expires_in"] - 60
    )

    return _access_token


def query_igdb(endpoint, query):
    """Send an authenticated query, with process-local pacing."""
    global _next_query_at

    with _query_lock:
        token = get_access_token()
        delay = _next_query_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)

        _next_query_at = time.monotonic() + _query_interval
        response = requests.post(
            f"{IGDB_API_URL}/{endpoint}",
            headers={
                "Client-ID": settings.IGDB_CLIENT_ID,
                "Authorization": f"Bearer {token}",
            },
            data=query,
            timeout=15,
        )

    response.raise_for_status()
    return response.json()


def get_games(limit=10, offset=0):
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Limit must be between 1 and 100.")

    if not isinstance(offset, int) or not 0 <= offset <= 10000:
        raise ValueError("Offset must be between 0 and 10000.")

    query = f"""
        fields {CATALOG_GAME_FIELDS};
        where total_rating_count > 20
              & cover != null
              & first_release_date != null;
        sort total_rating_count desc;
        limit {limit};
        offset {offset};
    """

    games = query_igdb("games", query)
    if not isinstance(games, list):
        raise ValueError("IGDB returned an unexpected catalog response.")
    return games


def search_games(name, limit=5):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Game name cannot be empty.")

    if not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("Limit must be between 1 and 20.")

    # Escape special characters
    safe_name = (
        name.strip()
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )

    query = f"""
        search "{safe_name}";
        fields name, slug, first_release_date,
               cover.image_id, genres.name,
               involved_companies.company.name,
               involved_companies.developer;
        limit {limit};
    """

    games = query_igdb("games", query)

    # Normalize the searched title
    search_name = " ".join(
        name.casefold().split()
    )

    # Put exact matches first
    games.sort(
        key=lambda game: (
            " ".join(
                (game.get("name") or "").casefold().split()
            ) != search_name,
            (game.get("name") or "").casefold()
        )
    )

    return games


def get_game_by_id(igdb_id):
    if not isinstance(igdb_id, int) or igdb_id <= 0:
        raise ValueError("Invalid IGDB game ID.")

    query = f"""
        fields {CATALOG_GAME_FIELDS};
        where id = {igdb_id};
        limit 1;
    """

    games = query_igdb("games", query)

    if not isinstance(games, list) or len(games) > 1:
        raise ValueError("IGDB returned an unexpected game response.")
    if not games:
        return None

    return games[0]

