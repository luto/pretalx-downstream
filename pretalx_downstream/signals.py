from datetime import timedelta

from django.dispatch import receiver
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import ugettext_lazy as _
from pretalx.common.signals import periodic_task
from pretalx.event.models import Event
from pretalx.orga.signals import nav_event_settings

from .tasks import task_refresh_upstream_schedule


@receiver(periodic_task)
def refresh_upstream_schedule(sender, request=None, **kwargs):
    for event in Event.objects.all():
        if event.settings.downstream_upstream_url:
            interval = event.settings.downstream_interval or 5
            try:
                interval = int(interval)
            except TypeError:
                interval = 5
            interval = timedelta(minutes=interval)
            last_pulled = event.settings.downstream_last_sync
            if not last_pulled or now() - last_pulled > interval:
                task_refresh_upstream_schedule.apply_async(kwargs={'event_slug': event.slug})


@receiver(nav_event_settings)
def register_upstream_settings(sender, request, **kwargs):
    if not request.user.has_perm('orga.change_settings', request.event):
        return []
    return [
        {
            'label': _('Upstream'),
            'url': reverse(
                'plugins:pretalx_downstream:settings',
                kwargs={'event': request.event.slug},
            ),
            'active': request.resolver_match.url_name
            == 'plugins:pretalx_downstream:settings',
        }
    ]
