from collections import Counter

import requests
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden
from django.urls import reverse
from django.db.models import Q, Avg, Count
from django.core.exceptions import PermissionDenied

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.decorators import login_required

from django.views.decorators.http import require_GET, require_POST

from .models import Game, Review, LibraryEntry, UserProfile, SteamAccount, SteamOwnedGame
from .forms import ReviewForm, LibraryEntryForm, UserProfileForm, UserEditForm
from django.core.paginator import Paginator
from games.services.igdb import search_games as search_igdb_games
from django.views.decorators.http import require_POST
from games.services.game_import import import_game_from_igdb
from datetime import datetime, timezone
from games.models import Game
from games.services.steam import search_steam_games
from games.services.game_import import import_game_from_steam
from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from .models import FriendRequest
from django.db import transaction
from django.views.decorators.cache import never_cache
from random import randrange


def home(request):
    # Select a random featured game with an image
    featured_games = (
        Game.objects
        .exclude(background_image="")
        .order_by("id")
    )

    featured_count = featured_games.count()

    featured_game = (
        featured_games[randrange(featured_count)]
        if featured_count > 0
        else None
    )

    # Load games with their community ratings
    discover_games = (
        Game.objects
        .exclude(background_image="")
        .annotate(
            avg_rating=Avg("review__rating"),
            review_count=Count("review")
        )
        .order_by("-id")[:6]
    )

    # Find the highest-rated games
    community_picks = (
        Game.objects
        .annotate(
            avg_rating=Avg("review__rating"),
            review_count=Count("review")
        )
        .filter(review_count__gt=0)
        .order_by("-avg_rating", "-review_count", "title")[:4]
    )

    context = {
        "featured_game": featured_game,
        "discover_games": discover_games,
        "community_picks": community_picks,
    }

    return render(request, "games/home.html", context)

def games_list(request):
    games = Game.objects.order_by("id")

    paginator = Paginator(games, 20)

    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    context = {
        "games": page_obj,
        "page_obj": page_obj,
    }

    if request.GET.get("load_more") == "1":
        return render(
            request,
            "games/_game_cards.html",
            context
        )

    return render(
        request,
        "games/games_list.html",
        context
    )


def game_detail(request, game_id):
    game = get_object_or_404(Game, id=game_id)

    reviews = (
        Review.objects
        .filter(game=game)
        .select_related("user", "user__profile")
    )

    is_in_library = False

    if request.user.is_authenticated:
        is_in_library = LibraryEntry.objects.filter(
            user=request.user,
            game=game
        ).exists()

    is_ajax = (
        request.headers.get("X-Requested-With")
        == "XMLHttpRequest"
    )

    if request.method == "POST":

        if not request.user.is_authenticated:
            if is_ajax:
                return JsonResponse({
                    "success": False,
                    "message": "You must be logged in."
                }, status=403)

            return redirect("login")

        form = ReviewForm(request.POST)

        if form.is_valid():
            review = form.save(commit=False)
            review.user = request.user
            review.game = game
            review.save()

            if is_ajax:
                profile = getattr(
                    request.user,
                    "profile",
                    None
                )

                display_name = request.user.username
                avatar_url = ""

                if profile:
                    display_name = (
                        profile.profile_name
                        or request.user.username
                    )

                    if profile.avatar:
                        avatar_url = profile.avatar.url

                return JsonResponse({
                    "success": True,
                    "review": {
                        "id": review.id,
                        "rating": review.rating,
                        "comment": review.comment,
                        "date": review.created_at.strftime("%d %b %Y"),
                        "author": display_name,
                        "avatar_url": avatar_url,
                        "profile_url": reverse(
                            "public_profile",
                            kwargs={
                                "user_id": request.user.id
                            }
                        ),
                        "edit_url": reverse(
                            "edit_review",
                            kwargs={
                                "review_id": review.id
                            }
                        ),
                        "delete_url": reverse(
                            "delete_review",
                            kwargs={
                                "review_id": review.id
                            }
                        )
                    }
                }, status=201)

            return redirect(
                reverse(
                    "game_detail",
                    kwargs={"game_id": game.id}
                ) + "#reviews"
            )

        if is_ajax:
            return JsonResponse({
                "success": False,
                "errors": form.errors.get_json_data()
            }, status=400)

    else:
        form = ReviewForm()

    return render(request, "games/game_detail.html", {
        "game": game,
        "reviews": reviews,
        "form": form,
        "is_in_library": is_in_library
    })

def register(request):
    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "POST":
        form = UserCreationForm(request.POST)

        is_ajax = (
            request.headers.get("X-Requested-With")
            == "XMLHttpRequest"
        )

        if form.is_valid():
            form.save()

            if is_ajax:
                return JsonResponse({
                    "success": True,
                    "message": "Account created successfully!",
                    "redirect_url": reverse("login")
                }, status=201)

            return redirect("login")

        if is_ajax:
            return JsonResponse({
                "success": False,
                "message": "Please correct the errors below.",
                "errors": form.errors.get_json_data()
            }, status=400)

    else:
        form = UserCreationForm()

    return render(request, "games/register.html", {
        "form": form
    })

def login_view(request):
    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        is_ajax = (
            request.headers.get("X-Requested-With")
            == "XMLHttpRequest"
        )

        if not username or not password:
            message = "Please enter your username and password."

            if is_ajax:
                return JsonResponse({
                    "success": False,
                    "message": message
                }, status=400)

            return render(request, "games/login.html", {
                "error": message
            })

        user = authenticate(
            request,
            username=username,
            password=password
        )

        if user is not None:
            login(request, user)

            if is_ajax:
                return JsonResponse({
                    "success": True,
                    "redirect_url": reverse("home")
                })

            return redirect("home")

        message = "Invalid username or password."

        if is_ajax:
            return JsonResponse({
                "success": False,
                "message": message
            }, status=400)

        return render(request, "games/login.html", {
            "error": message
        })

    return render(request, "games/login.html")


def logout_view(request):
    logout(request)
    return redirect('home')

@login_required
@require_POST
def delete_review(request, review_id):
    review = get_object_or_404(Review, id=review_id)

    if review.user != request.user and not request.user.is_staff:
        return HttpResponseForbidden("You cannot delete this review.")

    game_id = review.game.id
    review.delete()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"success": True})

    return redirect('game_detail', game_id=game_id)

@login_required
def edit_review(request, review_id):
    review = get_object_or_404(
        Review,
        id=review_id,
        user=request.user
    )

    is_ajax = (
        request.headers.get("X-Requested-With")
        == "XMLHttpRequest"
    )

    if request.method == "POST":
        form = ReviewForm(request.POST, instance=review)

        if form.is_valid():
            updated_review = form.save()

            if is_ajax:
                return JsonResponse({
                    "success": True,
                    "review": {
                        "id": updated_review.id,
                        "rating": updated_review.rating,
                        "comment": updated_review.comment,
                    }
                })

            return redirect(
                reverse(
                    "game_detail",
                    kwargs={"game_id": review.game.id}
                ) + "#reviews"
            )

        if is_ajax:
            return JsonResponse({
                "success": False,
                "errors": form.errors.get_json_data()
            }, status=400)

    else:
        form = ReviewForm(instance=review)

    return render(request, "games/edit_review.html", {
        "form": form,
        "review": review
    })


def library(request):
    if not request.user.is_authenticated:
        return redirect('login')

    library_entries = LibraryEntry.objects.filter(
        user=request.user
    )

    return render(request, 'games/library.html', {
        'library_entries': library_entries
    })

@login_required
@require_GET
def library_data(request):

    # Normalize titles for exact comparison
    def normalize_title(title):
        return "".join(
            character
            for character in (title or "").casefold()
            if character.isalnum()
        )

    library_entries = (
        LibraryEntry.objects
        .filter(user=request.user)
        .select_related("game")
        .order_by("game__title", "pk")
    )

    steam_entries = list(
        SteamOwnedGame.objects
        .filter(steam_account__user=request.user)
        .select_related("game")
        .order_by("steam_app_id")
    )

    steam_account = (
        SteamAccount.objects
        .filter(user=request.user)
        .only("last_synced_at")
        .first()
    )

    if not steam_account:
        missing_steam_playtime = "Not connected"
    elif not steam_account.last_synced_at:
        missing_steam_playtime = "Not synced"
    else:
        missing_steam_playtime = "Not in Steam Library"

    games = []

    gamehub_games_by_id = {}
    gamehub_games_by_steam_id = {}
    gamehub_games_by_title = {}
    ambiguous_titles = set()

    # Build the GameHub library
    for entry in library_entries:

        if entry.game_id in gamehub_games_by_id:
            continue

        game_data = {
            "id": entry.game_id,
            "title": entry.game.title,
            "genre": entry.game.genre or "Unknown",
            "image": entry.game.background_image or "",

            "status": entry.status,
            "status_display": entry.get_status_display(),
            "added_at": entry.added_at,

            "playtime_minutes": None,
            "playtime_hours": None,
            "playtime_display": missing_steam_playtime,

            "is_on_steam": False,

            "details_url": reverse(
                "game_detail",
                kwargs={"game_id": entry.game_id}
            ),

            "edit_url": reverse(
                "edit_library_entry",
                kwargs={"game_id": entry.game_id}
            ),

            "remove_url": reverse(
                "remove_from_library",
                kwargs={"game_id": entry.game_id}
            ),

            "steam_url": None,
        }

        gamehub_games_by_id[entry.game_id] = game_data

        if entry.game.steam_id:
            gamehub_games_by_steam_id[entry.game.steam_id] = game_data

        title_key = normalize_title(entry.game.title)

        if title_key in gamehub_games_by_title:
            ambiguous_titles.add(title_key)
        else:
            gamehub_games_by_title[title_key] = entry.game

        games.append(game_data)

    steam_title_counts = Counter(normalize_title(entry.name) for entry in steam_entries)
    steam_link_counts = Counter(entry.game_id for entry in steam_entries if entry.game_id)
    steam_catalog_games = {}
    steam_matches = {}
    matched_game_ids = set()

    # Reserve AppID and unambiguous catalog links before considering titles.
    for steam_entry in steam_entries:
        app_id = steam_entry.steam_app_id
        catalog_game = steam_entry.game
        if catalog_game and (
            catalog_game.steam_id not in (None, app_id)
            or (catalog_game.steam_id is None and steam_link_counts[catalog_game.pk] > 1)
        ):
            catalog_game = None
        steam_catalog_games[app_id] = catalog_game

        game_data = gamehub_games_by_steam_id.get(app_id)
        if game_data is None and catalog_game:
            game_data = gamehub_games_by_id.get(catalog_game.pk)

        if game_data:
            steam_matches[app_id] = game_data
            matched_game_ids.add(game_data["id"])

    # Merge Steam games with existing GameHub entries
    for steam_entry in steam_entries:

        playtime_minutes = steam_entry.playtime_minutes
        playtime_hours = steam_entry.playtime_hours
        if playtime_hours is None:
            playtime_display = "Unavailable"
        elif 0 < playtime_minutes < 60:
            playtime_display = f"{playtime_minutes} min"
        else:
            unit = "hour" if playtime_hours == 1 else "hours"
            playtime_display = f"{playtime_hours:g} {unit}"

        # Generate the Steam store URL
        steam_url = (
            f"https://store.steampowered.com/"
            f"app/{steam_entry.steam_app_id}/"
        )

        # Generate the Steam cover URL
        steam_image = (
            "https://shared.fastly.steamstatic.com/"
            f"store_item_assets/steam/apps/{steam_entry.steam_app_id}/"
            "library_600x900.jpg"
        )

        steam_data = {
            "is_on_steam": True,
            "steam_app_id": steam_entry.steam_app_id,
            "playtime_minutes": playtime_minutes,
            "playtime_hours": playtime_hours,
            "playtime_display": playtime_display,
            "steam_url": steam_url,
        }

        # Only match by title when neither record identifies another app.
        game_data = steam_matches.get(steam_entry.steam_app_id)
        title_key = normalize_title(steam_entry.name)
        title_match = gamehub_games_by_title.get(title_key)

        if (
            game_data is None
            and steam_entry.game_id is None
            and title_key
            and title_key not in ambiguous_titles
            and steam_title_counts[title_key] == 1
            and title_match
            and title_match.steam_id is None
            and title_match.pk not in matched_game_ids
        ):
            game_data = gamehub_games_by_id[title_match.pk]

        # Update an existing card instead of creating a duplicate
        if game_data:
            game_data.update(steam_data, image=game_data["image"] or steam_image)
            continue

        # Create a separate card only when no GameHub entry matches
        catalog_game = steam_catalog_games[steam_entry.steam_app_id]

        if catalog_game:
            title = catalog_game.title
            genre = catalog_game.genre or "Unknown"
            image = catalog_game.background_image or steam_image

            details_url = reverse(
                "game_detail",
                kwargs={"game_id": catalog_game.id}
            )
        else:
            title = steam_entry.name
            genre = "Steam Game"
            image = steam_image
            details_url = None

        games.append({
            "id": f"steam-{steam_entry.steam_app_id}",

            "title": title,
            "genre": genre,
            "image": image,

            "status": None,
            "status_display": "Not Added",
            "added_at": None,

            "add_to_library_url": reverse(
                "add_steam_game_to_library",
                kwargs={"steam_id": steam_entry.steam_app_id}
            ),

            "details_url": details_url,
            "edit_url": None,
            "remove_url": None,

            **steam_data,
        })

    # Sort all games alphabetically
    games.sort(
        key=lambda game: game["title"].casefold()
    )

    status_choices = [
        {"value": value, "label": label}
        for value, label in LibraryEntry._meta.get_field(
            "status"
        ).choices
    ]

    return JsonResponse({
        "games": games,
        "status_choices": status_choices,
    })

@login_required
@require_POST
def add_to_library(request, game_id):
    game = get_object_or_404(Game, id=game_id)

    entry, created = LibraryEntry.objects.get_or_create(
        user=request.user,
        game=game
    )

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({
            "success": True,
            "created": created,
            "message": (
                "Game added to library."
                if created
                else "Game is already in your library."
            )
        })

    return redirect("library")


@login_required
@require_POST
def add_steam_game_to_library(request, steam_id):
    # Find the Steam game in the current user's Steam library.
    steam_entry = (
        SteamOwnedGame.objects
        .filter(
            steam_account__user=request.user,
            steam_app_id=steam_id
        )
        .select_related("game")
        .first()
    )

    if not steam_entry:
        return JsonResponse({
            "success": False,
            "error": "Steam game not found in your library."
        }, status=404)

    # Reuse the linked GameHub game or import it from Steam.
    game = steam_entry.game
    if game and game.steam_id is None:
        other_apps = SteamOwnedGame.objects.filter(
            steam_account_id=steam_entry.steam_account_id, game=game
        ).exclude(pk=steam_entry.pk)
        if other_apps.exists():
            game = None

    if game is None or game.steam_id not in (None, steam_id):
        try:
            game, created_game = import_game_from_steam(steam_id)

        except (requests.RequestException, ValueError, KeyError):
            return JsonResponse({
                "success": False,
                "error": "Unable to import the Steam game."
            }, status=502)

        if game is None:
            return JsonResponse({
                "success": False,
                "error": "Game details could not be retrieved from Steam."
            }, status=404)

    # Link the Steam entry and create the personal library entry.
    with transaction.atomic():
        steam_entry.game = game
        steam_entry.save(update_fields=["game"])

        library_entry, created = LibraryEntry.objects.get_or_create(
            user=request.user,
            game=game
        )

    return JsonResponse({
        "success": True,
        "created": created,
        "game_id": game.id,
        "title": game.title,
        "status": library_entry.status,
        "status_display": library_entry.get_status_display(),

        "edit_url": reverse(
            "edit_library_entry",
            kwargs={"game_id": game.id}
        ),

        "remove_url": reverse(
            "remove_from_library",
            kwargs={"game_id": game.id}
        ),

        "message": (
            "Game added to your GameHub library."
            if created
            else "Game is already in your GameHub library."
        )
    })


@require_POST
@login_required
def remove_from_library(request, game_id):
    library_entry = get_object_or_404(
        LibraryEntry,
        user=request.user,
        game_id=game_id
    )

    library_entry.delete()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({
            "success": True,
            "message": "Game removed from library."
        })

    return redirect("library")

@login_required
def edit_library_entry(request, game_id):
    library_entry = get_object_or_404(
        LibraryEntry,
        user=request.user,
        game_id=game_id
    )

    is_ajax = (
        request.headers.get("X-Requested-With")
        == "XMLHttpRequest"
    )

    if request.method == "POST":
        form = LibraryEntryForm(
            request.POST,
            instance=library_entry
        )

        if form.is_valid():
            entry = form.save()

            if is_ajax:
                return JsonResponse({
                    "success": True,
                    "status": entry.status,
                    "status_display": entry.get_status_display()
                })

            return redirect("library")

        if is_ajax:
            return JsonResponse({
                "success": False,
                "errors": form.errors.get_json_data()
            }, status=400)

    else:
        form = LibraryEntryForm(instance=library_entry)

    return render(request, "games/edit_library_entry.html", {
        "form": form,
        "library_entry": library_entry
    })


def search_games(request):
    query = request.GET.get('q', '')

    if query:
        games = Game.objects.filter(
            title__icontains=query
        )[:5]
    else:
        games = []

    results = []

    for game in games:
        results.append({
            'id': game.id,
            'title': game.title,
            'genre': game.genre,
            'release_year': game.release_year
        })

    return JsonResponse({
        'games': results
    })




def search_results(request):
    query = request.GET.get("q", "").strip()
    genre = request.GET.get("genre", "").strip()
    sort = request.GET.get("sort", "title")

    # Available genres from SQLite
    genres = sorted(
        set(
            Game.objects.exclude(genre="")
            .values_list("genre", flat=True)
        )
    )

    # Search local games
    games = Game.objects.all()

    if query:
        games = games.filter(title__icontains=query)

    if genre:
        games = games.filter(genre=genre)

    if sort == "newest":
        games = games.order_by("-release_year", "title")
    elif sort == "oldest":
        games = games.order_by("release_year", "title")
    else:
        games = games.order_by("title")

    if request.GET.get("format") != "json":
        return render(
            request,
            "games/search_results.html",
            {"query": query}
        )

    results = []
    seen_igdb_ids = set()
    seen_steam_ids = set()

    # Normalize titles for cautious duplicate detection
    def normalize_title(title):
        return " ".join((title or "").casefold().split())

    # Keep track of local records
    local_titles = set()

    # 1. SQLITE
    for game in games:
        results.append({
            "id": game.id,
            "title": game.title,
            "genre": game.genre,
            "developer": game.developer,
            "release_year": game.release_year,
            "image": game.background_image,
            "url": reverse(
                "game_detail",
                kwargs={"game_id": game.id}
            ),
            "source": "local",
        })

        local_titles.add(normalize_title(game.title))

        if game.igdb_id:
            seen_igdb_ids.add(game.igdb_id)

        if game.steam_id:
            seen_steam_ids.add(game.steam_id)

    # Only search external APIs when there is a query
    # and no active genre filter.
    if query and not genre:
        cache_key = (
            "gamehub_unified_search_v1:"
            + query.casefold()
        )

        external_results = cache.get(cache_key)

        if external_results is None:
            external_results = []

            # 2. IGDB
            try:
                igdb_games = search_igdb_games(
                    query,
                    limit=10
                )

                for item in igdb_games:
                    igdb_id = item.get("id")
                    title = item.get("name")

                    if not igdb_id or not title:
                        continue

                    cover = item.get("cover") or {}
                    image_id = cover.get("image_id")

                    image_url = ""

                    if image_id:
                        image_url = (
                            "https://images.igdb.com/igdb/"
                            "image/upload/"
                            f"t_cover_big/{image_id}.jpg"
                        )

                    game_genres = [
                        g["name"]
                        for g in item.get("genres", [])
                        if g.get("name")
                    ]

                    developer = "Unknown"

                    for company in item.get(
                        "involved_companies", []
                    ):
                        if company.get("developer"):
                            developer = (
                                company.get("company") or {}
                            ).get("name") or "Unknown"
                            break

                    release_year = ""

                    timestamp = item.get(
                        "first_release_date"
                    )

                    if timestamp:
                        release_year = datetime.fromtimestamp(
                            timestamp,
                            tz=timezone.utc
                        ).year

                    external_results.append({
                        "id": igdb_id,
                        "title": title,
                        "genre": (
                            ", ".join(game_genres)
                            or "Unknown"
                        ),
                        "developer": developer,
                        "release_year": release_year,
                        "image": image_url,
                        "url": None,
                        "source": "igdb",
                    })

            except (
                requests.RequestException,
                ValueError,
                KeyError
            ):
                pass

            # 3. STEAM
            try:
                steam_games = search_steam_games(
                    query,
                    limit=10
                )

                for item in steam_games:
                    steam_id = item.get("id")
                    title = item.get("title")

                    if not steam_id or not title:
                        continue

                    external_results.append({
                        "id": steam_id,
                        "title": title,
                        "genre": "Unknown",
                        "developer": "Unknown",
                        "release_year": "",
                        "image": item.get("image", ""),
                        "url": None,
                        "source": "steam",
                    })

            except (
                requests.RequestException,
                ValueError,
                KeyError
            ):
                pass

            cache.set(
                cache_key,
                external_results,
                timeout=300
            )

        # 4. MERGE RESULTS
        # Prefer local games over external copies.
        for item in external_results:
            source = item["source"]
            title_key = normalize_title(item["title"])

            if source == "igdb":
                if item["id"] in seen_igdb_ids:
                    continue

            if source == "steam":
                if item["id"] in seen_steam_ids:
                    continue

            # Conservative title deduplication:
            # only suppress an external result when an
            # identical title is already represented.
            if title_key in local_titles:
                continue

            results.append(item)
            local_titles.add(title_key)

            if source == "igdb":
                seen_igdb_ids.add(item["id"])

            if source == "steam":
                seen_steam_ids.add(item["id"])

    # 5. SORT RESULTS
    search_key = normalize_title(query)

    def sort_key(game):
        title_key = normalize_title(game["title"])

        exact_match = title_key == search_key
        starts_with = title_key.startswith(search_key)

        if sort == "newest":
            return (
                not exact_match,
                not starts_with,
                -(game["release_year"] or 0),
                title_key,
            )

        if sort == "oldest":
            return (
                not exact_match,
                not starts_with,
                game["release_year"] or 9999,
                title_key,
            )

        return (
            not exact_match,
            not starts_with,
            title_key,
        )


    if query:
        results.sort(key=sort_key)

    # PAGINATION - 12 games per page
    paginator = Paginator(results, 12)

    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    return JsonResponse({
        "games": list(page_obj.object_list),
        "genres": genres,
        "count": paginator.count,
        "page": page_obj.number,
        "total_pages": paginator.num_pages,
        "has_next": page_obj.has_next(),
    })



@login_required
def profile(request):
    user_profile, created = UserProfile.objects.get_or_create(
        user=request.user
    )

    user_reviews = Review.objects.filter(
        user=request.user
    ).select_related("game").order_by("-created_at")

    library_entries = LibraryEntry.objects.filter(
        user=request.user
    ).select_related("game")

    context = {
        "user_profile": user_profile,
        "user_reviews": user_reviews,
        "library_entries": library_entries,
        "reviews_count": user_reviews.count(),
        "games_count": library_entries.count()
    }

    return render(request, "games/profile.html", context)

@login_required
def edit_profile(request):
    user_profile, created = UserProfile.objects.get_or_create(
        user=request.user
    )

    if request.method == "POST":
        user_form = UserEditForm(
            request.POST,
            instance=request.user
        )

        profile_form = UserProfileForm(
            request.POST,
            request.FILES,
            instance=user_profile
        )

        is_ajax = (
            request.headers.get("X-Requested-With")
            == "XMLHttpRequest"
        )

        user_valid = user_form.is_valid()
        profile_valid = profile_form.is_valid()

        if user_valid and profile_valid:
            user_form.save()
            profile_form.save()

            if is_ajax:
                return JsonResponse({
                    "success": True,
                    "message": "Profile updated successfully!",
                    "redirect_url": reverse("profile")
                })

            return redirect("profile")

        if is_ajax:
            errors = {}

            for field, messages in user_form.errors.get_json_data().items():
                errors[field] = messages

            for field, messages in profile_form.errors.get_json_data().items():
                errors[field] = messages

            return JsonResponse({
                "success": False,
                "message": "Please correct the errors below.",
                "errors": errors
            }, status=400)

    else:
        user_form = UserEditForm(instance=request.user)
        profile_form = UserProfileForm(instance=user_profile)

    return render(request, "games/edit_profile.html", {
        "user_form": user_form,
        "profile_form": profile_form
    })


@login_required
def public_profile(request, user_id):
    # Намираме потребителя
    profile_user = get_object_or_404(User, id=user_id)

    # Информация за профила
    user_profile = UserProfile.objects.filter(
        user=profile_user
    ).first()

    # Ревюта
    user_reviews = Review.objects.filter(
        user=profile_user
    ).select_related("game").order_by("-created_at")

    # Лична библиотека
    library_entries = LibraryEntry.objects.filter(
        user=profile_user
    ).select_related("game")

    # Проверяваме приятелството между двамата потребители
    friendship = None
    friendship_status = "none"

    if request.user != profile_user:
        friendship = FriendRequest.objects.filter(
            Q(sender=request.user, receiver=profile_user) |
            Q(sender=profile_user, receiver=request.user)
        ).order_by("-created_at").first()

        if friendship:
            if friendship.status == FriendRequest.Status.ACCEPTED:
                friendship_status = "friends"

            elif friendship.status == FriendRequest.Status.PENDING:
                if friendship.sender_id == request.user.id:
                    friendship_status = "sent"
                else:
                    friendship_status = "received"

    context = {
        "profile_user": profile_user,
        "user_profile": user_profile,
        "user_reviews": user_reviews,
        "library_entries": library_entries,
        "reviews_count": user_reviews.count(),
        "games_count": library_entries.count(),
        "friendship": friendship,
        "friendship_status": friendship_status,
    }

    return render(request, "games/public_profile.html", context)



@login_required
@require_POST
def delete_review_from_profile(request, review_id):

    # Проверяваме дали потребителят има право да трие ревюта
    if not request.user.has_perm('games.delete_review'):
        raise PermissionDenied

    # Намираме ревюто
    review = get_object_or_404(Review, id=review_id)

    # Запазваме ID на автора преди изтриването
    profile_user_id = review.user_id

    # Изтриваме ревюто
    review.delete()

    # Връщаме администратора в публичния профил
    return redirect('public_profile', user_id=profile_user_id)


@require_POST
def import_igdb_game(request, igdb_id):
    try:
        game, created = import_game_from_igdb(igdb_id)

        if game is None:
            return JsonResponse(
                {"error": "Game not found on IGDB."},
                status=404
            )

        return JsonResponse({
            "success": True,
            "created": created,
            "game_id": game.id,
            "url": reverse(
                "game_detail",
                kwargs={"game_id": game.id}
            )
        })

    except (requests.RequestException, ValueError, TypeError, KeyError):
        return JsonResponse(
            {"error": "IGDB is temporarily unavailable."},
            status=503
        )


@require_POST
def import_steam_game(request, steam_id):
    try:
        game, created = import_game_from_steam(steam_id)

        if game is None:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Game not found on Steam.",
                },
                status=404,
            )

        return JsonResponse({
            "success": True,
            "created": created,
            "game_id": game.id,
            "url": reverse(
                "game_detail",
                kwargs={"game_id": game.id}
            ),
        })

    except (requests.RequestException, ValueError, KeyError):
        return JsonResponse(
            {
                "success": False,
                "error": "Unable to import the Steam game.",
            },
            status=502,
        )


@login_required
@require_POST
def send_friend_request(request, user_id):
    User = get_user_model()

    receiver = get_object_or_404(User, pk=user_id)

    # Prevent users from sending requests to themselves
    if receiver == request.user:
        messages.error(
            request,
            "You cannot send a friend request to yourself."
        )
        return redirect("public_profile", user_id=receiver.id)

    # Check for an existing outgoing request
    existing_request = FriendRequest.objects.filter(
        sender=request.user,
        receiver=receiver
    ).first()

    if existing_request:

        if existing_request.status == FriendRequest.Status.ACCEPTED:
            messages.info(
                request,
                "You are already friends with this user."
            )

        elif existing_request.status == FriendRequest.Status.PENDING:
            messages.info(
                request,
                "You have already sent a friend request."
            )

        elif existing_request.status == FriendRequest.Status.REJECTED:
            # Reuse the rejected request instead of creating a duplicate
            existing_request.status = FriendRequest.Status.PENDING
            existing_request.save(update_fields=["status"])

            messages.success(
                request,
                "Friend request sent successfully."
            )

        return redirect("public_profile", user_id=receiver.id)

    # Check for an existing incoming request
    reverse_request = FriendRequest.objects.filter(
        sender=receiver,
        receiver=request.user
    ).first()

    if reverse_request:

        if reverse_request.status == FriendRequest.Status.ACCEPTED:
            messages.info(
                request,
                "You are already friends with this user."
            )

        elif reverse_request.status == FriendRequest.Status.PENDING:
            messages.info(
                request,
                "This user has already sent you a friend request."
            )

        elif reverse_request.status == FriendRequest.Status.REJECTED:
            # Reuse the rejected request as a new outgoing request
            reverse_request.delete()

            FriendRequest.objects.create(
                sender=request.user,
                receiver=receiver
            )

            messages.success(
                request,
                "Friend request sent successfully."
            )

        return redirect("public_profile", user_id=receiver.id)

    # Create a new friend request
    FriendRequest.objects.create(
        sender=request.user,
        receiver=receiver
    )

    messages.success(
        request,
        "Friend request sent successfully."
    )

    return redirect("public_profile", user_id=receiver.id)


@login_required
@require_POST
def accept_friend_request(request, request_id):
    with transaction.atomic():
        friend_request = get_object_or_404(
            FriendRequest.objects.select_for_update(),
            pk=request_id,
            receiver=request.user,
            status=FriendRequest.Status.PENDING
        )

        friend_request.status = FriendRequest.Status.ACCEPTED
        friend_request.save(update_fields=["status"])

    messages.success(
        request,
        "Friend request accepted successfully."
    )

    return redirect("friend_requests_page")

@login_required
@require_POST
def decline_friend_request(request, request_id):
    with transaction.atomic():
        friend_request = get_object_or_404(
            FriendRequest.objects.select_for_update(),
            pk=request_id,
            receiver=request.user,
            status=FriendRequest.Status.PENDING
        )

        friend_request.status = FriendRequest.Status.REJECTED
        friend_request.save(update_fields=["status"])

    messages.success(
        request,
        "Friend request declined."
    )

    return redirect("friend_requests_page")


@login_required
def friend_requests_page(request):
    pending_requests = (
        FriendRequest.objects
        .filter(
            receiver=request.user,
            status=FriendRequest.Status.PENDING
        )
        .select_related("sender")
        .order_by("-created_at")
    )

    return render(
        request,
        "games/friend_requests.html",
        {
            "pending_requests": pending_requests
        }
    )

@login_required
def find_friends(request):
    User = get_user_model()

    query = request.GET.get("q", "").strip()

    users = User.objects.exclude(
        pk=request.user.pk
    ).order_by("username")

    if query:
        users = users.filter(
            Q(username__icontains=query)
        )

    return render(
        request,
        "games/find_friends.html",
        {
            "users": users,
            "query": query,
        }
    )


@login_required
@never_cache
def my_friends(request):
    # Get all accepted friendships involving the current user
    friendships = FriendRequest.objects.filter(
        Q(sender=request.user) |
        Q(receiver=request.user),
        status=FriendRequest.Status.ACCEPTED
    ).select_related(
        "sender",
        "receiver",
        "sender__profile",
        "receiver__profile"
    ).order_by("-created_at")

    # Build the friends list
    friends = []

    for friendship in friendships:
        if friendship.sender_id == request.user.id:
            friend = friendship.receiver
        else:
            friend = friendship.sender

        friends.append(friend)

    return render(
        request,
        "games/my_friends.html",
        {
            "friends": friends,
            "friends_count": len(friends),
        }
    )

@login_required
@require_POST
def cancel_friend_request(request, request_id):
    friend_request = get_object_or_404(
        FriendRequest,
        id=request_id,
        sender=request.user,
        status=FriendRequest.Status.PENDING
    )

    friend_request.delete()

    messages.success(
        request,
        "Friend request cancelled successfully."
    )

    return redirect(
        "public_profile",
        user_id=friend_request.receiver_id
    )

@login_required
@require_POST
def remove_friend(request, user_id):
    User = get_user_model()

    friend_user = get_object_or_404(User, pk=user_id)

    # Find an accepted friendship in either direction
    friendship = FriendRequest.objects.filter(
        Q(sender=request.user, receiver=friend_user) |
        Q(sender=friend_user, receiver=request.user),
        status=FriendRequest.Status.ACCEPTED
    ).first()

    if friendship:
        friendship.delete()

        messages.success(
            request,
            "Friend removed successfully."
        )
    else:
        messages.error(
            request,
            "You are not friends with this user."
        )

    return redirect("public_profile", user_id=friend_user.id)
