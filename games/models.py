from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator


class Game(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField()
    genre = models.CharField(max_length=100)
    release_year = models.PositiveIntegerField()
    developer = models.CharField(max_length=150)

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