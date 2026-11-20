import hashlib
import hmac
import io
import json
import time
import uuid
from datetime import timedelta
from unittest import mock

import requests
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from github_integration import github_api
from github_integration.models import GitHubConnection, PullRequestEvent, Repository, WebhookDelivery
from github_integration.tests import make_connection, make_user

from . import services
from .models import AgentEvent, Finding, PullRequest, ReviewRun
from .tasks import process_pr_event

SHA_A, SHA_B = 'a' * 40, 'b' * 40
WEBHOOK_SECRET = 'test-webhook-secret'
Status = ReviewRun.Status


def changed_file(path, changes=10, patch='@@ -1 +1 @@\n-a\n+b', status='modified'):
    item = {'filename': path, 'status': status, 'additions': changes // 2, 'deletions': changes - changes // 2,
            'changes': changes}
    if patch is not None:
        item['patch'] = patch
    return item


class FakeGitHub:
    """Stands in for the github_api functions the worker calls."""

    def __init__(self, head_sha=SHA_A, files=None):
        self.head_sha = head_sha
        self.files = files if files is not None else [changed_file('app.py')]
        self.reviews, self.posted = [], []

    def pull_request(self, installation_id, repo, number):
        return {'head': {'sha': self.head_sha}}

    def pull_request_files(self, installation_id, repo, number):
        return self.files

    def pull_request_reviews(self, installation_id, repo, number):
        return self.reviews

    def create_review(self, installation_id, repo, number, commit_id, body):
        review = {'id': 1000 + len(self.posted), 'body': body, 'commit_id': commit_id}
        self.posted.append(review)
        self.reviews.append(review)
        return review['id']


class WorkerTestCase(TestCase):
    fake_github = True  # replace the github_api functions with FakeGitHub

    def setUp(self):
        self.connection = make_connection(make_user('ada@example.com'), 42, repos=[(10, 'ada/app')], monitor_all=True)
        self.repo = self.connection.repositories.get()
        self.github = FakeGitHub()
        if not self.fake_github:
            return
        patcher = mock.patch.multiple(github_api, pull_request=self.github.pull_request,
                                      pull_request_files=self.github.pull_request_files,
                                      pull_request_reviews=self.github.pull_request_reviews,
                                      create_review=mock.Mock(side_effect=self.github.create_review))
        patcher.start()
        self.addCleanup(patcher.stop)

    def pr_event(self, action='opened', sha=SHA_A, draft=False, number=7):
        delivery = WebhookDelivery.objects.create(delivery_id=str(uuid.uuid4()), event='pull_request')
        PullRequestEvent.objects.create(
            repository=self.repo, delivery=delivery, action=action, number=number, title='Fix bug',
            author_login='ada', html_url=f'https://github.com/ada/app/pull/{number}', head_sha=sha,
            base_ref='main', head_ref='fix', state='open', is_draft=draft)
        return delivery.delivery_id

    def process(self, delivery_id):
        return process_pr_event.apply(args=[delivery_id])

    def events(self, run):
        return list(run.events.values_list('event_type', flat=True))


# ---- webhook -> queue -----------------------------------------------------------------------------

@override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET)
class WebhookQueueTests(TestCase):
    def setUp(self):
        self.connection = make_connection(make_user('ada@example.com'), 42, repos=[(10, 'ada/app'), (11, 'ada/lib')])
        self.connection.repositories.filter(github_id=10).update(monitored=True)

    def send(self, repo_id=10, action='opened', delivery='d-1', secret=WEBHOOK_SECRET):
        body = json.dumps({
            'action': action, 'installation': {'id': 42},
            'repository': {'id': repo_id, 'full_name': 'ada/x', 'private': False},
            'pull_request': {'number': 7, 'title': 'Fix', 'user': {'login': 'ada'}, 'draft': False, 'state': 'open',
                             'html_url': 'https://github.com/ada/app/pull/7', 'base': {'ref': 'main'},
                             'head': {'sha': SHA_A, 'ref': 'fix'}},
        }).encode()
        signature = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(reverse('github:webhook'), body, content_type='application/json',
                                HTTP_X_GITHUB_EVENT='pull_request', HTTP_X_GITHUB_DELIVERY=delivery,
                                HTTP_X_HUB_SIGNATURE_256=signature)

    @mock.patch.object(process_pr_event, 'delay')
    def test_queues_the_job_on_commit_and_returns_202(self, delay):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.send()
        self.assertEqual(response.status_code, 202)
        delay.assert_not_called()  # nothing is queued before the transaction commits
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        delay.assert_called_once_with('d-1')

    @mock.patch.object(process_pr_event, 'delay')
    def test_bad_signature_is_401_and_queues_nothing(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.send(secret='wrong').status_code, 401)
        delay.assert_not_called()

    @mock.patch.object(process_pr_event, 'delay')
    def test_repeated_delivery_is_queued_once(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            self.send()
            self.assertEqual(self.send().status_code, 202)
        delay.assert_called_once()

    @mock.patch.object(process_pr_event, 'delay')
    def test_unselected_repo_and_unhandled_action_queue_nothing(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            self.send(repo_id=11, delivery='d-unselected')
            self.send(action='closed', delivery='d-closed')
        delay.assert_not_called()


# ---- worker ---------------------------------------------------------------------------------------

class RunLifecycleTests(WorkerTestCase):
    def test_each_handled_action_creates_a_posted_run(self):
        for number, action in enumerate(['opened', 'synchronize', 'reopened', 'ready_for_review'], start=1):
            self.process(self.pr_event(action=action, number=number))
        self.assertEqual(set(ReviewRun.objects.values_list('status', flat=True)), {Status.POSTED})
        self.assertEqual(ReviewRun.objects.count(), 4)

    def test_posted_run_records_review_and_event_trail(self):
        self.process(self.pr_event())
        run = ReviewRun.objects.get()
        self.assertEqual((run.status, run.github_review_id, run.files_reviewed), (Status.POSTED, 1000, ['app.py']))
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(self.events(run), ['run_started', 'files_fetched', 'review_posted', 'run_finished'])
        review = self.github.posted[0]
        self.assertEqual(review['commit_id'], SHA_A)
        self.assertIn('Files reviewed: 1', review['body'])
        pull_request = PullRequest.objects.get()
        self.assertEqual((pull_request.base_ref, pull_request.head_ref, pull_request.head_sha), ('main', 'fix', SHA_A))

    def test_draft_is_skipped_without_calling_github(self):
        self.process(self.pr_event(draft=True))
        run = ReviewRun.objects.get()
        self.assertEqual(run.status, Status.SKIPPED)
        self.assertEqual(self.events(run), ['run_skipped'])
        github_api.create_review.assert_not_called()

    def test_draft_then_ready_for_review_gets_reviewed(self):
        self.process(self.pr_event(draft=True))
        self.process(self.pr_event(action='ready_for_review'))
        self.assertEqual(ReviewRun.objects.get().status, Status.POSTED)

    def test_unhandled_action_does_nothing(self):
        self.process(self.pr_event(action='closed'))
        self.assertFalse(ReviewRun.objects.exists())

    def test_same_head_sha_twice_gives_one_run_and_one_review(self):
        self.process(self.pr_event(action='opened'))
        self.process(self.pr_event(action='reopened'))
        self.assertEqual(ReviewRun.objects.count(), 1)
        self.assertEqual(len(self.github.posted), 1)

    def test_new_commit_supersedes_older_queued_or_running_run(self):
        pull_request = PullRequest.objects.create(repository=self.repo, number=7, title='t', author_login='ada',
                                                  base_ref='main', head_ref='fix', head_sha=SHA_A, state='open',
                                                  html_url='https://github.com/ada/app/pull/7')
        old = ReviewRun.objects.create(pull_request=pull_request, head_sha=SHA_A, status=Status.RUNNING,
                                       started_at=timezone.now())
        self.github.head_sha = SHA_B
        self.process(self.pr_event(action='synchronize', sha=SHA_B))
        old.refresh_from_db()
        self.assertEqual(old.status, Status.SUPERSEDED)
        self.assertEqual(self.events(old), ['run_superseded'])
        self.assertEqual(ReviewRun.objects.get(head_sha=SHA_B).status, Status.POSTED)

    def test_head_moved_during_processing_supersedes_and_posts_nothing(self):
        self.github.head_sha = SHA_B  # GitHub already has a newer commit
        self.process(self.pr_event(sha=SHA_A))
        run = ReviewRun.objects.get()
        self.assertEqual(run.status, Status.SUPERSEDED)
        self.assertIn('run_superseded', self.events(run))
        github_api.create_review.assert_not_called()

    def test_review_found_on_github_is_not_posted_again(self):
        # the POST succeeded but its response was lost: the retry must find the review, not post a second one
        def reviews_on_github(*args):
            return [{'id': 555, 'body': services.MARKER.format(ReviewRun.objects.get().pk) + '\nearlier attempt'}]

        with mock.patch.object(github_api, 'pull_request_reviews', side_effect=reviews_on_github):
            self.process(self.pr_event())
        run = ReviewRun.objects.get()
        self.assertEqual((run.status, run.github_review_id), (Status.POSTED, 555))
        github_api.create_review.assert_not_called()
        self.assertIn('review_already_posted', self.events(run))


class RecoveryTests(WorkerTestCase):
    def stuck_run(self, started_minutes_ago):
        delivery_id = self.pr_event()
        pull_request = services._upsert_pull_request(PullRequestEvent.objects.get(delivery__delivery_id=delivery_id))
        ReviewRun.objects.create(pull_request=pull_request, head_sha=SHA_A, status=Status.RUNNING,
                                 started_at=timezone.now() - timedelta(minutes=started_minutes_ago))
        return delivery_id

    def test_redelivered_job_takes_over_a_run_its_dead_worker_left_running(self):
        delivery_id = self.stuck_run(started_minutes_ago=0)
        run = services.start_run(delivery_id, redelivered=True)
        self.assertEqual(run.status, Status.RUNNING)

    def test_stale_running_run_is_taken_over(self):
        self.assertIsNotNone(services.start_run(self.stuck_run(started_minutes_ago=10)))

    def test_fresh_running_run_is_left_to_its_worker(self):
        self.assertIsNone(services.start_run(self.stuck_run(started_minutes_ago=0)))


class RetryTests(WorkerTestCase):
    def test_transient_error_is_retried_then_succeeds(self):
        calls = {'n': 0}

        def flaky(*args):
            calls['n'] += 1
            if calls['n'] == 1:
                raise github_api.GitHubError('GitHub returned 502', 502)
            return self.github.create_review(*args)

        github_api.create_review.side_effect = flaky
        self.process(self.pr_event())
        run = ReviewRun.objects.get()
        self.assertEqual(run.status, Status.POSTED)
        self.assertEqual(calls['n'], 2)
        self.assertIn('run_retry_scheduled', self.events(run))

    def test_secondary_rate_limit_is_retried(self):
        errors = iter([github_api.GitHubError('secondary rate limit', 403, rate_limited=True)])

        def limited(*args):
            error = next(errors, None)
            if error:
                raise error
            return self.github.pull_request_files(*args)

        with mock.patch.object(github_api, 'pull_request_files', side_effect=limited):
            self.process(self.pr_event())
        self.assertEqual(ReviewRun.objects.get().status, Status.POSTED)

    def test_our_own_4xx_is_not_retried_and_fails_the_run(self):
        github_api.create_review.side_effect = github_api.GitHubError('GitHub returned 422', 422)
        self.process(self.pr_event())
        run = ReviewRun.objects.get()
        self.assertEqual(run.status, Status.FAILED)
        self.assertIn('422', run.error)
        self.assertEqual(github_api.create_review.call_count, 1)
        self.assertEqual(self.events(run)[-1], 'run_failed')

    def test_retries_stop_after_four_and_fail_the_run(self):
        github_api.create_review.side_effect = github_api.GitHubError('GitHub returned 503', 503)
        self.process(self.pr_event())
        run = ReviewRun.objects.get()
        self.assertEqual(run.status, Status.FAILED)
        self.assertEqual(github_api.create_review.call_count, 5)  # first attempt + 4 retries
        self.assertEqual(self.events(run).count('run_retry_scheduled'), 4)

    def test_unexpected_error_fails_the_run_instead_of_leaving_it_running(self):
        github_api.create_review.side_effect = RuntimeError('bug')
        result = self.process(self.pr_event())
        self.assertTrue(result.failed())
        run = ReviewRun.objects.get()
        self.assertEqual(run.status, Status.FAILED)
        self.assertIn('RuntimeError: bug', run.error)

    def test_backoff_is_exponential_with_jitter_and_honours_retry_after(self):
        for retries in range(4):
            delays = {services.backoff(retries) for _ in range(20)}
            self.assertTrue(all(1 <= delay <= 5 * 2 ** retries for delay in delays))
            self.assertGreater(len(delays), 1)
        self.assertGreaterEqual(services.backoff(0, retry_after=60), 60)


@override_settings(REVIEW_MAX_FILES=2, REVIEW_MAX_CHANGED_LINES=100)
class FileSelectionTests(TestCase):
    def test_skip_patterns_each_have_a_reason(self):
        files = [
            changed_file('src/app.py'),
            changed_file('package-lock.json'),
            changed_file('static/app.min.js'),
            changed_file('api/service_pb2.py'),
            changed_file('vendor/lib/x.go'),
            changed_file('logo.png', patch=None),
            changed_file('data/huge.csv', patch=None),
            changed_file('old.py', status='removed'),
        ]
        reviewed, skipped = services.select_files(files)
        self.assertEqual([f['filename'] for f in reviewed], ['src/app.py'])
        self.assertEqual({s['path']: s['reason'] for s in skipped}, {
            'package-lock.json': 'lockfile', 'static/app.min.js': 'minified', 'api/service_pb2.py': 'generated',
            'vendor/lib/x.go': 'vendored', 'logo.png': 'binary', 'data/huge.csv': 'no_patch', 'old.py': 'removed',
        })

    def test_file_and_line_limits(self):
        reviewed, skipped = services.select_files([
            changed_file('a.py', changes=60), changed_file('b.py', changes=60), changed_file('c.py', changes=30),
            changed_file('d.py', changes=1),
        ])
        self.assertEqual([f['filename'] for f in reviewed], ['a.py', 'c.py'])
        self.assertEqual(skipped, [{'path': 'b.py', 'reason': 'over_line_limit'},
                                   {'path': 'd.py', 'reason': 'over_file_limit'}])


# ---- append-only events ---------------------------------------------------------------------------

class AgentEventAppendOnlyTests(WorkerTestCase):
    def setUp(self):
        super().setUp()
        self.process(self.pr_event())
        self.run_ = ReviewRun.objects.get()
        self.event = self.run_.events.first()

    def test_python_rejects_update_and_delete(self):
        self.event.status = 'tampered'
        for attempt in (self.event.save, self.event.delete, lambda: AgentEvent.objects.update(status='x'),
                        lambda: AgentEvent.objects.all().delete()):
            with self.assertRaises(ValidationError):
                attempt()

    def test_database_rejects_update_delete_and_truncate(self):
        for sql in ('UPDATE reviews_agentevent SET status = %s', 'DELETE FROM reviews_agentevent',
                    'TRUNCATE reviews_agentevent'):
            with self.subTest(sql=sql), self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(sql, ['x'] if '%s' in sql else None)

    def test_disconnecting_keeps_the_audit_trail(self):
        count = AgentEvent.objects.count()
        GitHubConnection.objects.all().delete()
        self.assertFalse(ReviewRun.objects.exists())
        self.assertEqual(AgentEvent.objects.count(), count)


class FindingTests(WorkerTestCase):
    def test_confidence_must_be_between_0_and_1(self):
        self.process(self.pr_event())
        finding = Finding(run=ReviewRun.objects.get(), agent_type='security', severity='high', category='x',
                          file_path='a.py', title='t', message='m', confidence=1.5)
        with self.assertRaises(ValidationError):
            finding.full_clean()
        with self.assertRaises(DatabaseError), transaction.atomic():
            finding.save()


# ---- token cache ----------------------------------------------------------------------------------

def token_response(token, expires_in):
    expires_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + expires_in))
    response = mock.Mock(status_code=201)
    response.json.return_value = {'token': token, 'expires_at': expires_at}
    return response


@mock.patch.object(github_api, 'app_jwt', return_value='app-jwt')
class TokenCacheTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_token_is_reused_within_its_lifetime(self, _):
        with mock.patch.object(github_api, '_request', return_value=token_response('ghs_one', 3600)) as request:
            self.assertEqual(github_api.installation_token(42), 'ghs_one')
            self.assertEqual(github_api.installation_token(42), 'ghs_one')
        self.assertEqual(request.call_count, 1)

    def test_cache_expires_five_minutes_before_github(self, _):
        with mock.patch.object(github_api, '_request', return_value=token_response('ghs_one', 3600)), \
                mock.patch.object(github_api.cache, 'set') as cache_set:
            github_api.installation_token(42)
        timeout = cache_set.call_args.kwargs['timeout']
        self.assertTrue(3600 - 300 - 5 <= timeout <= 3600 - 300)

    def test_token_near_expiry_is_refreshed(self, _):
        responses = [token_response('ghs_old', 120), token_response('ghs_new', 3600)]
        with mock.patch.object(github_api, '_request', side_effect=responses):
            self.assertEqual(github_api.installation_token(42), 'ghs_old')
            self.assertEqual(github_api.installation_token(42), 'ghs_new')

    def test_401_clears_the_cached_token_and_retries_once(self, _):
        cache.set(github_api._token_cache_key(42), 'ghs_revoked', 3000)
        calls = []

        def fake_request(method, url, token=None, **kwargs):
            calls.append((url, token))
            if url.endswith('/access_tokens'):
                return token_response('ghs_fresh', 3600)
            if token == 'ghs_revoked':
                raise github_api.GitHubError('GitHub returned 401', 401)
            return mock.Mock(json=lambda: {'head': {'sha': SHA_A}})

        with mock.patch.object(github_api, '_request', side_effect=fake_request):
            self.assertEqual(github_api.pull_request(42, 'ada/app', 7)['head']['sha'], SHA_A)
        self.assertEqual([token for _, token in calls], ['ghs_revoked', 'app-jwt', 'ghs_fresh'])
        self.assertEqual(cache.get(github_api._token_cache_key(42)), 'ghs_fresh')


class FakeHTTPResponse:
    def __init__(self, status_code, data, headers=None):
        self.status_code, self._data, self.headers, self.links = status_code, data, headers or {}, {}

    def json(self):
        return self._data


@override_settings(GITHUB_APP_ID='1')
class EndToEndHTTPTests(WorkerTestCase):
    """The whole worker against a fake GitHub at the HTTP level: proves tokens never reach the DB or events."""

    fake_github = False  # the real github_api functions, with HTTP faked underneath

    def setUp(self):
        super().setUp()
        cache.clear()
        patcher = mock.patch.object(github_api, 'app_jwt', return_value='app-jwt-secret')
        patcher.start()
        self.addCleanup(patcher.stop)

    def respond(self, method, url, headers=None, **kwargs):
        if url.endswith('/access_tokens'):
            return FakeHTTPResponse(201, {'token': 'ghs_supersecret', 'expires_at': '2999-01-01T00:00:00Z'})
        if '/files' in url:
            return FakeHTTPResponse(200, [changed_file('app.py'), changed_file('yarn.lock')])
        if method == 'GET' and url.endswith('/reviews?per_page=100'):
            return FakeHTTPResponse(200, [])
        if method == 'POST' and url.endswith('/reviews'):
            return FakeHTTPResponse(200, {'id': 77})
        if '/pulls/7' in url:
            return FakeHTTPResponse(200, {'head': {'sha': SHA_A}})
        raise AssertionError(f'unexpected call {method} {url}')

    def test_full_run_never_stores_tokens(self):
        with mock.patch.object(requests, 'request', side_effect=self.respond) as http:
            self.process(self.pr_event())
        run = ReviewRun.objects.get()
        self.assertEqual((run.status, run.github_review_id), (Status.POSTED, 77))
        self.assertEqual(run.files_skipped, [{'path': 'yarn.lock', 'reason': 'lockfile'}])
        self.assertTrue(all(call.kwargs['timeout'] for call in http.call_args_list))
        stored = json.dumps(list(AgentEvent.objects.values()), default=str) + json.dumps(list(ReviewRun.objects.values()), default=str)
        for secret in ('ghs_supersecret', 'app-jwt-secret'):
            self.assertNotIn(secret, stored)


class OverviewRunStatusTests(WorkerTestCase):
    def test_last_pull_request_column_shows_the_latest_run(self):
        self.process(self.pr_event())
        self.client.force_login(self.connection.user)
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'href="https://github.com/ada/app/pull/7"')
        self.assertContains(response, 'Posted')


class MigrationsTests(TestCase):
    def test_models_and_migrations_agree(self):
        # e.g. changing EMBEDDING_DIMENSIONS without a migration fails here
        call_command('makemigrations', '--check', '--dry-run', stdout=io.StringIO())
