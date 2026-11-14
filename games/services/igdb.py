
import time

import requests
from django.conf import settings


IGDB_API_URL = "https://api.igdb.com/v4"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"

_access_token = None
_token_expires_at = 0


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

def get_games(limit=10, offset=0):
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Limit must be between 1 and 100.")

    if not isinstance(offset, int) or not 0 <= offset <= 10000:
        raise ValueError("Offset must be between 0 and 10000.")

    token = get_access_token()

    headers = {
        "Client-ID": settings.IGDB_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }

    query = f"""
        fields name, summary, first_release_date,
               cover.image_id,
               genres.name,
               involved_companies.company.name,
               involved_companies.developer,
               total_rating,
               total_rating_count;
        where total_rating_count > 20
              & cover != null
              & first_release_date != null;
        sort total_rating_count desc;
        limit {limit};
        offset {offset};
    """

    response = requests.post(
        f"{IGDB_API_URL}/games",
        headers=headers,
        data=query,
        timeout=15,
    )

    response.raise_for_status()
    return response.json()

def search_games(name, limit=5):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Game name cannot be empty.")

    if not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("Limit must be between 1 and 20.")

    token = get_access_token()

    headers = {
        "Client-ID": settings.IGDB_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }

    # Escape characters used by IGDB search strings
    safe_name = name.strip().replace("\\", "\\\\").replace('"', '\\"')

    query = f"""
        search "{safe_name}";
        fields name, slug, first_release_date,
               cover.image_id, genres.name,
               involved_companies.company.name,
               involved_companies.developer;
        limit {limit};
    """

    response = requests.post(
        f"{IGDB_API_URL}/games",
        headers=headers,
        data=query,
        timeout=15,
    )

    response.raise_for_status()
    return response.json()


def get_game_by_id(igdb_id):
    if not isinstance(igdb_id, int) or igdb_id <= 0:
        raise ValueError("Invalid IGDB game ID.")

    token = get_access_token()

    headers = {
        "Client-ID": settings.IGDB_CLIENT_ID,
        "Authorization": f"Bearer {token}",
    }

    query = f"""
        fields name, summary, first_release_date,
               cover.image_id,
               genres.name,
               involved_companies.company.name,
               involved_companies.developer;
        where id = {igdb_id};
        limit 1;
    """

    response = requests.post(
        f"{IGDB_API_URL}/games",
        headers=headers,
        data=query,
        timeout=15,
    )

    response.raise_for_status()

    games = response.json()

    if not games:
        return None

    return games[0]

