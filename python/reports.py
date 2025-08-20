import json
import os
from config_manager import config_manager


def sizeof_fmt(num, suffix="B"):
    for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"


total_size = 0

# Get file paths from config manager
tag_sums_path = config_manager.get_tag_sums_path()
workspace_usage_path = os.path.join(config_manager.get_output_dir(), "workspace_env_usage_output.json")
model_usage_path = os.path.join(config_manager.get_output_dir(), "model_env_usage_output.json")

with open(tag_sums_path) as tag_sums:
    tag_data = json.load(tag_sums)

with open(workspace_usage_path, 'r') as wksp:
    with open(model_usage_path, 'r') as model:
        for key in tag_data.keys():
            if key in wksp.read():
                print("{} is in use in a workspace".format(key))
            else:
                if key in model.read():
                    print("{} is in use in a model".format(key))
                else:
                    human_readable_size = sizeof_fmt(tag_data[key]["size"])
                    total_size += tag_data[key]["size"]
                    print(key, human_readable_size)

human_readable_total_size = sizeof_fmt(total_size)
print("You could free up {} by deleting unused Docker tags.".format(human_readable_total_size))
