from django.contrib import admin
from .models import Game, Review, LibraryEntry


admin.site.register(Game)
admin.site.register(Review)
admin.site.register(LibraryEntry)