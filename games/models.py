from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.conf import settings

class Game(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField()
    genre = models.CharField(max_length=100)
    release_year = models.PositiveIntegerField()
    developer = models.CharField(max_length=150)

    igdb_id = models.PositiveIntegerField(
        unique=True,
        null=True,
        blank=True
    )


    steam_id = models.PositiveIntegerField(
      unique=True,
      null=True,
      blank=True
    )


    background_image = models.URLField(
        max_length=500,
        blank=True
    )

    def __str__(self):
        return self.title

class Review(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE
    )

    game = models.ForeignKey(
        Game,
        on_delete=models.CASCADE
    )

    rating = models.PositiveIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(10)
        ]
    )

    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.game.title}"


class LibraryEntry(models.Model):
    STATUS_CHOICES = [
        ('playing', 'Playing'),
        ('completed', 'Completed'),
        ('plan_to_play', 'Plan to Play'),
        ('dropped', 'Dropped'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE
    )

    game = models.ForeignKey(
        Game,
        on_delete=models.CASCADE
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='plan_to_play'
    )

    def __str__(self):
        return f"{self.user.username} - {self.game.title}"

class UserProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='profile'
    )

    profile_name = models.CharField(
        max_length=50,
        blank=True
    )

    bio = models.TextField(
        max_length=500,
        blank=True
    )

    avatar = models.ImageField(
        upload_to='profile_avatars/',
        blank=True,
        null=True
    )

    def __str__(self):
        return f"{self.user.username}'s Profile"


class FriendRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"

    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sent_friend_requests"
    )

    receiver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="received_friend_requests"
    )

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["sender", "receiver"],
                name="unique_friend_request"
            ),
            models.CheckConstraint(
                condition=~models.Q(
                    sender=models.F("receiver")
                ),
                name="prevent_self_friend_request"
            )
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.sender} -> {self.receiver} "
            f"({self.status})"
        )


class SteamAccount(models.Model):
    # Connect one GameHub user to one Steam account
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="steam_account"
    )

    # Store the verified SteamID64
    steam_id = models.CharField(
        max_length=20,
        unique=True
    )

    connected_at = models.DateTimeField(
        auto_now_add=True
    )

    last_synced_at = models.DateTimeField(
        null=True,
        blank=True
    )

    def __str__(self):
        return f"{self.user.username} - Steam {self.steam_id}"


class SteamOwnedGame(models.Model):
    # Steam account that owns this game
    steam_account = models.ForeignKey(
        SteamAccount,
        on_delete=models.CASCADE,
        related_name="owned_games"
    )

    # Steam AppID
    steam_app_id = models.PositiveIntegerField()

    # Game title from Steam
    name = models.CharField(
        max_length=255
    )

    # Total playtime reported by Steam
    playtime_minutes = models.PositiveIntegerField(
        default=0
    )

    # Optional connection to the GameHub catalog
    game = models.ForeignKey(
        Game,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="steam_library_entries"
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["steam_account", "steam_app_id"],
                name="unique_steam_owned_game"
            )
        ]

    def __str__(self):
        return f"{self.steam_account.user.username} - {self.name}"
