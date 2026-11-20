from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from github_integration.models import Repository, WebhookDelivery


class PullRequest(models.Model):
    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name='pull_requests')
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=300)
    author_login = models.CharField(max_length=100)
    base_ref = models.CharField(max_length=255)
    head_ref = models.CharField(max_length=255)
    head_sha = models.CharField(max_length=40)
    state = models.CharField(max_length=20)
    is_draft = models.BooleanField(default=False)
    html_url = models.URLField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['repository', 'number'], name='unique_pr_per_repo')]

    def __str__(self):
        return f'{self.repository} #{self.number}'


class ReviewRun(models.Model):
    """One review of one commit of a pull request."""

    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        RUNNING = 'running', 'Running'
        POSTED = 'posted', 'Posted'
        AWAITING_HUMAN = 'awaiting_human', 'Awaiting human approval'
        FAILED = 'failed', 'Failed'
        SUPERSEDED = 'superseded', 'Superseded by a newer commit'
        SKIPPED = 'skipped', 'Skipped'

    pull_request = models.ForeignKey(PullRequest, on_delete=models.CASCADE, related_name='runs')
    head_sha = models.CharField(max_length=40)
    delivery = models.ForeignKey(WebhookDelivery, on_delete=models.SET_NULL, null=True, blank=True, related_name='runs')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    files_reviewed = models.JSONField(default=list, blank=True)  # paths
    files_skipped = models.JSONField(default=list, blank=True)  # [{"path": ..., "reason": ...}]
    additions = models.PositiveIntegerField(default=0)
    deletions = models.PositiveIntegerField(default=0)
    total_cost = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    github_review_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['pull_request', 'head_sha'], name='unique_run_per_commit')]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.pull_request} @ {self.head_sha[:7]} ({self.status})'


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Agent events are append-only.')

    def delete(self):
        raise ValidationError('Agent events are append-only.')


class AgentEvent(models.Model):
    """Audit trail of everything a run did. Append-only here and, on Postgres, in the database too."""

    # no DB constraint and DO_NOTHING: deleting a run (e.g. on disconnect) leaves its audit trail in place
    run = models.ForeignKey(ReviewRun, on_delete=models.DO_NOTHING, db_constraint=False, related_name='events')
    timestamp = models.DateTimeField(auto_now_add=True)
    event_type = models.CharField(max_length=50)
    agent = models.CharField(max_length=50, null=True, blank=True)
    model = models.CharField(max_length=100, null=True, blank=True)
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    cost = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, default='ok')
    metadata = models.JSONField(default=dict, blank=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ['timestamp', 'id']

    def __str__(self):
        return f'{self.event_type} ({self.status})'

    def save(self, *args, **kwargs):
        if self.pk is not None or not self._state.adding:
            raise ValidationError('Agent events are append-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Agent events are append-only.')


class Finding(models.Model):
    """A review comment produced by an agent (filled in by Module 3)."""

    class AgentType(models.TextChoices):
        SECURITY = 'security'
        QUALITY = 'quality'
        TESTING = 'testing'
        DOCS = 'docs'

    class Severity(models.TextChoices):
        CRITICAL = 'critical'
        HIGH = 'high'
        MEDIUM = 'medium'
        LOW = 'low'
        INFO = 'info'

    class Status(models.TextChoices):
        PENDING = 'pending'
        POSTED = 'posted'
        PENDING_APPROVAL = 'pending_approval'
        REJECTED = 'rejected'
        DISPUTED = 'disputed'

    run = models.ForeignKey(ReviewRun, on_delete=models.CASCADE, related_name='findings')
    agent_type = models.CharField(max_length=20, choices=AgentType.choices)
    severity = models.CharField(max_length=20, choices=Severity.choices)
    category = models.CharField(max_length=100)
    file_path = models.CharField(max_length=500)
    line_start = models.PositiveIntegerField(null=True, blank=True)
    line_end = models.PositiveIntegerField(null=True, blank=True)
    title = models.CharField(max_length=300)
    message = models.TextField()
    rationale = models.TextField(blank=True)
    confidence = models.FloatField(validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    github_comment_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(confidence__gte=0, confidence__lte=1),
                                              name='finding_confidence_0_to_1')]

    def __str__(self):
        return f'[{self.severity}] {self.title}'
