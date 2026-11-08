import datetime as dt
import boto3
from pynamodb.models import Model
from pynamodb.attributes import UnicodeAttribute, NumberAttribute, UTCDateTimeAttribute
from pynamodb.exceptions import AttributeDeserializationError

TABLE_NAME = "RallyBot"
REGION = "us-east-2"

boto3.setup_default_session(region_name=REGION)


def _legacy_pickle_iso(raw: dict) -> str | None:
	"""Extract the ISO datetime string from a legacy jsonpickle-encoded
	datetime attribute value (raw DynamoDB type M, written by the
	pre-PynamoDB era, see commit a83034e). Shape:
	{"py/object": {"S": "datetime.datetime"},
	 "__reduce__": {"L": [<class map>, {"L": [{"S": "<iso>"}, <tz map>]}]}}
	Returns None when the map doesn't match that shape."""
	try:
		return raw["__reduce__"]["L"][1]["L"][0]["S"]
	except (KeyError, IndexError, TypeError):
		return None


class LegacyUTCDateTimeAttribute(UTCDateTimeAttribute):
	"""UTCDateTimeAttribute that also reads legacy rows whose datetime
	values were stored as jsonpickle-encoded maps (raw type M) instead of
	strings. PynamoDB raises AttributeDeserializationError on those at
	get_value(); this subclass converts them to the canonical
	'%Y-%m-%dT%H:%M:%S.%f+0000' string form before deserializing, so old
	rows deserialize instead of killing every scan/query that touches them."""

	def get_value(self, value):
		if "M" in value:
			iso = _legacy_pickle_iso(value["M"])
			if iso is None:
				raise AttributeDeserializationError(self.attr_name, self.attr_type)
			try:
				parsed = dt.datetime.fromisoformat(iso)
			except ValueError:
				raise AttributeDeserializationError(self.attr_name, self.attr_type)
			if parsed.tzinfo is None:
				parsed = parsed.replace(tzinfo=dt.timezone.utc)
			return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+0000")
		return super().get_value(value)

class RallyBotModel(Model):
	class Meta:
		table_name = TABLE_NAME
		region = REGION

	id = UnicodeAttribute(hash_key=True)
	sort = NumberAttribute(range_key=True)
	data = UnicodeAttribute(null=True)

class DynamoDBClient:
	def __init__(self):
		self._dynamodb = boto3.resource('dynamodb')
		self._table = self._dynamodb.Table(TABLE_NAME)

	def read_raw(self, id, sort) -> dict | None:
		raw_value = self._table.get_item(Key={'id': id, 'sort': sort})
		if 'Item' not in raw_value:
			return None
		return raw_value['Item']

	def delete_raw(self, id, sort) -> bool:
		try:
			self._table.delete_item(Key={'id': id, 'sort': sort})
			return True
		except Exception as e:
			print(f"Error deleting item with id {id} and sort {sort}: {e}")
			return False
