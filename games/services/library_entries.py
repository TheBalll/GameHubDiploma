from time import sleep

from django.db import OperationalError, connection, transaction
from django.db.models import F, Q

from games.models import Game, LibraryEntry, SteamIgdbIdentity, SteamOwnedGame

from .game_import import fetch_igdb_game_defaults


SQLITE_ADD_ATTEMPTS = 4
SQLITE_RETRY_SECONDS = 0.025


def _add_entry(user, games, *, igdb_id=None, defaults=None, steam_entry=None):
    for attempt in range(SQLITE_ADD_ATTEMPTS):
        try:
            with transaction.atomic():
                if connection.vendor == "sqlite":
                    # Reserve the SQLite write lock before reading, without updating any rows.
                    LibraryEntry.objects.filter(pk__isnull=True).update(status=F("status"))
                game = games.select_for_update().first()
                if game is None and defaults is not None:
                    game, _ = Game.objects.get_or_create(igdb_id=igdb_id, defaults=defaults)
                    game = Game.objects.select_for_update().get(pk=game.pk)
                if game is None:
                    return None, False

                if steam_entry is not None:
                    SteamOwnedGame.objects.filter(pk=steam_entry.pk, steam_account__user=user).update(game=game)
                return LibraryEntry.objects.get_or_create(user=user, game=game)
        except OperationalError as error:
            if (
                connection.vendor != "sqlite" or "locked" not in str(error).lower()
                or attempt + 1 == SQLITE_ADD_ATTEMPTS
            ):
                raise
            sleep(SQLITE_RETRY_SECONDS * (attempt + 1))


def add_catalog_game(user, game_id, *, steam_entry=None):
    return _add_entry(user, Game.objects.filter(pk=game_id), steam_entry=steam_entry)


def add_igdb_recommendation(user, igdb_id, *, steam_app_ids=()):
    confirmed_apps = SteamIgdbIdentity.objects.filter(
        status=SteamIgdbIdentity.Status.MATCHED, metadata_id=igdb_id,
    ).values("steam_app_id")
    matches = Q(steam_id__in=confirmed_apps)
    if steam_app_ids:
        checked_apps = SteamIgdbIdentity.objects.exclude(status=SteamIgdbIdentity.Status.MISSING).values("steam_app_id")
        matches |= Q(steam_id__in=steam_app_ids) & ~Q(steam_id__in=checked_apps)
    games = Game.objects.filter(
        Q(igdb_id=igdb_id) | (Q(igdb_id__isnull=True) & matches)
    ).order_by(F("igdb_id").desc(nulls_last=True), "pk")
    defaults = None if games.exists() else fetch_igdb_game_defaults(igdb_id)
    return _add_entry(user, games, igdb_id=igdb_id, defaults=defaults)
