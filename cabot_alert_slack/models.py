import json
import os
import StringIO

import requests

from cabot.cabotapp.alert import AlertPlugin
from cabot.cabotapp.models import GraphiteStatusCheck
from celery.utils.log import get_task_logger
from django.conf import settings
from django.template import Template, Context

logger = get_task_logger(__name__)

TEXT_TEMPLATE = "<{{ scheme }}://{{ host }}{% url 'service' pk=service.id %}|{{ service.name }}> {{ message }}"
URL_TEMPLATE = "{{ scheme }}://{{ host }}{% url 'result' pk=check.last_result.id %}"
MESSAGES_BY_STATUS = {
    "PASSING": "has returned to normal! :up:",
    "WARNING": "is reporting WARNING. :warning:",
    "ERROR": "is reporting ERROR. :negative_squared_cross_mark:",
    "CRITICAL": "is reporting CRITICAL errors! :skull::sos:",
}


class SlackAlert(AlertPlugin):
    name = "Slack"
    author = "Neil Williams"

    def send_alert(self, service, users, duty_officers):
        self._send_alert(service, acked=False)

    def send_alert_update(self, service, users, duty_officers):
        self._send_alert(service, acked=True)

    def _generate_graph_url(self, check):
        graph_buffer = self._get_graph(check)

        graph_url = self._slack_file_upload(check, graph_buffer)
        return graph_url

    def _slack_file_upload(self, check, graph_buffer):
        response = requests.post(
            "https://slack.com/api/files.upload",
            data={
                'token': os.environ["SLACK_TOKEN"],
            },
            files={'file': graph_buffer},
        ).json()

        if response['ok']:
            return response['file']['url_private']

    def _get_graph(self, check):
        targets = [
            check.metric,
            'alias(constantLine(%s),"Threshold")' % check.value,
        ]

        targets.extend(os.environ.get("SLACK_CUSTOM_TARGETS", []))

        payload = {
            'target': targets,
            'from': settings.GRAPHITE_FROM,
            'template': 'solarized-light',
            'width': 600,
        }

        response = requests.get(
            settings.GRAPHITE_API + 'render',
            params=payload,
            stream=True,
        )

        return StringIO.StringIO(response.content)

    def _send_alert(self, service, acked):
        overall_status = service.overall_status
        if not (acked and overall_status != "PASSING"):
            message = MESSAGES_BY_STATUS[overall_status]
        else:
            user = service.unexpired_acknowledgement().user
            name = user.first_name or user.username
            message = "is being worked on by {}. :hammer:".format(name)

        context = Context({
            "scheme": settings.WWW_SCHEME,
            "host": settings.WWW_HTTP_HOST,
            "service": service,
            "message": message,
        })
        text = Template(TEXT_TEMPLATE).render(context)

        attachments = []
        for check in service.all_failing_checks():
            if check.importance == "WARNING":
                color = "warning"
            else:
                color = "danger"

            check_context = Context({
                "scheme": settings.WWW_SCHEME,
                "host": settings.WWW_HTTP_HOST,
                "check": check,
            })
            url = Template(URL_TEMPLATE).render(check_context)

            attachment = {
                "fallback": "{}: {}: {}".format(check.name, check.importance, url),
                "title": check.name,
                "title_link": url,
                "text": check.last_result().error,
                "color": color,
            }
            attachments.append(attachment)

            if (isinstance(check, GraphiteStatusCheck) and
                    os.environ.get("SLACK_TOKEN", False)):
                try:
                    graph_url = self._generate_graph_url(check)
                    # TODO: this might need to be an .update to the [-1] element of attachments
                    #       or just add to "attachment" and then do the append after this if block
                    #       Also - slack seems to not like using internal Slack image links as the
                    #       image_url of a message attachment. Waiting to hear back.
                    attachments.append({
                        "image_url": graph_url,
                    })
                except:
                    logger.error("Couldn't fetch graph.")

        self._send_slack_webhook(text, attachments)

    def _send_slack_webhook(self, text, attachments):
        url = os.environ["SLACK_WEBHOOK_URL"]
        channel = os.environ["SLACK_ALERT_CHANNEL"]

        response = requests.post(url, data=json.dumps({
            "username": "Cabot",
            "icon_emoji": ":dog2:",
            "channel": channel,
            "text": text,
            "attachments": attachments,
        }))
        response.raise_for_status()
