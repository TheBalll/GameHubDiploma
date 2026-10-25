from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('games/', views.games_list, name='games_list'),
    path('games/<int:game_id>/', views.game_detail, name='game_detail'),

    path('register/', views.register, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    path('reviews/<int:review_id>/delete/', views.delete_review, name='delete_review'),

    path('library/', views.library, name='library'),
    path('games/<int:game_id>/add-to-library/', views.add_to_library, name='add_to_library'),
    path('games/<int:game_id>/remove-from-library/', views.remove_from_library, name='remove_from_library'),
    path('games/<int:game_id>/edit-library/',views.edit_library_entry,name='edit_library_entry'
),
]