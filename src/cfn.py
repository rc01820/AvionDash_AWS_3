"""Minimal CloudFormation custom-resource plumbing (no cfnresponse dependency).

`run(event, context, create_or_update, delete)` always answers CloudFormation:
a custom resource that never responds leaves the stack hanging for an hour.
"""
import json
import logging
import urllib.request

log = logging.getLogger("avionpulse.cfn")


def respond(event, context, status, reason="", data=None, physical_id=None):
    body = json.dumps({
        "Status": status,
        "Reason": f"{reason} (log stream: {getattr(context, 'log_stream_name', '-')})"[:1000],
        "PhysicalResourceId": physical_id or event.get("PhysicalResourceId") or event["LogicalResourceId"],
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    }).encode()
    req = urllib.request.Request(event["ResponseURL"], data=body, method="PUT",
                                 headers={"content-type": "", "content-length": str(len(body))})
    urllib.request.urlopen(req, timeout=10)


def run(event, context, create_or_update, delete=None, physical_id=None):
    kind = event.get("RequestType")
    log.info("Custom resource %s %s", event.get("LogicalResourceId"), kind)
    try:
        props = event.get("ResourceProperties", {})
        if kind in ("Create", "Update"):
            reason, data = create_or_update(props)
        else:
            reason, data = (delete(props) if delete else ("nothing to do on delete", {}))
        respond(event, context, "SUCCESS", reason, data, physical_id)
    except Exception as e:  # noqa: BLE001 — report every failure back to CloudFormation
        log.exception("Custom resource failed")
        respond(event, context, "FAILED", f"{type(e).__name__}: {e}", physical_id=physical_id)
