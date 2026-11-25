from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from typing import Literal

from django.db.models import Avg, Count, Exists, OuterRef, Q

from games.models import Game, IgdbGameMetadata, Review, SteamIgdbIdentity

from .igdb import igdb_cover_url
from .igdb_recommendation_candidates import (
    DEFAULT_EXTERNAL_LIMIT,
    MAX_EXTERNAL_LIMIT,
    discover_external_candidates,
)
from .preference_profile import (
    FEATURES,
    GENRE_PLACEHOLDERS,
    GameIdentity,
    SCORE_EPSILON,
    _feature_ids,
    _identity,
    build_user_preference_profile,
)


DEFAULT_LIMIT = 12
FEATURE_WEIGHTS = {"genres": 0.50, "themes": 0.23, "game_modes": 0.17, "keywords": 0.10}
MATCH_FEATURES = ("genres", "themes", "game_modes")
MIN_MATCH_FEATURES = 2
MIN_MATCH_GAMES = 2
MIN_STEAM_ONLY_MATCH_GAMES = 3
MIN_MATCH_PLAYTIME_MINUTES = 120
MIN_SIMILARITY_SEEDS = 2
SIMILAR_GAMES_WEIGHT = 0.10
QUALITY_WEIGHT = 0.05
QUALITY_PRIOR = 0.50
QUALITY_PRIOR_VOTES = 20
MAX_GENERAL_QUALITY_DISCOUNT = 0.50
DIVERSITY_MIN_SCORE_RATIO = 0.95
IGDB_RATING_FIELDS = ("total_rating", "rating", "aggregated_rating")

FEATURE_REASON_CODES = {
    "genres": "preferred_genres",
    "catalog_genres": "preferred_genres",
    "themes": "preferred_themes",
    "game_modes": "preferred_game_modes",
    "keywords": "preferred_keywords",
}


@dataclass(frozen=True)
class RecommendationReason:
    code: str
    feature_ids: tuple[int, ...] = ()
    labels: tuple[str, ...] = ()
    source_igdb_ids: tuple[int, ...] = ()
    established: bool = False


@dataclass(frozen=True)
class LibraryTarget:
    kind: Literal["gamehub", "igdb"]
    id: int


@dataclass(frozen=True)
class RankedGame:
    game_id: int | None
    title: str
    identity: GameIdentity
    ranking_score: float
    match_score: int | None
    reasons: tuple[RecommendationReason, ...]
    igdb_id: int | None
    cover: str | None
    genres: tuple[str, ...]
    genre_ids: tuple[int, ...]
    release_year: int | None
    source: Literal["local", "external"]
    add_to_library: LibraryTarget
    genre_names_by_id: tuple[tuple[int, str], ...] = ()
    steam_app_ids: tuple[int, ...] = ()
    franchise_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class RecommendationResults:
    mode: Literal["personalized", "general"]
    items: tuple[RankedGame, ...]


@dataclass(frozen=True)
class _Candidate:
    identity: GameIdentity
    metadata: IgdbGameMetadata | None
    title: str
    genres: tuple[str, ...]
    cover: str | None
    release_year: int | None
    game_id: int | None = None
    local_rating_samples: tuple[tuple[float, int], ...] = ()
    steam_app_ids: tuple[int, ...] = ()
    genre_names_by_id: tuple[tuple[int, str], ...] = ()
    feature_names: tuple[tuple[str, int, str], ...] = ()
    franchise_ids: tuple[int, ...] = ()

    def ranked(self, score, match_score, reasons):
        kind, value = self.identity
        return RankedGame(
            game_id=self.game_id, title=self.title, identity=self.identity, ranking_score=score,
            match_score=match_score, reasons=reasons, igdb_id=value if kind == "igdb" else None,
            cover=self.cover, genres=self.genres, genre_ids=_feature_ids(self.metadata, "genres"),
            release_year=self.release_year, source="local" if self.game_id is not None else "external",
            add_to_library=LibraryTarget("gamehub", self.game_id) if self.game_id is not None
            else LibraryTarget("igdb", value),
            genre_names_by_id=self.genre_names_by_id,
            steam_app_ids=self.steam_app_ids,
            franchise_ids=self.franchise_ids,
        )


def _catalog_groups(profile):
    catalog = list(Game.objects.only(
        "title", "igdb_id", "steam_id", "genre", "background_image", "release_year",
    ).order_by("pk"))
    app_ids = {game.steam_id for game in catalog if game.steam_id and not game.igdb_id}
    mappings = SteamIgdbIdentity.objects.only("status", "metadata").in_bulk(app_ids)

    excluded_keys = {game.identity for game in profile.games}
    excluded_games = {game_id for game in profile.games for game_id in game.gamehub_ids}
    excluded_apps = {app_id for game in profile.games for app_id in game.steam_app_ids}
    groups = defaultdict(list)
    for game in catalog:
        mapping = mappings.get(game.steam_id)
        mapped_id = mapping.metadata_id if mapping and mapping.status == SteamIgdbIdentity.Status.MATCHED else None
        identity = _identity(game.igdb_id or mapped_id, game.steam_id, game.pk)
        if game.pk in excluded_games or game.steam_id in excluded_apps:
            excluded_keys.add(identity)
        groups[identity].append(game)

    reserved_ids = {value for kind, value in groups.keys() | excluded_keys if kind == "igdb"}
    reserved_apps = excluded_apps | {game.steam_id for game in catalog if game.steam_id}
    eligible = {identity: games for identity, games in groups.items() if identity not in excluded_keys}
    linkable_apps = {
        app_id for kind, app_id in eligible if kind == "steam"
        and (app_id not in mappings or mappings[app_id].status == SteamIgdbIdentity.Status.MISSING)
    }
    return eligible, reserved_ids, reserved_apps, linkable_apps


def _local_rating_samples():
    valid_ratings = Review.objects.filter(rating__range=(1, 10))
    newer_review = valid_ratings.filter(game_id=OuterRef("game_id"), user_id=OuterRef("user_id")).filter(
        Q(created_at__gt=OuterRef("created_at"))
        | Q(created_at=OuterRef("created_at"), pk__gt=OuterRef("pk"))
    )
    rows = (
        valid_ratings.filter(~Exists(newer_review))
        .values("game_id").annotate(average=Avg("rating"), votes=Count("pk"))
        .values_list("game_id", "average", "votes")
    )
    return {game_id: (average / 10, votes) for game_id, average, votes in rows}


def _quality_score(metadata, local_samples):
    samples = []
    if metadata:
        for field in IGDB_RATING_FIELDS:
            rating = getattr(metadata, field)
            votes = getattr(metadata, f"{field}_count")
            if rating is not None and 0 <= rating <= 100 and votes:
                samples.append((rating / 100, votes))
    samples.extend(local_samples)
    # Use one sample because IGDB rating sources can overlap.
    rating, votes = max(samples, default=(QUALITY_PRIOR, 0), key=lambda sample: sample[1])
    return (rating * votes + QUALITY_PRIOR * QUALITY_PRIOR_VOTES) / (votes + QUALITY_PRIOR_VOTES)


def _general_score(quality, penalty):
    # Weak overlap has little effect, while concentrated negative evidence reduces quality more.
    return quality * (1 - MAX_GENERAL_QUALITY_DISCOUNT * penalty ** 2)


def _has_positive_explicit_signal(game):
    return (game.rating is not None and game.rating >= 7) or game.library_status in {"playing", "completed"}


class _PreferenceEvidence:
    def __init__(self, profile):
        self.preferences = {}
        self.reliable_sources = defaultdict(set)
        self.explicit_sources = set()
        self.liked_by_igdb = {}
        for feature in FEATURES:
            preferences = getattr(profile, feature)
            mass = sum(preferences.positive.values()) + sum(preferences.negative.values())
            if mass:
                self.preferences[feature] = (preferences, mass)

        for game in profile.games:
            if game.score <= 0:
                continue
            kind, value = game.identity
            if kind == "igdb":
                self.liked_by_igdb[value] = game
            if _has_positive_explicit_signal(game):
                self.explicit_sources.add(game.identity)
            elif (game.playtime_minutes or 0) < MIN_MATCH_PLAYTIME_MINUTES:
                continue
            for feature in FEATURES:
                for tag in getattr(game, feature):
                    self.reliable_sources[feature, tag].add(game.identity)

    def candidate_features(self, candidate, labels):
        for feature, weight in FEATURE_WEIGHTS.items():
            values = _feature_ids(candidate.metadata, feature)
            channel = feature
            if feature == "genres" and (not values or feature not in self.preferences):
                channel, values = "catalog_genres", tuple(sorted(labels))
            if channel in self.preferences and values:
                preferences, mass = self.preferences[channel]
                yield feature, channel, weight, values, preferences, mass

    def has_reliable_support(self, sources):
        explicit = not self.explicit_sources.isdisjoint(sources)
        return len(sources) >= (MIN_MATCH_GAMES if explicit else MIN_STEAM_ONLY_MATCH_GAMES)


def _compare_content(candidate, evidence, feature_names):
    affinity = 0.0
    reasons = []
    supported_features = 0
    sources = set()
    labels = {label.casefold(): label for label in candidate.genres}
    source_contributions = Counter()
    for feature, channel, weight, values, preferences, _ in evidence.candidate_features(candidate, labels):
        strengths = {value: preferences.strengths.get(value, 0.0) for value in values}
        positive = max(0.0, max(strengths.values()))
        negative = max(0.0, -min(strengths.values()))
        contribution = weight * (positive - negative)
        affinity += contribution
        if positive - negative <= SCORE_EPSILON:
            continue

        matches = [tag for tag in values if abs(strengths[tag] - positive) <= SCORE_EPSILON]
        support = {tag: evidence.reliable_sources.get((channel, tag), ()) for tag in matches}
        witnesses = set().union(*support.values())
        if feature in MATCH_FEATURES and witnesses:
            supported_features += 1
            sources.update(witnesses)

        # Prefer a well-supported, named tag when several tags contribute equally.
        tag = min(matches, key=lambda value: (-len(support[value]), (channel, value) not in feature_names, value))
        for source in witnesses:
            source_contributions[source] += contribution
        label = labels[tag] if channel == "catalog_genres" else feature_names.get((feature, tag))
        reason = RecommendationReason(
            FEATURE_REASON_CODES[channel],
            feature_ids=() if channel == "catalog_genres" else (tag,),
            labels=(label,) if label else (),
            established=evidence.has_reliable_support(support[tag]),
        )
        reasons.append((contribution, reason, support[tag]))

    if affinity <= SCORE_EPSILON:
        return 0.0, None, ()
    match_score = None
    if supported_features >= MIN_MATCH_FEATURES and evidence.has_reliable_support(sources):
        # Remove floating-point noise before rounding exact half percentages.
        match_score = round(round(100 * affinity, 10))
    explanations = []
    for _, reason, support in sorted(
        reasons, key=lambda entry: (-round(entry[0], 12), not entry[1].established, not entry[1].labels),
    ):
        source_ids = sorted(
            (value for kind, value in support if kind == "igdb"),
            key=lambda value: (
                -source_contributions["igdb", value], -evidence.liked_by_igdb[value].score, value,
            ),
        )
        explanations.append(replace(reason, source_igdb_ids=tuple(source_ids[:2])))
    return affinity, match_score, tuple(explanations)


def _similar_sources(known_sources, metadata_by_igdb):
    forward_links = defaultdict(set)
    for igdb_id in known_sources:
        for target_id in _feature_ids(metadata_by_igdb.get(igdb_id), "similar_games"):
            forward_links[target_id].add(igdb_id)
    return forward_links


def _compare_similarity(metadata, igdb_id, known_sources, forward_links, strongest_source):
    if igdb_id is None or not strongest_source:
        return 0.0, ()
    incoming = set(_feature_ids(metadata, "similar_games")) & known_sources.keys()
    related = incoming | forward_links.get(igdb_id, set())
    if not related:
        return 0.0, ()
    # Either direction is valid. Use the strongest seed once even if both directions exist.
    source_id = min(related, key=lambda value: (-known_sources[value].score, value))
    source = known_sources[source_id]
    if source.rating is not None and source.rating >= 7:
        code = "similar_to_enjoyed_games"
    elif (source.playtime_minutes or 0) >= MIN_MATCH_PLAYTIME_MINUTES:
        code = "similar_to_games_with_significant_playtime"
    elif _has_positive_explicit_signal(source):
        code = "similar_to_library_activity"
    else:
        code = "similar_to_played_games"
    return source.score / strongest_source, (RecommendationReason(code, source_igdb_ids=(source_id,)),)


def _local_candidates(groups, metadata_by_igdb):
    local_ratings = _local_rating_samples() if groups else {}
    for identity, games in groups.items():
        kind, value = identity
        metadata = metadata_by_igdb.get(value) if kind == "igdb" else None
        game = min(games, key=lambda item: (not item.igdb_id, item.pk))
        labels = {
            label.strip().casefold(): label.strip()
            for label in game.genre.split(",")
            if label.strip() and label.strip().casefold() not in GENRE_PLACEHOLDERS
        }
        yield _Candidate(
            identity=identity, metadata=metadata, title=game.title, genres=tuple(labels.values()),
            cover=game.background_image or igdb_cover_url(metadata.cover_image_id if metadata else None),
            release_year=game.release_year or None, game_id=game.pk,
            local_rating_samples=tuple(local_ratings[item.pk] for item in games if item.pk in local_ratings),
        )


def _with_external_candidates(local_candidates, cards, reserved_apps, linkable_apps):
    app_counts = Counter(app_id for card, _ in cards for app_id in card.steam_app_ids)
    external = []
    links = {}
    for card, metadata in cards:
        image_id = card.cover_image_id or (metadata.cover_image_id if metadata else None)
        candidate = _Candidate(
            identity=("igdb", card.igdb_id), metadata=metadata, title=card.title, genres=card.genres,
            cover=igdb_cover_url(image_id), release_year=card.release_year, steam_app_ids=card.steam_app_ids,
            genre_names_by_id=card.genre_names_by_id,
            feature_names=card.feature_names, franchise_ids=card.franchise_ids,
        )
        matches = reserved_apps.intersection(card.steam_app_ids)
        if not matches:
            external.append(candidate)
        elif len(matches) == 1 and matches <= linkable_apps:
            app_id = next(iter(matches))
            if app_counts[app_id] == 1:
                links[app_id] = candidate

    # Use a unique AppID match for this ranking only. Stored identities are left to Steam enrichment.
    promoted_apps = set()
    local = []
    for candidate in local_candidates:
        kind, value = candidate.identity
        match = links.get(value) if kind == "steam" else None
        if match:
            promoted_apps.update(match.steam_app_ids)
            candidate = replace(
                candidate, identity=match.identity, metadata=match.metadata, genres=candidate.genres or match.genres,
                cover=candidate.cover or match.cover, release_year=candidate.release_year or match.release_year,
                genre_names_by_id=match.genre_names_by_id,
                feature_names=match.feature_names, franchise_ids=match.franchise_ids,
            )
        local.append(candidate)
    return [*local, *(candidate for candidate in external if not promoted_apps.intersection(candidate.steam_app_ids))]


def _ranked_items(items, limit, *, diversify=False):
    ranked = sorted(items, key=lambda pair: (
        -pair[0].ranking_score, pair[0].title.casefold(), pair[0].game_id is None,
        pair[0].game_id or pair[0].identity[1],
    ))
    seen_app_ids = set()
    unique = []
    for item, app_ids in ranked:
        if seen_app_ids.intersection(app_ids):
            continue
        seen_app_ids.update(app_ids)
        unique.append(item)
    if not diversify:
        return tuple(unique[:limit])

    finalists = [
        (item, {(reason.code, tag) for reason in item.reasons if reason.established
                for tag in (reason.feature_ids or tuple(label.casefold() for label in reason.labels))})
        for item in unique
    ]
    selected = []
    interest_counts = Counter()
    seen_franchises = set()
    while finalists and len(selected) < limit:
        # Diversity only breaks close races and leaves the original scores intact.
        minimum = finalists[0][0].ranking_score * DIVERSITY_MIN_SCORE_RATIO
        best = 0
        best_priority = (False, -len(selected))
        for index, (candidate, interests) in enumerate(finalists):
            if candidate.ranking_score < minimum:
                break
            least_repeated = min((interest_counts[interest] for interest in interests), default=len(selected))
            priority = (
                seen_franchises.isdisjoint(candidate.franchise_ids),
                -least_repeated,
            )
            if priority > best_priority:
                best, best_priority = index, priority
        item, interests = finalists.pop(best)
        selected.append(item)
        interest_counts.update(interests)
        seen_franchises.update(item.franchise_ids)
    return tuple(selected)


def _rank_recommendations(user, limit, external_limit):
    if type(limit) is not int or limit <= 0:
        raise ValueError("Recommendation limit must be a positive integer.")
    profile = build_user_preference_profile(user)
    groups, reserved_ids, reserved_apps, linkable_apps = _catalog_groups(profile)
    if not groups and not external_limit:
        return RecommendationResults("general", ())
    evidence = _PreferenceEvidence(profile)
    known_sources = evidence.liked_by_igdb
    if not evidence.preferences and len(known_sources) < MIN_SIMILARITY_SEEDS:
        known_sources = {}
    igdb_ids = {value for kind, value in groups if kind == "igdb"} | known_sources.keys()
    metadata_by_igdb = IgdbGameMetadata.objects.in_bulk(igdb_ids)
    forward_links = _similar_sources(known_sources, metadata_by_igdb)
    strongest_source = max((game.score for game in known_sources.values()), default=0.0)
    candidates = list(_local_candidates(groups, metadata_by_igdb))
    if external_limit:
        cards = discover_external_candidates(
            profile, known_sources, metadata_by_igdb, reserved_ids, limit=external_limit,
        )
        if cards:
            candidates = _with_external_candidates(candidates, cards, reserved_apps, linkable_apps)

    feature_names = {}
    for candidate in candidates:
        for feature, tag, name in candidate.feature_names:
            feature_names.setdefault((feature, tag), name)
        for tag, name in candidate.genre_names_by_id:
            feature_names.setdefault(("genres", tag), name)

    personalized, general_candidates = [], []
    for candidate in candidates:
        affinity, match_score, reasons = _compare_content(candidate, evidence, feature_names)
        if not affinity:
            if candidate.game_id is not None:
                general_candidates.append(candidate)
            continue

        kind, value = candidate.identity
        similarity, similar_reasons = _compare_similarity(
            candidate.metadata, value if kind == "igdb" else None, known_sources, forward_links, strongest_source,
        )
        quality = _quality_score(candidate.metadata, candidate.local_rating_samples)
        score = affinity * (1 - SIMILAR_GAMES_WEIGHT + SIMILAR_GAMES_WEIGHT * similarity)
        score *= 1 - QUALITY_WEIGHT + QUALITY_WEIGHT * quality
        item = candidate.ranked(score, match_score, reasons + similar_reasons)
        personalized.append((item, candidate.steam_app_ids))

    if personalized:
        return RecommendationResults("personalized", _ranked_items(personalized, limit, diversify=True))

    general = []
    for candidate in general_candidates:
        labels = {label.casefold() for label in candidate.genres}
        penalty = 0.0
        for _, _, weight, values, preferences, mass in evidence.candidate_features(candidate, labels):
            penalty += weight * (sum(preferences.negative.get(value, 0) for value in values) / mass)
        quality = _quality_score(candidate.metadata, candidate.local_rating_samples)
        item = candidate.ranked(_general_score(quality, penalty), None, (RecommendationReason("general_catalog"),))
        general.append((item, ()))
    return RecommendationResults("general", _ranked_items(general, limit))


def rank_local_recommendations(user, *, limit=DEFAULT_LIMIT) -> RecommendationResults:
    """Rank the local catalog without making any IGDB requests."""
    return _rank_recommendations(user, limit, 0)


def rank_recommendations(user, *, limit=DEFAULT_LIMIT, external_limit=DEFAULT_EXTERNAL_LIMIT) -> RecommendationResults:
    """Rank local and external candidates without adding games to GameHub."""
    if type(external_limit) is not int or not 0 <= external_limit <= MAX_EXTERNAL_LIMIT:
        raise ValueError(f"External recommendation limit must be between 0 and {MAX_EXTERNAL_LIMIT}.")
    return _rank_recommendations(user, limit, external_limit)
