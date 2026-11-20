import hashlib
import hmac
import json
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import github_api
from .models import GitHubConnection, WebhookDelivery
from .services import handle_event, sync_repositories

STATE_SESSION_KEY = 'github_install_state'
Status = GitHubConnection.Status


def _connection(user):
    return GitHubConnection.objects.filter(user=user).first()


@login_required
def overview(request):
    connection = _connection(request.user)
    # shortcut: the only app page today, so this check is the "setup first" gate; make it middleware when more pages exist
    if connection is None or not connection.is_configured:
        return redirect('github:setup')

    repositories = connection.repositories.select_related('connection')
    query = request.GET.get('q', '').strip()
    if query:
        repositories = repositories.filter(full_name__icontains=query)
    page = Paginator(repositories, 25).get_page(request.GET.get('page'))
    return render(request, 'github_integration/overview.html', {
        'connection': connection,
        'page': page,
        'query': query,
        'total_count': connection.repositories.count(),
        'monitored_count': connection.repositories.filter(monitored=True).count(),
    })


@login_required
def setup(request):
    connection = _connection(request.user)
    step = 2 if connection and connection.status == Status.ACTIVE else 1
    return render(request, 'github_integration/setup.html', {
        'connection': connection,
        'step': step,
        'repositories': connection.repositories.all() if step == 2 else [],
        'app_ready': bool(settings.GITHUB_APP_SLUG),
    })


@login_required
def connect(request):
    if not settings.GITHUB_APP_SLUG:
        messages.error(request, 'The GitHub App is not configured on this server yet.')
        return redirect('github:setup')
    state = secrets.token_urlsafe(32)
    request.session[STATE_SESSION_KEY] = state
    return redirect(f'https://github.com/apps/{settings.GITHUB_APP_SLUG}/installations/new?{urlencode({"state": state})}')


@login_required
def callback(request):
    expected_state = request.session.pop(STATE_SESSION_KEY, None)
    if not expected_state or not secrets.compare_digest(expected_state, request.GET.get('state', '')):
        messages.error(request, 'That GitHub connection link was invalid or has expired. Please try again.')
        return redirect('github:setup')

    if request.GET.get('setup_action') == 'request':
        connection, _ = GitHubConnection.objects.get_or_create(user=request.user)
        if connection.status != Status.ACTIVE:
            connection.status = Status.PENDING
            connection.save(update_fields=['status', 'updated_at'])
        messages.info(request, 'Installation requested. An organisation admin must approve it on GitHub.')
        return redirect('github:setup')

    try:
        installation_id = int(request.GET['installation_id'])
        user_token = github_api.exchange_code(request.GET['code'])
        # the installation_id in the URL can be forged: only accept one this GitHub user can actually access
        if installation_id not in github_api.user_installation_ids(user_token):
            messages.error(request, "Your GitHub account doesn't have access to that installation.")
            return redirect('github:setup')
        account = github_api.installation_account(installation_id)
    except (KeyError, ValueError):
        messages.error(request, 'GitHub sent back an incomplete response. Please try connecting again.')
        return redirect('github:setup')
    except github_api.GitHubError:
        messages.error(request, "We couldn't verify the installation with GitHub. Please try again.")
        return redirect('github:setup')

    if GitHubConnection.objects.filter(installation_id=installation_id).exclude(user=request.user).exists():
        messages.error(request, f'{account["login"]} is already connected to another pragent account.')
        return redirect('github:setup')
    try:
        connection, _ = GitHubConnection.objects.update_or_create(user=request.user, defaults={
            'installation_id': installation_id,
            'account_login': account['login'],
            'account_type': account['type'],
            'account_avatar_url': account['avatar_url'],
            'status': Status.ACTIVE,
        })
    except IntegrityError:
        messages.error(request, f'{account["login"]} is already connected to another pragent account.')
        return redirect('github:setup')

    try:
        sync_repositories(connection)
    except github_api.GitHubError:
        messages.error(request, "Connected, but we couldn't load your repositories yet. Please connect again shortly.")
    # a reconnect after lost access returns to the overview; a first connection continues to step 2
    return redirect('home' if connection.configured_at else 'github:setup')


@login_required
@require_POST
def configure(request):
    connection = get_object_or_404(GitHubConnection, user=request.user, status=Status.ACTIVE)
    monitor_all = request.POST.get('scope') == 'all'
    selected = {int(pk) for pk in request.POST.getlist('repositories') if pk.isdigit()}
    repositories = connection.repositories.all()  # scoped: ids from other users' repos never match
    if not monitor_all and not repositories.filter(pk__in=selected).exists():
        messages.error(request, 'Choose at least one repository, or monitor all repositories.')
        return redirect('github:setup')

    with transaction.atomic():
        repositories.update(monitored=False)
        repositories.filter(pk__in=selected).update(monitored=True)
        connection.monitor_all = monitor_all
        connection.configured_at = connection.configured_at or timezone.now()
        connection.save(update_fields=['monitor_all', 'configured_at', 'updated_at'])
    messages.success(request, 'GitHub configuration saved.')
    return redirect('home')


@login_required
@require_POST
def refresh(request):
    """Re-read the repository list from GitHub, for when a webhook was missed."""
    connection = get_object_or_404(GitHubConnection, user=request.user, status=Status.ACTIVE)
    try:
        sync_repositories(connection)
    except github_api.GitHubError:
        messages.error(request, "We couldn't reach GitHub to refresh your repositories. Please try again.")
    else:
        messages.success(request, 'Repository list refreshed from GitHub.')
    return redirect(request.POST.get('next') if request.POST.get('next') == reverse('github:setup') else 'home')


@login_required
@require_POST
def disconnect(request):
    GitHubConnection.objects.filter(user=request.user).delete()
    messages.info(request, "Disconnected. To remove pragent's access completely, uninstall the app in your GitHub settings.")
    return redirect('github:setup')


@csrf_exempt
@require_POST
def webhook(request):
    secret = settings.GITHUB_WEBHOOK_SECRET
    signature = request.headers.get('X-Hub-Signature-256', '')
    if not secret or not signature:
        return HttpResponse(status=401)
    expected = 'sha256=' + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return HttpResponse(status=401)

    delivery_id = request.headers.get('X-GitHub-Delivery', '')
    event = request.headers.get('X-GitHub-Event', '')
    try:
        payload = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)
    if not delivery_id or not isinstance(payload, dict):
        return HttpResponse(status=400)

    # shortcut: processed inside the request; move to a task queue once review jobs exist
    try:
        with transaction.atomic():
            delivery, created = WebhookDelivery.objects.get_or_create(delivery_id=delivery_id, defaults={'event': event})
            if created:
                handle_event(event, payload, delivery)
    except (KeyError, TypeError):
        return HttpResponse(status=400)
    return HttpResponse(status=200)
