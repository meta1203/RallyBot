import boto3
import os
import jsonpickle
import datetime

boto3.setup_default_session(region_name="us-east-2")

# encode datetime objects as an ISO 8601 format string
class DatetimeHandler(jsonpickle.handlers.BaseHandler):
	def flatten(self, obj, data):
		return obj.isoformat()
	def restore(self, obj):
		return datetime.datetime.fromisoformat(obj)
jsonpickle.register(datetime.datetime, DatetimeHandler)

class DynamoDBClient:
	def __init__(self):
		self.table_name = "RallyBot"
		self.dynamodb = boto3.resource('dynamodb')
		self.table = self.dynamodb.Table(self.table_name)
		self.pickler = jsonpickle.pickler.Pickler()
		self.unpickler = jsonpickle.unpickler.Unpickler()

	def write_item(self, item):
		return self.table.put_item(Item=self.pickler.flatten(item))
	
	def read_item(self, id, sort):
		if 'Item' not in self.table.get_item(Key={'id': id, 'sort': sort}):
			return None
		return self.unpickler.restore(self.table.get_item(Key={'id': id, 'sort': sort})['Item'])
	
	def delete_item(self, id, sort):
		return self.table.delete_item(Key={'id': id, 'sort': sort})

class TableItem:
	id: str
	sort: int