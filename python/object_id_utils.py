from logging_utils import get_logger
from typing import List, Dict

logger = get_logger(__name__)


def read_object_ids_from_file(file_path: str) -> List[str]:
	"""Read 24-char hex ObjectIDs from a file.

	Accepted formats per non-comment line:
	- "<ObjectID>" (one per line)
	- "environment:<ObjectID>" or "model:<ObjectID>" (typed); the type is ignored here

	Returns a flat list of valid IDs.
	"""
	object_ids: List[str] = []
	try:
		with open(file_path, 'r') as f:
			for line_num, raw in enumerate(f, 1):
				line = raw.strip()
				if not line or line.startswith('#'):
					continue
				if ':' in line:
					prefix, _, rest = line.partition(':')
					obj_id = rest.strip()
				else:
					obj_id = line
				if len(obj_id) != 24:
					logger.warning(f"ObjectID '{obj_id}' on line {line_num} is not 24 characters")
					continue
				try:
					int(obj_id, 16)
					object_ids.append(obj_id)
				except ValueError:
					logger.warning(f"Invalid ObjectID '{obj_id}' on line {line_num}")
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
	- "model:<ObjectID>"
	- "<ObjectID>" (placed under 'any')

	Returns a dict like { 'environment': [...], 'model': [...], 'any': [...] }
	Only includes keys that have values.
	"""
	result: Dict[str, List[str]] = {"environment": [], "model": [], "any": []}
	try:
		with open(file_path, 'r') as f:
			for line_num, raw in enumerate(f, 1):
				line = raw.strip()
				if not line or line.startswith('#'):
					continue
				kind = 'any'
				value = line
				if ':' in line:
					prefix, _, rest = line.partition(':')
					pref = prefix.lower().strip()
					if pref in ('environment', 'env'):
						kind = 'environment'
					elif pref in ('model',):
						kind = 'model'
					value = rest.strip()
				if len(value) != 24:
					logger.warning(f"ObjectID '{value}' on line {line_num} is not 24 characters")
					continue
				try:
					int(value, 16)
					result[kind].append(value)
				except ValueError:
					logger.warning(f"Invalid ObjectID '{value}' on line {line_num}")
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
