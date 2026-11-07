from django import forms
from django.contrib.auth.models import User
from .models import Review, LibraryEntry, UserProfile

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
        fields = ['status']

class UserEditForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['username']

        widgets = {
            'username': forms.TextInput(attrs={
                'placeholder': 'Enter your username'
            })
        }


class UserProfileForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ['profile_name', 'bio', 'avatar']

        widgets = {
            'profile_name': forms.TextInput(attrs={
                'placeholder': 'Enter your profile name'
            }),
            'bio': forms.Textarea(attrs={
                'rows': 4,
                'placeholder': 'Tell us about yourself...'
            })
        }