"""Data tier access: one DynamoDB table (single-table design).

Item layout (PK / SK):
  AIRPORT  / <IATA>            airport directory
  AIRCRAFT / <tail number>     fleet registry
  FLIGHT   / <flight number>   live flight state (rewritten by the simulator)
  USER     / <username>        demo users (pbkdf2 password hashes)
  EVENT    / <iso ts>#<id>     ops / maintenance events written by the worker (TTL 7d)
  CONFIG   / FAULTS | SECRET   chaos state and JWT signing secret
  CONFIG   / CLASSIC          on-demand Classic tier record (size, expiry, shared secret)
  INTEG    / <channel>        last-seen counters for stream / sns / bus / recorder / turnaround

The table is PROVISIONED at 5 RCU / 5 WCU so it sits inside the DynamoDB
always-free allowance and so the dynamodb_throttle fault produces real
ProvisionedThroughputExceededException errors.

With AVIONPULSE_LOCAL=1 an in-memory FakeStore with the same interface is used.
"""
import copy
import logging
import threading
import time
from decimal import Decimal

import config

log = logging.getLogger("avionpulse.store")


class SimulatedAwsError(Exception):
    """Local stand-in for botocore ClientError (same .response shape)."""

    def __init__(self, code, message):
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


def err_code(exc):
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return resp.get("Error", {}).get("Code") or type(exc).__name__
    return type(exc).__name__


def to_ddb(obj):
    """Floats -> Decimal (DynamoDB rejects float)."""
    if isinstance(obj, float):
        return Decimal(str(round(obj, 6)))
    if isinstance(obj, dict):
        return {k: to_ddb(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [to_ddb(v) for v in obj]
    return obj


def from_ddb(obj):
    """Decimal -> int/float for JSON responses."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_ddb(v) for v in obj]
    return obj


class DynamoStore:
    def __init__(self):
        import boto3
        from botocore.config import Config

        cfg = Config(
            retries={"mode": "standard", "max_attempts": 3},
            connect_timeout=3,
            read_timeout=5,
        )
        self._resource = boto3.resource("dynamodb", config=cfg)
        self.table = self._resource.Table(config.TABLE_NAME)
        self.client = self._resource.meta.client

    def query_pk(self, pk, limit=None, descending=False, consistent=False):
        from boto3.dynamodb.conditions import Key

        kwargs = {
            "KeyConditionExpression": Key("PK").eq(pk),
            "ScanIndexForward": not descending,
            "ConsistentRead": consistent,
        }
        if limit:
            kwargs["Limit"] = limit
        items = []
        while True:
            page = self.table.query(**kwargs)
            items.extend(page.get("Items", []))
            if limit and len(items) >= limit:
                return from_ddb(items[:limit])
            if "LastEvaluatedKey" not in page:
                return from_ddb(items)
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def scan_all(self, consistent=False):
        kwargs = {"ConsistentRead": consistent}
        items = []
        while True:
            page = self.table.scan(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return from_ddb(items)
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def get(self, pk, sk, consistent=False):
        resp = self.table.get_item(Key={"PK": pk, "SK": sk}, ConsistentRead=consistent)
        item = resp.get("Item")
        return from_ddb(item) if item else None

    def put(self, item):
        self.table.put_item(Item=to_ddb(item))

    def put_many(self, items, pace_s=0.2):
        """Paced writes: 5 WCU means ~5 small items/second sustained."""
        for item in items:
            self.put(item)
            if pace_s:
                time.sleep(pace_s)

    def update_attrs(self, pk, sk, attrs):
        names = {f"#a{i}": k for i, k in enumerate(attrs)}
        values = {f":v{i}": to_ddb(v) for i, v in enumerate(attrs.values())}
        expr = "SET " + ", ".join(f"#a{i} = :v{i}" for i in range(len(attrs)))
        self.table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def incr(self, pk, sk, field, n, attrs):
        names = {"#c": field, **{f"#a{i}": k for i, k in enumerate(attrs)}}
        values = {":n": n, **{f":v{i}": to_ddb(v) for i, v in enumerate(attrs.values())}}
        sets = ", ".join(f"#a{i} = :v{i}" for i in range(len(attrs)))
        self.table.update_item(Key={"PK": pk, "SK": sk},
                               UpdateExpression=f"ADD #c :n" + (f" SET {sets}" if sets else ""),
                               ExpressionAttributeNames=names, ExpressionAttributeValues=values)

    def probe_restricted_table(self):
        """iam_access_denied fault: the role has no rights on this table."""
        return self.client.describe_table(TableName=config.TABLE_NAME + "-restricted-ledger")


class FakeStore:
    """Thread-safe in-memory table for local development."""

    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()

    @staticmethod
    def _capacity(pk):
        """Mimic ProvisionedThroughputExceeded while dynamodb_capacity_cut is on.
        CONFIG items are exempt so the chaos switches themselves keep working."""
        if pk != "CONFIG":
            import infra
            if infra.local_capacity_throttled():
                raise SimulatedAwsError("ProvisionedThroughputExceededException",
                                        "The level of configured provisioned throughput for the table was exceeded.")

    def query_pk(self, pk, limit=None, descending=False, consistent=False):
        self._capacity(pk)
        with self._lock:
            rows = [copy.deepcopy(v) for (p, _), v in self._items.items() if p == pk]
        rows.sort(key=lambda r: r["SK"], reverse=descending)
        return rows[:limit] if limit else rows

    def scan_all(self, consistent=False):
        with self._lock:
            return [copy.deepcopy(v) for v in self._items.values()]

    def get(self, pk, sk, consistent=False):
        with self._lock:
            item = self._items.get((pk, sk))
            return copy.deepcopy(item) if item else None

    def put(self, item):
        self._capacity(item["PK"])
        with self._lock:
            self._items[(item["PK"], item["SK"])] = from_ddb(to_ddb(copy.deepcopy(item)))

    def put_many(self, items, pace_s=0.2):
        for item in items:
            self.put(item)

    def update_attrs(self, pk, sk, attrs):
        with self._lock:
            item = self._items.setdefault((pk, sk), {"PK": pk, "SK": sk})
            item.update(from_ddb(to_ddb(copy.deepcopy(attrs))))

    def incr(self, pk, sk, field, n, attrs):
        with self._lock:
            item = self._items.setdefault((pk, sk), {"PK": pk, "SK": sk})
            item[field] = item.get(field, 0) + n
            item.update(from_ddb(to_ddb(copy.deepcopy(attrs))))

    def probe_restricted_table(self):
        raise SimulatedAwsError(
            "AccessDeniedException",
            "User: arn:aws:sts::000000000000:assumed-role/avionpulse-api-role is not authorized "
            "to perform: dynamodb:DescribeTable on resource: "
            f"{config.TABLE_NAME}-restricted-ledger",
        )


_store = None
_store_lock = threading.Lock()


def get_store():
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = FakeStore() if config.LOCAL else DynamoStore()
    return _store
