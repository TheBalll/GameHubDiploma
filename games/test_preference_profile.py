from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from games.models import Game, IgdbGameMetadata, LibraryEntry, Review, SteamAccount, SteamIgdbIdentity, SteamOwnedGame
from games.services.preference_profile import (
    PLAYTIME_CAP_MINUTES,
    PLAYTIME_WEIGHT,
    STATUS_SIGNALS,
    STATUS_WEIGHT,
    UserPreferenceProfile,
    build_user_preference_profile,
)


class UserPreferenceProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="profile-player")

    def game(self, title="Test Game", **fields):
        return Game.objects.create(
            title=title, release_year=2020, developer="Test Studio", **fields,
        )

    def connect(self, user=None, steam_id="123"):
        return SteamAccount.objects.create(user=user or self.user, steam_id=steam_id)

    def own(self, account, app_id, minutes=None, game=None, name="Test Game"):
        return SteamOwnedGame.objects.create(
            steam_account=account, steam_app_id=app_id, playtime_minutes=minutes, game=game, name=name,
        )

    def add(self, game, status="plan_to_play", user=None):
        return LibraryEntry.objects.create(user=user or self.user, game=game, status=status)

    def review(self, game, rating, user=None):
        return Review.objects.create(user=user or self.user, game=game, rating=rating, comment="Test review")

    def map_app(self, app_id, metadata=None, status=SteamIgdbIdentity.Status.MATCHED, candidates=()):
        return SteamIgdbIdentity.objects.create(
            steam_app_id=app_id, metadata=metadata, status=status,
            last_checked=timezone.now(), candidate_igdb_ids=list(candidates),
        )

    def profiles_by_id(self, user=None):
        return {game.identity: game for game in build_user_preference_profile(user or self.user).games}

    def test_feature_strength_uses_weighted_shares_and_distinct_support_without_dilution(self):
        for igdb_id, rating, genres in (
            (101, 10, [11, 12, 13, 14, 15]), (102, 7, [11, 12]), (103, 1, [11, 16]),
        ):
            game = self.game(igdb_id=igdb_id)
            self.review(game, rating)
            IgdbGameMetadata.objects.create(igdb_id=igdb_id, genres=genres)
        self.review(game, 1)

        preferences = build_user_preference_profile(self.user).genres

        self.assertEqual(preferences.supporting_games, 3)
        self.assertNotIn(11, preferences.strengths)
        self.assertEqual(preferences.strengths[12], 1)
        self.assertAlmostEqual(preferences.strengths[13], .5 / 1.4)
        self.assertEqual(preferences.strengths[16], -.5)
        self.assertAlmostEqual(preferences.positive[12], (.70 / 5 + .28 / 2) / (2 * .70 + .28))

    def test_merges_both_libraries_and_uses_steam_only_games_and_all_igdb_features(self):
        account = self.connect()
        catalog_game = self.game(igdb_id=101, genre="Legacy genre")
        self.add(catalog_game, "completed")
        self.review(catalog_game, 9)
        metadata = IgdbGameMetadata.objects.create(
            igdb_id=101, genres=[32, 31, 32], themes=[41], game_modes=[51], keywords=[61],
        )
        self.map_app(201, metadata)
        self.map_app(202, metadata)
        self.own(account, 201, 800)
        self.own(account, 202, 400)
        steam_metadata = IgdbGameMetadata.objects.create(
            igdb_id=102, genres=[31], themes=[41], game_modes=[51], keywords=[62],
        )
        self.map_app(203, steam_metadata)
        self.own(account, 203, 200)

        profile = build_user_preference_profile(self.user)
        self.assertEqual([game.identity for game in profile.games], [("igdb", 101), ("igdb", 102)])
        merged, steam_only = profile.games
        self.assertEqual(merged.gamehub_ids, (catalog_game.pk,))
        self.assertEqual(merged.steam_app_ids, (201, 202))
        self.assertTrue(merged.owned_on_steam)
        self.assertEqual(merged.playtime_minutes, 800)
        self.assertEqual(merged.library_status, "completed")
        self.assertEqual(merged.rating, 9)
        self.assertTrue(merged.has_review)
        self.assertEqual(merged.genres, (31, 32))
        self.assertEqual(merged.catalog_genres, ())
        self.assertEqual(steam_only.gamehub_ids, ())
        self.assertIsNone(steam_only.library_status)
        self.assertTrue(profile.has_preferences)
        self.assertEqual(profile.genres.supporting_games, 2)
        self.assertAlmostEqual(profile.genres.positive[31], merged.score / 2 + steam_only.score)
        self.assertAlmostEqual(profile.genres.positive[32], merged.score / 2)
        self.assertSetEqual(set(profile.themes.positive), {41})
        self.assertSetEqual(set(profile.game_modes.positive), {51})
        self.assertSetEqual(set(profile.keywords.positive), {61, 62})

    def test_no_steam_account_and_review_only_game_can_supply_preferences(self):
        saved = self.game(steam_id=201)
        review_only = self.game(igdb_id=102)
        self.add(saved, "playing")
        self.review(review_only, 9)
        metadata = IgdbGameMetadata.objects.create(igdb_id=101, genres=[31])
        self.map_app(201, metadata)
        IgdbGameMetadata.objects.create(igdb_id=102, genres=[31])

        profile = build_user_preference_profile(self.user)
        self.assertEqual(len(profile.games), 2)
        first, second = profile.games
        self.assertEqual(first.identity, ("igdb", 101))
        self.assertEqual(first.steam_app_ids, (201,))
        self.assertFalse(first.owned_on_steam)
        self.assertIsNone(first.playtime_minutes)
        self.assertTrue(second.has_review)
        self.assertIsNone(second.library_status)
        self.assertEqual(profile.genres.supporting_games, 2)

    def test_exact_app_id_wins_over_stale_link_and_conflicting_cached_igdb_id(self):
        account = self.connect()
        actual = self.game(steam_id=201, igdb_id=101)
        other = self.game(igdb_id=102)
        self.add(actual, "playing")
        self.add(other, "completed")
        IgdbGameMetadata.objects.create(igdb_id=101, genres=[31])
        wrong_metadata = IgdbGameMetadata.objects.create(igdb_id=102, genres=[32])
        self.map_app(201, wrong_metadata)
        self.own(account, 201, 60, game=other)

        items = self.profiles_by_id()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[("igdb", 101)].playtime_minutes, 60)
        self.assertEqual(items[("igdb", 101)].genres, (31,))
        self.assertIsNone(items[("igdb", 102)].playtime_minutes)
        self.assertFalse(items[("igdb", 102)].owned_on_steam)

    def test_same_titles_and_unconfirmed_candidates_do_not_merge_games(self):
        account = self.connect()
        catalog_game = self.game(igdb_id=101)
        self.add(catalog_game)
        self.map_app(201, status=SteamIgdbIdentity.Status.MISSING)
        self.map_app(202, status=SteamIgdbIdentity.Status.AMBIGUOUS, candidates=(101, 102))
        self.map_app(203, status=SteamIgdbIdentity.Status.CONFLICT, candidates=(101,))
        for app_id in (201, 202, 203):
            self.own(account, app_id, 60, game=catalog_game)

        items = self.profiles_by_id()
        self.assertEqual(set(items), {("igdb", 101), ("steam", 201), ("steam", 202), ("steam", 203)})
        self.assertFalse(items[("igdb", 101)].owned_on_steam)

    def test_reuses_unambiguous_catalog_link_when_no_explicit_id_conflicts(self):
        account = self.connect()
        linked = self.game()
        self.add(linked, "playing")
        self.own(account, 201, 60, game=linked)
        conflicting = self.game(igdb_id=102)
        self.add(conflicting, "playing")
        self.own(account, 202, 30, game=conflicting)
        metadata = IgdbGameMetadata.objects.create(igdb_id=103)
        self.map_app(202, metadata)

        items = self.profiles_by_id()
        self.assertEqual(set(items), {("steam", 201), ("igdb", 102), ("igdb", 103)})
        self.assertEqual(items[("steam", 201)].gamehub_ids, (linked.pk,))
        self.assertEqual(items[("steam", 201)].playtime_minutes, 60)
        self.assertIsNone(items[("igdb", 102)].playtime_minutes)
        self.assertEqual(items[("igdb", 103)].playtime_minutes, 30)

    def test_personal_ratings_keep_their_sign_and_neutral_reviews_stay_neutral(self):
        account = self.connect()
        for app_id, rating, status in ((201, 7, "dropped"), (202, 4, "completed"), (203, 6, "plan_to_play")):
            game = self.game(steam_id=app_id, igdb_id=app_id)
            IgdbGameMetadata.objects.create(igdb_id=app_id, genres=[31])
            self.add(game, status)
            self.review(game, rating)
            self.own(account, app_id, PLAYTIME_CAP_MINUTES if app_id == 202 else 0)

        profile = build_user_preference_profile(self.user)
        high, low, neutral = profile.games
        self.assertGreater(high.score, 0)
        self.assertLess(low.score, 0)
        self.assertTrue(neutral.has_review)
        self.assertEqual(neutral.score, 0)
        self.assertEqual(profile.genres.supporting_games, 2)
        self.assertAlmostEqual(profile.genres.positive[31], high.score)
        self.assertAlmostEqual(profile.genres.negative[31], abs(low.score))

    def test_library_status_scores_distinguish_engagement_intent_and_dropped_games(self):
        game_ids = {}
        for status in ("playing", "completed", "plan_to_play", "dropped"):
            game = self.game(genre="Test genre")
            self.add(game, status)
            game_ids[status] = game.pk

        items = self.profiles_by_id()
        for status, game_id in game_ids.items():
            with self.subTest(status=status):
                item = items[("gamehub", game_id)]
                self.assertEqual(item.library_status, status)
                self.assertAlmostEqual(item.score, STATUS_SIGNALS[status] * STATUS_WEIGHT)
        profile = build_user_preference_profile(self.user)
        self.assertEqual(profile.catalog_genres.supporting_games, 3)
        self.assertGreater(profile.catalog_genres.positive["test genre"], 0)
        self.assertGreater(profile.catalog_genres.negative["test genre"], 0)

    def test_playtime_is_capped_and_preserves_zero_and_missing_values(self):
        account = self.connect()
        playtimes = (None, 0, 1, 60, PLAYTIME_CAP_MINUTES, PLAYTIME_CAP_MINUTES * 500)
        for app_id, playtime in enumerate(playtimes, start=201):
            self.own(account, app_id, playtime)

        profile = build_user_preference_profile(self.user)
        self.assertEqual(tuple(game.playtime_minutes for game in profile.games), playtimes)
        missing, zero, one_minute, one_hour, capped, extreme = (game.score for game in profile.games)
        self.assertEqual(missing, 0)
        self.assertEqual(zero, 0)
        self.assertGreater(one_minute, 0)
        self.assertGreater(one_hour, one_minute)
        self.assertLess(one_hour, capped)
        self.assertEqual(capped, PLAYTIME_WEIGHT)
        self.assertEqual(extreme, capped)
        self.assertFalse(profile.has_preferences)

    def test_missing_or_partial_metadata_keeps_catalog_genres_separate(self):
        first = self.game(igdb_id=101, genre=" Unknown, Space, space, Steam Game ")
        second = self.game(igdb_id=102, genre="Space, Action")
        IgdbGameMetadata.objects.create(igdb_id=102, themes=[31, 31, True, -1, "32"], keywords={})
        self.add(first, "playing")
        self.add(second, "completed")

        profile = build_user_preference_profile(self.user)
        self.assertEqual(profile.games[0].genres, ())
        self.assertEqual(profile.games[0].catalog_genres, ("space",))
        self.assertEqual(profile.games[1].themes, (31,))
        self.assertEqual(profile.games[1].keywords, ())
        self.assertEqual(profile.genres.supporting_games, 0)
        self.assertEqual(profile.themes.supporting_games, 1)
        self.assertEqual(profile.themes.positive, {})
        self.assertEqual(profile.catalog_genres.supporting_games, 2)
        self.assertSetEqual(set(profile.catalog_genres.positive), {"space", "action"})

    def test_empty_and_sparse_users_do_not_get_aggregate_preferences(self):
        self.assertEqual(build_user_preference_profile(self.user), UserPreferenceProfile())
        with self.assertNumQueries(0):
            self.assertEqual(build_user_preference_profile(AnonymousUser()), UserPreferenceProfile())

        rated = self.game(igdb_id=101)
        neutral = self.game(igdb_id=102)
        for game in (rated, neutral):
            IgdbGameMetadata.objects.create(igdb_id=game.igdb_id, genres=[31])
            self.add(game)
        self.review(rated, 10)

        profile = build_user_preference_profile(self.user)
        self.assertFalse(profile.has_preferences)
        self.assertEqual(profile.genres.supporting_games, 1)
        self.assertEqual(profile.genres.positive, {})
        self.assertGreater(profile.games[0].score, 0)
        self.assertEqual(profile.games[1].score, 0)

    def test_users_share_metadata_but_their_library_review_and_playtime_are_isolated(self):
        other_user = User.objects.create_user(username="another-player")
        first_account = self.connect()
        other_account = self.connect(other_user, "456")
        shared = self.game(steam_id=201, igdb_id=101)
        metadata = IgdbGameMetadata.objects.create(igdb_id=101, genres=[31])
        self.map_app(201, metadata)
        self.own(first_account, 201, 30)
        self.own(other_account, 201, 900)
        self.own(other_account, 202, 75)
        self.add(shared, "playing", self.user)
        self.add(shared, "dropped", other_user)
        self.review(shared, 10, self.user)
        self.review(shared, 1, other_user)

        first = build_user_preference_profile(self.user).games
        other = build_user_preference_profile(other_user).games
        self.assertEqual(len(first), 1)
        self.assertEqual(len(other), 2)
        self.assertEqual(first[0].genres, other[0].genres)
        self.assertEqual((first[0].playtime_minutes, first[0].library_status, first[0].rating), (30, "playing", 10))
        self.assertEqual((other[0].playtime_minutes, other[0].library_status, other[0].rating), (900, "dropped", 1))

    def test_results_are_deterministic_and_normalized_with_multiple_signals(self):
        date = timezone.now()
        for igdb_id, rating in ((101, 1), (102, 10), (103, 10)):
            game = self.game(igdb_id=igdb_id)
            metadata = IgdbGameMetadata.objects.create(igdb_id=igdb_id, genres=[32, 31])
            alias = self.game(steam_id=igdb_id + 100)
            self.map_app(alias.steam_id, metadata)
            self.add(game, "playing")
            self.add(alias, "completed")
            self.review(game, 5)
            self.review(game, rating)
        LibraryEntry.objects.filter(user=self.user).update(added_at=date)
        Review.objects.filter(user=self.user).update(created_at=date)

        with patch("requests.sessions.Session.request") as request, CaptureQueriesContext(connection) as queries:
            first = build_user_preference_profile(self.user)
            second = build_user_preference_profile(self.user)

        request.assert_not_called()
        self.assertTrue(all(query["sql"].lstrip().startswith("SELECT") for query in queries))
        self.assertEqual(first, second)
        self.assertEqual([game.library_status for game in first.games], ["completed"] * 3)
        self.assertEqual([game.rating for game in first.games], [1, 10, 10])
        self.assertEqual(list(first.genres.positive), [31, 32])
        self.assertEqual(list(first.genres.negative), [31, 32])
        self.assertAlmostEqual(sum(first.genres.positive.values()) + sum(first.genres.negative.values()), 1.0)
