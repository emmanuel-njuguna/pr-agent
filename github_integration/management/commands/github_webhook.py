from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from github_integration import github_api


class Command(BaseCommand):
    help = (
        "Show where GitHub sends this App's webhooks, or with --set point them straight at this "
        "application (PUBLIC_BASE_URL or the given base URL), so no relay is needed."
    )

    def add_arguments(self, parser):
        parser.add_argument('--set', nargs='?', const='', metavar='BASE_URL', dest='base_url',
                            help='Public base URL of this application, e.g. https://pragent.example.com. '
                                 'Defaults to the PUBLIC_BASE_URL setting.')

    def handle(self, *args, base_url=None, **options):
        if not (settings.GITHUB_APP_ID and settings.GITHUB_APP_PRIVATE_KEY_PATH):
            raise CommandError('GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH must be set.')
        try:
            if base_url is None:
                self.stdout.write(f'GitHub sends webhooks to: {github_api.webhook_url() or "(not set)"}')
                return
            url = webhook_target(base_url or settings.PUBLIC_BASE_URL)
            if not settings.GITHUB_WEBHOOK_SECRET:
                raise CommandError('GITHUB_WEBHOOK_SECRET must be set, or every delivery would be rejected.')
            self.stdout.write(self.style.SUCCESS(f'GitHub now sends webhooks to: {github_api.set_webhook_url(url)}'))
        except github_api.GitHubError as exc:
            raise CommandError(str(exc)) from exc


def webhook_target(base_url):
    """https://host[/prefix] -> https://host[/prefix]/github/webhook/, refusing URLs GitHub can't or shouldn't use."""
    if not base_url:
        raise CommandError('Pass a base URL with --set, or set PUBLIC_BASE_URL.')
    parts = urlsplit(base_url)
    if parts.scheme != 'https' or not parts.hostname:
        raise CommandError(f'{base_url!r} must be an https:// URL: GitHub signs payloads but they are not encrypted.')
    if parts.hostname in ('localhost', '127.0.0.1', '::1'):
        raise CommandError("GitHub can't reach localhost. Use the relay for local development.")
    return base_url.rstrip('/') + reverse('github:webhook')
