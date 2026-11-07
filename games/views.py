from django.shortcuts import render, get_object_or_404, redirect
from .models import Game, Review, LibraryEntry, UserProfile
from .forms import ReviewForm, LibraryEntryForm, UserProfileForm
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.urls import reverse

def home(request):
    return render(request, 'games/home.html')


def games_list(request):
    games = Game.objects.all()

    return render(request, 'games/games_list.html', {
        'games': games
    })

def game_detail(request, game_id):
    game = get_object_or_404(Game, id=game_id)

    reviews = Review.objects.filter(game=game)

    if request.method == 'POST':
        form = ReviewForm(request.POST)

        if form.is_valid() and request.user.is_authenticated:
            review = form.save(commit=False)

            review.user = request.user
            review.game = game

            review.save()

            return redirect(reverse('game_detail', kwargs={'game_id': game.id}) + '#reviews')

    else:
        form = ReviewForm()

    return render(request, 'games/game_detail.html', {
        'game': game,
        'reviews': reviews,
        'form': form
    })

def register(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)

        if form.is_valid():
            form.save()
            return redirect('login')

    else:
        form = UserCreationForm()

    return render(request, 'games/register.html', {
        'form': form
    })

def login_view(request):
    if request.method == 'POST':
        username = request.POST['username']
        password = request.POST['password']

        user = authenticate(
            request,
            username=username,
            password=password
        )

        if user is not None:
            login(request, user)
            return redirect('home')

    return render(request, 'games/login.html')

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

    if request.method == 'POST':
        form = ReviewForm(request.POST, instance=review)

        if form.is_valid():
            form.save()
            return redirect(reverse('game_detail', kwargs={'game_id': review.game.id}) + '#reviews')
    else:
        form = ReviewForm(instance=review)

    return render(request, 'games/edit_review.html', {
        'form': form,
        'review': review
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

def add_to_library(request, game_id):
    if not request.user.is_authenticated:
        return redirect('login')

    game = get_object_or_404(Game, id=game_id)

    LibraryEntry.objects.get_or_create(
        user=request.user,
        game=game
    )

    return redirect('library')


@require_POST
def remove_from_library(request, game_id):
    if not request.user.is_authenticated:
        return redirect('login')

    library_entry = get_object_or_404(
        LibraryEntry,
        user=request.user,
        game_id=game_id
    )

    library_entry.delete()

    return redirect('library')


def edit_library_entry(request, game_id):
    if not request.user.is_authenticated:
        return redirect('login')

    library_entry = get_object_or_404(
        LibraryEntry,
        user=request.user,
        game_id=game_id
    )

    if request.method == 'POST':
        form = LibraryEntryForm(
            request.POST,
            instance=library_entry
        )

        if form.is_valid():
            form.save()
            return redirect('library')

    else:
        form = LibraryEntryForm(
            instance=library_entry
        )

    return render(request, 'games/edit_library_entry.html', {
        'form': form,
        'library_entry': library_entry
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
    query = request.GET.get('q', '').strip()

    if query:
        games = Game.objects.filter(
            title__icontains=query
        )
    else:
        games = Game.objects.none()

    return render(request, 'games/search_results.html', {
        'games': games,
        'query': query
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

    if request.method == 'POST':
        profile_form = UserProfileForm(
            request.POST,
            request.FILES,
            instance=user_profile
        )

        if profile_form.is_valid():
            profile_form.save()
            return redirect('profile')

    else:
        profile_form = UserProfileForm(instance=user_profile)

    return render(request, 'games/edit_profile.html', {
        'profile_form': profile_form
    })