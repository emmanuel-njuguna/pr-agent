from django.urls import path

from . import views

app_name = 'github'

urlpatterns = [
    path('setup/', views.setup, name='setup'),
    path('connect/', views.connect, name='connect'),
    path('callback/', views.callback, name='callback'),
    path('configure/', views.configure, name='configure'),
    path('disconnect/', views.disconnect, name='disconnect'),
    path('webhook/', views.webhook, name='webhook'),
]
