import hashlib
import hmac
import json
import tempfile
from unittest import mock

import jwt
from allauth.account.models import EmailAddress
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.apps import apps
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from . import github_api
from .models import GitHubConnection, PullRequestEvent, Repository, WebhookDelivery
from .views import STATE_SESSION_KEY

WEBHOOK_SECRET = 'test-webhook-secret'
PASSWORD = 'a-Strong-pass-123'


def make_user(email):
    user = User.objects.create_user(username=email.split('@')[0], email=email, password=PASSWORD)
    EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user


def make_connection(user, installation_id, repos=(), monitor_all=False, configured=True):
    connection = GitHubConnection.objects.create(
        user=user, installation_id=installation_id, account_login=f'acct{installation_id}', account_type='User',
        status=GitHubConnection.Status.ACTIVE, monitor_all=monitor_all,
        configured_at=timezone.now() if configured else None,
    )
    for github_id, name in repos:
        Repository.objects.create(connection=connection, github_id=github_id, full_name=name)
    return connection


class RoutingTests(TestCase):
    def test_first_login_lands_on_setup(self):
        make_user('ada@example.com')
        response = self.client.post(reverse('account_login'), {'login': 'ada@example.com', 'password': PASSWORD}, follow=True)
        self.assertRedirects(response, reverse('github:setup'))

    def test_configured_user_sees_overview(self):
        user = make_user('ada@example.com')
        make_connection(user, 1, repos=[(10, 'ada/app')])
        self.client.force_login(user)
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ada/app')

    def test_pending_connection_still_goes_to_setup(self):
        user = make_user('ada@example.com')
        GitHubConnection.objects.create(user=user)
        self.client.force_login(user)
        self.assertRedirects(self.client.get(reverse('home')), reverse('github:setup'))

    @override_settings(GITHUB_APP_SLUG='pragent-test')
    def test_connect_stores_state_and_redirects_to_github(self):
        self.client.force_login(make_user('ada@example.com'))
        response = self.client.get(reverse('github:connect'))
        state = self.client.session[STATE_SESSION_KEY]
        self.assertEqual(response['Location'], f'https://github.com/apps/pragent-test/installations/new?state={state}')


@mock.patch.object(github_api, 'installation_repositories', return_value=[
    {'id': 10, 'full_name': 'ada/app', 'private': True}, {'id': 11, 'full_name': 'ada/lib', 'private': False},
])
@mock.patch.object(github_api, 'installation_account', return_value={'login': 'ada', 'type': 'User', 'avatar_url': ''})
@mock.patch.object(github_api, 'user_installation_ids', return_value={42})
@mock.patch.object(github_api, 'exchange_code', return_value='short-lived-user-token')
class CallbackTests(TestCase):
    def setUp(self):
        self.user = make_user('ada@example.com')
        self.client.force_login(self.user)

    def callback(self, state='good-state', session_state='good-state', **params):
        if session_state:
            session = self.client.session
            session[STATE_SESSION_KEY] = session_state
            session.save()
        query = {'state': state, 'installation_id': '42', 'code': 'abc', 'setup_action': 'install', **params}
        return self.client.get(reverse('github:callback'), query)

    def test_valid_callback_creates_active_connection_with_repositories(self, *mocks):
        response = self.callback()
        self.assertRedirects(response, reverse('github:setup'))
        connection = GitHubConnection.objects.get(user=self.user)
        self.assertEqual((connection.installation_id, connection.status), (42, GitHubConnection.Status.ACTIVE))
        self.assertEqual(sorted(connection.repositories.values_list('full_name', flat=True)), ['ada/app', 'ada/lib'])

    def test_missing_state_is_rejected(self, *mocks):
        self.callback(session_state=None)
        self.assertFalse(GitHubConnection.objects.exists())

    def test_wrong_state_is_rejected(self, *mocks):
        self.callback(state='forged-state')
        self.assertFalse(GitHubConnection.objects.exists())

    def test_state_is_single_use(self, *mocks):
        self.callback()
        self.assertNotIn(STATE_SESSION_KEY, self.client.session)

    def test_installation_the_user_cannot_access_is_rejected(self, exchange_code, user_installation_ids, *mocks):
        user_installation_ids.return_value = {7}
        self.callback()
        self.assertFalse(GitHubConnection.objects.exists())

    def test_installation_owned_by_another_user_is_rejected(self, *mocks):
        make_connection(make_user('bob@example.com'), 42)
        self.callback()
        self.assertFalse(GitHubConnection.objects.filter(user=self.user).exists())

    def test_org_approval_request_marks_connection_pending(self, exchange_code, *mocks):
        self.callback(setup_action='request', installation_id='')
        self.assertEqual(GitHubConnection.objects.get(user=self.user).status, GitHubConnection.Status.PENDING)
        exchange_code.assert_not_called()


class ConfigureIsolationTests(TestCase):
    def setUp(self):
        self.alice = make_user('alice@example.com')
        self.bob = make_user('bob@example.com')
        self.alice_conn = make_connection(self.alice, 1, repos=[(10, 'alice/secret')], configured=False)
        self.bob_conn = make_connection(self.bob, 2, repos=[(20, 'bob/app')], configured=False)
        self.alice_repo = self.alice_conn.repositories.get()
        self.bob_repo = self.bob_conn.repositories.get()
        self.client.force_login(self.bob)

    def test_other_users_repository_ids_are_ignored(self):
        self.client.post(reverse('github:configure'), {'scope': 'selected', 'repositories': [self.alice_repo.pk, self.bob_repo.pk]})
        self.alice_repo.refresh_from_db()
        self.bob_repo.refresh_from_db()
        self.assertFalse(self.alice_repo.monitored)
        self.assertTrue(self.bob_repo.monitored)

    def test_selecting_only_foreign_repositories_saves_nothing(self):
        self.client.post(reverse('github:configure'), {'scope': 'selected', 'repositories': [self.alice_repo.pk]})
        self.bob_conn.refresh_from_db()
        self.assertIsNone(self.bob_conn.configured_at)

    def test_overview_never_lists_another_users_repositories(self):
        self.client.post(reverse('github:configure'), {'scope': 'all'})
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'bob/app')
        self.assertNotContains(response, 'alice/secret')


@override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET)
class WebhookTests(TestCase):
    def setUp(self):
        self.connection = make_connection(make_user('ada@example.com'), 42, repos=[(10, 'ada/app'), (11, 'ada/lib')])
        self.connection.repositories.filter(github_id=10).update(monitored=True)

    def send(self, event, payload, delivery='delivery-1', secret=WEBHOOK_SECRET, signature=None):
        body = json.dumps(payload).encode()
        if signature is None:
            signature = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {'HTTP_X_GITHUB_EVENT': event, 'HTTP_X_GITHUB_DELIVERY': delivery}
        if signature:
            headers['HTTP_X_HUB_SIGNATURE_256'] = signature
        return self.client.post(reverse('github:webhook'), body, content_type='application/json', **headers)

    def pull_request(self, repo_id, action='opened'):
        return {
            'action': action, 'installation': {'id': 42},
            'repository': {'id': repo_id, 'full_name': 'ada/x', 'private': False},
            'pull_request': {'number': 7, 'title': 'Fix bug', 'user': {'login': 'ada'},
                             'html_url': 'https://github.com/ada/app/pull/7', 'head': {'sha': 'a' * 40}},
        }

    def test_valid_signature_on_monitored_repo_records_event(self):
        response = self.send('pull_request', self.pull_request(10))
        self.assertEqual(response.status_code, 200)
        event = PullRequestEvent.objects.get()
        self.assertEqual((event.repository.full_name, event.number, event.action), ('ada/app', 7, 'opened'))
        self.assertIsNotNone(event.repository.last_event_at)

    def test_invalid_signature_is_rejected(self):
        self.assertEqual(self.send('pull_request', self.pull_request(10), secret='wrong').status_code, 401)
        self.assertFalse(WebhookDelivery.objects.exists())

    def test_missing_signature_is_rejected(self):
        self.assertEqual(self.send('pull_request', self.pull_request(10), signature='').status_code, 401)

    def test_duplicate_delivery_is_processed_once(self):
        self.send('pull_request', self.pull_request(10))
        self.assertEqual(self.send('pull_request', self.pull_request(10)).status_code, 200)
        self.assertEqual(PullRequestEvent.objects.count(), 1)

    def test_unmonitored_repo_is_ignored(self):
        self.send('pull_request', self.pull_request(11))
        self.assertFalse(PullRequestEvent.objects.exists())

    def test_monitor_all_covers_every_repo(self):
        self.connection.monitor_all = True
        self.connection.save()
        self.send('pull_request', self.pull_request(11))
        self.assertEqual(PullRequestEvent.objects.get().repository.full_name, 'ada/lib')

    def test_closed_action_is_ignored(self):
        self.send('pull_request', self.pull_request(10, action='closed'))
        self.assertFalse(PullRequestEvent.objects.exists())

    def test_installation_deleted_needs_attention(self):
        self.send('installation', {'action': 'deleted', 'installation': {'id': 42}})
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, GitHubConnection.Status.NEEDS_ATTENTION)

    def test_url_without_trailing_slash_is_accepted(self):
        # smee-client strips the trailing slash; this used to be a 500 from APPEND_SLASH
        body = json.dumps(self.pull_request(10)).encode()
        signature = 'sha256=' + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        response = self.client.post('/github/webhook', body, content_type='application/json', HTTP_X_GITHUB_EVENT='pull_request',
                                    HTTP_X_GITHUB_DELIVERY='no-slash', HTTP_X_HUB_SIGNATURE_256=signature)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PullRequestEvent.objects.count(), 1)

    def test_installation_repositories_added_and_removed(self):
        self.send('installation_repositories', {
            'action': 'added', 'installation': {'id': 42},
            'repositories_added': [{'id': 12, 'full_name': 'ada/new', 'private': True}],
            'repositories_removed': [{'id': 11}],
        })
        self.assertEqual(sorted(self.connection.repositories.values_list('full_name', flat=True)), ['ada/app', 'ada/new'])


class RefreshTests(TestCase):
    def setUp(self):
        self.user = make_user('ada@example.com')
        self.connection = make_connection(self.user, 42, repos=[(10, 'ada/app')], monitor_all=True)
        self.client.force_login(self.user)

    @mock.patch.object(github_api, 'installation_repositories', return_value=[
        {'id': 10, 'full_name': 'ada/app', 'private': True}, {'id': 12, 'full_name': 'ada/new-repo', 'private': False},
    ])
    def test_refresh_picks_up_a_newly_created_repository(self, _):
        response = self.client.post(reverse('github:refresh'))
        self.assertRedirects(response, reverse('home'))
        self.assertEqual(sorted(self.connection.repositories.values_list('full_name', flat=True)), ['ada/app', 'ada/new-repo'])
        self.assertTrue(self.connection.repositories.get(github_id=12).is_monitored)

    @mock.patch.object(github_api, 'installation_repositories', side_effect=github_api.GitHubError('down', 502))
    def test_refresh_keeps_repositories_when_github_is_down(self, _):
        self.client.post(reverse('github:refresh'))
        self.assertEqual(self.connection.repositories.count(), 1)

    def test_refresh_only_goes_back_to_setup_never_elsewhere(self):
        with mock.patch.object(github_api, 'installation_repositories', return_value=[]):
            response = self.client.post(reverse('github:refresh'), {'next': 'https://evil.example/'})
        self.assertRedirects(response, reverse('home'))


class SecretsTests(TestCase):
    def test_app_jwt_is_signed_for_the_app(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        with tempfile.NamedTemporaryFile(suffix='.pem') as key_file:
            key_file.write(pem)
            key_file.flush()
            with override_settings(GITHUB_APP_ID='123', GITHUB_APP_PRIVATE_KEY_PATH=key_file.name):
                token = github_api.app_jwt()
        claims = jwt.decode(token, key.public_key(), algorithms=['RS256'])
        self.assertEqual(claims['iss'], '123')
        self.assertEqual(claims['exp'] - claims['iat'], 600)

    def test_no_model_stores_a_token_or_secret(self):
        for model in apps.get_app_config('github_integration').get_models():
            for field in model._meta.get_fields():
                self.assertNotRegex(field.name, 'token|secret|key', f'{model.__name__}.{field.name}')
