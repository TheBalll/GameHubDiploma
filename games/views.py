import requests
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden
from django.urls import reverse
from django.db.models import Q
from django.core.exceptions import PermissionDenied

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.decorators import login_required

from django.views.decorators.http import require_GET, require_POST

from .models import Game, Review, LibraryEntry, UserProfile
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

def home(request):
    return render(request, 'games/home.html')



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
    entries = (
        LibraryEntry.objects
        .filter(user=request.user)
        .select_related("game")
        .order_by("game__title")
    )

    games = []

    for entry in entries:
        games.append({
            "id": entry.game.id,
            "title": entry.game.title,
            "genre": entry.game.genre,
            "status": entry.status,
            "status_display": entry.get_status_display(),
            "details_url": reverse(
                "game_detail",
                kwargs={"game_id": entry.game.id}
            ),
            "edit_url": reverse(
                "edit_library_entry",
                kwargs={"game_id": entry.game.id}
            ),
            "remove_url": reverse(
                "remove_from_library",
                kwargs={"game_id": entry.game.id}
            ),
        })

    status_choices = [
        {"value": value, "label": label}
        for value, label in LibraryEntry._meta.get_field("status").choices
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
    # Намираме потребителя, чийто профил искаме да отворим
    profile_user = get_object_or_404(User, id=user_id)

    # Извличаме допълнителната информация за профила
    user_profile = UserProfile.objects.filter(
        user=profile_user
    ).first()

    # Извличаме ревютата на потребителя
    user_reviews = Review.objects.filter(
        user=profile_user
    ).select_related("game").order_by("-created_at")

    # Извличаме игрите от неговата библиотека
    library_entries = LibraryEntry.objects.filter(
        user=profile_user
    ).select_related("game")

    context = {
        "profile_user": profile_user,
        "user_profile": user_profile,
        "user_reviews": user_reviews,
        "library_entries": library_entries,
        "reviews_count": user_reviews.count(),
        "games_count": library_entries.count()
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

    except requests.RequestException:
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
