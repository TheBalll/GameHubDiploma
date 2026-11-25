from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import signing
from django.core.cache import cache
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from requests import HTTPError, Response, Timeout

from games.models import Game, IgdbGameMetadata, LibraryEntry, Review, SteamIgdbIdentity
from games.recommendation_views import ADD_TOKEN_MAX_AGE, ADD_TOKEN_SALT
from games.services.library_entries import add_catalog_game, add_igdb_recommendation


@override_settings(CACHES={
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "recommendation-view-tests"},
})
class RecommendationViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("recommendation-player")
        cls.other = User.objects.create_user("recommendation-other")

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client.force_login(self.user)
        api_patcher = patch("games.services.igdb_recommendation_candidates.query_igdb", return_value=[])
        self.api = api_patcher.start()
        self.addCleanup(api_patcher.stop)
        transport_patcher = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected HTTP"))
        transport = transport_patcher.start()
        self.addCleanup(transport_patcher.stop)
        self.addCleanup(transport.assert_not_called)

    def game(self, title, igdb_id=None, **fields):
        return Game.objects.create(title=title, igdb_id=igdb_id, release_year=2020, developer="Test Studio", **fields)

    def signals(self, similar=None):
        for igdb_id, title in ((101, "Seed Game"), (102, "Second Seed")):
            game = self.game(title, igdb_id)
            Review.objects.create(user=self.user, game=game, rating=10)
            IgdbGameMetadata.objects.create(
                igdb_id=igdb_id, genres=[31], themes=[41], similar_games=similar if igdb_id == 101 else [],
            )

    def recommendations(self):
        return self.client.get(reverse("recommendation_data"))

    def token(self, igdb_id, user=None, steam_app_ids=()):
        return signing.dumps([(user or self.user).pk, igdb_id, steam_app_ids], salt=ADD_TOKEN_SALT)

    def test_home_renders_the_section_without_loading_recommendations(self):
        with patch("games.recommendation_views.rank_recommendations") as rank:
            page = self.client.get(reverse("home"))
        rank.assert_not_called()
        self.assertContains(page, reverse("recommendation_data"))
        self.assertContains(page, "recommendations.js")
        self.assertContains(page, "Loading recommendations")
        guest = Client().get(reverse("home"))
        self.assertContains(guest, "Sign In for Recommendations")
        self.assertNotContains(guest, "recommendations.js")

    def test_endpoints_require_login_and_enforce_http_methods(self):
        guest = Client()
        read = reverse("recommendation_data")
        add = reverse("add_recommended_igdb_game", args=[201])
        self.assertEqual(guest.get(read).status_code, 401)
        self.assertEqual(guest.post(add).status_code, 401)
        self.assertEqual(self.client.post(read).status_code, 405)
        self.assertEqual(self.client.get(add).status_code, 405)
        self.api.assert_not_called()

    def test_personalized_results_use_names_match_scores_and_signed_external_targets(self):
        self.signals(similar=[203])
        local = self.game("Local Game", 201, genre="Adventure")
        IgdbGameMetadata.objects.create(igdb_id=201, genres=[31], themes=[41])
        self.api.side_effect = [
            [{"id": 202}],
            [{"id": 202, "name": "External Game", "genres": [{"id": 31, "name": "Adventure"}],
              "themes": [{"id": 41, "name": "Fantasy"}]},
             {"id": 203, "name": "Similar Game", "themes": [41]}],
        ]

        response = self.recommendations()

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])
        body = response.json()
        self.assertEqual(body["mode"], "personalized")
        items = {item["title"]: item for item in body["items"]}
        self.assertEqual(items["Local Game"]["game_url"], reverse("game_detail", args=[local.pk]))
        self.assertEqual(items["Local Game"]["add_url"], reverse("add_to_library", args=[local.pk]))
        self.assertEqual(items["Local Game"]["match_score"], 73)
        self.assertEqual(items["External Game"]["match_score"], 73)
        self.assertEqual(items["External Game"]["source"], "external")
        named_reason = "Matches your preferred genres (Adventure) and themes (Fantasy)."
        self.assertEqual(items["Local Game"]["reason"], named_reason)
        self.assertEqual(items["External Game"]["reason"], named_reason)
        self.assertEqual(items["Similar Game"]["reason"], "Matches your preferred themes (Fantasy). Similar to Seed Game.")
        self.assertIsNone(items["Similar Game"]["match_score"])
        self.assertEqual(
            signing.loads(items["External Game"]["add_token"], salt=ADD_TOKEN_SALT), [self.user.pk, 202, []],
        )
        self.assertFalse(Game.objects.filter(igdb_id__in=[202, 203]).exists())
        self.assertFalse(LibraryEntry.objects.exists())
        self.assertEqual(self.api.call_count, 2)

    def test_reason_uses_strongest_net_contribution_and_avoids_claims_from_one_match(self):
        for igdb_id, genre in ((101, 11), (102, 12), (103, 12)):
            source = self.game(f"Reviewed Game {igdb_id}", igdb_id)
            Review.objects.create(user=self.user, game=source, rating=10)
            IgdbGameMetadata.objects.create(igdb_id=igdb_id, genres=[genre], themes=[21])
        strongest = self.game("Genre and theme", 201)
        IgdbGameMetadata.objects.create(igdb_id=201, genres=[11], themes=[21])
        singleton = self.game("One supported genre", 202)
        IgdbGameMetadata.objects.create(igdb_id=202, genres=[11])

        response = self.recommendations()

        self.assertEqual(response.status_code, 200)
        items = {item["title"]: item for item in response.json()["items"]}
        self.assertEqual(items[strongest.title]["reason"], "Shares a theme with Reviewed Game 101.")
        self.assertIsNone(items[singleton.title]["match_score"])
        self.assertEqual(items[singleton.title]["reason"], "Shares a genre with Reviewed Game 101.")
        self.api.assert_called_once()

    def test_reason_names_the_history_game_supporting_the_most_positive_contributions(self):
        for igdb_id, title, theme in ((101, "Genre source", 22), (102, "Genre and theme source", 21),
                                      (103, "Second combined source", 21)):
            source = self.game(title, igdb_id)
            Review.objects.create(user=self.user, game=source, rating=10)
            IgdbGameMetadata.objects.create(igdb_id=igdb_id, genres=[11], themes=[theme])
        self.game("Local candidate", 201)
        IgdbGameMetadata.objects.create(igdb_id=201, genres=[11], themes=[21])

        [item] = self.recommendations().json()["items"]

        self.assertEqual(item["reason"], "Shares a genre and a theme with Genre and theme source.")

    def test_reason_uses_game_mode_name_from_the_existing_candidate_batch(self):
        for igdb_id in (101, 102):
            source = self.game("Reviewed game", igdb_id)
            Review.objects.create(user=self.user, game=source, rating=10)
            IgdbGameMetadata.objects.create(igdb_id=igdb_id, game_modes=[31])
        self.api.side_effect = [[{"id": 201}], [{
            "id": 201, "name": "External game", "game_modes": [{"id": 31, "name": "Co-operative"}],
        }]]

        [item] = self.recommendations().json()["items"]

        self.assertEqual(item["reason"], "Matches your preferred game modes (Co-operative).")
        self.assertIsNone(item["match_score"])
        self.assertEqual(self.api.call_count, 2)

    def test_general_empty_user_isolation_and_exclusion(self):
        catalog = self.game("Catalog Game", 201)
        LibraryEntry.objects.create(user=self.user, game=catalog)
        first = self.recommendations()
        self.assertEqual(first.json(), {"mode": "general", "items": []})
        self.client.force_login(self.other)
        response = self.client.get(reverse("recommendation_data"), {"user": self.user.pk})
        self.assertEqual(response.json()["mode"], "general")
        [item] = response.json()["items"]
        self.assertEqual(item["title"], catalog.title)
        self.assertIsNone(item["match_score"])
        self.assertIsNone(item["reason"])
        self.api.assert_not_called()

    def test_igdb_failure_returns_local_cards(self):
        self.signals()
        local = self.game("Local Game", 201)
        IgdbGameMetadata.objects.create(igdb_id=201, genres=[31])
        self.api.side_effect = Timeout()
        response = self.recommendations()
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["title"] for item in response.json()["items"]], [local.title])
        self.api.assert_called_once()

    def test_local_add_reuses_existing_endpoint_and_preserves_status_and_date_on_repeat(self):
        catalog = self.game("Catalog Game")
        url = reverse("add_to_library", args=[catalog.pk])
        first = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(first.json()["created"])
        entry = LibraryEntry.objects.get(user=self.user, game=catalog)
        entry.status = "completed"
        entry.save(update_fields=["status"])
        second = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertFalse(second.json()["created"])
        self.assertEqual(LibraryEntry.objects.filter(game=catalog).count(), 1)
        self.assertEqual(LibraryEntry.objects.get(pk=entry.pk).status, "completed")
        self.assertEqual(LibraryEntry.objects.get(pk=entry.pk).added_at, entry.added_at)
        self.api.assert_not_called()

    @patch("games.services.game_import.get_game_by_id")
    def test_external_add_uses_verified_details_and_repeat_add_needs_no_api(self, get_game):
        get_game.return_value = {"id": 201, "name": "Verified Title", "genres": [{"name": "Adventure"}]}
        url = reverse("add_recommended_igdb_game", args=[201])
        data = {"token": self.token(201), "title": "Untrusted Title", "user": self.other.pk, "rating": 10}
        self.assertTrue(self.client.post(url, data).json()["created"])
        second = self.client.post(url, data)
        self.assertFalse(second.json()["created"])
        get_game.assert_called_once_with(201)
        [game] = Game.objects.filter(igdb_id=201)
        self.assertEqual(game.title, "Verified Title")
        self.assertEqual(list(LibraryEntry.objects.filter(game=game).values_list("user_id", flat=True)), [self.user.pk])
        self.assertFalse(Review.objects.exists())
        self.assertEqual(self.api.call_count, 0)

    @patch("games.services.game_import.get_game_by_id")
    def test_external_add_reuses_catalog_alias_with_confirmed_identity(self, get_game):
        game = self.game("Steam Catalog Game", steam_id=401)
        metadata = IgdbGameMetadata.objects.create(igdb_id=201)
        SteamIgdbIdentity.objects.create(
            steam_app_id=401, metadata=metadata, status=SteamIgdbIdentity.Status.MATCHED,
            last_checked=timezone.now(),
        )
        url = reverse("add_recommended_igdb_game", args=[201])
        self.assertTrue(self.client.post(url, {"token": self.token(201)}).json()["created"])
        self.assertEqual(LibraryEntry.objects.get(user=self.user).game_id, game.pk)
        self.assertFalse(Game.objects.filter(igdb_id=201).exists())
        get_game.assert_not_called()

    @patch("games.services.game_import.get_game_by_id")
    def test_external_add_reuses_catalog_game_created_after_recommendation(self, get_game):
        token = self.token(201, steam_app_ids=(401,))
        game = self.game("Recently imported catalog game", steam_id=401)
        url = reverse("add_recommended_igdb_game", args=[201])
        response = self.client.post(url, {"token": token, "steam_app_ids": [402]})
        self.assertTrue(response.json()["created"])
        self.assertEqual(LibraryEntry.objects.get(user=self.user).game_id, game.pk)
        self.assertEqual(Game.objects.count(), 1)
        get_game.assert_not_called()

    @patch("games.services.game_import.get_game_by_id")
    def test_invalid_tampered_or_expired_external_targets_are_rejected(self, get_game):
        url = reverse("add_recommended_igdb_game", args=[201])
        for token in ("", "invalid", self.token(202), self.token(201, self.other)):
            self.assertEqual(self.client.post(url, {"token": token}).status_code, 403)
        for invalid in (0, 2**63):
            bad_url = reverse("add_recommended_igdb_game", args=[invalid])
            self.assertEqual(self.client.post(bad_url).status_code, 400)
        with patch("django.core.signing.time.time", return_value=100):
            expired = self.token(201)
        with patch("django.core.signing.time.time", return_value=100 + ADD_TOKEN_MAX_AGE + 1):
            self.assertEqual(self.client.post(url, {"token": expired}).status_code, 403)
        get_game.assert_not_called()
        self.assertFalse(Game.objects.exists())

    @patch("games.services.game_import.get_game_by_id")
    def test_external_add_handles_missing_wrong_identity_timeout_and_rate_limit(self, get_game):
        url = reverse("add_recommended_igdb_game", args=[201])
        token = {"token": self.token(201)}
        get_game.return_value = None
        self.assertEqual(self.client.post(url, token).status_code, 404)
        get_game.return_value = {"id": 202, "name": "Wrong Game"}
        self.assertEqual(self.client.post(url, token).status_code, 503)
        limited = Response()
        limited.status_code = 429
        for error in (Timeout(), HTTPError(response=limited)):
            get_game.side_effect = error
            self.assertEqual(self.client.post(url, token).status_code, 503)
        self.assertFalse(Game.objects.exists())
        self.assertFalse(LibraryEntry.objects.exists())

    @patch("games.services.game_import.get_game_by_id", return_value={"id": 201, "name": "Verified Title"})
    def test_add_endpoints_enforce_csrf(self, get_game):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        browser.get(reverse("home"))
        cookie = browser.cookies["csrftoken"].value
        catalog = self.game("Catalog Game")
        local_url = reverse("add_to_library", args=[catalog.pk])
        external_url = reverse("add_recommended_igdb_game", args=[201])
        data = {"token": self.token(201)}
        self.assertEqual(browser.post(local_url).status_code, 403)
        self.assertEqual(browser.post(external_url, data).status_code, 403)
        get_game.assert_not_called()
        self.assertEqual(browser.post(local_url, HTTP_X_CSRFTOKEN=cookie).status_code, 302)
        self.assertEqual(browser.post(external_url, data, HTTP_X_CSRFTOKEN=cookie).status_code, 200)


class ConcurrentLibraryAddTests(TransactionTestCase):
    def run_in_parallel(self, add):
        def worker():
            try:
                return add()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            return [future.result(timeout=10) for future in futures]

    def test_simultaneous_local_adds_save_one_entry(self):
        player = User.objects.create_user("parallel-player")
        game = Game.objects.create(title="Catalog Game", release_year=2020)
        barrier = Barrier(2)

        def add():
            barrier.wait(timeout=5)
            return add_catalog_game(player, game.pk)[1]

        self.assertCountEqual(self.run_in_parallel(add), [True, False])
        self.assertEqual(LibraryEntry.objects.count(), 1)

    def test_simultaneous_external_adds_save_one_game_and_entry(self):
        player = User.objects.create_user("parallel-external-player")
        barrier = Barrier(2)

        def fetch(igdb_id):
            barrier.wait(timeout=5)
            return {"title": "Verified Game", "release_year": 2020}

        with patch("games.services.library_entries.fetch_igdb_game_defaults", side_effect=fetch):
            outcomes = self.run_in_parallel(lambda: add_igdb_recommendation(player, 201)[1])

        self.assertCountEqual(outcomes, [True, False])
        self.assertEqual(Game.objects.filter(igdb_id=201).count(), 1)
        self.assertEqual(LibraryEntry.objects.count(), 1)
