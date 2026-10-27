"""Runtime configuration, read once from the Lambda environment.

Everything has a local default so the same code runs under scripts/local_server.py
(AVIONPULSE_LOCAL=1) with an in-memory data store and simulated AWS calls.
"""
import os

LOCAL = os.environ.get("AVIONPULSE_LOCAL") == "1"

STACK = os.environ.get("STACK_NAME", "avionpulse")
REGION = os.environ.get("AWS_REGION", "local")
ACCOUNT = os.environ.get("ACCOUNT_ID", "000000000000")

TABLE_NAME = os.environ.get("TABLE_NAME", "avionpulse-local-data")
QUEUE_URL = os.environ.get("QUEUE_URL", "local://events")
DLQ_URL = os.environ.get("DLQ_URL", "local://events-dlq")
BUCKET = os.environ.get("BUCKET", "local-bucket")

API_FN = os.environ.get("API_FN", "avionpulse-api")
WORKER_FN = os.environ.get("WORKER_FN", "avionpulse-worker")
SIM_FN = os.environ.get("SIM_FN", "avionpulse-simulator")
PROCESSOR_FN = os.environ.get("PROCESSOR_FN", "avionpulse-processor")

# ── v3 serverless resources ───────────────────────────────────────────────
API_ID = os.environ.get("API_ID", "")
API_INTEGRATION_ID = os.environ.get("API_INTEGRATION_ID", "local-integration")
SIM_RULE = os.environ.get("SIM_RULE", "avionpulse-simulator-tick")
BUS_NAME = os.environ.get("BUS_NAME", "avionpulse-flight-ops")
BUS_RULE = os.environ.get("BUS_RULE", "avionpulse-flight-status")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "arn:aws:sns:local:000000000000:avionpulse-crew-alerts")
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "arn:aws:states:local:000000000000:stateMachine:avionpulse-turnaround")
RECORDER_BUCKET = os.environ.get("RECORDER_BUCKET", "local-recorder")
FLAGS_PARAM = os.environ.get("FLAGS_PARAM", "/avionpulse/config/flags")
KMS_KEY_ID = os.environ.get("KMS_KEY_ID", "local-key")
APP_ROLE_ARN = os.environ.get("APP_ROLE_ARN", "arn:aws:iam::000000000000:role/avionpulse-app")
WORKER_ROLE = os.environ.get("WORKER_ROLE", "avionpulse-worker-role")
WORKER_DATA_POLICY_ARN = os.environ.get("WORKER_DATA_POLICY_ARN",
                                        "arn:aws:iam::000000000000:policy/avionpulse-worker-data")

# Baselines the chaos engine restores (they mirror the template).
DEFAULT_RCU = int(os.environ.get("DEFAULT_RCU", "5"))
DEFAULT_WCU = int(os.environ.get("DEFAULT_WCU", "5"))
QUEUE_RETENTION_S = int(os.environ.get("QUEUE_RETENTION_S", "86400"))
DEFAULT_FLAGS = {"board_page_size": 100, "weather_enabled": True, "maintenance_intake": True}

# ── Classic tier (on-demand second stack) ─────────────────────────────────
CLASSIC_STACK = os.environ.get("CLASSIC_STACK", STACK + "-classic")
CLASSIC_CFN_ROLE_ARN = os.environ.get("CLASSIC_CFN_ROLE_ARN", "")
CLASSIC_INSTANCE_PROFILE_ARN = os.environ.get("CLASSIC_INSTANCE_PROFILE_ARN", "")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
CODE_BUCKET = os.environ.get("CODE_BUCKET", "")
CODE_KEY = os.environ.get("CODE_KEY", "")
CLASSIC_MAX_HOURS = 8
FIS_TEMPLATES = {
    "fis_cpu_stress": os.environ.get("FIS_CPU_TEMPLATE", "EXTlocalcpu"),
    "fis_network_latency": os.environ.get("FIS_LATENCY_TEMPLATE", "EXTlocallatency"),
    "fis_instance_terminate": os.environ.get("FIS_TERMINATE_TEMPLATE", "EXTlocalterminate"),
    "fis_az_outage": os.environ.get("FIS_AZ_TEMPLATE", "EXTlocalaz"),
}

APP_ENV = os.environ.get("APP_ENV", "local")
APP_VERSION = os.environ.get("APP_VERSION", "3.0.0")
DD_SITE = os.environ.get("DD_SITE", "datadoghq.com")
DD_API_KEY_SSM_NAME = os.environ.get("DD_API_KEY_SSM_NAME", "")
DATADOG_ENABLED = bool(os.environ.get("DD_LAMBDA_HANDLER")) and not LOCAL

# API Gateway default-route throttle set by the template. The api_throttling
# fault drops the stage to 1 rps and restores these values when cleared.
DEFAULT_RATE = float(os.environ.get("DEFAULT_RATE", "20"))
DEFAULT_BURST = int(os.environ.get("DEFAULT_BURST", "40"))

# Every fault auto-reverts after this many minutes unless the operator picks
# another TTL. Keeps a forgotten chaos experiment from running up a bill.
FAULT_TTL_DEFAULT_MIN = int(os.environ.get("FAULT_TTL_MIN", "30"))
FAULT_TTL_MAX_MIN = 120

JWT_FALLBACK_SECRET = os.environ.get("JWT_SECRET", "avionpulse-local-secret")
TOKEN_TTL_S = 8 * 3600

# Local dev only: where the web/ folder lives (for the NOTAM "S3" object).
LOCAL_WEB_ROOT = os.environ.get(
    "LOCAL_WEB_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web"),
)


def fn_arn(name):
    return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{name}"
