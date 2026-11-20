from django.contrib import admin

from .models import GitHubConnection, PullRequestEvent, Repository, WebhookDelivery


@admin.register(GitHubConnection)
class GitHubConnectionAdmin(admin.ModelAdmin):
    list_display = ['user', 'account_login', 'installation_id', 'status', 'monitor_all', 'configured_at']
    list_filter = ['status']


@admin.register(Repository)
class RepositoryAdmin(admin.ModelAdmin):
    list_display = ['full_name', 'connection', 'private', 'monitored', 'last_event_at']
    search_fields = ['full_name']


@admin.register(PullRequestEvent)
class PullRequestEventAdmin(admin.ModelAdmin):
    list_display = ['repository', 'number', 'action', 'author_login', 'received_at']
    list_filter = ['action']


admin.site.register(WebhookDelivery)
