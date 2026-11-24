from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Literal

from .igdb import query_igdb


APP_IDS_PER_QUERY = 100
IGDB_PAGE_SIZE = 500


class IgdbMappingError(ValueError):
    """IGDB did not provide a complete, usable mapping response."""


@dataclass(frozen=True)
class SteamIgdbMatch:
    status: Literal["matched", "missing", "ambiguous", "conflict"]
    candidate_igdb_ids: tuple[int, ...]

    @property
    def igdb_id(self) -> int | None:
        if self.status == "matched":
            return self.candidate_igdb_ids[0]
        return None


def _is_positive_id(value):
    return type(value) is int and 0 < value <= 2**63 - 1


def _query_rows(endpoint, query):
    rows = query_igdb(endpoint, query)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise IgdbMappingError("IGDB returned an invalid list of records.")
    return rows


class SteamIgdbMapper:
    """Resolve public Steam AppIDs without reading or writing any user data."""

    @cached_property
    def _steam_source_id(self):
        rows = _query_rows(
            "external_game_sources",
            'fields id, name; where name ~ "Steam"; limit 2;',
        )
        ids = {
            row["id"]
            for row in rows
            if str(row.get("name", "")).casefold() == "steam"
            and _is_positive_id(row.get("id"))
        }
        if len(ids) != 1:
            raise IgdbMappingError("IGDB did not identify a unique Steam source.")
        return ids.pop()

    def _external_game_rows(self, requested_uids):
        steam_source_id = self._steam_source_id
        quoted_uids = ",".join(f'"{uid}"' for uid in requested_uids)
        last_id = 0
        while True:
            rows = _query_rows(
                "external_games",
                f"""
                    fields id, uid, game, external_game_source;
                    where external_game_source = {steam_source_id}
                          & uid = ({quoted_uids}) & id > {last_id};
                    sort id asc;
                    limit {IGDB_PAGE_SIZE};
                """,
            )
            if len(rows) > IGDB_PAGE_SIZE:
                raise IgdbMappingError("IGDB exceeded the requested mapping page size.")
            yield from rows
            if len(rows) < IGDB_PAGE_SIZE:
                return
            next_id = rows[-1].get("id")
            if not _is_positive_id(next_id) or next_id <= last_id:
                raise IgdbMappingError("IGDB mapping pagination did not advance.")
            last_id = next_id

    def resolve(
        self, steam_app_ids: Iterable[int], *, known_igdb_ids: Mapping[int, int] | None = None
    ) -> dict[int, SteamIgdbMatch]:
        """Return one outcome per AppID, with optional confirmed local IDs."""
        app_ids = set()
        for app_id in steam_app_ids:
            if not _is_positive_id(app_id):
                raise ValueError("Steam AppIDs must be positive integers.")
            app_ids.add(app_id)

        known_ids = known_igdb_ids or {}
        if any(not _is_positive_id(known_ids[app_id]) for app_id in app_ids & known_ids.keys()):
            raise ValueError("Known IGDB IDs must be positive integers.")

        ordered_app_ids = sorted(app_ids)
        candidates = defaultdict(set)
        for start in range(0, len(ordered_app_ids), APP_IDS_PER_QUERY):
            batch = ordered_app_ids[start:start + APP_IDS_PER_QUERY]
            app_ids_by_uid = {str(app_id): app_id for app_id in batch}
            for row in self._external_game_rows(app_ids_by_uid):
                uid = row.get("uid")
                source_id = row.get("external_game_source")
                if not _is_positive_id(source_id) or not isinstance(uid, str):
                    raise IgdbMappingError("IGDB returned an invalid external game identity.")
                if source_id != self._steam_source_id or uid not in app_ids_by_uid:
                    continue
                igdb_id = row.get("game")
                if igdb_id is None:
                    continue
                if not _is_positive_id(igdb_id):
                    raise IgdbMappingError("IGDB returned an invalid game ID.")
                candidates[app_ids_by_uid[uid]].add(igdb_id)

        results = {}
        for app_id in ordered_app_ids:
            ids = tuple(sorted(candidates[app_id]))
            if not ids:
                status = "missing"
            elif len(ids) > 1:
                status = "ambiguous"
            elif app_id in known_ids and known_ids[app_id] != ids[0]:
                status = "conflict"
            else:
                status = "matched"
            results[app_id] = SteamIgdbMatch(status, ids)
        return results
