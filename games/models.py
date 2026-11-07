from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator

class Game(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField()
    genre = models.CharField(max_length=100)
    release_year = models.PositiveIntegerField()
    developer = models.CharField(max_length=150)

    rawg_id = models.PositiveIntegerField(
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