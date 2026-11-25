from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from games.models import Game, LibraryEntry, SteamAccount, SteamOwnedGame


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
