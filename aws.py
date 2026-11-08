import boto3
from pynamodb.models import Model
from pynamodb.attributes import UnicodeAttribute, NumberAttribute

TABLE_NAME = "RallyBot"
REGION = "us-east-2"

boto3.setup_default_session(region_name=REGION)

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
