import os
import runpy
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.checks import Tags, run_checks
from django.test import Client, SimpleTestCase
from django.urls import reverse


class DevelopmentSettingsTests(SimpleTestCase):
    def load_settings(self, **env):
        with patch.dict(os.environ, {"DJANGO_SECRET_KEY": "test-only-key", **env}, clear=True), patch(
            "dotenv.load_dotenv",
        ):
            return runpy.run_path(str(Path(__file__).with_name("settings.py")))

    def test_debug_accepts_true_values_and_defaults_to_false(self):
        self.assertIs(self.load_settings()["DEBUG"], False)
        for value in ("true", "1", "yes", " ON ", "TRUE"):
            with self.subTest(value=value):
                self.assertIs(self.load_settings(DJANGO_DEBUG=value)["DEBUG"], True)
        for value in ("False", "0", "no", "off", "", "unexpected"):
            with self.subTest(value=value):
                self.assertIs(self.load_settings(DJANGO_DEBUG=value)["DEBUG"], False)

    def test_local_hosts_are_available_with_debug_off(self):
        config = self.load_settings()
        self.assertEqual(config["ALLOWED_HOSTS"], ["localhost", "127.0.0.1", "[::1]"])
        self.assertEqual(config["CSRF_TRUSTED_ORIGINS"], [])
        with self.settings(DEBUG=False, ALLOWED_HOSTS=config["ALLOWED_HOSTS"]):
            for host in config["ALLOWED_HOSTS"]:
                self.assertEqual(self.client.get(reverse("login"), HTTP_HOST=f"{host}:8000").status_code, 200)
            self.assertEqual(self.client.get(reverse("login"), HTTP_HOST="unlisted.example").status_code, 400)

    def test_codespaces_origin_allows_valid_csrf_token_and_rejects_other_origins(self):
        config = self.load_settings(
            CODESPACE_NAME="example-workspace",
            GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN="app.github.dev",
        )
        host = "example-workspace-8000.app.github.dev"
        self.assertIn(host, config["ALLOWED_HOSTS"])
        self.assertEqual(config["CSRF_TRUSTED_ORIGINS"], [f"https://{host}"])
        with self.settings(
            DEBUG=False, ALLOWED_HOSTS=config["ALLOWED_HOSTS"], CSRF_TRUSTED_ORIGINS=config["CSRF_TRUSTED_ORIGINS"],
        ):
            client = Client(enforce_csrf_checks=True)
            self.assertEqual(client.get(reverse("login"), HTTP_HOST=host).status_code, 200)
            options = {"HTTP_HOST": host, "HTTP_X_CSRFTOKEN": client.cookies[settings.CSRF_COOKIE_NAME].value}
            self.assertEqual(client.post(reverse("logout"), HTTP_ORIGIN=f"https://{host}", **options).status_code, 302)
            self.assertEqual(client.post(reverse("logout"), HTTP_ORIGIN="https://other.app.github.dev", **options).status_code, 403)
            self.assertEqual(run_checks(tags=[Tags.security]), [])

    def test_optional_exact_settings_and_ignores_wildcards(self):
        config = self.load_settings(
            DJANGO_ALLOWED_HOSTS=" *, custom.example ",
            DJANGO_CSRF_TRUSTED_ORIGINS="https://*.example, https://custom.example",
            CODESPACE_NAME="invalid/name",
        )
        self.assertEqual(config["ALLOWED_HOSTS"], ["localhost", "127.0.0.1", "[::1]", "custom.example"])
        self.assertEqual(config["CSRF_TRUSTED_ORIGINS"], ["https://custom.example"])
