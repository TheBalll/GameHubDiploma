from math import log1p
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from games.models import Game, IgdbGameMetadata, LibraryEntry, Review, SteamAccount, SteamIgdbIdentity, SteamOwnedGame
from games.services.recommendation_ranking import _general_score, rank_local_recommendations


class LocalRecommendationRankingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="ranking-player")
        cls.other = User.objects.create_user(username="ranking-other")

    def game(self, title, *, igdb_id=None, app_id=None, genre="", metadata=None):
        if metadata is not None:
            IgdbGameMetadata.objects.create(igdb_id=igdb_id, **metadata)
        return Game.objects.create(
            title=title, igdb_id=igdb_id, steam_id=app_id, genre=genre,
            release_year=2020, developer="Test Studio",
        )

    def save(self, game, status="plan_to_play", user=None):
        return LibraryEntry.objects.create(game=game, user=user or self.user, status=status)

    def review(self, game, rating, user=None):
        return Review.objects.create(game=game, user=user or self.user, rating=rating, comment="Test review")

    def own(self, account, app_id, minutes=None, game=None):
        return SteamOwnedGame.objects.create(
            steam_account=account, steam_app_id=app_id, name="Steam Game", playtime_minutes=minutes, game=game,
        )

    def map_app(self, app_id, igdb_id=None, *, status=SteamIgdbIdentity.Status.MATCHED, candidates=()):
        return SteamIgdbIdentity.objects.create(
            steam_app_id=app_id, metadata_id=igdb_id, status=status,
            last_checked=timezone.now(), candidate_igdb_ids=list(candidates),
        )

    def test_positive_preferences_outrank_quality_for_gamehub_only_user(self):
        for igdb_id in (101, 102):
            liked = self.game("Liked", igdb_id=igdb_id, metadata={
                "genres": [11], "themes": [21], "game_modes": [31], "keywords": [41],
            })
            self.review(liked, 10)

        best = self.game("Best match", igdb_id=201, metadata={
            "genres": [11], "themes": [21], "game_modes": [31], "keywords": [41],
            "total_rating": 10, "total_rating_count": 900,
        })
        partial = self.game("Partial match", igdb_id=202, metadata={
            "genres": [11], "total_rating": 100, "total_rating_count": 2000,
        })
        self.game("Popular but unrelated", igdb_id=203, metadata={
            "genres": [99], "total_rating": 100, "total_rating_count": 10000,
        })

        result = rank_local_recommendations(self.user)
        self.assertEqual(result.mode, "personalized")
        self.assertEqual([item.game_id for item in result.items], [best.pk, partial.pk])
        self.assertEqual(result.items[0].match_score, 100)
        self.assertIsNone(result.items[1].match_score)
        self.assertEqual(
            [(reason.code, reason.feature_ids) for reason in result.items[0].reasons],
            [("preferred_genres", (11,)), ("preferred_themes", (21,)),
             ("preferred_game_modes", (31,)), ("preferred_keywords", (41,))],
        )

    def test_diversity_promotes_a_supported_interest_only_within_the_score_window(self):
        for igdb_id, theme in ((101, 21), (102, 21), (103, 22), (104, 22), (105, 23), (106, 23)):
            self.review(self.game("Liked", igdb_id=igdb_id, metadata={"genres": [11], "themes": [theme]}), 10)
        first = self.game("A first fit", igdb_id=201, metadata={"genres": [11], "themes": [21]})
        repeat = self.game("B repeat fit", igdb_id=202, metadata={"genres": [11], "themes": [21]})
        distinct = self.game("C distinct fit", igdb_id=203, metadata={"genres": [11], "themes": [22]})
        repeat_again = self.game("D repeated theme", igdb_id=204, metadata={"genres": [11], "themes": [21]})
        less_covered = self.game("E less covered theme", igdb_id=205, metadata={"genres": [11], "themes": [22]})
        weak = self.game("F weak fit", igdb_id=206, metadata={"themes": [23]})
        self.game("G missing metadata", igdb_id=207)

        first_two = rank_local_recommendations(self.user, limit=2)
        self.assertEqual([item.game_id for item in first_two.items], [first.pk, distinct.pk])
        all_results = rank_local_recommendations(self.user)
        self.assertEqual([item.game_id for item in all_results.items], [
            first.pk, distinct.pk, repeat.pk, less_covered.pk, repeat_again.pk, weak.pk,
        ])
        for item in all_results.items[:-1]:
            self.assertEqual(item.match_score, 58)
            self.assertAlmostEqual(item.ranking_score, (0.50 + 0.23 / 3) * 0.90 * 0.975)
        self.assertEqual(all_results.items[-1].reasons[0].feature_ids, (23,))
        self.assertTrue(all_results.items[-1].reasons[0].established)
        self.assertEqual(rank_local_recommendations(self.user), all_results)

    def test_a_single_interest_keeps_score_order_without_promoting_unrelated_candidates(self):
        for igdb_id in (101, 102):
            self.review(self.game("Liked", igdb_id=igdb_id, metadata={"genres": [11]}), 10)
        good = self.game("Z higher quality", igdb_id=201, metadata={
            "genres": [11], "total_rating": 90, "total_rating_count": 100,
        })
        plain = self.game("A lower quality", igdb_id=202, metadata={"genres": [11], "themes": [22]})
        self.game("Unrelated", igdb_id=203, metadata={"genres": [12]})

        self.assertEqual([item.game_id for item in rank_local_recommendations(self.user).items], [good.pk, plain.pk])

    def test_disliked_traits_reduce_rank_and_quality_cannot_rescue_incompatible_game(self):
        for igdb_id, genre, rating in ((101, 11, 10), (102, 11, 10), (103, 12, 1)):
            metadata = {"genres": [genre], "themes": [21 + (genre == 12)]}
            self.review(self.game("Signal", igdb_id=igdb_id, metadata=metadata), rating)

        clean = self.game("Clean match", igdb_id=201, metadata={"genres": [11], "themes": [21]})
        mixed = self.game("Both genres", igdb_id=202, metadata={
            "genres": [11, 12], "themes": [21], "total_rating": 100, "total_rating_count": 10000,
        })
        self.game("Incompatible", igdb_id=203, metadata={
            "genres": [12], "similar_games": [101], "total_rating": 100, "total_rating_count": 10000,
        })
        self.game("Disliked only", igdb_id=204, metadata={
            "genres": [12], "themes": [22], "total_rating": 100, "total_rating_count": 10000,
        })

        result = rank_local_recommendations(self.user)
        self.assertEqual([item.game_id for item in result.items[:2]], [clean.pk, mixed.pk])
        self.assertNotIn(204, [item.identity[1] for item in result.items])
        self.assertGreater(result.items[0].match_score, result.items[1].match_score)
        self.assertEqual(result.items[1].reasons[0].feature_ids, (11,))

    def test_excludes_owned_saved_reviewed_and_canonical_aliases_without_title_matching(self):
        account = SteamAccount.objects.create(user=self.user, steam_id="1001")
        saved = self.game("Duplicate title", igdb_id=101)
        self.save(saved)
        self.game("Saved alias", app_id=301)
        IgdbGameMetadata.objects.create(igdb_id=101)
        self.map_app(301, 101)
        self.review(self.game("Reviewed without external IDs"), 5)
        self.game("Owned by exact AppID", app_id=302)
        self.own(account, 302)
        self.game("Owned by mapped IGDB ID", igdb_id=102)
        IgdbGameMetadata.objects.create(igdb_id=102)
        self.map_app(303, 102)
        self.own(account, 303)
        linked = self.game("Owned by explicit catalog link")
        self.own(account, 304, game=linked)
        unrelated = self.game("Duplicate title", igdb_id=103, app_id=305)
        self.map_app(304, status=SteamIgdbIdentity.Status.AMBIGUOUS, candidates=(103, 104))
        unreviewed = self.game("Someone else's review")
        self.review(unreviewed, 10, self.other)

        result = rank_local_recommendations(self.user)
        self.assertEqual(result.mode, "general")
        self.assertEqual([item.game_id for item in result.items], [unreviewed.pk, unrelated.pk])

    def test_collapses_mapped_duplicates_and_uses_explicit_catalog_id_on_conflict(self):
        self.game("First alias", app_id=301)
        self.game("Second alias", app_id=302)
        canonical = self.game("Catalog entry", igdb_id=101, metadata={})
        conflicting = self.game("Same title as another game", igdb_id=102, app_id=303)
        unresolved = self.game("Same title as another game", app_id=304)
        for app_id in (301, 302, 303):
            self.map_app(app_id, 101)
        self.map_app(304, status=SteamIgdbIdentity.Status.CONFLICT, candidates=(101,))

        items = rank_local_recommendations(self.user).items
        self.assertEqual([item.game_id for item in items], [canonical.pk, conflicting.pk, unresolved.pk])
        self.assertEqual([item.identity for item in items], [("igdb", 101), ("igdb", 102), ("steam", 304)])

    def test_steam_only_scores_distinguish_genres_and_need_three_established_games_for_match(self):
        account = SteamAccount.objects.create(user=self.user, steam_id="1001")
        dominant = self.game("Strong fit", igdb_id=201, metadata={"genres": [11], "game_modes": [31]})
        weaker = self.game("Weak fit", igdb_id=202, metadata={"genres": [12], "game_modes": [32]})
        for igdb_id, genre, mode, minutes in ((101, 11, 31, 600), (102, 12, 32, 1)):
            IgdbGameMetadata.objects.create(igdb_id=igdb_id, genres=[genre], game_modes=[mode])
            self.map_app(igdb_id + 200, igdb_id)
            self.own(account, igdb_id + 200, minutes)

        items = rank_local_recommendations(self.user).items
        self.assertEqual([item.game_id for item in items], [dominant.pk, weaker.pk])
        affinity = .67 * .5 * log1p(600) / (log1p(600) + log1p(1))
        self.assertAlmostEqual(items[0].ranking_score, affinity * .90 * .975)
        self.assertIsNone(items[0].match_score)

        for igdb_id, minutes in ((103, 130), (104, 200)):
            IgdbGameMetadata.objects.create(igdb_id=igdb_id, genres=[11], game_modes=[31])
            self.map_app(igdb_id + 200, igdb_id)
            self.own(account, igdb_id + 200, minutes)

        stronger = rank_local_recommendations(self.user).items[0]
        self.assertEqual(stronger.game_id, dominant.pk)
        positive_hours = sum(log1p(minutes) for minutes in (600, 130, 200))
        self.assertEqual(stronger.match_score, round(67 * positive_hours / (positive_hours + log1p(1))))

    def test_match_needs_distinct_sources_relevant_to_candidate_and_multiple_core_features(self):
        self.save(self.game("Explicit source", igdb_id=101, metadata={
            "genres": [11], "themes": [21], "game_modes": [31], "keywords": [41],
        }), "completed")
        self.save(self.game("Other source", igdb_id=102, metadata={
            "genres": [12], "themes": [22], "game_modes": [32], "keywords": [42],
        }), "playing")
        one_source = self.game("One matching source", igdb_id=201, metadata={
            "genres": [11], "themes": [21], "game_modes": [31], "keywords": [41],
        })
        one_core_feature = self.game("One core feature and keywords", igdb_id=202, metadata={
            "genres": [11, 12], "keywords": [41, 42],
        })
        enough = self.game("Enough evidence", igdb_id=203, metadata={"genres": [11], "game_modes": [32]})

        items = {item.game_id: item for item in rank_local_recommendations(self.user).items}
        self.assertIsNone(items[one_source.pk].match_score)
        self.assertIsNone(items[one_core_feature.pk].match_score)
        self.assertEqual(items[enough.pk].match_score, round(100 * (.50 * .60 / 2 + .17 * .40 / 2)))

    def test_similarity_uses_both_directions_with_real_reason_and_no_double_credit(self):
        liked = self.game("Explicit source", igdb_id=101, metadata={"genres": [11], "similar_games": [201, 202, 204]})
        self.review(liked, 10)
        self.review(self.game("Another source", igdb_id=102, metadata={"genres": [11]}), 8)
        forward = self.game("A forward", igdb_id=201, metadata={"genres": [11]})
        both = self.game("B both", igdb_id=202, metadata={"genres": [11], "similar_games": [101, 101]})
        inbound = self.game("C inbound", igdb_id=203, metadata={"genres": [11], "similar_games": [101]})
        self.game("Similar without content", igdb_id=204)

        result = rank_local_recommendations(self.user)
        self.assertEqual(result.mode, "personalized")
        self.assertEqual([item.game_id for item in result.items], [forward.pk, both.pk, inbound.pk])
        self.assertEqual(len({item.ranking_score for item in result.items}), 1)
        for item in result.items:
            self.assertIsNone(item.match_score)
            self.assertEqual([reason.code for reason in item.reasons], ["preferred_genres", "similar_to_enjoyed_games"])
            self.assertEqual(item.reasons[1].source_igdb_ids, (101,))

    def test_similarity_from_steam_playtime_returns_the_played_seed_id(self):
        account = SteamAccount.objects.create(user=self.user, steam_id="1001")
        for igdb_id, minutes in ((101, 400), (102, 15)):
            IgdbGameMetadata.objects.create(igdb_id=igdb_id, genres=[11], similar_games=[igdb_id + 100])
            self.map_app(igdb_id + 200, igdb_id)
            self.own(account, igdb_id + 200, minutes)
        first = self.game("Z long session relation", igdb_id=201, metadata={"genres": [11]})
        second = self.game("A short session relation", igdb_id=202, metadata={"genres": [11]})

        result = rank_local_recommendations(self.user)
        self.assertEqual([item.game_id for item in result.items], [first.pk, second.pk])
        self.assertEqual(
            [(item.reasons[1].code, item.reasons[1].source_igdb_ids) for item in result.items],
            [("similar_to_games_with_significant_playtime", (101,)), ("similar_to_played_games", (102,))],
        )

    def test_single_related_seed_does_not_make_a_sparse_profile_personalized(self):
        linked_source = self.game("Single source", igdb_id=101, metadata={"similar_games": [201]})
        self.review(linked_source, 10)
        candidate = self.game("Candidate", igdb_id=201)

        result = rank_local_recommendations(self.user)
        self.assertEqual(result.mode, "general")
        self.assertEqual([item.game_id for item in result.items], [candidate.pk])
        self.assertIsNone(result.items[0].match_score)

    def test_similarity_only_adjusts_ranking_within_its_weight(self):
        for igdb_id in (101, 102):
            source = self.game("Source", igdb_id=igdb_id, metadata={
                "genres": [11], "themes": [21], "similar_games": [201, 204],
            })
            self.review(source, 10)
        linked = self.game("Both signals", igdb_id=201, metadata={"genres": [11], "themes": [21]})
        content_only = self.game("Matching content", igdb_id=202, metadata={"genres": [11], "themes": [21]})
        self.game("Similar with no affinity", igdb_id=204, metadata={"genres": [99]})

        items = rank_local_recommendations(self.user).items
        self.assertEqual([item.game_id for item in items], [linked.pk, content_only.pk])
        self.assertAlmostEqual(items[0].ranking_score, .73 * .975)
        self.assertAlmostEqual(items[1].ranking_score, .73 * .90 * .975)
        self.assertEqual([item.match_score for item in items], [73, 73])
        self.assertEqual(items[0].reasons[0].code, "preferred_genres")
        self.assertEqual(items[0].reasons[-1].code, "similar_to_enjoyed_games")

    def test_shared_characteristics_cancel_without_erasing_distinct_dislikes(self):
        for igdb_id, genre, rating in ((101, 11, 10), (102, 11, 10), (103, 12, 1), (104, 12, 1)):
            self.review(self.game("Source", igdb_id=igdb_id, metadata={"genres": [genre], "game_modes": [31]}), rating)
        match = self.game("Match", igdb_id=201, metadata={"genres": [11], "game_modes": [31]})
        self.game("Only shared", igdb_id=202, metadata={"game_modes": [31]})
        self.game("Distinct dislike", igdb_id=203, metadata={"genres": [12], "game_modes": [31]})

        [item] = rank_local_recommendations(self.user).items

        self.assertEqual(item.game_id, match.pk)
        self.assertAlmostEqual(item.ranking_score, .50 * .90 * .975)
        self.assertIsNone(item.match_score)
        self.assertEqual([reason.code for reason in item.reasons], ["preferred_genres"])

    def test_diverse_interests_do_not_reward_extra_matching_or_unmatched_tags_in_one_category(self):
        for index, genre in enumerate((11, 11, 12, 12, 13, 13), 101):
            self.review(self.game("Source", igdb_id=index, metadata={"genres": [genre], "themes": [genre + 10]}), 10)
        exact = self.game("A one interest", igdb_id=201, metadata={"genres": [11], "themes": [21]})
        broad = self.game("B three interests", igdb_id=202, metadata={"genres": [11, 12, 13], "themes": [21, 22, 23]})
        extra = self.game("C unmatched extras", igdb_id=203, metadata={"genres": [11, 91], "themes": [21, 92]})

        items = rank_local_recommendations(self.user).items

        self.assertEqual([item.game_id for item in items], [exact.pk, broad.pk, extra.pk])
        for item in items:
            self.assertAlmostEqual(item.ranking_score, .73 / 3 * .90 * .975)
            self.assertEqual(item.match_score, 24)

    def test_match_score_rounds_exact_half_consistently(self):
        for igdb_id, rating, metadata in (
            (101, 10, {"genres": [11], "themes": [21], "keywords": [42]}),
            (102, 10, {"genres": [11]}),
            (103, 1, {"themes": [22], "keywords": [41]}),
        ):
            self.review(self.game("Source", igdb_id=igdb_id, metadata=metadata), rating)
        self.game("Half point match", igdb_id=201, metadata={"genres": [11], "themes": [21], "keywords": [41]})

        [item] = rank_local_recommendations(self.user).items

        self.assertAlmostEqual(item.ranking_score, .565 * .90 * .975)
        self.assertEqual(item.match_score, 56)

    def test_catalog_genre_fallback_uses_reliable_local_labels(self):
        for _ in range(2):
            self.save(self.game("History", genre="Test Genre, Unknown"), "completed")
        matched = self.game("Local label", genre="tEsT Genre, Steam Game")
        self.game("Only a placeholder", genre="Unknown, Steam Game")

        items = rank_local_recommendations(self.user).items
        self.assertEqual([item.game_id for item in items], [matched.pk])
        self.assertIsNone(items[0].match_score)
        self.assertEqual(items[0].reasons[0].labels, ("tEsT Genre",))
        self.assertEqual(items[0].reasons[0].feature_ids, ())

    def test_genre_ids_take_precedence_over_overlapping_catalog_labels(self):
        for igdb_id in (101, 102):
            self.review(self.game("ID source", igdb_id=igdb_id, metadata={"genres": [11]}), 10)
            self.review(self.game("Label source", genre="Local genre"), 10)
        first = self.game("A known genre", igdb_id=201, genre="Local genre", metadata={"genres": [11]})
        self.game("Conflicting label", igdb_id=202, genre="Local genre", metadata={"genres": [12]})
        second = self.game("B fallback genre", genre="Local genre")

        items = rank_local_recommendations(self.user).items
        self.assertEqual([item.game_id for item in items], [first.pk, second.pk])
        self.assertEqual(items[0].reasons[0].feature_ids, (11,))
        self.assertEqual(items[1].reasons[0].labels, ("Local genre",))

    def test_cold_start_uses_review_confidence_and_never_claims_personalization(self):
        prior_game = self.game("No rating", igdb_id=201, metadata={"total_rating": 100})
        one_vote = self.game("One high vote", igdb_id=202, metadata={
            "total_rating": 100, "total_rating_count": 1, "rating": 100, "rating_count": 1,
        })
        consensus = self.game("Many good votes", igdb_id=203, metadata={"rating": 82, "rating_count": 80})
        local_reviews = self.game("Local votes")
        self.review(local_reviews, 10, self.other)
        self.review(local_reviews, 10, self.other)
        self.review(local_reviews, 1, self.other)
        saved = self.game("Saved", igdb_id=101, metadata={"total_rating": 100, "total_rating_count": 4000})
        self.save(saved, "completed")
        self.review(saved, 10)

        result = rank_local_recommendations(self.user)
        self.assertEqual(result.mode, "general")
        self.assertEqual([item.game_id for item in result.items], [consensus.pk, one_vote.pk, prior_game.pk, local_reviews.pk])
        self.assertAlmostEqual(result.items[1].ranking_score, 11 / 21)
        self.assertAlmostEqual(result.items[-1].ranking_score, 10.1 / 21)
        self.assertTrue(all(item.match_score is None for item in result.items))
        self.assertTrue(all(item.reasons[0].code == "general_catalog" for item in result.items))

    def test_negative_only_catalog_stays_available_with_common_and_distinctive_overlap(self):
        disliked_features = {"genres": [11], "themes": [21], "game_modes": [31], "keywords": [41]}
        for igdb_id in (101, 102):
            disliked = self.game("Disliked", igdb_id=igdb_id, metadata=disliked_features)
            self.save(disliked, "dropped")
        quality_fields = {"total_rating": 90, "total_rating_count": 80}
        strong = self.game("A close negative match", igdb_id=201, metadata={
            **disliked_features, **quality_fields,
        })
        weak = self.game("Z shares the common mode", igdb_id=202, metadata={
            "genres": [12], "themes": [22], "game_modes": [31], **quality_fields,
        })

        result = rank_local_recommendations(self.user)

        self.assertEqual(result.mode, "general")
        self.assertEqual([item.game_id for item in result.items], [weak.pk, strong.pk])
        self.assertAlmostEqual(result.items[0].ranking_score, .82 * (1 - .5 * .17 ** 2))
        self.assertAlmostEqual(result.items[1].ranking_score, .82 * .5)
        self.assertTrue(all(item.match_score is None for item in result.items))
        self.assertTrue(all(item.reasons[0].code == "general_catalog" for item in result.items))

    def test_general_score_is_bounded_and_monotone_with_negative_overlap(self):
        penalties = (0, .001, .01, .17, .5, .75, 1)
        for quality in (0, .2, .5, .8, 1):
            with self.subTest(quality=quality):
                scores = [_general_score(quality, penalty) for penalty in penalties]
                self.assertEqual(scores[0], quality)
                self.assertEqual(scores[-1], quality / 2)
                self.assertEqual(scores, sorted(scores, reverse=True))
                self.assertTrue(all(quality / 2 <= score <= quality for score in scores))
        self.assertAlmostEqual(_general_score(.8, .001), .7999996)

    def test_mixed_and_sparse_profiles_keep_eligible_general_candidates(self):
        liked = self.game("Single liked game", igdb_id=101, metadata={"genres": [11], "game_modes": [31]})
        self.review(liked, 10)
        overlapping = self.game("Overlap", igdb_id=201, metadata={"genres": [12], "game_modes": [31]})
        unknown = self.game("No metadata or rating")

        sparse = rank_local_recommendations(self.user)
        self.assertEqual(sparse.mode, "general")
        self.assertEqual([item.ranking_score for item in sparse.items], [.5, .5])

        for igdb_id in (102, 103):
            disliked = self.game("Disliked", igdb_id=igdb_id, metadata={"genres": [12], "game_modes": [31]})
            self.review(disliked, 1)

        result = rank_local_recommendations(self.user)

        self.assertEqual(result.mode, "general")
        self.assertEqual([item.game_id for item in result.items], [unknown.pk, overlapping.pk])
        self.assertEqual(result.items[0].ranking_score, .5)
        self.assertAlmostEqual(result.items[1].ranking_score, .5 * (1 - .5 * (.67 * 2 / 3) ** 2))
        self.assertIsNone(result.items[1].match_score)

    def test_empty_history_orders_by_quality_without_metadata_or_rating_defaults_biasing_it(self):
        rated = self.game("Well rated", igdb_id=201, metadata={"total_rating": 80, "total_rating_count": 80})
        missing = self.game("A without metadata")
        unrated = self.game("B without votes", igdb_id=202, metadata={"total_rating": 100})

        result = rank_local_recommendations(self.user, limit=3)

        self.assertEqual(result.mode, "general")
        self.assertEqual(result, rank_local_recommendations(self.user, limit=3))
        self.assertEqual([item.game_id for item in result.items], [rated.pk, missing.pk, unrated.pk])
        self.assertEqual([item.ranking_score for item in result.items], [.74, .5, .5])
        self.assertEqual(rank_local_recommendations(self.user, limit=2).items, result.items[:2])
        self.assertTrue(all(item.match_score is None for item in result.items))

    def test_general_is_empty_when_every_catalog_identity_is_excluded(self):
        saved = self.game("Saved original", igdb_id=101, metadata={})
        self.save(saved)
        self.game("Saved alias", app_id=301)
        self.map_app(301, 101)
        self.review(self.game("Reviewed original"), 1)
        account = SteamAccount.objects.create(user=self.user, steam_id="1001")
        self.game("Owned original", igdb_id=102, metadata={})
        self.map_app(302, 102)
        self.own(account, 302)

        result = rank_local_recommendations(self.user)

        self.assertEqual(result.mode, "general")
        self.assertEqual(result.items, ())

    def test_users_have_isolated_ordering_and_exclusions(self):
        candidates = {}
        for genre, user in ((11, self.user), (12, self.other)):
            for igdb_id in (genre * 10, genre * 10 + 1):
                source = self.game("Source", igdb_id=igdb_id, metadata={"genres": [genre]})
                self.review(source, 10, user)
            candidates[genre] = self.game("Candidate", igdb_id=genre * 20, metadata={"genres": [genre]})
        self.save(candidates[12], user=self.user)

        first = rank_local_recommendations(self.user)
        second = rank_local_recommendations(self.other)
        self.assertEqual([item.game_id for item in first.items], [candidates[11].pk])
        self.assertEqual(second.items[0].game_id, candidates[12].pk)

    def test_deterministic_ties_and_limit(self):
        self.game("Zulu")
        first = self.game("alpha")
        second = self.game("ALPHA")
        results = rank_local_recommendations(AnonymousUser(), limit=2)
        self.assertEqual(results, rank_local_recommendations(AnonymousUser(), limit=2))
        self.assertEqual([item.game_id for item in results.items], [first.pk, second.pk])
        for limit in (0, -1, True, "2"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                rank_local_recommendations(self.user, limit=limit)

    def test_catalog_growth_does_not_add_per_candidate_queries_or_call_network(self):
        account = SteamAccount.objects.create(user=self.user, steam_id="1001")
        first = self.game("First", igdb_id=101, metadata={"genres": [11]})
        self.review(first, 10)
        IgdbGameMetadata.objects.create(igdb_id=102, genres=[11])
        self.map_app(302, 102)
        self.own(account, 302, 200)
        self.game("Matched", igdb_id=201, metadata={"genres": [11]})
        self.game("Alias", app_id=401)
        self.map_app(401, 201)

        with patch("requests.sessions.Session.request") as request:
            with CaptureQueriesContext(connection) as small_catalog:
                rank_local_recommendations(self.user)
            for index in range(35):
                igdb_id = 500 + index
                self.game("Extra alias", app_id=igdb_id)
                self.game("Extra candidate", igdb_id=igdb_id, metadata={"genres": [11]})
                self.map_app(igdb_id, igdb_id)
            with CaptureQueriesContext(connection) as large_catalog:
                result = rank_local_recommendations(self.user, limit=50)

        request.assert_not_called()
        self.assertEqual(len(result.items), 36)
        self.assertEqual(len(small_catalog), len(large_catalog))
        self.assertLessEqual(len(large_catalog), 11)
        self.assertTrue(all(query["sql"].lstrip().startswith("SELECT") for query in large_catalog))
