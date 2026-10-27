"""Custom metrics and logging.

When the Datadog Lambda layer is attached, metrics go out through the Datadog
Lambda Extension (no CloudWatch custom-metric charges). Without it, each metric
is written as a structured log line so a Datadog log-based metric (or a quick
CloudWatch Logs Insights query) can still pick it up.
"""
import logging
import os
import sys

import config

_lambda_metric = None
_resolved = False


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if config.LOCAL and not root.handlers:
        logging.basicConfig(
            stream=sys.stdout,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    # boto noise isn't useful in a demo
    for noisy in ("botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _resolve():
    global _lambda_metric, _resolved
    if not _resolved:
        _resolved = True
        if not config.LOCAL and os.environ.get("DD_LAMBDA_HANDLER"):
            try:
                from datadog_lambda.metric import lambda_metric

                _lambda_metric = lambda_metric
            except ImportError:
                _lambda_metric = None
    return _lambda_metric


_mlog = logging.getLogger("avionpulse.metric")


def metric(name, value, tags=None):
    tags = list(tags or []) + [f"env:{config.APP_ENV}"]
    fn = _resolve()
    if fn:
        fn(name, value, tags=tags)
    else:
        _mlog.info("metric %s=%s %s", name, value, ",".join(tags),
                   extra={"metric_name": name, "metric_value": value, "metric_tags": tags})


# ── Datadog events (chaos toggles, Classic tier launch/teardown) ──────────
_elog = logging.getLogger("avionpulse.event")
_api_key = {"value": None, "tried": False}


def _dd_api_key():
    if not _api_key["tried"]:
        _api_key["tried"] = True
        try:
            import boto3

            _api_key["value"] = boto3.client("ssm").get_parameter(
                Name=config.DD_API_KEY_SSM_NAME, WithDecryption=True)["Parameter"]["Value"]
        except Exception as e:  # noqa: BLE001 — events are best effort
            _elog.warning("Datadog API key unavailable for events: %s", e)
    return _api_key["value"]


def event(title, text, tags=None, alert_type="info"):
    """Log the event and, when Datadog is enabled, post it to the Events API so
    it shows up as an overlay on dashboards and in Event Management."""
    tags = list(tags or []) + ["app:avionpulse", f"env:{config.APP_ENV}", "source:avionpulse-chaos"]
    _elog.warning("[EVENT] %s — %s", title, text, extra={"event_title": title, "event_tags": tags})
    if not config.DATADOG_ENABLED or not config.DD_API_KEY_SSM_NAME:
        return
    key = _dd_api_key()
    if not key:
        return
    import json
    import urllib.request

    body = json.dumps({"title": title, "text": text, "tags": tags, "alert_type": alert_type,
                       "source_type_name": "amazon web services"}).encode()
    req = urllib.request.Request(f"https://api.{config.DD_SITE}/api/v1/events", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "DD-API-KEY": key})
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception as e:  # noqa: BLE001
        _elog.warning("Datadog event post failed: %s", e)
