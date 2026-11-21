
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from django.conf import settings


STEAM_OWNED_GAMES_URL = (
    "https://api.steampowered.com/"
    "IPlayerService/GetOwnedGames/v1/"
)


class SteamSyncError(Exception):
    pass


def get_owned_games(steam_id):
    # Read the API key from server-side settings
    api_key = settings.STEAM_API_KEY

    if not api_key:
        raise SteamSyncError(
            "Steam API key is not configured."
        )

    # Request the user's available Steam games
    parameters = {
        "steamid": steam_id,
        "include_appinfo": 1,
        "include_played_free_games": 1,
        "format": "json",
    }

    url = STEAM_OWNED_GAMES_URL + "?" + urlencode(parameters)

    # Keep the API key outside the URL
    request = Request(
        url,
        headers={
            "x-webapi-key": api_key,
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=15) as response:
            data = json.load(response)

    except HTTPError:
        raise SteamSyncError(
            "Steam rejected the API request. "
            "Check the Steam API key."
        )

    except (URLError, TimeoutError, OSError):
        raise SteamSyncError(
            "Could not connect to Steam. Please try again."
        )

    except (ValueError, UnicodeError):
        raise SteamSyncError(
            "Steam returned an invalid response."
        )

    # Validate the API response before importing games
    if not isinstance(data, dict):
        raise SteamSyncError(
            "Steam returned an unexpected response."
        )

    result = data.get("response")

    if not isinstance(result, dict):
        raise SteamSyncError(
            "Steam library data is unavailable."
        )

    # An empty response may indicate private Game details
    if "game_count" not in result:
        raise SteamSyncError(
            "Steam did not share your library. "
            "Check your Steam Game details privacy settings."
        )

    games = result.get("games", [])

    if not isinstance(games, list):
        raise SteamSyncError(
            "Steam returned an invalid games list."
        )

    return games
