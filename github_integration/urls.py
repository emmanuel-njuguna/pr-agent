from django.urls import path

from . import views

app_name = 'github'

urlpatterns = [
    path('setup/', views.setup, name='setup'),
    path('connect/', views.connect, name='connect'),
    path('callback/', views.callback, name='callback'),
    path('configure/', views.configure, name='configure'),
    path('refresh/', views.refresh, name='refresh'),
    path('disconnect/', views.disconnect, name='disconnect'),
    path('webhook/', views.webhook, name='webhook'),
    # relays such as smee-client strip the trailing slash, and APPEND_SLASH can't redirect a POST
    path('webhook', views.webhook),
]
