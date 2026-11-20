"""Review runs: turn a stored pull request event into a review posted on GitHub.

Module 2 posts a placeholder review; Module 3 replaces `review_body` with the AI agents' findings.
"""
import fnmatch
import posixpath
import random
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from github_integration import github_api
from github_integration.models import PullRequestEvent, WebhookDelivery
from github_integration.services import PULL_REQUEST_ACTIONS

from .models import AgentEvent, PullRequest, ReviewRun

Status = ReviewRun.Status
MARKER = '<!-- pragent-run:{} -->'  # lets a retry recognise a review it already posted
DONE = {Status.POSTED, Status.SUPERSEDED, Status.AWAITING_HUMAN}


def log(run, event_type, status='ok', latency_ms=0, **metadata):
    return AgentEvent.objects.create(run=run, event_type=event_type, status=status,
                                     latency_ms=latency_ms, metadata=metadata)


def _elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)


# ---- 1-3: claim a run -----------------------------------------------------------------------------

def start_run(delivery_id, redelivered=False):
    """Create or reuse the run for this delivery's commit and mark it running. None when there is nothing to do.

    redelivered: the broker re-sent this job because the worker holding it died, so a 'running' run
    belongs to that dead worker and is taken over instead of being left stuck.
    """
    delivery = WebhookDelivery.objects.filter(delivery_id=delivery_id).first()
    pr_event = PullRequestEvent.objects.filter(delivery=delivery).select_related('repository').first() if delivery else None
    if pr_event is None or pr_event.action not in PULL_REQUEST_ACTIONS:
        return None

    with transaction.atomic():
        pull_request = _upsert_pull_request(pr_event)
        run, _ = ReviewRun.objects.get_or_create(pull_request=pull_request, head_sha=pr_event.head_sha,
                                                 defaults={'delivery': delivery})
        run = ReviewRun.objects.select_for_update().get(pk=run.pk)  # one worker claims a run at a time
        if not _claimable(run, pull_request, redelivered):
            return None
        if pull_request.is_draft:
            _finish(run, Status.SKIPPED)
            log(run, 'run_skipped', reason='draft', action=pr_event.action)
            return None
        _supersede_older_runs(run)
        run.status, run.started_at, run.finished_at, run.error = Status.RUNNING, timezone.now(), None, ''
        run.save(update_fields=['status', 'started_at', 'finished_at', 'error'])
        log(run, 'run_started', action=pr_event.action, delivery_id=delivery_id, redelivered=redelivered)
    return run


def _upsert_pull_request(pr_event):
    # take PR details from the newest event, so a late or retried older event can't roll them back
    latest = PullRequestEvent.objects.filter(repository=pr_event.repository, number=pr_event.number) \
        .order_by('-received_at', '-id').first()
    pull_request, _ = PullRequest.objects.update_or_create(
        repository=latest.repository, number=latest.number,
        defaults={'title': latest.title, 'author_login': latest.author_login, 'base_ref': latest.base_ref,
                  'head_ref': latest.head_ref, 'head_sha': latest.head_sha, 'state': latest.state,
                  'is_draft': latest.is_draft, 'html_url': latest.html_url},
    )
    return pull_request


def _claimable(run, pull_request, redelivered):
    if run.status in DONE:
        return False
    if run.status == Status.SKIPPED:
        return not pull_request.is_draft  # skipped as a draft, now ready for review
    if run.status == Status.RUNNING:
        stale = run.started_at and run.started_at < timezone.now() - timedelta(seconds=settings.REVIEW_TASK_TIME_LIMIT)
        return redelivered or bool(stale)
    return True  # queued (first attempt or a scheduled retry) or failed


def _supersede_older_runs(run):
    older = run.pull_request.runs.select_for_update().filter(
        status__in=[Status.QUEUED, Status.RUNNING], created_at__lt=run.created_at).exclude(pk=run.pk)
    for old in older:
        _finish(old, Status.SUPERSEDED)
        log(old, 'run_superseded', by_run=run.pk, by_sha=run.head_sha)


def _finish(run, status, error=''):
    run.status, run.finished_at, run.error = status, timezone.now(), error
    run.save(update_fields=['status', 'finished_at', 'error'])


# ---- 4-7: review the commit -----------------------------------------------------------------------

def review(run):
    """Fetch the diff, apply limits, and post the review unless a newer commit has arrived."""
    pull_request = run.pull_request
    installation_id = pull_request.repository.connection.installation_id
    repo, number = pull_request.repository.full_name, pull_request.number

    started = time.monotonic()
    files = github_api.pull_request_files(installation_id, repo, number)
    reviewed, skipped = select_files(files)
    run.files_reviewed = [f['filename'] for f in reviewed]
    run.files_skipped = skipped
    run.additions = sum(f.get('additions', 0) for f in reviewed)
    run.deletions = sum(f.get('deletions', 0) for f in reviewed)
    run.save(update_fields=['files_reviewed', 'files_skipped', 'additions', 'deletions'])
    log(run, 'files_fetched', latency_ms=_elapsed_ms(started), total=len(files), reviewed=len(reviewed),
        skipped=len(skipped), additions=run.additions, deletions=run.deletions)
    for item in skipped:
        log(run, 'file_skipped', **item)

    started = time.monotonic()
    current_sha = github_api.pull_request(installation_id, repo, number)['head']['sha']
    run.refresh_from_db(fields=['status', 'github_review_id'])
    if run.status != Status.RUNNING:
        return run  # a newer run superseded this one meanwhile (and logged it)
    if current_sha != run.head_sha:
        _finish(run, Status.SUPERSEDED)
        log(run, 'run_superseded', latency_ms=_elapsed_ms(started), current_sha=current_sha, reason='head_moved')
        return run

    if run.github_review_id is None:
        started = time.monotonic()
        run.github_review_id = _existing_review_id(installation_id, repo, number, run)
        if run.github_review_id is None:
            run.github_review_id = github_api.create_review(installation_id, repo, number, run.head_sha, review_body(run))
            log(run, 'review_posted', latency_ms=_elapsed_ms(started), github_review_id=run.github_review_id)
        else:
            log(run, 'review_already_posted', latency_ms=_elapsed_ms(started), github_review_id=run.github_review_id)
        run.save(update_fields=['github_review_id'])
    _finish(run, Status.POSTED)
    log(run, 'run_finished', duration_ms=int((run.finished_at - run.started_at).total_seconds() * 1000))
    return run


def _existing_review_id(installation_id, repo, number, run):
    """A review this run already posted (e.g. the POST succeeded but its response was lost), so retries never double-post."""
    marker = MARKER.format(run.pk)
    for existing in github_api.pull_request_reviews(installation_id, repo, number):
        if marker in (existing.get('body') or ''):
            return existing['id']
    return None


def select_files(files):
    """Split GitHub's changed files into (reviewed, skipped) using skip patterns and size limits."""
    reviewed, skipped, changed_lines = [], [], 0
    for item in files:
        path = item['filename']
        reason = skip_reason(item)
        if reason is None and len(reviewed) >= settings.REVIEW_MAX_FILES:
            reason = 'over_file_limit'
        if reason is None and changed_lines + item.get('changes', 0) > settings.REVIEW_MAX_CHANGED_LINES:
            reason = 'over_line_limit'
        if reason:
            skipped.append({'path': path, 'reason': reason})
        else:
            reviewed.append(item)
            changed_lines += item.get('changes', 0)
    return reviewed, skipped


def skip_reason(item):
    path = item['filename']
    if item.get('status') == 'removed':
        return 'removed'
    name = posixpath.basename(path)
    for reason, patterns in settings.REVIEW_SKIP_PATTERNS.items():
        if any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern) for pattern in patterns):
            return reason
    if not item.get('patch'):
        return 'no_patch'  # binary, or too large for GitHub to include a diff
    return None


def review_body(run):
    """Placeholder review text. Module 3 replaces this with the agents' summary."""
    lines = [
        MARKER.format(run.pk),
        f'**pragent** reviewed commit `{run.head_sha[:7]}`.',
        '',
        f'- Files reviewed: {len(run.files_reviewed)} (+{run.additions} / -{run.deletions} lines)',
        f'- Files skipped: {len(run.files_skipped)}',
    ]
    for item in run.files_skipped[:20]:
        lines.append(f'  - `{item["path"]}`: {item["reason"].replace("_", " ")}')
    if len(run.files_skipped) > 20:
        lines.append(f'  - and {len(run.files_skipped) - 20} more')
    lines += ['', '_Automated review comments are coming soon. This is a placeholder review._']
    return '\n'.join(lines)


# ---- 8: retries and failure -----------------------------------------------------------------------

def backoff(retries, retry_after=None):
    """Exponential backoff with full jitter: up to 5s, 10s, 20s, 40s; GitHub's Retry-After wins if longer."""
    delay = random.uniform(1, 5 * 2 ** retries)
    return max(delay, retry_after or 0)


def schedule_retry(run, exc, countdown):
    _finish(run, Status.QUEUED, error=str(exc))
    run.finished_at = None
    run.save(update_fields=['finished_at'])
    log(run, 'run_retry_scheduled', status='retrying', error=str(exc), http_status=exc.status,
        countdown_s=round(countdown, 1))


def fail(run, exc):
    error = f'{exc.__class__.__name__}: {exc}'[:2000]
    _finish(run, Status.FAILED, error=error)
    log(run, 'run_failed', status='error', error=error, http_status=getattr(exc, 'status', None))
