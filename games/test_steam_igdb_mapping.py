import re
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from requests import HTTPError

from .services import igdb
from .services.steam_igdb_mapping import IgdbMappingError, SteamIgdbMapper


class SteamIgdbMapperTests(SimpleTestCase):
    def setUp(self):
        self.mapper = SteamIgdbMapper()
        self.source = [{"id": 17, "name": "Steam"}]
        query = patch("games.services.steam_igdb_mapping.query_igdb")
        self.query = query.start()
        self.addCleanup(query.stop)

    def row(self, row_id, app_id, game_id, source_id=17):
        return {
            "id": row_id,
            "uid": str(app_id),
            "game": game_id,
            "external_game_source": source_id,
            "name": "Identical title",
        }

    def test_distinguishes_confirmed_missing_ambiguous_and_conflicting(self):
        self.query.side_effect = [
            self.source,
            [
                self.row(1, 101, 11),
                self.row(2, 101, 11),
                self.row(3, 102, 12),
                self.row(4, 102, 13),
                self.row(5, 103, 14, source_id=99),
                self.row(6, 103, None),
                self.row(7, 104, 15),
                self.row(8, 105, 11),
                self.row(9, 999, 19),
            ],
        ]

        matches = self.mapper.resolve(
            [101, 102, 103, 104, 105, 101, 106],
            known_igdb_ids={102: 12, 104: 16, 106: 19},
        )

        self.assertEqual(
            {app_id: match.status for app_id, match in matches.items()},
            {
                101: "matched", 102: "ambiguous", 103: "missing",
                104: "conflict", 105: "matched", 106: "missing",
            },
        )
        self.assertEqual(matches[101].igdb_id, 11)
        self.assertEqual(matches[105].igdb_id, 11)
        self.assertEqual(matches[102].candidate_igdb_ids, (12, 13))
        self.assertEqual(matches[104].candidate_igdb_ids, (15,))
        self.assertIsNone(matches[102].igdb_id)
        self.assertIsNone(matches[104].igdb_id)
        endpoint, query = self.query.call_args.args
        self.assertEqual(endpoint, "external_games")
        self.assertIn("external_game_source = 17", query)
        self.assertIn('uid = ("101","102","103","104","105","106")', query)

    def test_batches_unique_app_ids_and_reuses_the_source_lookup(self):
        def respond(endpoint, query):
            if endpoint == "external_game_sources":
                return self.source
            app_ids = [int(uid) for uid in re.findall(r'"(\d+)"', query)]
            return [self.row(app_id, app_id, app_id + 1000) for app_id in app_ids]

        self.query.side_effect = respond
        requested = list(range(100, 206))
        matches = self.mapper.resolve(reversed(requested + [120, 205]))

        self.assertEqual(list(matches), requested)
        self.assertTrue(all(match.status == "matched" for match in matches.values()))
        calls = self.query.call_args_list
        self.assertEqual([call.args[0] for call in calls],
                         ["external_game_sources", "external_games", "external_games"])
        self.assertEqual(
            [len(re.findall(r'"(\d+)"', call.args[1])) for call in calls[1:]],
            [100, 6],
        )
        self.mapper.resolve([300])
        self.assertEqual(self.query.call_count, 4)
        self.assertEqual(self.query.call_args.args[0], "external_games")

    @patch("games.services.steam_igdb_mapping.IGDB_PAGE_SIZE", 2)
    def test_a_later_page_can_make_a_match_ambiguous(self):
        self.query.side_effect = [
            self.source,
            [self.row(1, 100, 10), self.row(2, 100, 10)],
            [self.row(3, 100, 20)],
        ]

        match = self.mapper.resolve([100])[100]

        self.assertEqual(match.status, "ambiguous")
        self.assertEqual(match.candidate_igdb_ids, (10, 20))
        self.assertIn("id > 2", self.query.call_args.args[1])

    @patch("games.services.steam_igdb_mapping.IGDB_PAGE_SIZE", 1)
    def test_stalled_pagination_returns_no_partial_results(self):
        page = [self.row(1, 100, 10)]
        self.query.side_effect = [self.source, page, page]

        with self.assertRaisesMessage(IgdbMappingError, "pagination did not advance"):
            self.mapper.resolve([100])

    def test_empty_or_invalid_app_ids_do_not_call_igdb(self):
        self.assertEqual(self.mapper.resolve([]), {})
        for app_id in (0, -1, True, "101"):
            with self.subTest(app_id=app_id), self.assertRaises(ValueError):
                self.mapper.resolve([app_id])
        with self.assertRaises(ValueError):
            self.mapper.resolve([101], known_igdb_ids={101: 0})
        self.query.assert_not_called()

    def test_requires_exactly_one_steam_source(self):
        for sources in ([], self.source + [{"id": 18, "name": "STEAM"}]):
            with self.subTest(source_count=len(sources)):
                self.query.return_value = sources
                with self.assertRaisesMessage(IgdbMappingError, "unique Steam source"):
                    SteamIgdbMapper().resolve([100])

    def test_invalid_response_is_not_reported_as_missing(self):
        for rows in (
            {"error": "invalid"}, [None], [{}], [self.row(1, 101, "invalid")],
            [self.row(1, 101, 19, source_id=17.0)],
        ):
            with self.subTest(response_type=type(rows).__name__):
                self.query.side_effect = [self.source, rows]
                with self.assertRaises(IgdbMappingError):
                    SteamIgdbMapper().resolve([101])

    def test_429_is_not_reported_as_missing(self):
        response = Mock(status_code=429)
        self.query.side_effect = [self.source, HTTPError(response=response)]

        with self.assertRaises(HTTPError) as error:
            self.mapper.resolve([101])

        self.assertEqual(error.exception.response.status_code, 429)


@override_settings(IGDB_CLIENT_ID="mock-client", IGDB_CLIENT_SECRET="mock-secret")
class IgdbQueryTests(SimpleTestCase):
    def test_reuses_authentication_and_paces_requests_across_endpoints(self):
        token_response = Mock()
        token_response.json.return_value = {"access_token": "mock-token", "expires_in": 3600}
        source_response = Mock()
        source_response.json.return_value = [{"id": 17, "name": "Steam"}]
        games_response = Mock()
        games_response.json.return_value = [
            {"id": 20, "name": "Example sequel"}, {"id": 19, "name": "Example"}
        ]

        with (
            patch.multiple(igdb, _access_token=None, _token_expires_at=0, _next_query_at=0),
            patch.object(igdb.requests, "post", side_effect=[
                token_response, source_response, games_response
            ]) as post,
            patch.object(igdb.time, "monotonic", side_effect=[10.0, 10.0, 10.1, 10.26]),
            patch.object(igdb.time, "sleep") as sleep,
        ):
            sources = igdb.query_igdb("external_game_sources", "fields id,name;")
            games = igdb.search_games("Example")

        self.assertEqual(sources, [{"id": 17, "name": "Steam"}])
        self.assertEqual([game["id"] for game in games], [19, 20])
        self.assertEqual([call.args[0] for call in post.call_args_list], [
            igdb.TWITCH_TOKEN_URL,
            f"{igdb.IGDB_API_URL}/external_game_sources",
            f"{igdb.IGDB_API_URL}/games",
        ])
        for call in post.call_args_list[1:]:
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer mock-token")
            self.assertEqual(call.kwargs["headers"]["Client-ID"], "mock-client")
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.16)
