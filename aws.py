import boto3
import jsonpickle
import datetime
import decimal
import os
import io
from pynamodb.expressions.condition import Condition
from pynamodb.models import Model
from pynamodb.attributes import UnicodeAttribute, NumberAttribute

TABLE_NAME = "RallyBot"
REGION = "us-east-2"

boto3.setup_default_session(region_name=REGION)

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
		table_name = TABLE_NAME
		region = REGION

	id = UnicodeAttribute(hash_key=True)
	sort = NumberAttribute(range_key=True)
	data = UnicodeAttribute(null=True)

class DynamoDBClient:
	def __init__(self):
		self.pickler = jsonpickle.pickler.Pickler()
		self.unpickler = jsonpickle.unpickler.Unpickler()
		self._dynamodb = boto3.resource('dynamodb')
		self._table = self._dynamodb.Table(TABLE_NAME)

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
	
	def read_item(self, id, sort) -> RallyBotModel | None:
		try:
			item = RallyBotModel.get(id, sort)
			return jsonpickle.decode(item.data)
		except RallyBotModel.DoesNotExist:
			return None
	
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

def upload_to_s3(image, filename: str) -> str:
	bucket_name = os.getenv('S3_BUCKET_NAME')
	if not bucket_name:
		raise ValueError("S3_BUCKET_NAME environment variable is not set")
	
	s3 = boto3.client('s3')
	img_byte_arr = io.BytesIO()
	image.save(img_byte_arr, format='WebP', lossless=True)
	img_byte_arr.seek(0)
	
	# Upload and set as public-read so Instagram can access it
	s3.put_object(
		Bucket=bucket_name,
		Key=filename,
		Body=img_byte_arr,
		ContentType='image/webp',
		ACL='public-read'
	)
	
	region = boto3.session.Session().region_name or 'us-east-1'
	return f"https://{bucket_name}.s3.{region}.amazonaws.com/{filename}"
