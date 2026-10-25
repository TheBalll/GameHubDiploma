from django.shortcuts import render, get_object_or_404, redirect
from .models import Game, Review
from .forms import ReviewForm
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import authenticate, login, logout

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

            return redirect('game_detail', game_id=game.id)

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

def delete_review(request, review_id):
    review = get_object_or_404(Review, id=review_id)

    if review.user == request.user:
        game_id = review.game.id
        review.delete()

        return redirect('game_detail', game_id=game_id)

    return redirect('game_detail', game_id=review.game.id)