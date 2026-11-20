from django.urls import path, re_path

from . import views

app_name = 'github'

urlpatterns = [
    path('setup/', views.setup, name='setup'),
    path('connect/', views.connect, name='connect'),
    path('callback/', views.callback, name='callback'),
    path('configure/', views.configure, name='configure'),
    path('refresh/', views.refresh, name='refresh'),
    path('disconnect/', views.disconnect, name='disconnect'),
    # trailing slash optional: relays such as smee-client strip it, and APPEND_SLASH can't redirect a POST
    re_path(r'^webhook/?$', views.webhook, name='webhook'),
]
