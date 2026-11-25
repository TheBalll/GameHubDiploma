from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from itertools import batched
from logging import getLogger
from math import ceil

from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import F, Max, Q
from django.utils import timezone
from requests import ConnectionError, RequestException, Timeout

from games.models import Game, IgdbGameMetadata, SteamIgdbIdentity, SteamOwnedGame, validate_igdb_id_list

from .igdb import ID_LIST_FIELDS, METADATA_FIELDS, RATING_FIELDS, igdb_cover_url, query_igdb
from .steam_igdb_mapping import APP_IDS_PER_QUERY, SteamIgdbMapper, _is_positive_id


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
API_ERRORS = (RequestException, KeyError, TypeError, ValueError)
logger = getLogger(__name__)


class MetadataFieldError(ValueError):
    def __init__(self, field_name, reason):
        super().__init__(reason)
        self.field_name = field_name


@dataclass(frozen=True)
class EnrichmentIssue:
    reason: str
    igdb_id: int | None = None
    steam_app_id: int | None = None
    field_name: str | None = None
    retryable: bool = False
    warning: bool = False

    def describe(self):
        identity = f"IGDB ID {self.igdb_id}" if self.igdb_id is not None else "IGDB ID unavailable"
        if self.steam_app_id is not None:
            identity += f" (Steam AppID {self.steam_app_id})"
        if self.field_name:
            identity += f" [{self.field_name}]"
        return f"{identity}: {self.reason}"


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
    without_igdb_id: int = 0
    retry_after_seconds: int | None = None
    issues: list[EnrichmentIssue] = field(default_factory=list)


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


def _report_api_error(summary, error, *, counts_by_id=None, steam_app_ids=(), known_ids=None):
    response = getattr(error, "response", None)
    status = response.status_code if response is not None else None
    if status == 429:
        summary.retry_after_seconds = _retry_after_seconds(response.headers.get("Retry-After"), timezone.now())
    if type(status) is int:
        reason = f"IGDB returned HTTP {status}."
        retryable = status in (408, 429) or 500 <= status <= 599
    elif isinstance(error, Timeout):
        reason, retryable = "IGDB request timed out.", True
    elif isinstance(error, ConnectionError):
        reason, retryable = "Could not connect to IGDB.", True
    else:
        reason, retryable = "IGDB returned an invalid response.", True

    if counts_by_id is not None:
        summary.failed += sum(counts_by_id.values())
        summary.issues.extend(
            EnrichmentIssue(reason, igdb_id=igdb_id, retryable=retryable) for igdb_id in counts_by_id
        )
    else:
        known_ids = known_ids or {}
        summary.failed += len(steam_app_ids)
        summary.issues.extend(
            EnrichmentIssue(reason, igdb_id=known_ids.get(app_id), steam_app_id=app_id, retryable=retryable)
            for app_id in steam_app_ids
        )


def _metadata_values(row, *, ignored_ratings=None):
    values = {}
    for field in ID_LIST_FIELDS:
        ids = row.get(field)
        if ids is not None:
            try:
                validate_igdb_id_list(ids)
            except ValidationError:
                raise MetadataFieldError(field, "Expected a list of positive IGDB IDs.") from None
            values[field] = sorted(set(ids))

    for field in RATING_FIELDS:
        count_field = f"{field}_count"
        count = row.get(count_field)
        rating = row.get(field)
        if count is not None and (type(count) is not int or not 0 <= count <= 2**31 - 1):
            raise MetadataFieldError(count_field, "Expected a non-negative integer rating count.")
        if rating is not None and (type(rating) not in (int, float) or not 0 <= rating <= 100):
            raise MetadataFieldError(field, "Expected a numeric rating between 0 and 100.")
        if count == 0:
            if rating not in (None, 0):
                # A rating without a vote sample must not replace a previously usable pair.
                if ignored_ratings is not None:
                    ignored_ratings.append(field)
                continue
            values[field] = None
            values[count_field] = count
        elif rating is not None and count is not None:
            values[field] = rating
            values[count_field] = count

    cover = row.get("cover")
    if cover is not None:
        if not isinstance(cover, dict):
            raise MetadataFieldError("cover", "Expected an IGDB cover object.")
        image_id = cover.get("image_id")
        if image_id is not None:
            if not igdb_cover_url(image_id) or len(image_id) > 100:
                raise MetadataFieldError("cover.image_id", "Expected a valid IGDB cover ID.")
            values["cover_image_id"] = image_id

    updated_at = row.get("updated_at")
    if updated_at is not None:
        if type(updated_at) is not int or updated_at <= 0:
            raise MetadataFieldError("updated_at", "Expected a positive Unix timestamp.")
        try:
            values["igdb_updated_at"] = datetime.fromtimestamp(updated_at, tz=UTC)
        except (ValueError, OverflowError, OSError):
            raise MetadataFieldError("updated_at", "Unix timestamp is outside the supported range.") from None
    return values


def _metadata_records(rows, requested, *, expanded_genres=False):
    by_id = {}
    duplicates = set()
    unexpected = False
    for row in rows:
        igdb_id = row.get("id") if isinstance(row, dict) else None
        if not _is_positive_id(igdb_id) or igdb_id not in requested:
            unexpected = True
            continue
        if igdb_id in by_id:
            duplicates.add(igdb_id)
        else:
            by_id[igdb_id] = row

    records, missing, issues = {}, set(), []
    for igdb_id in sorted(requested):
        if igdb_id in duplicates:
            issues.append(EnrichmentIssue("IGDB returned duplicate rows.", igdb_id, field_name="id"))
            continue
        row = by_id.get(igdb_id)
        if row is None:
            if unexpected:
                issues.append(EnrichmentIssue("IGDB response contained unexpected rows and omitted this ID.", igdb_id,
                                              field_name="id", retryable=True))
            else:
                missing.add(igdb_id)
            continue

        ignored_ratings = []
        try:
            if expanded_genres and isinstance(row.get("genres"), list):
                genres = [genre.get("id") if isinstance(genre, dict) else genre for genre in row["genres"]]
                row = dict(row)
                if all(_is_positive_id(genre) for genre in genres):
                    row["genres"] = genres
                else:
                    row.pop("genres")
            values = _metadata_values(row, ignored_ratings=ignored_ratings)
        except MetadataFieldError as error:
            issues.append(EnrichmentIssue(str(error), igdb_id, field_name=error.field_name))
            continue
        for rating in ignored_ratings:
            issues.append(EnrichmentIssue(
                "Ignored a positive rating with zero reported votes.", igdb_id,
                field_name=f"{rating}, {rating}_count", warning=True,
            ))
        if values:
            records[igdb_id] = values
        else:
            issues.append(EnrichmentIssue("IGDB returned no usable metadata fields.", igdb_id))
    return records, missing, issues


def _fetch_metadata(igdb_ids):
    id_list = ",".join(str(igdb_id) for igdb_id in igdb_ids)
    fields = ",".join(("id", *METADATA_FIELDS))
    rows = query_igdb("games", f"fields {fields}; where id = ({id_list}); limit {len(igdb_ids)};")
    if not isinstance(rows, list):
        raise ValueError("IGDB returned an invalid metadata response.")
    return _metadata_records(rows, set(igdb_ids))


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


def save_imported_metadata(rows, started_at):
    """Reuse metadata included in an IGDB catalog response. Import can succeed without it."""
    requested = {row["id"] for row in rows}
    records, _, issues = _metadata_records(rows, requested, expanded_genres=True)
    for issue in issues:
        if issue.field_name:
            logger.warning("%s", issue.describe())
    if records:
        try:
            _save_metadata(records, started_at)
        except DatabaseError:
            logger.warning("Could not save recommendation metadata from an IGDB import.")


def _enrich_metadata_batches(counts_by_id, summary, started_at, *, dry_run, separate_missing=False):
    for batch in batched(counts_by_id, METADATA_BATCH_SIZE):
        affected = sum(counts_by_id[igdb_id] for igdb_id in batch)
        if summary.retry_after_seconds is not None:
            summary.deferred += affected
            continue
        try:
            records, missing_ids, issues = _fetch_metadata(batch)
        except API_ERRORS as error:
            _report_api_error(summary, error, counts_by_id={igdb_id: counts_by_id[igdb_id] for igdb_id in batch})
            continue
        summary.issues.extend(issues)
        if not separate_missing:
            summary.issues.extend(
                EnrichmentIssue("IGDB did not return metadata for this ID.", igdb_id, retryable=True)
                for igdb_id in sorted(missing_ids)
            )
        if records and not dry_run:
            try:
                _save_metadata(records, started_at)
            except DatabaseError:
                summary.issues.extend(
                    EnrichmentIssue("Could not save metadata to the database.", igdb_id, retryable=True)
                    for igdb_id in records
                )
                records = {}
        enriched = sum(counts_by_id[igdb_id] for igdb_id in records)
        missing = sum(counts_by_id[igdb_id] for igdb_id in missing_ids) if separate_missing else 0
        summary.enriched += enriched
        summary.missing += missing
        summary.failed += affected - enriched - missing


def enrich_catalog_metadata(*, limit=None, force=False, dry_run=False):
    """Enrich distinct, valid IGDB IDs already present in the GameHub catalog."""
    _validate_limit(limit)
    started_at = timezone.now()
    catalog_ids = Game.objects.order_by("igdb_id").values_list("igdb_id", flat=True)
    igdb_ids = list(dict.fromkeys(igdb_id for igdb_id in catalog_ids if _is_positive_id(igdb_id)))
    summary = EnrichmentSummary(
        total=len(igdb_ids),
        without_igdb_id=sum(not _is_positive_id(igdb_id) for igdb_id in catalog_ids),
    )
    existing = IgdbGameMetadata.objects.only("fetched_at").in_bulk(igdb_ids) if not force else {}
    due = [igdb_id for igdb_id in igdb_ids if not _fresh_metadata(existing.get(igdb_id), started_at)]
    summary.skipped += len(igdb_ids) - len(due)
    selected = due[:limit]
    summary.deferred = len(due) - len(selected)
    _enrich_metadata_batches(dict.fromkeys(selected, 1), summary, started_at, dry_run=dry_run, separate_missing=True)
    return summary


def _validate_limit(limit):
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("Limit must be a positive integer.")


def enrich_steam_metadata(*, steam_games=None, limit=None, force=False, dry_run=False):
    """Enrich the synchronized apps, optionally scoped to a SteamOwnedGame queryset."""
    _validate_limit(limit)
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
            _report_api_error(summary, error, steam_app_ids=batch, known_ids=known_ids)
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
    to_enrich = {}
    for igdb_id, apps in apps_by_igdb.items():
        if not force and _fresh_metadata(existing_metadata.get(igdb_id), started_at):
            summary.enriched += len(apps)
        else:
            to_enrich[igdb_id] = len(apps)

    _enrich_metadata_batches(to_enrich, summary, started_at, dry_run=dry_run)
    return summary
