from django.contrib import admin
from .models import Game, Review, LibraryEntry, FriendRequest


admin.site.register(Game)
admin.site.register(Review)
admin.site.register(LibraryEntry)


@admin.register(FriendRequest)
class FriendRequestAdmin(admin.ModelAdmin):
    list_display = (
        "sender",
        "receiver",
        "status",
        "created_at",
    )

    list_filter = (
        "status",
        "created_at",
    )

    search_fields = (
        "sender__username",
        "receiver__username",
    )

    readonly_fields = (
        "created_at",
    )
