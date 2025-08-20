from typing import List
from logging_utils import get_logger

logger = get_logger(__name__)


def read_object_ids_from_file(file_path: str) -> List[str]:
	"""Read 24-char hex ObjectIDs from the first column of each non-comment line.
	Returns a list of valid IDs; logs warnings for invalid rows and errors for I/O issues.
	"""
	object_ids: List[str] = []
	try:
		with open(file_path, 'r') as f:
			for line_num, line in enumerate(f, 1):
				line = line.strip()
				if not line or line.startswith('#'):
					continue
				parts = line.split()
				if not parts:
					continue
				obj_id = parts[0]
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
