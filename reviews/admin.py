from django.contrib import admin

from .models import AgentEvent, CodeChunk, Finding, PullRequest, ReviewRun

EVENT_FIELDS = ['timestamp', 'event_type', 'status', 'agent', 'model', 'tokens_in', 'tokens_out', 'cost',
                'latency_ms', 'metadata']


class ReadOnlyMixin:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class AgentEventInline(ReadOnlyMixin, admin.TabularInline):
    model = AgentEvent
    fields = readonly_fields = EVENT_FIELDS
    extra = 0


class FindingInline(admin.TabularInline):
    model = Finding
    fields = ['agent_type', 'severity', 'file_path', 'line_start', 'title', 'confidence', 'status']
    extra = 0


@admin.register(PullRequest)
class PullRequestAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'title', 'author_login', 'state', 'is_draft', 'head_sha', 'updated_at']
    list_filter = ['state', 'is_draft']
    search_fields = ['title', 'repository__full_name', 'author_login']


@admin.register(ReviewRun)
class ReviewRunAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'status', 'created_at', 'started_at', 'finished_at', 'github_review_id']
    list_filter = ['status']
    search_fields = ['head_sha', 'pull_request__repository__full_name']
    readonly_fields = ['created_at']
    inlines = [AgentEventInline, FindingInline]


@admin.register(AgentEvent)
class AgentEventAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ['timestamp', 'run', 'event_type', 'status', 'latency_ms']
    list_filter = ['event_type', 'status']
    readonly_fields = ['run'] + EVENT_FIELDS


@admin.register(Finding)
class FindingAdmin(admin.ModelAdmin):
    list_display = ['title', 'run', 'agent_type', 'severity', 'confidence', 'status']
    list_filter = ['agent_type', 'severity', 'status']


@admin.register(CodeChunk)
class CodeChunkAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'commit_sha', 'content_hash']
    search_fields = ['file_path', 'repository__full_name']
    exclude = ['embedding']  # thousands of floats: not useful on screen
