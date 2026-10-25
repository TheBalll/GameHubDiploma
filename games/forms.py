from django import forms
from .models import Review, LibraryEntry


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review

        fields = [
            'rating',
            'comment'
        ]


class LibraryEntryForm(forms.ModelForm):
    class Meta:
        model = LibraryEntry

        fields = [
            'status',
            'hours_played'
        ]