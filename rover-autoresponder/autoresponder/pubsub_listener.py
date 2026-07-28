"""Cloud Pub/Sub streaming-pull listener for Gmail push notifications.

Each Gmail push publishes JSON {"emailAddress": ..., "historyId": ...}.
We hold a streaming pull subscription, so no public webhook is needed.
"""
import json
import logging

from google.cloud import pubsub_v1

from . import config

log = logging.getLogger(__name__)


def listen(on_notification):
    """Block, invoking on_notification(email_address, history_id) per message."""
    subscriber = pubsub_v1.SubscriberClient()
    sub_path = config.subscription_path()

    # Process one message at a time: the Gmail API service object (httplib2) and
    # the SQLite connection are shared, so we serialize rather than run callbacks
    # concurrently across the subscriber's worker threads.
    flow_control = pubsub_v1.types.FlowControl(max_messages=1)

    def callback(message):
        try:
            data = json.loads(message.data.decode("utf-8"))
            on_notification(data.get("emailAddress"), str(data.get("historyId")))
            message.ack()
        except Exception:
            log.exception("pubsub message handling failed; nacking for redelivery")
            message.nack()

    future = subscriber.subscribe(sub_path, callback=callback, flow_control=flow_control)
    log.info("listening on %s", sub_path)
    try:
        future.result()
    except KeyboardInterrupt:
        future.cancel()
        future.result()
