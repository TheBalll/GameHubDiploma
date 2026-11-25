from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from games.models import Game, IgdbGameMetadata, LibraryEntry, SteamAccount, SteamOwnedGame


class GameImportSecurityTests(TestCase):
    endpoints = (("import_igdb_game", 123), ("import_steam_game", 456))

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="import-user")
        cls.local = Game.objects.create(title="Local fixture game", genre="Puzzle", release_year=2021)

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        transport = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected HTTP request"))
        self.http = transport.start()
        self.addCleanup(transport.stop)
        self.addCleanup(self.http.assert_not_called)

    def assert_no_import(self):
        self.assertEqual(Game.objects.count(), 1)
        self.assertFalse(IgdbGameMetadata.objects.exists())
        self.assertFalse(LibraryEntry.objects.exists())
        self.assertFalse(SteamOwnedGame.objects.exists())

    @patch("games.services.game_import.get_steam_game_by_id")
    @patch("games.services.game_import.get_game_by_id")
    def test_guest_cannot_import_even_with_valid_csrf(self, igdb, steam):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("login"))
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        for name, external_id in self.endpoints:
            with self.subTest(name=name):
                response = client.post(reverse(name, args=[external_id]), HTTP_X_CSRFTOKEN=token)
                self.assertEqual(response.status_code, 401)
        self.assert_no_import()
        igdb.assert_not_called()
        steam.assert_not_called()

    def test_get_and_post_without_csrf_do_not_import_for_authenticated_user(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for name, external_id in self.endpoints:
            with self.subTest(name=name):
                url = reverse(name, args=[external_id])
                self.assertEqual(client.get(url).status_code, 405)
                self.assertEqual(client.post(url).status_code, 403)
        self.assert_no_import()

    @patch("games.services.game_import.get_steam_game_by_id", return_value={"name": "Steam fixture game"})
    @patch("games.services.game_import.get_game_by_id", return_value={"id": 123, "name": "IGDB fixture game"})
    def test_authenticated_imports_work_with_csrf_and_repeat_safely(self, igdb, steam):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        page = client.get(reverse("search_results"))
        self.assertContains(page, 'data-can-import="true"')
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        for name, external_id in self.endpoints:
            with self.subTest(name=name):
                url = reverse(name, args=[external_id])
                first = client.post(url, HTTP_X_CSRFTOKEN=token)
                second = client.post(url, HTTP_X_CSRFTOKEN=token)
                self.assertEqual(first.status_code, 200)
                self.assertEqual(second.status_code, 200)
                self.assertTrue(first.json()["created"])
                self.assertFalse(second.json()["created"])
                self.assertEqual(first.json()["game_id"], second.json()["game_id"])
                self.assertEqual(client.get(first.json()["url"]).status_code, 200)
        self.assertEqual(Game.objects.count(), 3)
        self.assertFalse(LibraryEntry.objects.exists())
        igdb.assert_called_once_with(123)
        steam.assert_called_once_with(456)

    @patch("games.views.search_steam_games", return_value=[{"id": 456, "title": "Steam fixture game"}])
    @patch("games.views.search_igdb_games", return_value=[
        {"id": 123, "name": "IGDB fixture game", "slug": "igdb-fixture-game"},
        {"id": 124, "name": "Unknown slug fixture", "slug": "../elsewhere"},
    ])
    def test_guest_can_search_and_view_games_without_catalog_writes(self, igdb, steam):
        self.assertContains(self.client.get(reverse("search_results")), 'data-can-import="false"')
        detail = reverse("game_detail", args=[self.local.pk])
        self.assertEqual(self.client.get(detail).status_code, 200)
        results = self.client.get(reverse("search_results"), {"format": "json", "q": "fixture game"})
        self.assertEqual(results.status_code, 200)
        items = {item["title"]: item for item in results.json()["games"]}
        self.assertEqual(items[self.local.title]["url"], detail)
        self.assertEqual(items["IGDB fixture game"]["store_url"], "https://www.igdb.com/games/igdb-fixture-game")
        self.assertEqual(items["Steam fixture game"]["store_url"], "https://store.steampowered.com/app/456/")
        self.assertIsNone(items["Unknown slug fixture"]["store_url"])
        filtered = self.client.get(reverse("search_results"), {"format": "json", "genre": "Puzzle"})
        self.assertEqual([item["id"] for item in filtered.json()["games"]], [self.local.pk])
        self.assert_no_import()
        igdb.assert_called_once()
        steam.assert_called_once()


class LibraryEntryIntegrityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.user = users.create_user(username="library-owner")
        cls.other = users.create_user(username="other-library-owner")
        cls.game = Game.objects.create(title="Shared fixture", release_year=2020)
        cls.second_game = Game.objects.create(title="Second fixture", release_year=2021)

    def test_database_enforces_uniqueness_for_each_user_and_game_pair(self):
        LibraryEntry.objects.create(user=self.user, game=self.game)
        with self.assertRaises(IntegrityError), transaction.atomic():
            LibraryEntry.objects.create(user=self.user, game=self.game)

        LibraryEntry.objects.create(user=self.user, game=self.second_game)
        LibraryEntry.objects.create(user=self.other, game=self.game)
        self.assertEqual(LibraryEntry.objects.count(), 3)

    @patch("games.services.game_import.get_steam_game_by_id", return_value={"name": "Owned fixture"})
    def test_steam_and_catalog_adds_share_one_entry_without_resetting_data(self, steam):
        steam_account = SteamAccount.objects.create(user=self.user, steam_id="2468")
        owned = SteamOwnedGame.objects.create(
            steam_account=steam_account, steam_app_id=456, name="Owned fixture", playtime_minutes=90,
        )
        self.client.force_login(self.user)
        steam_url = reverse("add_steam_game_to_library", args=[owned.steam_app_id])
        first = self.client.post(steam_url)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["created"])

        entry = LibraryEntry.objects.get(user=self.user, game_id=first.json()["game_id"])
        self.assertIsNotNone(entry.added_at)
        original_date = entry.added_at
        entry.status = "completed"
        entry.save(update_fields=["status"])
        catalog_url = reverse("add_to_library", args=[entry.game_id])
        catalog_repeat = self.client.post(catalog_url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        steam_repeat = self.client.post(steam_url)
        self.assertFalse(catalog_repeat.json()["created"])
        self.assertFalse(steam_repeat.json()["created"])
        entry.refresh_from_db()
        owned.refresh_from_db()
        self.assertEqual(entry.status, "completed")
        self.assertEqual(entry.added_at, original_date)
        self.assertEqual(owned.playtime_minutes, 90)
        self.assertEqual(owned.game_id, entry.game_id)
        self.assertEqual(LibraryEntry.objects.count(), 1)
        steam.assert_called_once_with(owned.steam_app_id)
