"""Thin client for the GitHub REST API calls the GitHub App needs. Tokens live only in the cache, never the DB."""
import time
from datetime import datetime
from pathlib import Path

import jwt
import requests
from django.conf import settings
from django.core.cache import cache

API = 'https://api.github.com'
TIMEOUT = (5, 20)  # connect, read: every call is bounded so a worker can never hang on GitHub
HEADERS = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
TOKEN_EARLY_EXPIRY = 5 * 60  # drop cached installation tokens 5 minutes before GitHub does


class GitHubError(Exception):
    def __init__(self, message, status=None, retry_after=None, rate_limited=False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after  # seconds GitHub asked us to wait, if it said
        self.rate_limited = rate_limited

    @property
    def lost_access(self):
        """The installation was removed or its access revoked."""
        return self.status in (401, 403, 404)

    @property
    def retryable(self):
        """Network errors, timeouts, 5xx, 429 and rate limits are worth retrying; other 4xx are our fault."""
        return self.status is None or self.status >= 500 or self.status == 429 or self.rate_limited


def _rate_limit_details(response):
    """(rate_limited, retry_after) for a 403/429, covering both primary and secondary rate limits."""
    retry_after = response.headers.get('Retry-After')
    if retry_after and retry_after.isdigit():
        return True, int(retry_after)
    if response.headers.get('X-RateLimit-Remaining') == '0':
        reset = response.headers.get('X-RateLimit-Reset', '')
        return True, max(int(reset) - int(time.time()), 1) if reset.isdigit() else None
    try:
        message = response.json().get('message', '')
    except ValueError:
        message = ''
    return 'rate limit' in message.lower(), None


def _request(method, url, token=None, accept=None, **kwargs):
    headers = dict(HEADERS, Accept=accept or HEADERS['Accept'])
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        response = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        # the exception text can include request details, so only its class is surfaced
        raise GitHubError(f'Could not reach GitHub: {exc.__class__.__name__}') from exc
    if response.status_code >= 400:
        rate_limited, retry_after = _rate_limit_details(response) if response.status_code in (403, 429) else (False, None)
        raise GitHubError(f'GitHub returned {response.status_code} for {method} {url}', response.status_code,
                          retry_after=retry_after, rate_limited=rate_limited)
    return response


def app_jwt():
    """Short-lived JWT that authenticates as the GitHub App itself."""
    now = int(time.time())
    private_key = Path(settings.GITHUB_APP_PRIVATE_KEY_PATH).read_text()
    return jwt.encode({'iat': now - 60, 'exp': now + 9 * 60, 'iss': str(settings.GITHUB_APP_ID)}, private_key, algorithm='RS256')


def webhook_url():
    """Where GitHub currently sends this App's webhooks (one URL for every installation)."""
    return _request('GET', f'{API}/app/hook/config', token=app_jwt()).json().get('url', '')


def set_webhook_url(url):
    """Point the App's webhook at url, signed with GITHUB_WEBHOOK_SECRET and with TLS verification on."""
    config = {'url': url, 'content_type': 'json', 'insecure_ssl': '0', 'secret': settings.GITHUB_WEBHOOK_SECRET}
    return _request('PATCH', f'{API}/app/hook/config', token=app_jwt(), json=config).json().get('url', '')


def _token_cache_key(installation_id):
    return f'github:installation-token:{installation_id}'


def installation_token(installation_id):
    """A cached installation token, minted again once it is within 5 minutes of expiring."""
    key = _token_cache_key(installation_id)
    token = cache.get(key)
    if token:
        return token
    data = _request('POST', f'{API}/app/installations/{installation_id}/access_tokens', token=app_jwt()).json()
    expires_at = datetime.fromisoformat(data['expires_at'].replace('Z', '+00:00')).timestamp()
    lifetime = int(expires_at - time.time()) - TOKEN_EARLY_EXPIRY
    if lifetime > 0:
        cache.set(key, data['token'], timeout=lifetime)
    return data['token']


def _installation_request(installation_id, method, url, **kwargs):
    """Call GitHub as the installation. A 401 means the cached token was revoked: drop it and retry once."""
    try:
        return _request(method, url, token=installation_token(installation_id), **kwargs)
    except GitHubError as exc:
        if exc.status != 401:
            raise
        cache.delete(_token_cache_key(installation_id))
        return _request(method, url, token=installation_token(installation_id), **kwargs)


def _paginate(installation_id, url, key=None):
    items = []
    while url:
        response = _installation_request(installation_id, 'GET', url)
        items += response.json()[key] if key else response.json()
        url = response.links.get('next', {}).get('url')
    return items


def pull_request(installation_id, full_name, number):
    return _installation_request(installation_id, 'GET', f'{API}/repos/{full_name}/pulls/{number}').json()


def pull_request_files(installation_id, full_name, number):
    """Changed files with their patches (GitHub returns at most 3000 files)."""
    return _paginate(installation_id, f'{API}/repos/{full_name}/pulls/{number}/files?per_page=100')


def pull_request_reviews(installation_id, full_name, number):
    return _paginate(installation_id, f'{API}/repos/{full_name}/pulls/{number}/reviews?per_page=100')


def create_review(installation_id, full_name, number, commit_id, body):
    """Post a COMMENT review pinned to commit_id and return its id."""
    response = _installation_request(installation_id, 'POST', f'{API}/repos/{full_name}/pulls/{number}/reviews',
                                     json={'commit_id': commit_id, 'body': body, 'event': 'COMMENT'})
    return response.json()['id']


def installation_account(installation_id):
    account = _request('GET', f'{API}/app/installations/{installation_id}', token=app_jwt()).json()['account']
    return {'login': account['login'], 'type': account['type'], 'avatar_url': account['avatar_url']}


def installation_repositories(installation_id):
    return _paginate(installation_id, f'{API}/installation/repositories?per_page=100', key='repositories')


def exchange_code(code):
    """Swap the OAuth code from the install callback for a short-lived user token (used once, never stored)."""
    response = _request('POST', 'https://github.com/login/oauth/access_token', accept='application/json', data={
        'client_id': settings.GITHUB_APP_CLIENT_ID,
        'client_secret': settings.GITHUB_APP_CLIENT_SECRET,
        'code': code,
    })
    token = response.json().get('access_token')
    if not token:
        raise GitHubError('GitHub did not accept the authorization code')
    return token


def user_installation_ids(user_token):
    url, ids = f'{API}/user/installations?per_page=100', set()
    while url:
        response = _request('GET', url, token=user_token)
        ids |= {inst['id'] for inst in response.json()['installations']}
        url = response.links.get('next', {}).get('url')
    return ids
