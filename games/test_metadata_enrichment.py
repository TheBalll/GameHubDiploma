import re
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from io import StringIO
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from requests import HTTPError, Response, Timeout

from .models import Game, IgdbGameMetadata, LibraryEntry, Review, SteamAccount, SteamIgdbIdentity, SteamOwnedGame
from .services.metadata_enrichment import (
    AMBIGUOUS_MAPPING_TTL,
    CONFIRMED_MAPPING_TTL,
    CONFLICT_MAPPING_TTL,
    GAME_METADATA_TTL,
    MISSING_MAPPING_TTL,
    enrich_steam_metadata,
)


class MetadataModelTests(TestCase):
    def test_each_identity_is_unique_and_several_apps_can_share_metadata(self):
        metadata = IgdbGameMetadata.objects.create(igdb_id=10)
        for app_id in (101, 102):
            SteamIgdbIdentity.objects.create(
                steam_app_id=app_id, metadata=metadata, status="matched", last_checked=timezone.now()
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            IgdbGameMetadata.objects.create(igdb_id=10)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SteamIgdbIdentity.objects.create(steam_app_id=101, status="missing", last_checked=timezone.now())

        self.assertEqual(metadata.steam_identities.count(), 2)
        self.assertIsNone(metadata.rating)

    def test_database_checks_positive_ids_rating_bounds_and_identity_relationships(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            IgdbGameMetadata.objects.create(igdb_id=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            IgdbGameMetadata.objects.create(igdb_id=10, total_rating=101)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SteamIgdbIdentity.objects.create(steam_app_id=101, status="matched", last_checked=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            SteamIgdbIdentity.objects.create(
                steam_app_id=101, status="missing", last_checked=timezone.now(), candidate_igdb_ids=[10]
            )

    def test_model_validation_rejects_invalid_id_lists_and_ambiguity_without_distinct_ids(self):
        with self.assertRaises(ValidationError):
            IgdbGameMetadata(igdb_id=10, themes=[True]).full_clean()
        with self.assertRaises(ValidationError):
            SteamIgdbIdentity(
                steam_app_id=101, status="ambiguous", last_checked=timezone.now(), candidate_igdb_ids=[10, 10],
            ).full_clean()


class MetadataEnrichmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.user = user_model.objects.create_user("metadata-a")
        cls.other_user = user_model.objects.create_user("metadata-b")
        cls.account = SteamAccount.objects.create(user=cls.user, steam_id="1")
        cls.other_account = SteamAccount.objects.create(user=cls.other_user, steam_id="2")

    def setUp(self):
        self.now = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
        self.mapped_ids = {}
        self.game_rows = {}
        self.api = Mock(side_effect=self.respond)
        for target in (
            "games.services.metadata_enrichment.query_igdb",
            "games.services.steam_igdb_mapping.query_igdb",
        ):
            api_patch = patch(target, self.api)
            api_patch.start()
            self.addCleanup(api_patch.stop)
        clock = patch("games.services.metadata_enrichment.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def own(self, app_id, minutes=None, *, account=None, game=None):
        return SteamOwnedGame.objects.create(
            steam_app_id=app_id,
            steam_account=self.account if account is None else account,
            name=f"Fixture game {app_id}",
            playtime_minutes=minutes,
            game=game,
        )

    def respond(self, endpoint, query):
        if endpoint == "external_game_sources":
            return [{"id": 7, "name": "Steam"}]
        if endpoint == "external_games":
            app_ids = [int(uid) for uid in re.findall(r'"(\d+)"', query)]
            return [
                {"id": index, "uid": str(app_id), "game": igdb_id, "external_game_source": 7}
                for index, (app_id, igdb_id) in enumerate(
                    ((app_id, igdb_id) for app_id in app_ids for igdb_id in self.mapped_ids.get(app_id, ())),
                    start=1,
                )
            ]
        self.assertEqual(endpoint, "games")
        ids = [int(item) for item in re.search(r"where id = \(([\d,]+)\)", query).group(1).split(",")]
        return [self.game_rows[igdb_id] for igdb_id in ids if igdb_id in self.game_rows]

    def queries(self, endpoint):
        return [call.args[1] for call in self.api.call_args_list if call.args[0] == endpoint]

    def cache_match(self, app_id, igdb_id, *, checked_at=None, fetched_at=None):
        metadata = IgdbGameMetadata.objects.create(
            igdb_id=igdb_id, genres=[1], rating=84, rating_count=20,
            fetched_at=self.now - GAME_METADATA_TTL if fetched_at is None else fetched_at,
        )
        return SteamIgdbIdentity.objects.create(
            steam_app_id=app_id, metadata=metadata, status="matched",
            last_checked=self.now if checked_at is None else checked_at,
        )

    def test_shares_metadata_between_users_and_multiple_app_ids(self):
        self.own(101, 20)
        self.own(101, 400, account=self.other_account)
        self.own(102, 90, account=self.other_account)
        self.mapped_ids.update({101: [10], 102: [10]})
        self.game_rows[10] = {"id": 10, "genres": [3]}

        first = enrich_steam_metadata(steam_games=self.account.owned_games.all())
        second = enrich_steam_metadata(steam_games=self.other_account.owned_games.all())

        self.assertEqual((first.enriched, second.enriched, second.skipped), (1, 1, 1))
        self.assertEqual(len(self.queries("games")), 1)
        self.assertEqual(IgdbGameMetadata.objects.count(), 1)
        self.assertEqual(SteamIgdbIdentity.objects.filter(metadata_id=10).count(), 2)
        self.assertNotIn('"101"', self.queries("external_games")[-1])

    @patch("games.services.metadata_enrichment.APP_IDS_PER_QUERY", 2)
    @patch("games.services.metadata_enrichment.METADATA_BATCH_SIZE", 2)
    def test_batches_by_priority_and_limit_advances_past_fresh_apps(self):
        for app_id, minutes in ((101, None), (102, 0), (103, 5), (104, 90), (105, 30)):
            self.own(app_id, minutes)
            self.mapped_ids[app_id] = [app_id + 1000]
            self.game_rows[app_id + 1000] = {"id": app_id + 1000, "genres": [3]}
        self.own(105, 120, account=self.other_account)

        first = enrich_steam_metadata(limit=3)
        self.assertEqual((first.total, first.enriched, first.deferred), (5, 3, 2))
        self.assertEqual(
            [re.findall(r'"(\d+)"', query) for query in self.queries("external_games")],
            [["104", "105"], ["103"]],
        )
        self.assertEqual(len(self.queries("external_game_sources")), 1)
        self.assertEqual(len(self.queries("games")), 2)
        self.assertIn("id = (1105,1104)", self.queries("games")[0])
        self.api.reset_mock()

        second = enrich_steam_metadata(limit=1)
        self.assertEqual((second.enriched, second.skipped, second.deferred), (1, 3, 1))
        self.assertIn('uid = ("102")', self.queries("external_games")[0])

    def test_mapping_and_metadata_expire_independently(self):
        self.own(101)
        self.own(102)
        old_metadata = self.cache_match(101, 10)
        old_mapping = self.cache_match(
            102, 11, checked_at=self.now - CONFIRMED_MAPPING_TTL, fetched_at=self.now
        )
        self.mapped_ids[102] = [11]
        self.game_rows[10] = {"id": 10, "genres": [5]}

        summary = enrich_steam_metadata()

        self.assertEqual(summary.enriched, 2)
        self.assertIn('uid = ("102")', self.queries("external_games")[0])
        self.assertIn("where id = (10)", self.queries("games")[0])
        self.assertEqual(IgdbGameMetadata.objects.get(pk=10).genres, [5])
        old_metadata.refresh_from_db()
        old_mapping.refresh_from_db()
        self.assertEqual(old_metadata.last_checked, self.now)
        self.assertEqual(old_mapping.last_checked, self.now)

    def test_unresolved_statuses_are_rechecked_only_on_expiry(self):
        for app_id, status, ttl, candidates in (
            (101, "missing", MISSING_MAPPING_TTL, []),
            (102, "ambiguous", AMBIGUOUS_MAPPING_TTL, [10, 11]),
            (103, "conflict", CONFLICT_MAPPING_TTL, [12]),
        ):
            self.own(app_id)
            SteamIgdbIdentity.objects.create(
                steam_app_id=app_id, status=status, candidate_igdb_ids=candidates,
                last_checked=self.now - ttl + timedelta(seconds=1),
            )

        self.assertEqual(enrich_steam_metadata().skipped, 3)
        self.api.assert_not_called()
        Game.objects.create(title="Known game", release_year=2020, steam_id=103, igdb_id=13)
        self.mapped_ids.update({102: [10, 11], 103: [12]})
        with patch("games.services.metadata_enrichment.timezone.now", return_value=self.now + timedelta(seconds=1)):
            summary = enrich_steam_metadata()
        self.assertEqual((summary.missing, summary.ambiguous, summary.conflict, summary.failed), (1, 1, 1, 0))
        self.assertFalse(IgdbGameMetadata.objects.exists())
        self.assertEqual(SteamIgdbIdentity.objects.get(pk=102).candidate_igdb_ids, [10, 11])
        self.assertEqual(SteamIgdbIdentity.objects.get(pk=103).status, "conflict")
        self.assertEqual(SteamIgdbIdentity.objects.get(pk=103).candidate_igdb_ids, [12])

    def test_force_refresh_updates_fresh_records_without_duplicates(self):
        self.own(101)
        self.mapped_ids[101] = [10]
        self.game_rows[10] = {"id": 10, "genres": [3]}
        enrich_steam_metadata()
        self.api.reset_mock()
        self.assertEqual(enrich_steam_metadata().skipped, 1)
        self.api.assert_not_called()
        self.game_rows[10]["genres"] = [5]

        summary = enrich_steam_metadata(force=True)

        self.assertEqual(summary.enriched, 1)
        self.assertEqual(len(self.queries("external_games")), 1)
        self.assertEqual(len(self.queries("games")), 1)
        self.assertEqual(IgdbGameMetadata.objects.get().genres, [5])
        self.assertEqual(SteamIgdbIdentity.objects.count(), 1)

    def test_command_dry_run_performs_reads_only_even_with_force(self):
        self.own(101, 120)
        self.own(102, 0)
        self.cache_match(101, 10, fetched_at=self.now)
        self.mapped_ids[101] = [10]
        self.game_rows[10] = {"id": 10, "genres": [9]}
        out = StringIO()

        with CaptureQueriesContext(connection) as queries:
            call_command("enrich_steam_metadata", "--dry-run", "--force", "--limit", "1", stdout=out)

        self.assertIn("Would enrich: 1", out.getvalue())
        self.assertIn("Deferred: 1", out.getvalue())
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries))
        self.assertEqual(IgdbGameMetadata.objects.get().genres, [1])
        self.assertEqual(SteamIgdbIdentity.objects.count(), 1)

    def test_command_rejects_invalid_limit_before_calling_igdb(self):
        with self.assertRaisesMessage(CommandError, "Limit must be a positive integer"):
            call_command("enrich_steam_metadata", "--limit", "0")
        self.api.assert_not_called()

    @patch("games.services.metadata_enrichment.METADATA_BATCH_SIZE", 1)
    def test_429_honors_retry_after_and_defers_further_batches(self):
        for app_id in (101, 102):
            self.own(app_id)
            self.cache_match(app_id, app_id + 100)
        response = Response()
        response.status_code = 429
        response.headers["Retry-After"] = format_datetime(self.now + timedelta(seconds=90), usegmt=True)
        self.api.side_effect = HTTPError(response=response)
        stderr = StringIO()
        out = StringIO()

        with self.assertRaises(CommandError):
            call_command("enrich_steam_metadata", stdout=out, stderr=stderr)

        self.assertIn("Retry after 90 seconds", stderr.getvalue())
        self.assertIn("Failed: 1. Deferred: 1", out.getvalue())
        self.assertEqual(self.api.call_count, 1)
        self.assertEqual(
            IgdbGameMetadata.objects.filter(fetched_at=self.now - GAME_METADATA_TTL).count(), 2
        )

    @patch("games.services.metadata_enrichment.APP_IDS_PER_QUERY", 1)
    def test_429_during_mapping_leaves_preceding_matches_and_defers_metadata(self):
        for app_id in (101, 102, 103):
            self.own(app_id)
        response = Response()
        response.status_code = 429
        response.headers["Retry-After"] = "120"
        self.api.side_effect = [
            [{"id": 7, "name": "Steam"}],
            [{"id": 1, "uid": "101", "game": 10, "external_game_source": 7}],
            HTTPError(response=response),
        ]

        summary = enrich_steam_metadata()

        self.assertEqual((summary.failed, summary.deferred, summary.retry_after_seconds), (1, 2, 120))
        self.assertEqual(SteamIgdbIdentity.objects.get().metadata_id, 10)
        self.assertIsNone(IgdbGameMetadata.objects.get().fetched_at)
        self.assertEqual(self.queries("games"), [])

    def test_partial_metadata_updates_only_usable_rows_and_preserves_other_values(self):
        for app_id in range(101, 105):
            self.own(app_id)
            self.cache_match(app_id, app_id + 100)
        self.api.side_effect = [[
            {"id": 201, "themes": [7, 7], "rating": None, "total_rating": 0, "total_rating_count": 0},
            {"id": 202, "themes": [8]},
            {"id": 202, "themes": [9]},
            {"id": 204, "genres": "not a list"},
            {"id": 999, "genres": [8]},
        ]]

        summary = enrich_steam_metadata()

        self.assertEqual((summary.enriched, summary.failed), (1, 3))
        updated = IgdbGameMetadata.objects.get(pk=201)
        self.assertEqual(updated.themes, [7])
        self.assertEqual(updated.genres, [1])
        self.assertEqual(updated.rating, 84)
        self.assertIsNone(updated.total_rating)
        self.assertEqual(updated.total_rating_count, 0)
        self.assertEqual(updated.fetched_at, self.now)
        self.assertEqual(IgdbGameMetadata.objects.count(), 4)
        self.assertEqual(IgdbGameMetadata.objects.filter(fetched_at=self.now - GAME_METADATA_TTL).count(), 3)

    def test_missing_metadata_leaves_confirmed_mapping_for_a_metadata_only_retry(self):
        self.own(101)
        self.mapped_ids[101] = [10]
        self.assertEqual(enrich_steam_metadata().failed, 1)
        self.assertEqual(SteamIgdbIdentity.objects.get().status, "matched")
        self.assertIsNone(IgdbGameMetadata.objects.get().fetched_at)
        self.api.reset_mock()
        self.game_rows[10] = {"id": 10, "genres": [], "rating": 0}

        summary = enrich_steam_metadata()

        self.assertEqual(summary.enriched, 1)
        self.assertEqual(self.queries("external_games"), [])
        self.assertEqual(IgdbGameMetadata.objects.get().genres, [])
        self.assertIsNone(IgdbGameMetadata.objects.get().rating)

    def test_failed_mapping_requests_do_not_replace_confirmed_results(self):
        self.own(101)
        checked_at = self.now - CONFIRMED_MAPPING_TTL
        self.cache_match(101, 10, checked_at=checked_at)
        for response in (Timeout(), [{}]):
            with self.subTest(response_type=type(response).__name__):
                self.api.side_effect = [[{"id": 7, "name": "Steam"}], response]
                summary = enrich_steam_metadata()
                identity = SteamIgdbIdentity.objects.get(pk=101)
                self.assertEqual((summary.failed, summary.missing), (1, 0))
                self.assertEqual((identity.status, identity.metadata_id, identity.last_checked), ("matched", 10, checked_at))
                self.assertEqual(IgdbGameMetadata.objects.get(pk=10).rating, 84)

    def test_http_failure_during_metadata_refresh_preserves_last_success(self):
        self.own(101)
        self.cache_match(101, 10)
        self.api.side_effect = Timeout()

        summary = enrich_steam_metadata()

        metadata = IgdbGameMetadata.objects.get(pk=10)
        self.assertEqual(summary.failed, 1)
        self.assertEqual((metadata.rating, metadata.fetched_at), (84, self.now - GAME_METADATA_TTL))

    def test_a_late_response_cannot_overwrite_a_newer_metadata_refresh(self):
        self.own(101)
        self.cache_match(101, 10)
        later = self.now + timedelta(seconds=5)

        def refresh_during_request(endpoint, query):
            self.assertEqual(endpoint, "games")
            IgdbGameMetadata.objects.filter(pk=10).update(genres=[9], fetched_at=later)
            return [{"id": 10, "genres": [3]}]

        self.api.side_effect = refresh_during_request

        self.assertEqual(enrich_steam_metadata().enriched, 1)
        metadata = IgdbGameMetadata.objects.get(pk=10)
        self.assertEqual((metadata.genres, metadata.fetched_at), ([9], later))

    def test_enrichment_changes_only_shared_metadata_tables(self):
        game = Game.objects.create(title="Local fixture", genre="Genre", release_year=2020, igdb_id=10, steam_id=101)
        LibraryEntry.objects.create(user=self.user, game=game, status="dropped")
        Review.objects.create(user=self.user, game=game, rating=8, comment="Fixture review")
        self.own(101, 180, game=game)
        self.own(102, 80)
        self.mapped_ids.update({101: [10], 102: [11]})
        self.game_rows.update({
            10: {"id": 10, "genres": [3], "cover": {"image_id": "fixture-cover"}, "updated_at": 1600000000},
            11: {"id": 11, "game_modes": [4], "keywords": [5], "similar_games": [10], "rating": 0, "rating_count": 1},
        })
        models = (Game, LibraryEntry, Review, SteamAccount, SteamOwnedGame)
        before = {model: list(model.objects.order_by("pk").values()) for model in models}

        summary = enrich_steam_metadata()

        self.assertEqual(summary.enriched, 2)
        self.assertEqual(IgdbGameMetadata.objects.count(), 2)
        self.assertEqual(IgdbGameMetadata.objects.get(pk=10).igdb_updated_at, datetime.fromtimestamp(1600000000, tz=UTC))
        self.assertEqual(IgdbGameMetadata.objects.get(pk=11).rating, 0)
        self.assertEqual({model: list(model.objects.order_by("pk").values()) for model in models}, before)
