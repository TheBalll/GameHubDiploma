from django.core import signing
from django.db import OperationalError
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from requests import RequestException

from .models import Game, SteamIgdbIdentity, SteamOwnedGame
from .services.library_entries import add_igdb_recommendation
from .services.recommendation_ranking import rank_recommendations


ADD_TOKEN_SALT = "gamehub.recommended-game"
ADD_TOKEN_MAX_AGE = 60 * 60
FEATURE_LABELS = {
    "preferred_genres": ("genre", "genres"),
    "preferred_themes": ("theme", "themes"),
    "preferred_game_modes": ("game mode", "game modes"),
    "preferred_keywords": ("keyword", "keywords"),
}


def _source_titles(user, items):
    source_ids = {
        igdb_id for item in items for reason in item.reasons for igdb_id in reason.source_igdb_ids
    }
    if not source_ids:
        return {}
    titles = {}
    rows = Game.objects.filter(igdb_id__in=source_ids).order_by("pk").values_list("igdb_id", "title")
    for igdb_id, title in rows:
        titles.setdefault(igdb_id, title)
    missing = source_ids - titles.keys()
    if not missing:
        return titles
    confirmed = dict(SteamIgdbIdentity.objects.filter(
        metadata_id__in=missing, status=SteamIgdbIdentity.Status.MATCHED,
    ).values_list("steam_app_id", "metadata_id"))
    if confirmed:
        for app_id, title in Game.objects.filter(steam_id__in=confirmed, igdb_id__isnull=True).order_by(
            "steam_id",
        ).values_list("steam_id", "title"):
            titles.setdefault(confirmed[app_id], title)
        for app_id, title in SteamOwnedGame.objects.filter(
            steam_account__user=user, steam_app_id__in=confirmed,
        ).order_by("steam_app_id").values_list("steam_app_id", "name"):
            titles.setdefault(confirmed[app_id], title)
    return titles


def _reason_text(item, titles):
    features = [reason for reason in item.reasons if reason.code in FEATURE_LABELS]
    if not features:
        return None

    reason = next((feature for feature in features if feature.established), features[0])
    singular, plural = FEATURE_LABELS[reason.code]
    anchor_id = next((igdb_id for igdb_id in reason.source_igdb_ids if igdb_id in titles), None)
    if reason.labels:
        names = ", ".join(reason.labels)
        if reason.established:
            secondary = next(
                (other for other in features if other is not reason and other.established and other.labels), None,
            )
            text = f"Matches your preferred {plural} ({names})"
            if secondary:
                text += f" and {FEATURE_LABELS[secondary.code][1]} ({', '.join(secondary.labels)})"
            text += "."
        else:
            text = f"Matches a {singular} from your gaming history ({names})."
    elif anchor_id is not None:
        secondary = next(
            (other for other in features if other is not reason and other.established and anchor_id in other.source_igdb_ids),
            None,
        )
        match = f"a {singular}"
        if secondary:
            match += f" and a {FEATURE_LABELS[secondary.code][0]}"
        text = f"Shares {match} with {titles[anchor_id]}."
    else:
        text = f"Matches a {singular} from your gaming history."

    similar_id = next(
        (igdb_id for other in item.reasons if other.code.startswith("similar_to_")
         for igdb_id in other.source_igdb_ids if igdb_id in titles),
        None,
    )
    if similar_id is not None:
        if similar_id == anchor_id and not reason.labels:
            return f"{text} IGDB also lists it as similar."
        return f"{text} Similar to {titles[similar_id]}."
    return text


def _card_data(item, user, titles, personalized):
    target = item.add_to_library
    local = target.kind == "gamehub"
    return {
        "title": item.title,
        "cover": item.cover,
        "genres": item.genres,
        "release_year": item.release_year,
        "source": item.source,
        "match_score": item.match_score if personalized else None,
        "reason": _reason_text(item, titles) if personalized else None,
        "game_url": reverse("game_detail", args=[target.id]) if local else None,
        "add_url": reverse("add_to_library" if local else "add_recommended_igdb_game", args=[target.id]),
        "add_token": None if local else signing.dumps([user.pk, target.id, item.steam_app_ids], salt=ADD_TOKEN_SALT),
    }


@never_cache
@require_GET
def recommendation_data(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Sign in to see recommendations."}, status=401)
    result = rank_recommendations(request.user)
    personalized = result.mode == "personalized"
    titles = _source_titles(request.user, result.items) if personalized else {}
    return JsonResponse({
        "mode": result.mode,
        "items": [_card_data(item, request.user, titles, personalized) for item in result.items],
    })


@never_cache
@require_POST
def add_recommended_igdb_game(request, igdb_id):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Sign in to add games to your library."}, status=401)
    if not 0 < igdb_id <= 2**63 - 1:
        return JsonResponse({"error": "Invalid game identifier."}, status=400)

    token = request.POST.get("token", "")
    try:
        target = signing.loads(token, salt=ADD_TOKEN_SALT, max_age=ADD_TOKEN_MAX_AGE)
    except signing.SignatureExpired:
        return JsonResponse({"error": "This recommendation expired. Reload the page and try again."}, status=403)
    except signing.BadSignature:
        target = None
    if not isinstance(target, list) or len(target) != 3 or target[:2] != [request.user.pk, igdb_id]:
        return JsonResponse({"error": "This recommendation is unavailable for your account."}, status=403)

    try:
        entry, created = add_igdb_recommendation(request.user, igdb_id, steam_app_ids=target[2])
    except (RequestException, OperationalError, ValueError, TypeError, KeyError):
        return JsonResponse({"error": "Unable to add the game right now. Please try again."}, status=503)
    if entry is None:
        return JsonResponse({"error": "This game could not be found on IGDB."}, status=404)
    return JsonResponse({
        "success": True,
        "created": created,
        "message": "Game added to library." if created else "Game is already in your library.",
    })
