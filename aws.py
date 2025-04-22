import boto3
import jsonpickle
import datetime
import decimal

boto3.setup_default_session(region_name="us-east-2")

# encode datetime objects as an ISO 8601 format string
@jsonpickle.register(datetime.datetime)
class DatePickleISO8601(jsonpickle.handlers.DatetimeHandler):
    def flatten(self, obj, data):
        pickler = self.context
        if not pickler.unpicklable:
            return str(obj)
        cls, args = obj.__reduce__()
        flatten = pickler.flatten
        payload = obj.isoformat()
        args = [payload] + [flatten(i, reset=False) for i in args[1:]]
        data['__reduce__'] = (flatten(cls, reset=False), args)
        return data

    def restore(self, data):
        cls, args = data['__reduce__']
        unpickler = self.context
        restore = unpickler.restore
        cls = restore(cls, reset=False)
        value = datetime.datetime.fromisoformat(args[0])
        return value

@jsonpickle.register(decimal.Decimal)
class DecimalHandler(jsonpickle.handlers.BaseHandler):
	def flatten(self, obj: decimal.Decimal, data):
		if obj.as_tuple().exponent == 0:
			return int(obj)
		else:
			return float(obj)
	def restore(self, obj):
		return float(obj)

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

if __name__ == "__main__":
	ddb = DynamoDBClient()
	test = ddb.read_item("event", 305964611)
	print(f"test result: {test.datetime}")
	print(jsonpickle.encode(test))