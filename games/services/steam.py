
import requests


STEAM_SEARCH_URL = (
    "https://store.steampowered.com/api/storesearch/"
)


def search_steam_games(name, limit=5):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Game name cannot be empty.")

    if not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("Limit must be between 1 and 20.")

    response = requests.get(
        STEAM_SEARCH_URL,
        params={
            "term": name.strip(),
            "l": "english",
            "cc": "us",
        },
        timeout=15,
    )

    response.raise_for_status()

    data = response.json()

    games = []

    for item in data.get("items", [])[:limit]:
        steam_id = item.get("id")
        title = item.get("name")

        if not steam_id or not title:
            continue

        games.append({
            "id": steam_id,
            "title": title,
            "image": item.get("tiny_image", ""),
            "source": "steam",
        })

    return games

def get_steam_game_by_id(steam_id):
    if not isinstance(steam_id, int) or steam_id <= 0:
        raise ValueError("Invalid Steam game ID.")

    response = requests.get(
        "https://store.steampowered.com/api/appdetails/",
        params={
            "appids": steam_id,
            "l": "english",
            "cc": "us",
        },
        timeout=15,
    )

    response.raise_for_status()

    result = response.json()
    game_data = result.get(str(steam_id), {})

    if not game_data.get("success"):
        return None

    return game_data.get("data")
