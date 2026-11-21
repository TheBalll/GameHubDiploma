from django.urls import path
from . import views
from . import steam_views


urlpatterns = [
    path('', views.home, name='home'),

    path('games/', views.games_list, name='games_list'),
    path('games/<int:game_id>/', views.game_detail, name='game_detail'),

    path('register/', views.register, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    path(
        'reviews/<int:review_id>/delete/',
        views.delete_review,
        name='delete_review'
    ),

    path('profile/', views.profile, name='profile'),
    path('profile/edit/', views.edit_profile, name='edit_profile'),
    
    path('library/', views.library, name='library'),
    path('library/data/', views.library_data, name='library_data'),

    path(
        'games/<int:game_id>/add-to-library/',
        views.add_to_library,
        name='add_to_library'
    ),

    path(
        'games/<int:game_id>/remove-from-library/',
        views.remove_from_library,
        name='remove_from_library'
    ),

    path(
        'games/<int:game_id>/edit-library/',
        views.edit_library_entry,
        name='edit_library_entry'
    ),

    path(
        'search-games/',
        views.search_games,
        name='search_games'
    ),

    path(
        'search/',
        views.search_results,
        name='search_results'
    ),

    path('reviews/<int:review_id>/edit/', views.edit_review, name='edit_review'),

    path('users/<int:user_id>/', views.public_profile, name='public_profile'),
   
    path('reviews/<int:review_id>/delete-from-profile/',views.delete_review_from_profile,name='delete_review_from_profile'),
    
    path(
    "games/import-igdb/<int:igdb_id>/",
    views.import_igdb_game,
    name="import_igdb_game"
    ),
    
    path(
    "games/import-steam/<int:steam_id>/",
    views.import_steam_game,
    name="import_steam_game",
    ),
    
    path(
    "friends/send/<int:user_id>/",
    views.send_friend_request,
    name="send_friend_request"
    ),


  path(
    "friends/accept/<int:request_id>/",
    views.accept_friend_request,
    name="accept_friend_request"
  ),

  path(
    "friends/decline/<int:request_id>/",
    views.decline_friend_request,
    name="decline_friend_request"
  ),

  path(
    "friends/requests/",
    views.friend_requests_page,
    name="friend_requests_page"
  ),

   path(
    "friends/find/",
    views.find_friends,
    name="find_friends"
  ),

  path(
    "friends/cancel/<int:request_id>/",
    views.cancel_friend_request,
    name="cancel_friend_request"
  ),

  path(
    "friends/remove/<int:user_id>/",
    views.remove_friend,
    name="remove_friend"
  ),

  path(
    "friends/",
    views.my_friends,
    name="my_friends"
  ),

  # STEAM ACCOUNT CONNECTION

  path(
    "steam/",
    steam_views.steam_settings_page,
    name="steam_settings"
  ),

  path(
    "steam/connect/",
    steam_views.steam_connect,
    name="steam_connect"
  ),

  path(
    "steam/callback/",
    steam_views.steam_callback,
    name="steam_callback"
  ),

  # STEAM LIBRARY SYNCHRONIZATION

  path(
    "steam/sync/",
    steam_views.steam_sync_library,
    name="steam_sync_library"
  ),
]