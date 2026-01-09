from logging_utils import get_logger
from typing import List, Dict

from bson import ObjectId

logger = get_logger(__name__)


def read_object_ids_from_file(file_path: str) -> List[str]:
	"""Read 24-char hex ObjectIDs from a file.

	Accepted formats per non-comment line:
	- "<ObjectID>" (one per line)
	- "environment:<ObjectID>" or "model:<ObjectID>" (typed); the type is ignored here

	Returns a flat list of valid IDs (as strings).
	"""
	object_ids: List[str] = []
	try:
		with open(file_path, 'r') as f:
			for line_num, raw in enumerate(f, 1):
				line = raw.strip()
				if not line or line.startswith('#'):
					continue
				if ':' in line:
					_, _, rest = line.partition(':')
					raw_value = rest.strip()
				else:
					raw_value = line
				try:
					oid = validate_object_id(raw_value, field_name=f"ObjectID on line {line_num}")
					object_ids.append(str(oid))
				except ValueError as e:
					logger.warning(str(e))
		return object_ids
	except FileNotFoundError:
		logger.error(f"File '{file_path}' not found")
		return []
	except Exception as e:
		logger.error(f"Error reading file '{file_path}': {e}")
		return []


def read_typed_object_ids_from_file(file_path: str) -> Dict[str, List[str]]:
	"""Read typed ObjectIDs from file.
	
	Accepted formats per non-comment line:
	- "environment:<ObjectID>"
	- "environmentRevision:<ObjectID>"
	- "model:<ObjectID>"
	- "modelVersion:<ObjectID>"
	
	Bare IDs (no prefix) are ignored to avoid ambiguity across collections.
	
	Returns a dict like { 'environment': [...], 'environment_revision': [...], 'model': [...], 'model_version': [...] }
	Only includes keys that have values.
	"""
	result: Dict[str, List[str]] = {
		"environment": [],
		"environment_revision": [],
		"model": [],
		"model_version": []
	}
	try:
		with open(file_path, 'r') as f:
			for line_num, raw in enumerate(f, 1):
				line = raw.strip()
				if not line or line.startswith('#'):
					continue
				kind = None
				value = None
				if ':' in line:
					prefix, _, rest = line.partition(':')
					pref = prefix.lower().strip()
					if pref in ('environment', 'env'):
						kind = 'environment'
					elif pref in ('environmentrevision', 'environment_revision', 'envrevision', 'env_rev', 'envrev'):
						kind = 'environment_revision'
					elif pref in ('model',):
						kind = 'model'
					elif pref in ('modelversion', 'model_version', 'model_ver', 'modelver'):
						kind = 'model_version'
					value = rest.strip()
				if not kind:
					logger.warning(
						f"Line {line_num}: missing or unknown type prefix. Expected environment:, environmentRevision:, model:, or modelVersion:. Skipping."
					)
					continue
				try:
					oid = validate_object_id(value, field_name=f"ObjectID on line {line_num}")
					result[kind].append(str(oid))
				except ValueError as e:
					logger.warning(str(e))
		# Remove empty keys
		return {k: v for k, v in result.items() if v}
	except FileNotFoundError:
		logger.error(f"File '{file_path}' not found")
		return {}
	except Exception as e:
		logger.error(f"Error reading file '{file_path}': {e}")
		return {}


def filter_values_by_object_ids(values: List[str], object_ids: List[str]) -> List[str]:
	"""Return values that start with any of the provided ObjectIDs."""
	if not object_ids:
		return values
	result: List[str] = []
	for v in values:
		for oid in object_ids:
			if v.startswith(oid):
				result.append(v)
				break
	return result


def starts_with_any_object_id(value: str, object_ids: List[str]) -> bool:
	"""Return True if the given value starts with any of the provided ObjectIDs."""
	for oid in object_ids or []:
		if value.startswith(oid):
			return True
	return False


def validate_object_id(value: str, *, field_name: str = "ObjectID") -> ObjectId:
	"""Validate a 24-char hex ObjectId string and return a bson.ObjectId.

	Raises:
	    ValueError: if the value is empty, wrong length, or not hex.
	"""
	if not value:
		raise ValueError(f"{field_name} is required")
	if len(value) != 24:
		raise ValueError(f"{field_name} '{value}' is not 24 characters")
	try:
		int(value, 16)
	except ValueError:
		raise ValueError(f"{field_name} '{value}' is not a valid hexadecimal ObjectId")
	return ObjectId(value)
