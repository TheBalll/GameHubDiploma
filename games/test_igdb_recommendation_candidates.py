from dataclasses import asdict
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from requests import HTTPError, Response, Timeout

from games.models import Game, IgdbGameMetadata, LibraryEntry, Review, SteamAccount, SteamIgdbIdentity, SteamOwnedGame
from games.services.igdb_recommendation_candidates import MAX_EXTERNAL_LIMIT, RATE_LIMIT_KEY
from games.services.recommendation_ranking import rank_local_recommendations, rank_recommendations


@override_settings(CACHES={
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "igdb-ranking-tests"},
})
class ExternalRecommendationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="external-player")
        cls.other = User.objects.create_user(username="external-other")

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        api = patch("games.services.igdb_recommendation_candidates.query_igdb")
        self.api = api.start()
        self.addCleanup(api.stop)
        transport = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected HTTP request"))
        self.transport = transport.start()
        self.addCleanup(transport.stop)
        self.addCleanup(self.transport.assert_not_called)

    def game(self, title, igdb_id=None, app_id=None, **fields):
        return Game.objects.create(
            title=title, igdb_id=igdb_id, steam_id=app_id, release_year=2020, developer="Test Studio", **fields,
        )

    def metadata(self, igdb_id, **fields):
        return IgdbGameMetadata.objects.create(igdb_id=igdb_id, **fields)

    def review(self, game, rating=10, user=None):
        return Review.objects.create(game=game, user=user or self.user, rating=rating, comment="Test review")

    def owned(self, user, app_id):
        account, _ = SteamAccount.objects.get_or_create(user=user, defaults={"steam_id": str(user.pk)})
        return SteamOwnedGame.objects.create(steam_account=account, steam_app_id=app_id, name="Steam Game")

    def matched(self, app_id, igdb_id):
        return SteamIgdbIdentity.objects.create(
            steam_app_id=app_id, metadata_id=igdb_id, status=SteamIgdbIdentity.Status.MATCHED,
            last_checked=timezone.now(),
        )

    def signals(self, *, user=None, genres=True, similar=None):
        for igdb_id in (101, 102):
            game, _ = Game.objects.get_or_create(
                igdb_id=igdb_id,
                defaults={"title": "Seed", "release_year": 2020, "developer": "Test Studio"},
            )
            data = {"genres": [11], "themes": [21], "game_modes": [31]} if genres else {}
            if similar and igdb_id == 101:
                data["similar_games"] = similar
            IgdbGameMetadata.objects.get_or_create(igdb_id=igdb_id, defaults=data)
            self.review(game, user=user)

    def card(self, igdb_id, title="External", *, app_id=None, **fields):
        row = {"id": igdb_id, "name": title, **fields}
        if app_id:
            row["external_games"] = [{"uid": str(app_id), "external_game_source": {"name": "Steam"}}]
        return row

    def test_discovers_similar_games_in_one_batch_without_recommending_unmatched_cards_or_writing(self):
        self.signals(genres=False, similar=[201])
        self.api.return_value = [self.card(201)]
        models = (Game, LibraryEntry, Review, SteamOwnedGame, SteamIgdbIdentity, IgdbGameMetadata)
        counts = [model.objects.count() for model in models]

        with CaptureQueriesContext(connection) as queries:
            result = rank_recommendations(self.user)

        self.api.assert_called_once()
        endpoint, query = self.api.call_args.args
        self.assertEqual(endpoint, "games")
        self.assertIn("where id = (201); limit 1;", query)
        self.assertEqual(result.mode, "general")
        self.assertEqual(result.items, ())
        self.assertEqual([model.objects.count() for model in models], counts)
        self.assertTrue(all(query["sql"].lstrip().startswith("SELECT") for query in queries))

    def test_similar_candidate_budget_is_shared_between_positive_seeds(self):
        self.signals(genres=False, similar=[201, 202, 203])
        self.metadata(103, similar_games=[201, 211, 212])
        self.review(self.game("Another seed", 103))
        self.api.return_value = [self.card(201), self.card(211)]

        rank_recommendations(self.user, external_limit=2)
        rank_recommendations(self.user, external_limit=2)

        self.api.assert_called_once()
        self.assertIn("where id = (201,211); limit 2;", self.api.call_args.args[1])

    def test_genre_discovery_combines_sources_with_identical_scoring_and_real_reasons(self):
        self.signals()
        local = self.game("A local", 201, genre="Adventure", background_image="https://example.test/cover.jpg")
        self.metadata(201, genres=[11], themes=[21], game_modes=[31])
        self.api.side_effect = [
            [{"id": 202}, {"id": 201}],
            [self.card(202, "B external", genres=[{"id": 11, "name": "Adventure"}], themes=[21],
                       game_modes=[31], first_release_date=1609459200, cover={"image_id": "testcover"})],
        ]

        result = rank_recommendations(self.user)

        discovery, detail = [call.args[1] for call in self.api.call_args_list]
        self.assertIn("genres = (11) | themes = (21) | game_modes = (31)", discovery)
        self.assertIn("where id = (202); limit 1;", detail)
        left, right = [asdict(item) for item in result.items]
        self.assertEqual(left["game_id"], local.pk)
        self.assertIsNone(right["game_id"])
        self.assertEqual((left["source"], right["source"]), ("local", "external"))
        self.assertEqual(left["ranking_score"], right["ranking_score"])
        self.assertEqual(left["match_score"], right["match_score"])
        self.assertEqual(right["match_score"], 90)
        self.assertAlmostEqual(right["ranking_score"], .90 * .90 * .975)
        self.assertEqual(left["reasons"], right["reasons"])
        self.assertEqual(right["reasons"][0]["labels"], ("Adventure",))
        self.assertEqual(right["reasons"][0]["feature_ids"], (11,))
        self.assertEqual(right["igdb_id"], 202)
        self.assertEqual(right["release_year"], 2021)
        self.assertTrue(right["cover"].endswith("/testcover.jpg"))
        self.assertEqual(right["genres"], ("Adventure",))
        self.assertEqual(right["genre_ids"], (11,))
        self.assertEqual(left["add_to_library"], {"kind": "gamehub", "id": local.pk})
        self.assertEqual(right["add_to_library"], {"kind": "igdb", "id": 202})

    def test_known_franchise_repeat_is_delayed_only_for_a_close_candidate(self):
        self.signals()
        local = self.game("Z local", 201)
        common = {"genres": [11], "themes": [21], "game_modes": [31]}
        self.metadata(201, **common)
        self.metadata(205, genres=[11], total_rating=100, total_rating_count=10000)
        self.game("Much weaker local", 205)
        self.api.side_effect = [[{"id": 202}, {"id": 203}], [
            self.card(202, "A external", franchises=[71, True], **common),
            self.card(203, "B repeat external", franchises=[71], **common),
        ]]

        items = rank_recommendations(self.user).items

        self.assertEqual([item.igdb_id for item in items], [202, 201, 203, 205])
        self.assertEqual(items[1].game_id, local.pk)
        self.assertEqual(items[0].franchise_ids, (71,))
        self.assertEqual({item.match_score for item in items[:3]}, {90})
        self.assertEqual({item.ranking_score for item in items[:3]}, {.90 * .90 * .975})
        self.assertEqual(rank_recommendations(self.user).items, items)
        self.assertEqual(self.api.call_count, 2)

    def test_negative_preferences_are_applied_to_external_games_and_discovery(self):
        self.signals()
        disliked = self.game("Disliked", 103)
        self.metadata(103, genres=[12], themes=[22])
        self.review(disliked, 1)
        self.api.side_effect = [
            [{"id": 201}, {"id": 202}, {"id": 203}],
            [self.card(201, genres=[11], themes=[21]),
             self.card(202, genres=[11, 12], themes=[21]),
             self.card(203, genres=[12], themes=[22], rating=100, rating_count=9999)],
        ]

        result = rank_recommendations(self.user)

        self.assertIn("genres = (11)", self.api.call_args_list[0].args[1])
        self.assertNotIn("genres = (11,12)", self.api.call_args_list[0].args[1])
        self.assertEqual([item.igdb_id for item in result.items], [201, 202])
        self.assertGreater(result.items[0].ranking_score, result.items[1].ranking_score)

    def test_excludes_known_personal_ids_and_exact_candidate_app_ids(self):
        self.signals(similar=list(range(201, 207)))
        saved = self.game("Saved", 201)
        LibraryEntry.objects.create(user=self.user, game=saved)
        reviewed = self.game("Reviewed", 202)
        self.review(reviewed, 5)
        self.metadata(203)
        self.matched(401, 203)
        self.owned(self.user, 401)
        self.owned(self.user, 402)
        existing = self.game("Existing AppID without a mapping", app_id=403)
        self.api.side_effect = [[], [
            self.card(204, app_id=402, genres=[11]),
            self.card(205, app_id=403, genres=[11]),
            self.card(206, "Saved", genres=[11],
                      external_games=[{"uid": "402", "external_game_source": {"name": "GOG"}}]),
        ]]

        result = rank_recommendations(self.user)

        self.assertIn("where id = (204,205,206);", self.api.call_args.args[1])
        self.assertEqual([item.igdb_id for item in result.items], [205, 206])
        self.assertEqual(result.items[0].game_id, existing.pk)
        self.assertEqual(result.items[0].source, "local")
        self.assertIsNone(Game.objects.get(pk=existing.pk).igdb_id)
        self.assertFalse(SteamIgdbIdentity.objects.filter(pk=403).exists())

    def test_keeps_local_record_unlinked_when_app_id_has_conflicting_candidates(self):
        self.signals(similar=[201, 202])
        existing = self.game("Existing AppID without a mapping", app_id=401)
        self.api.side_effect = [[], [self.card(201, app_id=401, genres=[11]), self.card(202, app_id=401, genres=[11])]]

        result = rank_recommendations(self.user)

        self.assertEqual(result.mode, "general")
        self.assertEqual([item.game_id for item in result.items], [existing.pk])
        self.assertIsNone(result.items[0].igdb_id)
        self.assertFalse(SteamIgdbIdentity.objects.filter(pk=401).exists())

    def test_prefers_existing_local_alias_and_deduplicates_external_app_ids(self):
        self.signals(similar=[201, 202, 203])
        local = self.game("A local", app_id=401)
        self.metadata(201, genres=[11])
        self.matched(401, 201)
        self.api.side_effect = [[], [
            self.card(203, "C external", app_id=402, genres=[11]),
            self.card(202, "B external", app_id=402, genres=[11]),
        ]]

        result = rank_recommendations(self.user)

        self.assertEqual([item.igdb_id for item in result.items], [201, 202])
        self.assertEqual(result.items[0].game_id, local.pk)
        self.assertEqual(result.items[0].source, "local")
        self.assertEqual(result.items[1].source, "external")

    def test_reuses_fresh_database_scoring_and_caches_discovery_and_cards(self):
        self.signals()
        self.metadata(
            201, genres=[11], themes=[21], game_modes=[31], total_rating=95,
            total_rating_count=600, cover_image_id="cachedcover", fetched_at=timezone.now(),
        )
        self.api.side_effect = [[{"id": 201}], [self.card(201)]]

        first = rank_recommendations(self.user)
        second = rank_recommendations(self.user)

        self.assertEqual(first, second)
        self.assertEqual(self.api.call_count, 2)
        fields = self.api.call_args.args[1].split(";")[0]
        self.assertIn("name", fields)
        self.assertNotIn("total_rating", fields)
        self.assertIn("game_modes.name", fields)
        self.assertIn("themes.name", fields)
        self.assertIn("franchises", fields)
        self.assertEqual(first.items[0].match_score, 90)
        self.assertTrue(first.items[0].cover.endswith("/cachedcover.jpg"))

    def test_partial_refresh_reuses_last_valid_scoring_fields(self):
        self.signals()
        self.metadata(201, genres=[11], themes=[21], cover_image_id="knowncover",
                      fetched_at=timezone.now() - timedelta(days=8))
        self.api.side_effect = [
            [{"id": 201}],
            [self.card(201, genres="invalid", themes=[True, -1], game_modes=[31])],
        ]

        item = rank_recommendations(self.user).items[0]

        self.assertEqual(item.genre_ids, (11,))
        self.assertEqual(item.match_score, 90)
        self.assertTrue(item.cover.endswith("/knowncover.jpg"))
        self.assertEqual(IgdbGameMetadata.objects.get(pk=201).genres, [11])
        self.assertIsNone(IgdbGameMetadata.objects.get(pk=201).game_modes)

    def test_bounds_candidate_size_and_api_requests_across_both_hydration_groups(self):
        self.signals(similar=list(range(201, 225)))
        self.metadata(201, genres=[11], fetched_at=timezone.now())
        self.api.side_effect = [
            [{"id": 301, "total_rating_count": 50}, {"id": 302}, {"id": 303}, {"id": 304}],
            [self.card(201, "A")],
            [self.card(202, "B", genres=[11]), self.card(301, "C", genres=[11]), self.card(302, "D", genres=[11])],
        ]

        result = rank_recommendations(self.user, external_limit=4)

        self.assertEqual(self.api.call_count, 3)
        self.assertIn("limit 4;", self.api.call_args_list[0].args[1])
        self.assertIn("where id = (201); limit 1;", self.api.call_args_list[1].args[1])
        self.assertIn("where id = (202,301,302); limit 3;", self.api.call_args_list[2].args[1])
        self.assertEqual({item.igdb_id for item in result.items}, {201, 202, 301, 302})
        self.assertEqual(rank_recommendations(self.user, external_limit=4), result)
        self.assertEqual(self.api.call_count, 3)
        for limit in (-1, MAX_EXTERNAL_LIMIT + 1, True, "4"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                rank_recommendations(self.user, external_limit=limit)
        self.assertEqual(rank_recommendations(self.user, external_limit=0), rank_local_recommendations(self.user))

    def test_timeout_falls_back_to_local_ranking_and_stops_further_requests(self):
        self.signals(similar=[202])
        local = self.game("Local", 201)
        self.metadata(201, genres=[11])
        self.api.side_effect = Timeout()

        result = rank_recommendations(self.user)

        self.api.assert_called_once()
        self.assertEqual(result, rank_local_recommendations(self.user))
        self.assertEqual(result.items[0].game_id, local.pk)

    def test_rate_limit_honors_retry_after_and_reuses_cards_already_fetched(self):
        self.signals(similar=[202])
        self.game("Local", 201)
        self.metadata(201, genres=[11])
        self.metadata(202, genres=[11], fetched_at=timezone.now())
        response = Response()
        response.status_code = 429
        response.headers["Retry-After"] = "83"
        self.api.side_effect = [
            [{"id": 203}],
            [self.card(202)],
            HTTPError(response=response),
        ]

        with patch.object(cache, "set", wraps=cache.set) as set_cache:
            first = rank_recommendations(self.user)
        set_cache.assert_any_call(RATE_LIMIT_KEY, True, timeout=83)
        self.assertEqual(self.api.call_count, 3)
        self.assertEqual({item.igdb_id for item in first.items}, {201, 202})
        self.assertEqual(rank_recommendations(self.user), first)
        self.assertEqual(self.api.call_count, 3)

    def test_bad_responses_fall_back_and_optional_fields_remain_empty(self):
        self.signals(similar=[201, 202, 203])
        local = self.game("Local")
        self.api.return_value = {"message": "unexpected"}
        self.assertEqual(rank_recommendations(self.user).items[0].game_id, local.pk)

        self.api.side_effect = [[], [
            None,
            self.card(202, title=" "),
            self.card(201, genres=[None, True], themes=[21], cover={"image_id": "../broken"}, first_release_date=10**100,
                      total_rating=150, total_rating_count="invalid"),
        ]]

        result = rank_recommendations(self.user)

        self.assertEqual([item.igdb_id for item in result.items], [201])
        item = result.items[0]
        self.assertIsNone(item.cover)
        self.assertIsNone(item.release_year)
        self.assertEqual(item.genres, ())
        self.assertEqual(item.genre_ids, ())
        self.assertIsNone(item.match_score)

    def test_duplicate_detail_ids_are_rejected_without_losing_other_cards(self):
        self.signals(similar=[201, 202, 203])
        self.api.side_effect = [[], [
            self.card(201, "First", genres=[11]), self.card(201, "Conflict", genres=[11]), self.card(202, genres=[11]),
        ]]

        result = rank_recommendations(self.user)

        self.assertEqual([item.igdb_id for item in result.items], [202])

    def test_cold_start_skips_the_api_and_keeps_local_exclusions(self):
        seed = self.game("Single seed", 101)
        self.metadata(101, genres=[11], similar_games=[201])
        self.review(seed)
        saved = self.game("Saved")
        LibraryEntry.objects.create(user=self.user, game=saved)
        generic = self.game("General")

        result = rank_recommendations(self.user)

        self.api.assert_not_called()
        self.assertEqual(result.mode, "general")
        self.assertEqual([item.game_id for item in result.items], [generic.pk])
        self.assertIsNone(result.items[0].match_score)
        self.assertEqual(result.items[0].source, "local")

    def test_shared_public_cache_still_applies_each_users_exclusions(self):
        self.signals()
        self.signals(user=self.other)
        local = self.game("Local", 201)
        self.metadata(201, genres=[11])
        self.api.side_effect = [[{"id": 202}], [self.card(202, app_id=401, genres=[11])]]
        self.owned(self.other, 401)

        first = rank_recommendations(self.user)
        second = rank_recommendations(self.other)

        self.assertEqual(self.api.call_count, 2)
        self.assertEqual({item.igdb_id for item in first.items}, {201, 202})
        self.assertEqual([item.game_id for item in second.items], [local.pk])
        self.assertEqual(rank_recommendations(self.user), first)
