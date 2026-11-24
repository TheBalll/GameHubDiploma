from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .models import Game, LibraryEntry, SteamAccount, SteamOwnedGame


class MyLibraryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("library-player")
        cls.steam_account = SteamAccount.objects.create(user=cls.user, steam_id="123")

    def setUp(self):
        self.client.force_login(self.user)

    def catalog_game(self, title, steam_id=None):
        return Game.objects.create(
            title=title,
            steam_id=steam_id,
            genre="Adventure, RPG",
            release_year=2020,
            developer="Test Studio",
        )

    def owned_game(self, app_id, name, minutes, game=None):
        return SteamOwnedGame.objects.create(
            steam_account=self.steam_account,
            steam_app_id=app_id,
            name=name,
            playtime_minutes=minutes,
            game=game,
        )

    def library_games(self):
        response = self.client.get(reverse("library_data"))
        self.assertEqual(response.status_code, 200)
        return {game["id"]: game for game in response.json()["games"]}

    def test_merges_app_id_catalog_link_and_unique_normalized_title(self):
        by_app_id = self.catalog_game("Original Catalog Name", steam_id=100)
        by_link = self.catalog_game("Linked Game")
        by_title = self.catalog_game("Exact: Title!")
        for game in (by_app_id, by_link, by_title):
            LibraryEntry.objects.create(user=self.user, game=game)

        self.owned_game(100, "Different Steam Name", 60)
        self.owned_game(101, "Another Steam Name", 75, game=by_link)
        self.owned_game(102, "exact-title", 1)

        games = self.library_games()
        self.assertEqual(set(games), {by_app_id.pk, by_link.pk, by_title.pk})
        self.assertEqual(games[by_app_id.pk]["playtime_minutes"], 60)
        self.assertEqual(games[by_link.pk]["playtime_minutes"], 75)
        self.assertEqual(games[by_title.pk]["playtime_display"], "1 min")

    def test_same_title_with_distinct_app_ids_preserves_both_games_and_times(self):
        catalog = self.catalog_game("Shared Title", steam_id=100)
        LibraryEntry.objects.create(user=self.user, game=catalog)
        self.owned_game(100, "Shared Title", 120, game=catalog)
        self.owned_game(101, "shared-title", 240)

        other_user = User.objects.create_user("other-library-player")
        other_account = SteamAccount.objects.create(user=other_user, steam_id="456")
        SteamOwnedGame.objects.create(
            steam_account=other_account, steam_app_id=102, name="Shared Title"
        )

        games = self.library_games()
        self.assertEqual(set(games), {catalog.pk, "steam-101"})
        self.assertEqual(games[catalog.pk]["steam_app_id"], 100)
        self.assertEqual(games[catalog.pk]["playtime_minutes"], 120)
        self.assertEqual(games["steam-101"]["playtime_minutes"], 240)
        self.assertIsNone(games["steam-101"]["status"])

    def test_ambiguous_title_does_not_claim_any_steam_app(self):
        catalog = self.catalog_game("Shared Title")
        LibraryEntry.objects.create(user=self.user, game=catalog)
        self.owned_game(100, "Shared Title", 45)
        self.owned_game(101, "Shared-Title", 60)

        games = self.library_games()
        self.assertEqual(set(games), {catalog.pk, "steam-100", "steam-101"})
        self.assertFalse(games[catalog.pk]["is_on_steam"])

    def test_title_match_cannot_override_an_existing_link_or_a_different_app_id(self):
        linked = self.catalog_game("Catalog Title")
        different_app = self.catalog_game("Other Edition", steam_id=200)
        for game in (linked, different_app):
            LibraryEntry.objects.create(user=self.user, game=game)
        self.owned_game(100, "Catalog Title", 30)
        self.owned_game(101, "Renamed on Steam", 60, game=linked)
        self.owned_game(102, "Other Edition", 120)

        games = self.library_games()
        self.assertEqual(set(games), {linked.pk, different_app.pk, "steam-100", "steam-102"})
        self.assertEqual(games[linked.pk]["steam_app_id"], 101)
        self.assertEqual(games[linked.pk]["playtime_minutes"], 60)
        self.assertIsNone(games[different_app.pk]["playtime_minutes"])

    @patch("games.views.import_game_from_steam")
    def test_ambiguous_catalog_link_is_not_reused_when_adding_an_app(self, import_game):
        shared_catalog = self.catalog_game("Shared Catalog Title")
        LibraryEntry.objects.create(user=self.user, game=shared_catalog)
        self.owned_game(100, "Steam App One", 15, game=shared_catalog)
        self.owned_game(101, "Steam App Two", 30, game=shared_catalog)
        separate_catalog = self.catalog_game("Steam App Two", steam_id=101)
        import_game.return_value = (separate_catalog, False)

        games = self.library_games()
        self.assertEqual(games["steam-100"]["title"], "Steam App One")
        self.assertEqual(games["steam-101"]["title"], "Steam App Two")
        self.assertIsNone(games["steam-101"]["details_url"])

        response = self.client.post(reverse("add_steam_game_to_library", args=[101]))
        self.assertTrue(response.json()["created"])
        import_game.assert_called_once_with(101)
        games = self.library_games()
        self.assertEqual(set(games), {shared_catalog.pk, separate_catalog.pk})
        self.assertEqual(games[shared_catalog.pk]["steam_app_id"], 100)
        self.assertEqual(games[separate_catalog.pk]["steam_app_id"], 101)

    @patch("games.steam_views.get_owned_games")
    def test_sync_preserves_zero_and_stores_unavailable_playtime_as_null(self, get_owned_games):
        get_owned_games.return_value = [
            {"appid": 100, "name": "Missing Playtime"},
            {"appid": 101, "name": "Zero Playtime", "playtime_forever": 0},
            {"appid": 102, "name": "Played", "playtime_forever": 80},
            {"appid": 103, "name": "Invalid Playtime", "playtime_forever": -1},
            {"appid": 104, "name": "Boolean Playtime", "playtime_forever": True},
        ]
        response = self.client.post(reverse("steam_sync_library"))
        self.assertRedirects(response, reverse("steam_settings"), fetch_redirect_response=False)
        self.assertEqual(
            dict(self.steam_account.owned_games.values_list("steam_app_id", "playtime_minutes")),
            {100: None, 101: 0, 102: 80, 103: None, 104: None},
        )
        games = self.library_games()
        self.assertIsNone(games["steam-100"]["playtime_hours"])
        self.assertEqual(games["steam-100"]["playtime_display"], "Unavailable")
        self.assertEqual(games["steam-101"]["playtime_minutes"], 0)
        self.assertEqual(games["steam-101"]["playtime_display"], "0 hours")
        self.assertContains(self.client.get(reverse("steam_settings")), "Unavailable")

        for order, first_games in (("most_played", [102, 101]), ("least_played", [101, 102])):
            with self.subTest(order=order):
                response = self.client.get(reverse("steam_library"), {"sort": order})
                self.assertContains(response, "Unavailable")
                app_ids = [item["entry"].steam_app_id for item in response.context["page_games"]]
                self.assertEqual(app_ids[:2], first_games)
                self.assertCountEqual(app_ids[2:], [100, 103, 104])

    def test_add_edit_and_remove_keep_date_and_steam_ownership_separate(self):
        catalog = self.catalog_game("Steam Game", steam_id=100)
        self.owned_game(100, "Steam Game", 80, game=catalog)
        catalog_only = self.catalog_game("GameHub Game")
        LibraryEntry.objects.create(user=self.user, game=catalog_only)

        self.assertIsNone(self.library_games()["steam-100"]["added_at"])
        add = reverse("add_steam_game_to_library", args=[100])
        response = self.client.post(add)
        self.assertTrue(response.json()["created"])
        games = self.library_games()
        self.assertNotIn("steam-100", games)
        self.assertIsNotNone(games[catalog.pk]["added_at"])
        self.assertTrue(games[catalog.pk]["details_url"])
        date = games[catalog.pk]["added_at"]
        self.assertFalse(self.client.post(add).json()["created"])

        response = self.client.post(
            reverse("edit_library_entry", args=[catalog.pk]),
            {"status": "completed"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        updated = self.library_games()[catalog.pk]
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["added_at"], date)

        for game in (catalog, catalog_only):
            response = self.client.post(reverse("remove_from_library", args=[game.pk]))
            self.assertRedirects(response, reverse("library"), fetch_redirect_response=False)
        games = self.library_games()
        self.assertEqual(set(games), {"steam-100"})
        self.assertEqual(games["steam-100"]["status_display"], "Not Added")
        self.assertIsNone(games["steam-100"]["added_at"])

    def test_adding_from_the_catalog_records_a_date(self):
        game = self.catalog_game("Catalog Game")
        response = self.client.post(reverse("add_to_library", args=[game.pk]))
        self.assertRedirects(response, reverse("library"), fetch_redirect_response=False)
        saved_game = self.library_games()[game.pk]
        self.assertIsNotNone(saved_game["added_at"])
        self.assertIsNone(saved_game["playtime_minutes"])


class MyLibraryMigrationTests(TransactionTestCase):
    def test_migrations_keep_existing_rows_without_inventing_dates_or_times(self):
        old_target = [("games", "0012_steamaccount_steamownedgame")]
        executor = MigrationExecutor(connection)
        latest_targets = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(old_target)
            models = executor.loader.project_state(old_target).apps
            user = models.get_model("auth", "User").objects.create(username="legacy-player")
            game = models.get_model("games", "Game").objects.create(
                title="Legacy Game",
                genre="Adventure",
                release_year=2019,
                developer="Test Studio",
            )
            entry = models.get_model("games", "LibraryEntry").objects.create(
                user_id=user.pk, game_id=game.pk, status="completed"
            )
            account = models.get_model("games", "SteamAccount").objects.create(
                user_id=user.pk, steam_id="321"
            )
            old_owned_games = models.get_model("games", "SteamOwnedGame")
            old_owned_games.objects.create(
                steam_account_id=account.pk, steam_app_id=100, name="Legacy Zero", playtime_minutes=0
            )
            old_owned_games.objects.create(
                steam_account_id=account.pk, steam_app_id=101, name="Legacy Played", playtime_minutes=90
            )

            MigrationExecutor(connection).migrate(latest_targets)
            saved_entry = LibraryEntry.objects.get(pk=entry.pk)
            self.assertIsNone(saved_entry.added_at)
            self.assertEqual(saved_entry.status, "completed")
            self.assertEqual(
                dict(
                    SteamOwnedGame.objects.filter(steam_account_id=account.pk)
                    .values_list("steam_app_id", "playtime_minutes")
                ),
                {100: 0, 101: 90},
            )
        finally:
            MigrationExecutor(connection).migrate(latest_targets)
