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

def home(request):
    return render(request, 'games/home.html')


def games_list(request):
    games = Game.objects.all()

    return render(request, 'games/games_list.html', {
        'games': games
    })

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

    # При заявка от JavaScript връщаме JSON
    if request.GET.get("format") == "json":

        games = Game.objects.all()

        # Търсене по име, жанр или разработчик
        if query:
            games = games.filter(
                Q(title__icontains=query) |
                Q(genre__icontains=query) |
                Q(developer__icontains=query)
            )

        # Филтриране по жанр
        if genre:
            games = games.filter(genre=genre)

        # Сортиране
        sorting_options = {
            "title": "title",
            "newest": "-release_year",
            "oldest": "release_year",
        }

        order = sorting_options.get(sort, "title")
        games = games.order_by(order, "id")

        # Всички жанрове идват от SQLite
        genres = list(
            Game.objects.order_by("genre")
            .values_list("genre", flat=True)
            .distinct()
        )

        results = []

        for game in games:
            results.append({
                "id": game.id,
                "title": game.title,
                "genre": game.genre,
                "developer": game.developer,
                "release_year": game.release_year,
                "url": reverse(
                    "game_detail",
                    kwargs={"game_id": game.id}
                )
            })

        return JsonResponse({
            "games": results,
            "genres": genres,
            "count": len(results)
        })

    # Обикновено отваряне на страницата
    return render(request, "games/search_results.html", {
        "query": query
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
