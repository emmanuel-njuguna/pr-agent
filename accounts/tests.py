import re

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import User

EMAIL = 'ada@example.com'
PASSWORD = 'a-Strong-pass-123'


class EmailAuthTests(TestCase):
    def setUp(self):
        cache.clear()  # allauth rate-limits confirmation emails per address via the cache

    def signup(self):
        return self.client.post(reverse('account_signup'), {
            'email': EMAIL, 'password1': PASSWORD, 'password2': PASSWORD,
        })

    def login(self):
        return self.client.post(reverse('account_login'), {'login': EMAIL, 'password': PASSWORD})

    def test_signup_creates_unverified_user_and_sends_confirmation(self):
        self.signup()
        user = User.objects.get(email=EMAIL)
        self.assertFalse(user.emailaddress_set.get().verified)
        self.assertEqual(len(mail.outbox), 1)

    def test_login_blocked_until_email_confirmed(self):
        self.signup()
        self.client.logout()

        self.login()
        self.assertNotIn('_auth_user_id', self.client.session)

        confirm_url = re.search(r'https?://\S+/accounts/confirm-email/\S+', mail.outbox[0].body).group()
        self.client.post(confirm_url)
        self.client.logout()

        self.login()
        self.assertEqual(int(self.client.session['_auth_user_id']), User.objects.get(email=EMAIL).pk)


class AuthPageTests(TestCase):
    def test_login_page_renders_design_with_github_post_form(self):
        html = self.client.get(reverse('account_login')).content.decode()
        self.assertIn('Sign in to pragent', html)
        self.assertIn(f'action="{reverse("github_login")}?process=login"', html)
        self.assertIn('name="login"', html)

    def test_signup_page_renders_design(self):
        html = self.client.get(reverse('account_signup')).content.decode()
        self.assertIn('Create your account', html)
        for name in ('email', 'password1', 'password2'):
            self.assertIn(f'name="{name}"', html)

    def test_failed_login_shows_error_and_keeps_email(self):
        html = self.client.post(reverse('account_login'), {'login': EMAIL, 'password': 'wrong'}).content.decode()
        self.assertIn('class="alert-error"', html)
        self.assertIn(f'value="{EMAIL}"', html)


@override_settings(SOCIALACCOUNT_PROVIDERS={
    'github': {'SCOPE': ['user:email'], 'APPS': [{'client_id': 'test-id', 'secret': 'test-secret'}]},
})
class GitHubAuthTests(TestCase):
    def test_github_login_redirects_to_github(self):
        response = self.client.post(reverse('github_login'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('https://github.com/login/oauth/authorize'))
        self.assertIn('client_id=test-id', response['Location'])
