from django.conf import settings
from django.db import models


class GitHubConnection(models.Model):
    """A user's GitHub App installation. Holds no tokens: they are minted on demand."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Waiting for organisation approval'
        ACTIVE = 'active', 'Active'
        NEEDS_ATTENTION = 'needs_attention', 'Needs attention'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='github_connection')
    # unique: one pragent user per installation
    installation_id = models.BigIntegerField(unique=True, null=True, blank=True)
    account_login = models.CharField(max_length=100, blank=True)
    account_type = models.CharField(max_length=20, blank=True)
    account_avatar_url = models.URLField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    monitor_all = models.BooleanField(default=True)
    configured_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.account_login or f'pending ({self.user})'

    @property
    def is_configured(self):
        return self.configured_at is not None and self.status != self.Status.PENDING

    @property
    def manage_url(self):
        if self.account_type == 'Organization':
            return f'https://github.com/organizations/{self.account_login}/settings/installations/{self.installation_id}'
        return f'https://github.com/settings/installations/{self.installation_id}'


class Repository(models.Model):
    connection = models.ForeignKey(GitHubConnection, on_delete=models.CASCADE, related_name='repositories')
    github_id = models.BigIntegerField()
    full_name = models.CharField(max_length=200)
    private = models.BooleanField(default=False)
    monitored = models.BooleanField(default=False)
    last_event_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'github_id'], name='unique_repo_per_connection')]
        ordering = ['full_name']
        verbose_name_plural = 'repositories'

    def __str__(self):
        return self.full_name

    @property
    def is_monitored(self):
        return self.connection.monitor_all or self.monitored


class WebhookDelivery(models.Model):
    delivery_id = models.CharField(max_length=64, unique=True)
    event = models.CharField(max_length=50)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = 'webhook deliveries'

    def __str__(self):
        return f'{self.event} {self.delivery_id}'


class PullRequestEvent(models.Model):
    """A pull request event on a monitored repository: the queue the review agent will read."""

    repository = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name='pull_request_events')
    delivery = models.OneToOneField(WebhookDelivery, on_delete=models.CASCADE)
    action = models.CharField(max_length=20)
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=300)
    author_login = models.CharField(max_length=100)
    html_url = models.URLField()
    head_sha = models.CharField(max_length=40)
    base_ref = models.CharField(max_length=255, blank=True)
    head_ref = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=20, blank=True)
    is_draft = models.BooleanField(default=False)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        return f'{self.repository} #{self.number} {self.action}'
