import boto3
import jsonpickle
import datetime
import decimal
from pynamodb.expressions.condition import Condition
from pynamodb.models import Model
from pynamodb.attributes import UnicodeAttribute, NumberAttribute

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

class RallyBotModel(Model):
	class Meta:
		table_name = "RallyBot"
		region = "us-east-2"

	id = UnicodeAttribute(hash_key=True)
	sort = NumberAttribute(range_key=True)
	data = UnicodeAttribute(null=True)

class DynamoDBClient:
	def __init__(self):
		self.pickler = jsonpickle.pickler.Pickler()
		self.unpickler = jsonpickle.unpickler.Unpickler()

	def write_item(self, item):
		if isinstance(item, Model):
			item.save()
			return True
		model_item = RallyBotModel(
			id=item.id,
			sort=item.sort,
			data=jsonpickle.encode(item)
		)
		model_item.save()
		return True
	
	def read_item(self, id, sort):
		try:
			item = RallyBotModel.get(id, sort)
			return jsonpickle.decode(item.data)
		except RallyBotModel.DoesNotExist:
			return None

if __name__ == "__main__":
	ddb = DynamoDBClient()
	test = ddb.read_item("event", 305964611)
	print(f"test result: {test.datetime}")
	print(jsonpickle.encode(test))