from django.utils import timezone

from . import github_api
from .models import GitHubConnection, PullRequestEvent, Repository

PULL_REQUEST_ACTIONS = {'opened', 'reopened', 'synchronize'}


def sync_repositories(connection):
    """Mirror the repositories the installation can access, keeping each repo's monitored flag."""
    try:
        repos = github_api.installation_repositories(connection.installation_id)
    except github_api.GitHubError as exc:
        if exc.lost_access:
            connection.status = GitHubConnection.Status.NEEDS_ATTENTION
            connection.save(update_fields=['status', 'updated_at'])
        raise
    for repo in repos:
        Repository.objects.update_or_create(
            connection=connection, github_id=repo['id'],
            defaults={'full_name': repo['full_name'], 'private': repo['private']},
        )
    connection.repositories.exclude(github_id__in=[repo['id'] for repo in repos]).delete()


def handle_event(event, payload, delivery):
    """Apply one verified webhook. Events for installations pragent doesn't know are ignored."""
    installation_id = (payload.get('installation') or {}).get('id')
    connection = GitHubConnection.objects.filter(installation_id=installation_id).first() if installation_id else None
    if connection is None:
        return
    action = payload.get('action')

    if event == 'installation':
        if action in ('deleted', 'suspend'):
            connection.status = GitHubConnection.Status.NEEDS_ATTENTION
        elif action in ('unsuspend', 'new_permissions_accepted'):
            connection.status = GitHubConnection.Status.ACTIVE
        else:
            return
        connection.save(update_fields=['status', 'updated_at'])

    elif event == 'installation_repositories':
        for repo in payload.get('repositories_added', []):
            Repository.objects.update_or_create(
                connection=connection, github_id=repo['id'],
                defaults={'full_name': repo['full_name'], 'private': repo['private']},
            )
        removed = [repo['id'] for repo in payload.get('repositories_removed', [])]
        connection.repositories.filter(github_id__in=removed).delete()

    elif event == 'pull_request' and action in PULL_REQUEST_ACTIONS:
        repo_data = payload['repository']
        repo = connection.repositories.filter(github_id=repo_data['id']).first()
        if repo is None and connection.monitor_all:
            # a repo we missed an installation_repositories event for; "all" covers it anyway
            repo = Repository.objects.create(
                connection=connection, github_id=repo_data['id'],
                full_name=repo_data['full_name'], private=repo_data['private'],
            )
        if repo is None or not repo.is_monitored:
            return
        pr = payload['pull_request']
        PullRequestEvent.objects.create(
            repository=repo, delivery=delivery, action=action, number=pr['number'], title=pr['title'][:300],
            author_login=pr['user']['login'], html_url=pr['html_url'], head_sha=pr['head']['sha'],
        )
        repo.last_event_at = timezone.now()
        repo.save(update_fields=['last_event_at'])
