from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import log1p

from django.db.models import F

from games.models import Game, IgdbGameMetadata, LibraryEntry, Review, SteamIgdbIdentity, SteamOwnedGame


RATING_WEIGHT = 0.70
STATUS_WEIGHT = 0.20
PLAYTIME_WEIGHT = 0.10
PLAYTIME_CAP_MINUTES = 40 * 60
STATUS_SIGNALS = {"playing": 0.50, "completed": 0.75, "plan_to_play": 0.0, "dropped": -1.0}
MIN_GAMES_PER_FEATURE = 2
SCORE_EPSILON = 1e-12

IGDB_FEATURES = ("genres", "themes", "game_modes", "keywords")
FEATURES = (*IGDB_FEATURES, "catalog_genres")
GENRE_PLACEHOLDERS = {"unknown", "steam game"}

GameIdentity = tuple[str, int]


@dataclass(frozen=True)
class GamePreference:
    identity: GameIdentity
    gamehub_ids: tuple[int, ...]
    steam_app_ids: tuple[int, ...]
    owned_on_steam: bool
    playtime_minutes: int | None
    library_status: str | None
    has_review: bool
    rating: int | None
    score: float
    genres: tuple[int, ...]
    themes: tuple[int, ...]
    game_modes: tuple[int, ...]
    keywords: tuple[int, ...]
    catalog_genres: tuple[str, ...]


@dataclass(frozen=True)
class FeaturePreferences:
    supporting_games: int = 0
    positive: dict[int | str, float] = field(default_factory=dict)
    negative: dict[int | str, float] = field(default_factory=dict)
    strengths: dict[int | str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class UserPreferenceProfile:
    games: tuple[GamePreference, ...] = ()
    genres: FeaturePreferences = field(default_factory=FeaturePreferences)
    themes: FeaturePreferences = field(default_factory=FeaturePreferences)
    game_modes: FeaturePreferences = field(default_factory=FeaturePreferences)
    keywords: FeaturePreferences = field(default_factory=FeaturePreferences)
    catalog_genres: FeaturePreferences = field(default_factory=FeaturePreferences)

    @property
    def has_preferences(self) -> bool:
        return any(getattr(self, feature).supporting_games >= MIN_GAMES_PER_FEATURE for feature in FEATURES)


@dataclass
class _GameEvidence:
    gamehub_ids: set[int] = field(default_factory=set)
    steam_app_ids: set[int] = field(default_factory=set)
    genre_labels: set[str] = field(default_factory=set)
    owned_on_steam: bool = False
    playtime_minutes: int | None = None
    library_status: str | None = None
    has_review: bool = False
    rating: int | None = None


def _game_score(evidence):
    rating = evidence.rating
    rating_signal = 0.0
    if rating is not None:
        if rating <= 4:
            rating_signal = (rating - 6) / 5
        elif rating >= 7:
            rating_signal = (rating - 5) / 5

    playtime_signal = log1p(min(evidence.playtime_minutes or 0, PLAYTIME_CAP_MINUTES)) / log1p(PLAYTIME_CAP_MINUTES)
    status_signal = STATUS_SIGNALS.get(evidence.library_status, 0.0)
    return RATING_WEIGHT * rating_signal + STATUS_WEIGHT * status_signal + PLAYTIME_WEIGHT * playtime_signal


def _identity(igdb_id=None, app_id=None, gamehub_id=None):
    if igdb_id:
        return ("igdb", igdb_id)
    if app_id:
        return ("steam", app_id)
    return ("gamehub", gamehub_id)


def _load_catalog_links(steam_games, personal_game_ids):
    catalog = Game.objects.only("steam_id", "igdb_id", "genre")
    catalog_games = catalog.in_bulk(personal_game_ids | {game_id for _, game_id, _ in steam_games if game_id})
    steam_apps = {app_id for app_id, _, _ in steam_games}
    games_by_app = {game.steam_id: game for game in catalog_games.values() if game.steam_id}
    games_by_app.update(catalog.in_bulk(steam_apps - games_by_app.keys(), field_name="steam_id"))
    mappings = SteamIgdbIdentity.objects.filter(status=SteamIgdbIdentity.Status.MATCHED).only("metadata")
    igdb_by_app = {
        app_id: mapping.metadata_id
        for app_id, mapping in mappings.in_bulk(steam_apps | games_by_app.keys()).items()
    }

    links = {}
    linked_counts = Counter(game_id for _, game_id, _ in steam_games if game_id)
    for app_id, linked_id, _ in steam_games:
        exact_game = games_by_app.get(app_id)
        if exact_game:
            links[app_id] = exact_game.pk
            catalog_games[exact_game.pk] = exact_game
            continue

        # Reuse a stored link only when no other AppID or confirmed IGDB ID contradicts it.
        linked_game = catalog_games.get(linked_id)
        mapped_igdb_id = igdb_by_app.get(app_id)
        if (
            linked_game and not linked_game.steam_id and linked_counts[linked_id] == 1
            and (not mapped_igdb_id or not linked_game.igdb_id or linked_game.igdb_id == mapped_igdb_id)
        ):
            links[app_id] = linked_id

    return catalog_games, links, igdb_by_app


def _collect_evidence(library, reviews, steam_games):
    personal_game_ids = {game_id for game_id, _ in library} | {game_id for game_id, _ in reviews}
    catalog, links, igdb_by_app = _load_catalog_links(steam_games, personal_game_ids)
    linked_apps = {game_id: app_id for app_id, game_id in links.items()}
    evidence = defaultdict(_GameEvidence)
    game_keys = {}
    for game_id in personal_game_ids | linked_apps.keys():
        game = catalog[game_id]
        app_id = game.steam_id or linked_apps.get(game_id)
        # An explicit catalog identity takes precedence over inconsistent cached metadata.
        key = _identity(game.igdb_id or igdb_by_app.get(app_id), app_id, game_id)
        game_keys[game_id] = key
        item = evidence[key]
        item.gamehub_ids.add(game_id)
        if app_id:
            item.steam_app_ids.add(app_id)
        item.genre_labels.update(label.strip().casefold() for label in game.genre.split(","))

    for game_id, status in library:
        item = evidence[game_keys[game_id]]
        if item.library_status is None:
            item.library_status = status

    for game_id, rating in reviews:
        item = evidence[game_keys[game_id]]
        item.has_review = True
        if item.rating is None and 1 <= rating <= 10:
            item.rating = rating

    for app_id, _, minutes in steam_games:
        key = game_keys.get(links.get(app_id)) or _identity(igdb_by_app.get(app_id), app_id)
        item = evidence[key]
        item.steam_app_ids.add(app_id)
        item.owned_on_steam = True
        if minutes is not None and (item.playtime_minutes is None or minutes > item.playtime_minutes):
            item.playtime_minutes = minutes

    return evidence


def _feature_ids(metadata, feature):
    values = getattr(metadata, feature, None)
    if not isinstance(values, list):
        return ()
    return tuple(sorted({value for value in values if type(value) is int and 0 < value <= 2**63 - 1}))


def _build_games(evidence):
    igdb_ids = [value for kind, value in evidence if kind == "igdb"]
    metadata_by_id = IgdbGameMetadata.objects.only(*IGDB_FEATURES).in_bulk(igdb_ids)
    for key, item in sorted(evidence.items()):
        kind, value = key
        metadata = metadata_by_id.get(value) if kind == "igdb" else None
        features = {feature: _feature_ids(metadata, feature) for feature in IGDB_FEATURES}
        fallback_genres = () if features["genres"] else tuple(sorted(item.genre_labels - GENRE_PLACEHOLDERS - {""}))
        yield GamePreference(
            identity=key,
            gamehub_ids=tuple(sorted(item.gamehub_ids)),
            steam_app_ids=tuple(sorted(item.steam_app_ids)),
            owned_on_steam=item.owned_on_steam,
            playtime_minutes=item.playtime_minutes,
            library_status=item.library_status,
            has_review=item.has_review,
            rating=item.rating,
            score=_game_score(item),
            catalog_genres=fallback_genres,
            **features,
        )


def _feature_preferences(games, feature):
    positive, negative = Counter(), Counter()
    positive_mass, negative_mass = Counter(), Counter()
    positive_count, negative_count = Counter(), Counter()
    supporting_games = 0
    mass = total_positive = total_negative = 0.0
    for game in games:
        values = getattr(game, feature)
        if not values or not game.score:
            continue
        supporting_games += 1
        weight = abs(game.score)
        mass += weight
        if game.score > 0:
            total_positive += weight
            legacy, tag_mass, tag_count = positive, positive_mass, positive_count
        else:
            total_negative += weight
            legacy, tag_mass, tag_count = negative, negative_mass, negative_count
        for value in values:
            legacy[value] += weight / len(values)
            tag_mass[value] += weight
            tag_count[value] += 1

    if supporting_games < MIN_GAMES_PER_FEATURE:
        return FeaturePreferences(supporting_games)

    strengths = {}
    for value in sorted(positive_mass.keys() | negative_mass.keys()):
        liked_share = positive_mass[value] / total_positive if total_positive else 0.0
        disliked_share = negative_mass[value] / total_negative if total_negative else 0.0
        difference = liked_share - disliked_share
        if abs(difference) > SCORE_EPSILON:
            count = positive_count[value] if difference > 0 else negative_count[value]
            strengths[value] = difference * min(1.0, count / MIN_GAMES_PER_FEATURE)

    # Preserve the original aggregates for candidate discovery and Catalog Suggestions.
    scale = max(1.0, mass)
    return FeaturePreferences(
        supporting_games=supporting_games,
        positive={value: positive[value] / scale for value in sorted(positive)},
        negative={value: negative[value] / scale for value in sorted(negative)},
        strengths=strengths,
    )


def build_user_preference_profile(user) -> UserPreferenceProfile:
    """Build a profile from the user's local data and shared IGDB metadata."""
    if not user.is_authenticated or user.pk is None:
        return UserPreferenceProfile()

    library = list(
        LibraryEntry.objects.filter(user_id=user.pk)
        .order_by(F("added_at").desc(nulls_last=True), "-pk").values_list("game_id", "status")
    )
    reviews = list(
        Review.objects.filter(user_id=user.pk)
        .order_by("-created_at", "-pk").values_list("game_id", "rating")
    )
    steam_games = list(
        SteamOwnedGame.objects.filter(steam_account__user_id=user.pk, steam_app_id__gt=0)
        .values_list("steam_app_id", "game_id", "playtime_minutes")
    )
    games = tuple(_build_games(_collect_evidence(library, reviews, steam_games)))
    return UserPreferenceProfile(games=games, **{feature: _feature_preferences(games, feature) for feature in FEATURES})
