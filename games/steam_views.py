
import re
import secrets
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import SteamAccount

from datetime import timedelta
from django.db import transaction

from .models import Game, SteamOwnedGame
from .steam_service import SteamSyncError, get_owned_games


OPENID_NAMESPACE = "http://specs.openid.net/auth/2.0"
OPENID_IDENTIFIER = (
    "http://specs.openid.net/auth/2.0/identifier_select"
)

STEAM_ID_PATTERN = re.compile(
    r"^https?://steamcommunity\.com/openid/id/(\d{17})$"
)

SESSION_KEY = "steam_openid_pending"
MAX_AUTH_AGE_SECONDS = 900



@login_required(login_url="login")
@require_GET
def steam_settings_page(request):
    # Get the connected Steam account
    steam_account = SteamAccount.objects.filter(
        user=request.user
    ).first()

    steam_games = []
    steam_games_count = 0

    if steam_account:
        # Count imported Steam games
        steam_games_count = steam_account.owned_games.count()

        # Show the games with the most playtime
        owned_games = (
            steam_account.owned_games
            .order_by("-playtime_minutes")[:8]
        )

        steam_games = [
            {
                "name": game.name,
                "hours": round(game.playtime_minutes / 60, 1),
            }
            for game in owned_games
        ]

    return render(
        request,
        "games/steam_settings.html",
        {
            "steam_account": steam_account,
            "steam_games": steam_games,
            "steam_games_count": steam_games_count,
        }
    )



@login_required(login_url="login")
@require_POST
def steam_connect(request):
    # Prevent replacing an existing connection
    if SteamAccount.objects.filter(user=request.user).exists():
        messages.info(
            request,
            "A Steam account is already connected."
        )
        return redirect("steam_settings")

    # Generate a unique state for this authentication attempt
    state = secrets.token_urlsafe(32)

    callback_url = request.build_absolute_uri(
        reverse("steam_callback")
    )

    return_to = callback_url + "?" + urlencode({
        "state": state
    })

    realm = request.build_absolute_uri("/")

    # Save the expected callback details in the user's session
    request.session[SESSION_KEY] = {
        "state": state,
        "user_id": request.user.pk,
        "return_to": return_to,
        "created_at": timezone.now().timestamp(),
    }

    parameters = {
        "openid.ns": OPENID_NAMESPACE,
        "openid.mode": "checkid_setup",
        "openid.return_to": return_to,
        "openid.realm": realm,
        "openid.claimed_id": OPENID_IDENTIFIER,
        "openid.identity": OPENID_IDENTIFIER,
    }

    steam_url = (
        settings.STEAM_OPENID_ENDPOINT
        + "?"
        + urlencode(parameters)
    )

    return redirect(steam_url)


def verify_steam_response(parameters):
    # Ask Steam to verify the signed authentication response
    verification_data = {
        key: value
        for key, value in parameters.items()
        if key.startswith("openid.")
    }

    verification_data["openid.mode"] = "check_authentication"

    encoded_data = urlencode(
        verification_data
    ).encode("utf-8")

    verification_request = Request(
        settings.STEAM_OPENID_ENDPOINT,
        data=encoded_data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
        method="POST"
    )

    try:
        with urlopen(
            verification_request,
            timeout=10
        ) as response:
            result = response.read(4096).decode("utf-8")

    except (URLError, TimeoutError, UnicodeError, ValueError):
        return False

    response_fields = {}

    for line in result.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            response_fields[key.strip()] = value.strip()

    return response_fields.get("is_valid") == "true"


@login_required(login_url="login")
@require_GET
def steam_callback(request):
    # Consume the pending attempt to prevent repeated callbacks
    pending = request.session.pop(SESSION_KEY, None)

    if not isinstance(pending, dict):
        messages.error(
            request,
            "Steam connection expired. Please try again."
        )
        return redirect("steam_settings")

    # Verify that the callback belongs to this user and session
    state = request.GET.get("state", "")
    expected_state = pending.get("state", "")

    if (
        not isinstance(state, str)
        or not isinstance(expected_state, str)
        or not expected_state
        or not secrets.compare_digest(state, expected_state)
        or pending.get("user_id") != request.user.pk
    ):
        messages.error(
            request,
            "Invalid Steam authentication state."
        )
        return redirect("steam_settings")

    # Reject expired authentication attempts
    created_at = pending.get("created_at", 0)

    if (
        not isinstance(created_at, (int, float))
        or not 0 <= timezone.now().timestamp() - created_at
        <= MAX_AUTH_AGE_SECONDS
    ):
        messages.error(
            request,
            "Steam connection expired. Please try again."
        )
        return redirect("steam_settings")

    if request.GET.get("openid.mode") == "cancel":
        messages.info(request, "Steam connection cancelled.")
        return redirect("steam_settings")

    required_fields = [
        "openid.ns",
        "openid.mode",
        "openid.op_endpoint",
        "openid.claimed_id",
        "openid.identity",
        "openid.return_to",
        "openid.response_nonce",
        "openid.signed",
        "openid.sig",
    ]

    # Reject missing or duplicated authentication parameters
    if any(
        len(request.GET.getlist(field)) != 1
        for field in required_fields
    ):
        messages.error(request, "Invalid Steam response.")
        return redirect("steam_settings")

    # Validate the OpenID provider and callback destination
    if (
        request.GET["openid.ns"] != OPENID_NAMESPACE
        or request.GET["openid.mode"] != "id_res"
        or request.GET["openid.op_endpoint"]
        != settings.STEAM_OPENID_ENDPOINT
        or request.GET["openid.return_to"]
        != pending.get("return_to")
    ):
        messages.error(request, "Invalid Steam response.")
        return redirect("steam_settings")

    # Ensure important identity fields were signed by Steam
    signed_fields = set(
        request.GET["openid.signed"].split(",")
    )

    expected_signed_fields = {
        "op_endpoint",
        "claimed_id",
        "identity",
        "return_to",
        "response_nonce",
    }

    if not expected_signed_fields.issubset(signed_fields):
        messages.error(request, "Invalid Steam signature.")
        return redirect("steam_settings")

    claimed_id = request.GET["openid.claimed_id"]
    identity = request.GET["openid.identity"]

    # Both identities must refer to the same Steam account
    if claimed_id != identity:
        messages.error(request, "Steam identity mismatch.")
        return redirect("steam_settings")

    match = STEAM_ID_PATTERN.fullmatch(claimed_id)

    if not match:
        messages.error(request, "Invalid Steam ID.")
        return redirect("steam_settings")

    # Never trust the callback without server-side verification
    if not verify_steam_response(request.GET):
        messages.error(
            request,
            "Steam could not verify your account."
        )
        return redirect("steam_settings")

    steam_id = match.group(1)

    # Prevent linking one Steam account to multiple GameHub users
    if SteamAccount.objects.filter(
        steam_id=steam_id
    ).exclude(user=request.user).exists():
        messages.error(
            request,
            "This Steam account is already linked to another user."
        )
        return redirect("steam_settings")

    if SteamAccount.objects.filter(user=request.user).exists():
        messages.error(
            request,
            "You already have a connected Steam account."
        )
        return redirect("steam_settings")

    # Save the verified Steam identity
    try:
        SteamAccount.objects.create(
            user=request.user,
            steam_id=steam_id
        )
    except IntegrityError:
        messages.error(
            request,
            "Steam account could not be linked."
        )
        return redirect("steam_settings")

    messages.success(
        request,
        "Steam account connected successfully."
    )

    return redirect("steam_settings")


@login_required(login_url="login")
@require_POST
def steam_sync_library(request):
    # Get the verified Steam account
    steam_account = SteamAccount.objects.filter(
        user=request.user
    ).first()

    if not steam_account:
        messages.error(
            request,
            "Connect your Steam account first."
        )
        return redirect("steam_settings")

    # Prevent excessive synchronization requests
    if steam_account.last_synced_at:
        elapsed = timezone.now() - steam_account.last_synced_at

        if elapsed < timedelta(seconds=60):
            messages.info(
                request,
                "Please wait before syncing again."
            )
            return redirect("steam_settings")

    # Fetch games from the official Steam API
    try:
        steam_games = get_owned_games(
            steam_account.steam_id
        )
    except SteamSyncError as error:
        messages.error(request, str(error))
        return redirect("steam_settings")

    # Collect valid Steam AppIDs
    valid_games = {}

    for item in steam_games:
        if not isinstance(item, dict):
            continue

        app_id = item.get("appid")

        if (
            not isinstance(app_id, int)
            or isinstance(app_id, bool)
            or app_id <= 0
        ):
            continue

        valid_games[app_id] = item

    # Match Steam games with the existing GameHub catalog
    matched_games = dict(
        Game.objects.filter(
            steam_id__in=valid_games.keys()
        ).values_list("steam_id", "id")
    )

    # Save or update the imported Steam games
    with transaction.atomic():

        for app_id, item in valid_games.items():

            raw_name = item.get("name")

            if isinstance(raw_name, str) and raw_name.strip():
                name = raw_name.strip()[:255]
            else:
                name = f"Steam App {app_id}"

            playtime = item.get("playtime_forever", 0)

            if (
                not isinstance(playtime, int)
                or isinstance(playtime, bool)
                or playtime < 0
            ):
                playtime = 0

            SteamOwnedGame.objects.update_or_create(
                steam_account=steam_account,
                steam_app_id=app_id,
                defaults={
                    "name": name,
                    "playtime_minutes": playtime,
                    "game_id": matched_games.get(app_id),
                }
            )

        # Update the last successful synchronization time
        steam_account.last_synced_at = timezone.now()
        steam_account.save(
            update_fields=["last_synced_at"]
        )

    messages.success(
        request,
        f"Steam library synced successfully! "
        f"{len(valid_games)} games imported or updated."
    )

    return redirect("steam_settings")
