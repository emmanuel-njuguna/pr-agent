from celery import shared_task
from django.conf import settings

from github_integration.github_api import GitHubError

from . import services

MAX_RETRIES = 4


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True, max_retries=MAX_RETRIES,
             soft_time_limit=settings.REVIEW_TASK_SOFT_TIME_LIMIT, time_limit=settings.REVIEW_TASK_TIME_LIMIT)
def process_pr_event(self, delivery_id):
    """Review the commit a stored pull request webhook points at. Safe to run more than once per delivery."""
    redelivered = bool((self.request.delivery_info or {}).get('redelivered'))
    run = services.start_run(delivery_id, redelivered=redelivered)
    if run is None:
        return None
    try:
        services.review(run)
    except GitHubError as exc:
        if exc.retryable and self.request.retries < MAX_RETRIES:
            countdown = services.backoff(self.request.retries, exc.retry_after)
            services.schedule_retry(run, exc, countdown)
            raise self.retry(exc=exc, countdown=countdown)
        services.fail(run, exc)
    except Exception as exc:  # includes SoftTimeLimitExceeded: never leave a run stuck in 'running'
        services.fail(run, exc)
        raise
    return run.pk
