"""Thin client for the GitHub REST API calls the GitHub App needs. No token is ever persisted."""
import time
from pathlib import Path

import jwt
import requests
from django.conf import settings

API = 'https://api.github.com'
TIMEOUT = 10
HEADERS = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}


class GitHubError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

    @property
    def lost_access(self):
        """The installation was removed or its access revoked."""
        return self.status in (401, 403, 404)


def _request(method, url, token=None, accept=None, **kwargs):
    headers = dict(HEADERS, Accept=accept or HEADERS['Accept'])
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        response = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise GitHubError(f'Could not reach GitHub: {exc.__class__.__name__}') from exc
    if response.status_code >= 400:
        raise GitHubError(f'GitHub returned {response.status_code} for {url}', response.status_code)
    return response


def app_jwt():
    """Short-lived JWT that authenticates as the GitHub App itself."""
    now = int(time.time())
    private_key = Path(settings.GITHUB_APP_PRIVATE_KEY_PATH).read_text()
    return jwt.encode({'iat': now - 60, 'exp': now + 9 * 60, 'iss': str(settings.GITHUB_APP_ID)}, private_key, algorithm='RS256')


def installation_token(installation_id):
    # shortcut: mints a fresh token on every call; cache it (~55 min) once review jobs call GitHub often
    response = _request('POST', f'{API}/app/installations/{installation_id}/access_tokens', token=app_jwt())
    return response.json()['token']


def installation_account(installation_id):
    account = _request('GET', f'{API}/app/installations/{installation_id}', token=app_jwt()).json()['account']
    return {'login': account['login'], 'type': account['type'], 'avatar_url': account['avatar_url']}


def installation_repositories(installation_id):
    token = installation_token(installation_id)
    url, repos = f'{API}/installation/repositories?per_page=100', []
    while url:
        response = _request('GET', url, token=token)
        repos += response.json()['repositories']
        url = response.links.get('next', {}).get('url')
    return repos


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
