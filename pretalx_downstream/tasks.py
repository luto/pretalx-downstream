import hashlib
import json
import xml.etree.ElementTree as ET
from contextlib import suppress
from datetime import timedelta

import requests
from dateutil.parser import parse
from django.db import transaction
from django.utils.timezone import now
from django.utils.translation import ugettext_lazy as _
from pretalx.celery_app import app
from pretalx.event.models import Event
from pretalx.person.models import SpeakerProfile, User
from pretalx.schedule.models import Room, TalkSlot
from pretalx.submission.models import Submission, SubmissionType, Track

from .models import UpstreamResult


@app.task()
def task_refresh_upstream_schedule(event_slug):
    event = Event.objects.get(slug__iexact=event_slug)
    url = event.settings.downstream_upstream_url
    if not url:
        raise Exception(_('No upstream URL was configured.'))

    response = requests.get(url)
    if response.status_code != 200:
        raise Exception(
            _('Could not retrieve schedule, received {} response.').format(
                response.status_code
            )
        )

    content = response.content.decode()
    last_result = event.upstream_results.order_by('timestamp').first()
    m = hashlib.sha256()
    m.update(response.content)
    if last_result and m == last_result.checksum:
        event.settings.upstream_last_sync = now()
        return

    root = ET.fromstring(content)
    schedule_version = root.find('version').text
    release_new_version = (
        not event.current_schedule or schedule_version != event.current_schedule.version
    )
    changes, schedule = process_frab(
        root, event, release_new_version=release_new_version
    )
    UpstreamResult.objects.create(
        event=event, schedule=schedule, changes=json.dumps(changes), content=content
    )


@transaction.atomic()
def process_frab(root, event, release_new_version):
    """Take an xml document root and an event, and releases a schedule with the data
    from the xml document. Copied directly from pretalx.schedule.utils.process_frab"""

    changes = dict()
    for day in root.findall('day'):
        for rm in day.findall('room'):
            room, _ = Room.objects.get_or_create(event=event, name=rm.attrib['name'])
            for talk in rm.findall('event'):
                changes.update(_create_talk(talk=talk, room=room, event=event))

    schedule = None
    if release_new_version:
        schedule_version = root.find('version').text
        try:
            event.wip_schedule.freeze(schedule_version, notify_speakers=False)
            schedule = event.schedules.get(version=schedule_version)
        except Exception:
            raise Exception(
                f'Could not import "{event.name}" schedule version "{schedule_version}": failed creating schedule release.'
            )

        schedule.talks.update(is_visible=True)
        start = schedule.talks.order_by('start').first().start
        end = schedule.talks.order_by('-end').first().end
        event.date_from = start.date()
        event.date_to = end.date()
        event.save()
    return changes, schedule


def _create_talk(*, talk, room, event):
    changes = dict()
    date = talk.find('date').text
    start = parse(date + ' ' + talk.find('start').text)
    hours, minutes = talk.find('duration').text.split(':')
    duration = timedelta(hours=int(hours), minutes=int(minutes))
    duration_in_minutes = duration.total_seconds() / 60
    try:
        end = parse(date + ' ' + talk.find('end').text)
    except AttributeError:
        end = start + duration
    sub_type = SubmissionType.objects.filter(
        event=event, name=talk.find('type').text, default_duration=duration_in_minutes
    ).first()

    if not sub_type:
        sub_type = SubmissionType.objects.create(
            name=talk.find('type').text or 'default',
            event=event,
            default_duration=duration_in_minutes,
        )

    tracks = Track.objects.filter(
        event=event, name__icontains=talk.find('track').text,
    )
    track = [t for t in tracks if str(t.name) == talk.find('track').text]

    if not track:
        track = Track.objects.create(
            name=talk.find('track').text or 'default',
            event=event,
        )
    else:
        track = track[0]

    optout = False
    with suppress(AttributeError):
        optout = talk.find('recording').find('optout').text == 'true'

    code = None
    if (
        Submission.objects.filter(code__iexact=talk.attrib['id'], event=event).exists()
        or not Submission.objects.filter(code__iexact=talk.attrib['id']).exists()
    ):
        code = talk.attrib['id']
    elif (
        Submission.objects.filter(
            code__iexact=talk.attrib['guid'][:16], event=event
        ).exists()
        or not Submission.objects.filter(code__iexact=talk.attrib['guid'][:16]).exists()
    ):
        code = talk.attrib['guid'][:16]

    sub, created = Submission.objects.get_or_create(
        event=event, code=code, defaults={'submission_type': sub_type}
    )
    sub.submission_type = sub_type
    sub.track = track

    change_tracking_data = {
        'title': talk.find('title').text,
        'description': talk.find('description').text,
        'abstract': talk.find('abstract').text,
        'content_locale': talk.find('language').text or 'en',
        'do_not_record': optout,
    }
    if talk.find('subtitle').text:
        change_tracking_data['description'] = (
            talk.find('subtitle').text
            + '\n'
            + (change_tracking_data['description'] or '')
        )

    for key, value in change_tracking_data.items():
        if not getattr(sub, key) == value:
            changes[key] = {'old': getattr(sub, key), 'new': value}
            setattr(sub, key, value)

    sub.save()

    for person in talk.find('persons').findall('person'):
        user = User.objects.filter(name=person.text[:60]).first()
        if not user:
            user = User(name=person.text, email=f'{person.text}@localhost')
            user.save()
            SpeakerProfile.objects.create(user=user, event=event)
        sub.speakers.add(user)

    slot, _ = TalkSlot.objects.get_or_create(
            submission=sub, schedule=event.wip_schedule, defaults={'is_visible': True}
    )
    slot.room = room
    slot.is_visible = True
    slot.start = start
    slot.end = end
    slot.save()
    if not created and changes:
        return {sub.code: changes}
    return dict()
