import re
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
from itertools import islice

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone

from games.models import IgdbGameMetadata

from .igdb import igdb_cover_url, igdb_release_year, query_igdb
from .metadata_enrichment import (
    API_ERRORS,
    GAME_METADATA_TTL,
    ID_LIST_FIELDS,
    RATING_FIELDS,
    _fresh_metadata,
    _metadata_values,
    _retry_after_seconds,
)
from .preference_profile import IGDB_FEATURES, _feature_ids
from .steam_igdb_mapping import _is_positive_id


DEFAULT_EXTERNAL_LIMIT = 24
MAX_EXTERNAL_LIMIT = 48
MAX_SIMILAR_SEEDS = 5
MAX_TAGS_PER_FEATURE = 3
DISCOVERY_CACHE_SECONDS = 24 * 60 * 60
CACHE_PREFIX = "igdb-recommendations:v1:"
RATE_LIMIT_KEY = CACHE_PREFIX + "retry-after"
DISPLAY_FIELDS = (
    "id", "name", "first_release_date", "cover.image_id", "genres.name",
    "themes.name", "game_modes.name", "franchises", "external_games.uid", "external_games.external_game_source.name",
)
SCORING_FIELDS = (
    "keywords", "similar_games",
    *RATING_FIELDS, *(f"{field}_count" for field in RATING_FIELDS),
)


@dataclass(frozen=True)
class ExternalIgdbGame:
    igdb_id: int
    title: str
    release_year: int | None
    cover_image_id: str | None
    genres: tuple[str, ...]
    steam_app_ids: tuple[int, ...]
    metadata: IgdbGameMetadata | None
    genre_names_by_id: tuple[tuple[int, str], ...] = ()
    feature_names: tuple[tuple[str, int, str], ...] = ()
    franchise_ids: tuple[int, ...] = ()


def _id_list(ids):
    return "(" + ",".join(map(str, ids)) + ")"


def _steam_app_ids(external_games):
    app_ids = set()
    if not isinstance(external_games, list):
        return ()
    for entry in external_games:
        source = entry.get("external_game_source") if isinstance(entry, dict) else None
        name = source.get("name") if isinstance(source, dict) else None
        if not isinstance(name, str) or name.strip().casefold() != "steam":
            continue
        uid = entry.get("uid")
        if isinstance(uid, str) and re.fullmatch(r"[0-9]{1,19}", uid) and _is_positive_id(int(uid)):
            app_ids.add(int(uid))
    return tuple(sorted(app_ids))


def _named_features(values):
    if not isinstance(values, list):
        return (), {}
    ids, names = set(), {}
    for value in values:
        feature_id = value.get("id") if isinstance(value, dict) else value
        if not _is_positive_id(feature_id):
            continue
        ids.add(feature_id)
        name = value.get("name") if isinstance(value, dict) else None
        if isinstance(name, str) and name.strip():
            names[feature_id] = name.strip()
    return tuple(sorted(ids)), names


def _external_game(row, *, include_scoring):
    title = row.get("name")
    if not isinstance(title, str) or not title.strip():
        return None

    genres, names_by_id = _named_features(row.get("genres"))
    raw_genres = row.get("genres")
    labels = {}
    for genre in raw_genres if isinstance(raw_genres, list) else ():
        name = genre.get("name") if isinstance(genre, dict) else None
        if isinstance(name, str) and name.strip():
            labels.setdefault(name.strip().casefold(), name.strip())
    values = {}
    feature_names = []
    for feature in ("genres", "themes", "game_modes"):
        ids, names = (genres, names_by_id) if feature == "genres" else _named_features(row.get(feature))
        if ids or (include_scoring and feature != "genres" and row.get(feature) == []):
            values[feature] = list(ids)
        if feature != "genres":
            feature_names.extend((feature, feature_id, name) for feature_id, name in sorted(names.items()))

    cover = row.get("cover")
    image_id = cover.get("image_id") if isinstance(cover, dict) else None
    if include_scoring:
        # One malformed optional field should not invalidate the rest of a card.
        for field in ("keywords", "similar_games"):
            if isinstance(row.get(field), list):
                valid = sorted({item for item in row[field] if _is_positive_id(item)})
                if valid or not row[field]:
                    values[field] = valid
        for field in RATING_FIELDS:
            count_field = f"{field}_count"
            try:
                values.update(_metadata_values({field: row.get(field), count_field: row.get(count_field)}))
            except (TypeError, ValueError, ValidationError):
                continue

    return ExternalIgdbGame(
        igdb_id=row["id"],
        title=title.strip(),
        release_year=igdb_release_year(row.get("first_release_date")),
        cover_image_id=image_id if igdb_cover_url(image_id) else None,
        genres=tuple(labels[key] for key in sorted(labels)),
        steam_app_ids=_steam_app_ids(row.get("external_games")),
        metadata=IgdbGameMetadata(igdb_id=row["id"], **values) if values else None,
        genre_names_by_id=tuple(sorted(names_by_id.items())),
        feature_names=tuple(feature_names),
        franchise_ids=_named_features(row.get("franchises"))[0],
    )


def _scoring_metadata(card, stored, now):
    if not card.metadata or not stored:
        return card.metadata or stored
    preferred, fallback = (stored, card.metadata) if _fresh_metadata(stored, now) else (card.metadata, stored)
    values = {}
    for field in ID_LIST_FIELDS:
        value = getattr(preferred, field)
        values[field] = value if value is not None else getattr(fallback, field)
    for field in RATING_FIELDS:
        count_field = f"{field}_count"
        rating, count = getattr(preferred, field), getattr(preferred, count_field)
        if count is None or (count and rating is None):
            rating, count = getattr(fallback, field), getattr(fallback, count_field)
        values.update({field: rating, count_field: count})
    return IgdbGameMetadata(igdb_id=card.igdb_id, cover_image_id=stored.cover_image_id, **values)


class _IgdbBatch:
    def __init__(self):
        self.failed = False

    def games(self, query, limit):
        if self.failed or cache.get(RATE_LIMIT_KEY):
            return None
        try:
            rows = query_igdb("games", query)
            if not isinstance(rows, list) or len(rows) > limit:
                raise ValueError("Unexpected IGDB candidate response.")
            return rows
        except API_ERRORS as error:
            self.failed = True
            response = getattr(error, "response", None)
            if response is not None and response.status_code == 429:
                delay = _retry_after_seconds(response.headers.get("Retry-After"), timezone.now())
                cache.set(RATE_LIMIT_KEY, True, timeout=max(1, delay))
            return None


def _preferred_filters(profile):
    filters = []
    for feature in IGDB_FEATURES:
        preferences = getattr(profile, feature)
        positive = {
            value: weight - preferences.negative.get(value, 0)
            for value, weight in preferences.positive.items()
            if _is_positive_id(value) and weight > preferences.negative.get(value, 0)
        }
        values = sorted(positive, key=lambda value: (-positive[value], value))[:MAX_TAGS_PER_FEATURE]
        if values:
            filters.append(f"{feature} = {_id_list(sorted(values))}")
    return filters


def _discovered_ids(filters, limit, api):
    query = f"fields id,total_rating_count; where ({' | '.join(filters)}); sort total_rating_count desc; limit {limit};"
    key = CACHE_PREFIX + "discovery:" + sha256(query.encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached
    rows = api.games(query, limit)
    if rows is None:
        return ()
    votes_by_id = {}
    for row in rows:
        if isinstance(row, dict) and _is_positive_id(row.get("id")):
            igdb_id = row["id"]
            count = row.get("total_rating_count")
            votes_by_id[igdb_id] = max(votes_by_id.get(igdb_id, 0), count if _is_positive_id(count) else 0)
    ids = tuple(sorted(votes_by_id, key=lambda igdb_id: (-votes_by_id[igdb_id], igdb_id)))
    if len(ids) == len(rows):
        cache.set(key, ids, timeout=DISCOVERY_CACHE_SECONDS)
    return ids


def _fetch_cards(ids, api, *, include_scoring):
    if not ids:
        return {}
    fields = (*DISPLAY_FIELDS, *SCORING_FIELDS) if include_scoring else DISPLAY_FIELDS
    query = f"fields {','.join(fields)}; where id = {_id_list(sorted(ids))}; limit {len(ids)};"
    rows = api.games(query, len(ids))
    cards = {}
    seen = set()
    for row in rows or ():
        if not isinstance(row, dict) or not _is_positive_id(row.get("id")) or row["id"] not in ids:
            continue
        igdb_id = row["id"]
        if igdb_id in seen:
            cards.pop(igdb_id, None)
            continue
        seen.add(igdb_id)
        card = _external_game(row, include_scoring=include_scoring)
        if card:
            cards[igdb_id] = card
    return cards


def _interleaved_similar_ids(seeds, seed_metadata, reserved_ids):
    pending = deque(iter(_feature_ids(seed_metadata.get(seed), "similar_games")) for seed in seeds)
    selected = {}
    while pending:
        links = pending.popleft()
        candidate = next((tag for tag in links if tag not in reserved_ids and tag not in selected), None)
        if candidate is not None:
            selected[candidate] = None
            pending.append(links)
    return selected


def discover_external_candidates(profile, known_sources, seed_metadata, reserved_ids, *, limit):
    """Return bounded public cards and stored metadata, without catalog or user writes."""
    filters = _preferred_filters(profile)
    seeds = sorted(known_sources, key=lambda igdb_id: (-known_sources[igdb_id].score, igdb_id))[:MAX_SIMILAR_SEEDS]
    similar_ids = _interleaved_similar_ids(seeds, seed_metadata, reserved_ids)
    similar_limit = (limit + 1) // 2 if filters else limit
    candidate_ids = list(islice(similar_ids, similar_limit))
    api = _IgdbBatch()
    if filters and len(candidate_ids) < limit:
        discovered = _discovered_ids(filters, limit, api)
        candidate_ids.extend(islice(
            (igdb_id for igdb_id in discovered if igdb_id not in reserved_ids and igdb_id not in similar_ids),
            limit - len(candidate_ids),
        ))
    selected = set(candidate_ids)
    candidate_ids.extend(islice((igdb_id for igdb_id in similar_ids if igdb_id not in selected),
                                limit - len(candidate_ids)))
    if not candidate_ids:
        return ()

    stored = IgdbGameMetadata.objects.in_bulk(candidate_ids)
    keys = {igdb_id: CACHE_PREFIX + f"card:{igdb_id}" for igdb_id in candidate_ids}
    cached = cache.get_many(keys.values())
    cards = {igdb_id: cached[key] for igdb_id, key in keys.items() if key in cached}
    missing = keys.keys() - cards.keys()
    now = timezone.now()
    fresh = {igdb_id for igdb_id in missing if _fresh_metadata(stored.get(igdb_id), now)}
    fetched = _fetch_cards(fresh, api, include_scoring=False)
    fetched.update(_fetch_cards(missing - fresh, api, include_scoring=True))
    if fetched:
        cache.set_many({keys[igdb_id]: card for igdb_id, card in fetched.items()},
                       timeout=int(GAME_METADATA_TTL.total_seconds()))
        cards.update(fetched)
    return tuple(
        (cards[igdb_id], _scoring_metadata(cards[igdb_id], stored.get(igdb_id), now))
        for igdb_id in candidate_ids if igdb_id in cards
    )
