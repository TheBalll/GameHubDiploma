from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
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
    added_at = models.DateTimeField(auto_now_add=True, null=True)

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
        null=True,
        blank=True
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

    @property
    def playtime_hours(self):
        if self.playtime_minutes is None:
            return None
        return round(self.playtime_minutes / 60, 1)

    def __str__(self):
        return f"{self.steam_account.user.username} - {self.name}"


def validate_igdb_id_list(value):
    if not isinstance(value, list) or any(type(item) is not int or not 0 < item <= 2**63 - 1 for item in value):
        raise ValidationError("Expected a list of positive IGDB IDs.")


class IgdbGameMetadata(models.Model):
    igdb_id = models.PositiveBigIntegerField(primary_key=True, validators=[MinValueValidator(1)])
    genres = models.JSONField(null=True, blank=True, validators=[validate_igdb_id_list])
    themes = models.JSONField(null=True, blank=True, validators=[validate_igdb_id_list])
    game_modes = models.JSONField(null=True, blank=True, validators=[validate_igdb_id_list])
    keywords = models.JSONField(null=True, blank=True, validators=[validate_igdb_id_list])
    similar_games = models.JSONField(null=True, blank=True, validators=[validate_igdb_id_list])

    rating = models.FloatField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)])
    rating_count = models.PositiveIntegerField(null=True, blank=True)
    aggregated_rating = models.FloatField(
        null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    aggregated_rating_count = models.PositiveIntegerField(null=True, blank=True)
    total_rating = models.FloatField(
        null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    total_rating_count = models.PositiveIntegerField(null=True, blank=True)

    cover_image_id = models.CharField(max_length=100, blank=True, default="")
    igdb_updated_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(igdb_id__gt=0), name="igdb_metadata_positive_id"),
            *[
                models.CheckConstraint(
                    condition=models.Q(**{f"{field}__isnull": True}) | models.Q(**{f"{field}__range": (0, 100)}),
                    name=f"igdb_metadata_valid_{field}",
                )
                for field in ("rating", "aggregated_rating", "total_rating")
            ],
        ]

    def __str__(self):
        return f"IGDB {self.igdb_id}"


class SteamIgdbIdentity(models.Model):
    class Status(models.TextChoices):
        MATCHED = "matched", "Matched"
        MISSING = "missing", "Missing"
        AMBIGUOUS = "ambiguous", "Ambiguous"
        CONFLICT = "conflict", "Conflict"

    steam_app_id = models.PositiveBigIntegerField(primary_key=True, validators=[MinValueValidator(1)])
    metadata = models.ForeignKey(
        IgdbGameMetadata,
        on_delete=models.PROTECT,
        related_name="steam_identities",
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=9, choices=Status.choices)
    last_checked = models.DateTimeField()
    candidate_igdb_ids = models.JSONField(default=list, blank=True, validators=[validate_igdb_id_list])

    class Meta:
        indexes = [models.Index(fields=["status", "last_checked"], name="steam_igdb_status_checked_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(steam_app_id__gt=0), name="steam_igdb_positive_app_id"),
            models.CheckConstraint(
                condition=models.Q(status="matched", metadata__isnull=False)
                | models.Q(status__in=("missing", "ambiguous", "conflict"), metadata__isnull=True),
                name="steam_igdb_status_matches_fk",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=("matched", "missing"), candidate_igdb_ids=[])
                | (models.Q(status__in=("ambiguous", "conflict")) & ~models.Q(candidate_igdb_ids=[])),
                name="steam_igdb_candidates_by_status",
            ),
        ]

    def clean(self):
        super().clean()
        candidate_ids = self.candidate_igdb_ids
        validate_igdb_id_list(candidate_ids)
        count = len(set(candidate_ids))
        if self.status == self.Status.AMBIGUOUS and count < 2:
            raise ValidationError({"candidate_igdb_ids": "An ambiguous match needs at least two distinct IDs."})
        if self.status == self.Status.CONFLICT and count != 1:
            raise ValidationError({"candidate_igdb_ids": "A conflict needs one IGDB candidate ID."})

    def __str__(self):
        return f"Steam AppID {self.steam_app_id} ({self.status})"
