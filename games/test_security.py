from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from games.models import FriendRequest, Game, IgdbGameMetadata, LibraryEntry, Review, SteamAccount, SteamOwnedGame


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


class LogoutSecurityTests(TestCase):
    def test_get_and_post_without_csrf_keep_the_session(self):
        user = get_user_model().objects.create_user(username="signed-in-player")
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        logout_url = reverse("logout")

        self.assertEqual(client.get(logout_url).status_code, 405)
        self.assertEqual(client.post(logout_url).status_code, 403)
        self.assertEqual(client.get(reverse("profile")).status_code, 200)

    def test_registration_login_and_logout_keep_their_redirects(self):
        client = Client(enforce_csrf_checks=True)
        password = "test-only-diploma-registration-617!"
        register_url = reverse("register")
        login_url = reverse("login")
        logout_url = reverse("logout")

        client.get(register_url)
        registered = client.post(register_url, {
            "username": "new-player", "password1": password, "password2": password,
            "csrfmiddlewaretoken": client.cookies[settings.CSRF_COOKIE_NAME].value,
        })
        self.assertRedirects(registered, login_url)
        logged_in = client.post(login_url, {
            "username": "new-player", "password": password,
            "csrfmiddlewaretoken": client.cookies[settings.CSRF_COOKIE_NAME].value,
        })
        self.assertRedirects(logged_in, reverse("home"))

        page = client.get(reverse("home"))
        self.assertContains(page, f'<form method="post" action="{logout_url}">')
        self.assertNotContains(page, f'href="{logout_url}"')
        logged_out = client.post(logout_url, {
            "csrfmiddlewaretoken": client.cookies[settings.CSRF_COOKIE_NAME].value,
        })
        self.assertRedirects(logged_out, reverse("home"))
        profile = client.get(reverse("profile"))
        self.assertEqual(profile.status_code, 302)
        self.assertFalse(profile.wsgi_request.user.is_authenticated)


class LoginRedirectTests(TestCase):
    credentials = {"username": "redirect-player", "password": "test-only-login-redirect-441!"}

    @classmethod
    def setUpTestData(cls):
        get_user_model().objects.create_user(**cls.credentials)

    def test_protected_routes_send_guests_to_the_named_login_page(self):
        routes = {
            "get": (
                ("profile", ()), ("edit_profile", ()), ("public_profile", (1,)),
                ("edit_review", (1,)), ("edit_library_entry", (1,)), ("library", ()), ("library_data", ()),
                ("friend_requests_page", ()), ("find_friends", ()), ("my_friends", ()),
                ("steam_settings", ()), ("steam_callback", ()), ("steam_library", ()),
            ),
            "post": (
                ("add_to_library", (1,)), ("remove_from_library", (1,)), ("add_steam_game_to_library", (1,)),
                ("delete_review", (1,)), ("delete_review_from_profile", (1,)), ("send_friend_request", (1,)),
                ("accept_friend_request", (1,)), ("decline_friend_request", (1,)),
                ("cancel_friend_request", (1,)), ("remove_friend", (1,)),
                ("steam_connect", ()), ("steam_sync_library", ()),
            ),
        }
        for method, endpoints in routes.items():
            for name, args in endpoints:
                with self.subTest(method=method, name=name):
                    url = reverse(name, args=args)
                    response = getattr(self.client, method)(url)
                    self.assertRedirects(response, f"{reverse('login')}?next={url}")

    def test_normal_login_preserves_next_query_and_keeps_it_after_an_error(self):
        target = reverse("profile") + "?tab=reviews&sort=newest"
        login_url = reverse("login")
        start = self.client.get(target)
        self.assertEqual(parse_qs(urlsplit(start.url).query), {"next": [target]})

        hidden_field = f'<input type="hidden" name="next" value="{target.replace("&", "&amp;")}">'
        self.assertContains(self.client.get(start.url), hidden_field)
        failed = self.client.post(login_url, {**self.credentials, "password": "incorrect", "next": target})
        self.assertContains(failed, hidden_field)
        result = self.client.post(login_url, {**self.credentials, "next": target})
        self.assertRedirects(result, target)

    def test_ajax_login_keeps_json_contract_and_validates_same_host_destinations(self):
        login_url = reverse("login")
        target = f"https://testserver{reverse('profile')}"
        response = self.client.post(
            login_url, {**self.credentials, "next": target}, secure=True, HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True, "redirect_url": target})

    def test_missing_next_returns_home_and_invalid_credentials_keep_json_error(self):
        login_url = reverse("login")
        ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
        failed = self.client.post(login_url, {**self.credentials, "password": "incorrect"}, **ajax)
        self.assertEqual(failed.status_code, 400)
        self.assertEqual(failed.json(), {"success": False, "message": "Invalid username or password."})
        success = self.client.post(login_url, self.credentials, **ajax)
        self.assertEqual(success.json(), {"success": True, "redirect_url": reverse("home")})

    def test_unsafe_next_falls_back_to_home_for_both_login_methods(self):
        for target in ("https://example.test/", "//example.test/", r"/\example.test/", "http://testserver/profile/"):
            with self.subTest(target=target):
                login_url = reverse("login")
                form_client = Client()
                get_response = form_client.get(login_url, {"next": target}, secure=True)
                self.assertNotContains(get_response, 'name="next"')
                result = form_client.post(login_url, {**self.credentials, "next": target}, secure=True)
                self.assertRedirects(result, reverse("home"), fetch_redirect_response=False)

                ajax = Client().post(
                    login_url, {**self.credentials, "next": target},
                    secure=True, HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                )
                self.assertEqual(ajax.json(), {"success": True, "redirect_url": reverse("home")})

    def test_json_endpoints_keep_their_authentication_responses(self):
        library = reverse("library_data")
        response = self.client.get(library, HTTP_ACCEPT="application/json")
        self.assertRedirects(response, f"{reverse('login')}?next={library}")
        for name, method, args in (
            ("recommendation_data", "get", ()), ("add_recommended_igdb_game", "post", (1,)),
            ("import_igdb_game", "post", (1,)), ("import_steam_game", "post", (1,)),
        ):
            with self.subTest(name=name):
                response = getattr(self.client, method)(reverse(name, args=args), HTTP_ACCEPT="application/json")
                self.assertEqual(response.status_code, 401)
                self.assertIn("error", response.json())


class PhaseCRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.user = users.create_user(username="regression-player")
        cls.other = users.create_user(username="regression-friend")
        cls.game = Game.objects.create(title="Regression fixture", genre="Puzzle", release_year=2022)

    def setUp(self):
        self.client.force_login(self.user)
        transport = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected HTTP request"))
        http = transport.start()
        self.addCleanup(transport.stop)
        self.addCleanup(http.assert_not_called)

    def test_catalog_game_details_ratings_and_profiles(self):
        self.assertContains(self.client.get(reverse("games_list")), self.game.title)
        edit = self.client.post(reverse("edit_profile"), {
            "username": self.user.username, "profile_name": "Regression Display Name", "bio": "Fixture profile",
        })
        self.assertRedirects(edit, reverse("profile"))

        detail_url = reverse("game_detail", args=[self.game.pk])
        review_response = self.client.post(
            detail_url, {"rating": 8, "comment": "Regression review"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(review_response.status_code, 201)
        self.assertEqual(Review.objects.get(user=self.user, game=self.game).rating, 8)
        self.assertContains(self.client.get(detail_url), "Regression review")
        self.assertContains(self.client.get(reverse("profile")), "Regression review")
        self.client.force_login(self.other)
        page = self.client.get(reverse("public_profile", args=[self.user.pk]))
        self.assertContains(page, "Regression Display Name")
        self.assertContains(page, "Regression review")

    def test_only_the_receiver_can_accept_a_friend_request(self):
        self.assertEqual(self.client.post(reverse("send_friend_request", args=[self.other.pk])).status_code, 302)
        pending = FriendRequest.objects.get(sender=self.user, receiver=self.other)
        accept_url = reverse("accept_friend_request", args=[pending.pk])
        self.assertEqual(self.client.post(accept_url).status_code, 404)
        self.client.force_login(self.other)
        self.assertRedirects(self.client.post(accept_url), reverse("friend_requests_page"))
        pending.refresh_from_db()
        self.assertEqual(pending.status, FriendRequest.Status.ACCEPTED)
        self.assertContains(self.client.get(reverse("my_friends")), self.user.username)

    @patch("games.steam_views.get_owned_games", return_value=[
        {"appid": 71, "name": "Known fixture", "playtime_forever": 0},
        {"appid": 72, "name": "Unlisted fixture"},
    ])
    def test_steam_sync_and_my_library_keep_catalog_and_personal_entries_separate(self, steam):
        self.game.steam_id = 71
        self.game.save(update_fields=["steam_id"])
        account = SteamAccount.objects.create(user=self.user, steam_id="123456")

        self.assertRedirects(self.client.post(reverse("steam_sync_library")), reverse("steam_settings"))
        steam.assert_called_once_with(account.steam_id)
        self.assertEqual(SteamOwnedGame.objects.get(steam_app_id=71).game_id, self.game.pk)
        self.assertIsNone(SteamOwnedGame.objects.get(steam_app_id=72).game_id)
        self.assertTrue(Game.objects.count() == 1 and not LibraryEntry.objects.exists())
        games = self.client.get(reverse("library_data")).json()["games"]
        self.assertCountEqual([game["playtime_minutes"] for game in games], [0, None])

    def test_admin_still_requires_staff_and_lists_registered_models(self):
        admin_url = reverse("admin:index")
        self.assertRedirects(self.client.get(admin_url), f'{reverse("admin:login")}?next={admin_url}')
        staff = get_user_model().objects.create_superuser(username="regression-staff")
        self.client.force_login(staff)
        self.assertEqual(self.client.get(admin_url).status_code, 200)
        for model in ("game", "review", "libraryentry", "friendrequest"):
            with self.subTest(model=model):
                self.assertEqual(self.client.get(reverse(f"admin:games_{model}_changelist")).status_code, 200)
