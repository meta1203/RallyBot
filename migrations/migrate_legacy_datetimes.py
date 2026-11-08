"""One-shot migration: rewrite legacy jsonpickle-encoded datetime attributes.

The pre-PynamoDB era (before commit a83034e) stored MeetupEvent.datetime /
endtime (and any other datetime attr) as raw DynamoDB maps containing a
jsonpickle-serialized datetime ({"py/object": "datetime.datetime", ...}).
PynamoDB's UTCDateTimeAttribute can't read those, which crashed hourly jobs
with AttributeDeserializationError. aws.LegacyUTCDateTimeAttribute now reads
them transparently; this script rewrites them in place to the canonical
'%Y-%m-%dT%H:%M:%S.%f+0000' string form so the data matches the current
format.

Dry-run by default. Run with --apply using credentials that can write the
RallyBot table:

    python migrations/migrate_legacy_datetimes.py --apply
"""
import sys
import datetime as dt

import boto3

TABLE_NAME = "RallyBot"
REGION = "us-east-2"

ATTRS = ("datetime", "endtime", "created_at")


def _legacy_pickle_iso(raw: dict) -> str:
	"""Extract the ISO datetime string from a legacy jsonpickle-encoded
	datetime attribute value (raw DynamoDB type M, written by the
	pre-PynamoDB era). Shape:
	{"py/object": {"S": "datetime.datetime"},
	 "__reduce__": {"L": [<class map>, {"L": [{"S": "<iso>"}, <tz map>]}]}}
	Raises KeyError/IndexError/TypeError when the map doesn't match."""
	return raw["__reduce__"]["L"][1]["L"][0]["S"]


def convert(raw: dict):
	"""Legacy M-map -> canonical pynamodb UTC string, or None if unusable."""
	try:
		iso = _legacy_pickle_iso(raw)
	except (KeyError, IndexError, TypeError):
		return None
	try:
		parsed = dt.datetime.fromisoformat(iso)
	except ValueError:
		return None
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=dt.timezone.utc)
	return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+0000")


def main(apply: bool) -> int:
	client = boto3.client("dynamodb", region_name=REGION)
	kw = dict(TableName=TABLE_NAME)
	resp = client.scan(**kw)
	items = resp["Items"]
	while "LastEvaluatedKey" in resp:
		kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
		resp = client.scan(**kw)
		items += resp["Items"]

	changes = 0
	unreadable = 0
	for item in items:
		key = {"id": item["id"], "sort": item["sort"]}
		updates = {}
		for attr in ATTRS:
			raw = item.get(attr)
			if raw is None or "M" not in raw:
				continue
			new_val = convert(raw["M"])
			if new_val is None:
				unreadable += 1
				print(f"UNREADABLE {key} attribute {attr}: raw value not in the expected jsonpickle shape")
				continue
			updates[attr] = new_val
		if not updates:
			continue
		changes += 1
		print(f"{'APPLY' if apply else 'DRY   '} {key}: "
			+ ", ".join(f"{a} -> {v}" for a, v in updates.items()))
		if apply:
			client.update_item(
				TableName=TABLE_NAME,
				Key=key,
				UpdateExpression="SET " + ", ".join(f"#{a} = :{a}" for a in updates),
				ExpressionAttributeNames={f"#{a}": a for a in updates},
				ExpressionAttributeValues={f":{a}": {"S": v} for a, v in updates.items()},
			)

	mode = "APPLIED" if apply else "DRY RUN (nothing written; rerun with --apply)"
	print(f"\n{mode}: {changes} row(s) need rewriting, {unreadable} unreadable value(s)")
	return 1 if unreadable else 0


if __name__ == "__main__":
	sys.exit(main(apply="--apply" in sys.argv))
