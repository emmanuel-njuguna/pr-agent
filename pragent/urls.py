"""URL configuration for pragent project."""
from django.contrib import admin
from django.urls import include, path

from github_integration.views import overview

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('allauth.urls')),
    path('github/', include('github_integration.urls')),
    path('', overview, name='home'),
]
