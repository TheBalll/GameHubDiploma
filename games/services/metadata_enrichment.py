from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from itertools import batched
from math import ceil

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Max, Q
from django.utils import timezone
from requests import RequestException

from games.models import Game, IgdbGameMetadata, SteamIgdbIdentity, SteamOwnedGame, validate_igdb_id_list

from .igdb import query_igdb
from .steam_igdb_mapping import APP_IDS_PER_QUERY, SteamIgdbMapper


CONFIRMED_MAPPING_TTL = timedelta(days=30)
MISSING_MAPPING_TTL = timedelta(days=1)
AMBIGUOUS_MAPPING_TTL = timedelta(days=7)
CONFLICT_MAPPING_TTL = timedelta(days=7)
GAME_METADATA_TTL = timedelta(days=7)
METADATA_BATCH_SIZE = 100
DEFAULT_RETRY_AFTER_SECONDS = 60

MAPPING_TTLS = {
    SteamIgdbIdentity.Status.MATCHED: CONFIRMED_MAPPING_TTL,
    SteamIgdbIdentity.Status.MISSING: MISSING_MAPPING_TTL,
    SteamIgdbIdentity.Status.AMBIGUOUS: AMBIGUOUS_MAPPING_TTL,
    SteamIgdbIdentity.Status.CONFLICT: CONFLICT_MAPPING_TTL,
}
ID_LIST_FIELDS = ("genres", "themes", "game_modes", "keywords", "similar_games")
RATING_FIELDS = ("rating", "aggregated_rating", "total_rating")
API_ERRORS = (RequestException, KeyError, TypeError, ValueError)


@dataclass
class EnrichmentSummary:
    total: int = 0
    enriched: int = 0
    skipped: int = 0
    missing: int = 0
    ambiguous: int = 0
    conflict: int = 0
    failed: int = 0
    deferred: int = 0
    retry_after_seconds: int | None = None


def _fresh_metadata(metadata, now):
    return metadata and metadata.fetched_at and metadata.fetched_at > now - GAME_METADATA_TTL


def _retry_after_seconds(value, now):
    if value:
        try:
            value = value.strip()
            if value.isascii() and value.isdecimal():
                return int(value)
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0, ceil((retry_at - now).total_seconds()))
        except (ValueError, OverflowError):
            pass
    return DEFAULT_RETRY_AFTER_SECONDS


def _report_api_error(summary, error, affected_apps):
    summary.failed += affected_apps
    response = getattr(error, "response", None)
    if response is not None and response.status_code == 429:
        summary.retry_after_seconds = _retry_after_seconds(response.headers.get("Retry-After"), timezone.now())


def _metadata_values(row):
    values = {}
    for field in ID_LIST_FIELDS:
        ids = row.get(field)
        if ids is not None:
            validate_igdb_id_list(ids)
            values[field] = sorted(set(ids))

    for field in RATING_FIELDS:
        count_field = f"{field}_count"
        count = row.get(count_field)
        rating = row.get(field)
        if count is not None:
            if type(count) is not int or not 0 <= count <= 2**31 - 1:
                raise ValueError("IGDB returned an invalid rating count.")
            values[count_field] = count
        if rating is not None and (type(rating) not in (int, float) or not 0 <= rating <= 100):
            raise ValueError("IGDB returned an invalid rating.")
        if count == 0:
            values[field] = None
        elif rating is not None and (rating != 0 or count is not None):
            values[field] = rating

    cover = row.get("cover")
    if cover is not None:
        if not isinstance(cover, dict):
            raise ValueError("IGDB returned an invalid cover.")
        image_id = cover.get("image_id")
        if image_id is not None:
            if not isinstance(image_id, str) or not image_id.strip() or len(image_id) > 100:
                raise ValueError("IGDB returned an invalid cover ID.")
            values["cover_image_id"] = image_id

    updated_at = row.get("updated_at")
    if updated_at is not None:
        if type(updated_at) is not int or updated_at <= 0:
            raise ValueError("IGDB returned an invalid update timestamp.")
        values["igdb_updated_at"] = datetime.fromtimestamp(updated_at, tz=UTC)
    return values


def _fetch_metadata(igdb_ids):
    requested = set(igdb_ids)
    id_list = ",".join(str(igdb_id) for igdb_id in igdb_ids)
    fields = ("id", *ID_LIST_FIELDS, *RATING_FIELDS, *(f"{field}_count" for field in RATING_FIELDS),
              "cover.image_id", "updated_at")
    rows = query_igdb("games", f"fields {','.join(fields)}; where id = ({id_list}); limit {len(igdb_ids)};")
    if not isinstance(rows, list):
        raise ValueError("IGDB returned an invalid metadata response.")

    seen = set()
    records = {}
    for row in rows:
        igdb_id = row.get("id") if isinstance(row, dict) else None
        if type(igdb_id) is not int or igdb_id not in requested:
            continue
        if igdb_id in seen:
            records.pop(igdb_id, None)
            continue
        seen.add(igdb_id)
        try:
            values = _metadata_values(row)
        except (ValidationError, ValueError, OverflowError, OSError):
            continue
        if values:
            records[igdb_id] = values
    return records


def _save_identities(matches, started_at):
    checked_at = timezone.now()
    saved = {}
    with transaction.atomic():
        for igdb_id in {match.igdb_id for match in matches.values()} - {None}:
            IgdbGameMetadata.objects.only("igdb_id").get_or_create(igdb_id=igdb_id)
        for app_id, match in matches.items():
            fields = {
                "metadata_id": match.igdb_id,
                "status": match.status,
                "last_checked": checked_at,
                "candidate_igdb_ids": list(match.candidate_igdb_ids) if match.status in ("ambiguous", "conflict") else [],
            }
            _, created = SteamIgdbIdentity.objects.only("steam_app_id").get_or_create(
                steam_app_id=app_id, defaults=fields
            )
            if created or SteamIgdbIdentity.objects.filter(pk=app_id, last_checked__lte=started_at).update(**fields):
                saved[app_id] = match
    return saved


def _save_metadata(records, started_at):
    fetched_at = timezone.now()
    with transaction.atomic():
        for igdb_id, fields in records.items():
            values = dict(fields, fetched_at=fetched_at)
            _, created = IgdbGameMetadata.objects.only("igdb_id").get_or_create(igdb_id=igdb_id, defaults=values)
            if not created:
                IgdbGameMetadata.objects.filter(
                    Q(fetched_at__isnull=True) | Q(fetched_at__lte=started_at), pk=igdb_id,
                ).update(**values)


def enrich_steam_metadata(*, steam_games=None, limit=None, force=False, dry_run=False):
    """Enrich the synchronized apps, optionally scoped to a SteamOwnedGame queryset."""
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("Limit must be a positive integer.")
    if steam_games is None:
        steam_games = SteamOwnedGame.objects.all()

    started_at = timezone.now()
    app_ids = list(
        steam_games.filter(steam_app_id__gt=0)
        .values("steam_app_id")
        .annotate(max_playtime=Max("playtime_minutes"))
        .order_by(F("max_playtime").desc(nulls_last=True), "steam_app_id")
        .values_list("steam_app_id", flat=True)
    )
    summary = EnrichmentSummary(total=len(app_ids))
    identities = (
        SteamIgdbIdentity.objects.select_related("metadata")
        .only("status", "last_checked", "metadata__fetched_at")
        .in_bulk(app_ids)
    )
    to_map = []
    matched_apps = {}
    for app_id in app_ids:
        identity = identities.get(app_id)
        mapping_due = (
            force or identity is None or identity.last_checked <= started_at - MAPPING_TTLS[identity.status]
        )
        if not mapping_due and (identity.metadata_id is None or _fresh_metadata(identity.metadata, started_at)):
            summary.skipped += 1
        elif limit is not None and len(to_map) + len(matched_apps) >= limit:
            summary.deferred += 1
        elif mapping_due:
            to_map.append(app_id)
        else:
            matched_apps[app_id] = identity.metadata_id

    mapper = SteamIgdbMapper()
    for batch in batched(to_map, APP_IDS_PER_QUERY):
        if summary.retry_after_seconds is not None:
            summary.deferred += len(batch)
            continue
        known_ids = dict(
            Game.objects.filter(steam_id__in=batch, igdb_id__gt=0).values_list("steam_id", "igdb_id")
        )
        try:
            matches = mapper.resolve(batch, known_igdb_ids=known_ids)
        except API_ERRORS as error:
            _report_api_error(summary, error, len(batch))
            continue
        if not dry_run:
            saved = _save_identities(matches, started_at)
            summary.skipped += len(matches) - len(saved)
            matches = saved

        statuses = Counter(match.status for match in matches.values())
        summary.missing += statuses["missing"]
        summary.ambiguous += statuses["ambiguous"]
        summary.conflict += statuses["conflict"]
        matched_apps.update(
            (app_id, match.igdb_id) for app_id, match in matches.items() if match.igdb_id is not None
        )

    apps_by_igdb = defaultdict(list)
    for app_id in app_ids:
        if app_id in matched_apps:
            apps_by_igdb[matched_apps[app_id]].append(app_id)
    existing_metadata = IgdbGameMetadata.objects.only("fetched_at").in_bulk(apps_by_igdb)
    to_enrich = []
    for igdb_id, apps in apps_by_igdb.items():
        if not force and _fresh_metadata(existing_metadata.get(igdb_id), started_at):
            summary.enriched += len(apps)
        else:
            to_enrich.append(igdb_id)

    for batch in batched(to_enrich, METADATA_BATCH_SIZE):
        affected_apps = sum(len(apps_by_igdb[igdb_id]) for igdb_id in batch)
        if summary.retry_after_seconds is not None:
            summary.deferred += affected_apps
            continue
        try:
            records = _fetch_metadata(batch)
        except API_ERRORS as error:
            _report_api_error(summary, error, affected_apps)
            continue
        if not dry_run:
            _save_metadata(records, started_at)
        enriched = sum(len(apps_by_igdb[igdb_id]) for igdb_id in records)
        summary.enriched += enriched
        summary.failed += affected_apps - enriched

    return summary
