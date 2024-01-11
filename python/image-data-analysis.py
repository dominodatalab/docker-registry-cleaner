import subprocess
import json
from collections import defaultdict
from tabulate import tabulate
import sys
import time
import os

def print_help():
    print("Usage: python image-data-analysis.py [options] [image1 image2 ...]")
    print("Get layer information for Container images in the specified repository.")
    print("Options:")
    print("  --registry-url URL     Container registry URL")
    print("  --repository-name NAME Container repository name")
    print("  -h, --help              Display this help message")
    print("If no images are provided, the default images will be used.")
    print("Example: python image-data-analysis.py --registry-url <registry_url> --repository-name <repository_name> environment model")

def inspect_workload(registry_url, prefix_to_remove, target_namespace, pod_name_prefixes, output_file):
    command = [
        "python3.10",
        "inspect-workload.py",
        "--registry-url",
        registry_url,
        "--prefix-to-remove",
        prefix_to_remove,
        "--target-namespace",
        target_namespace,
        "--pod-name-prefixes",
        *pod_name_prefixes,
        "--output-file",
        output_file
    ]
    subprocess.run(command)

def get_image_info(registry_url, repository_name, image, output_dir):
    layers_info = defaultdict(lambda: {'size': 0, 'tags': set()})

    try:
        output = subprocess.check_output(["skopeo", "list-tags", f"docker://{registry_url}/{repository_name}/{image}"], stderr=subprocess.DEVNULL)
        tags = json.loads(output.decode())['Tags']

        for tag in tags:
            print(f"Analyzing Tag {tag}...")
            show_spinner()
            output = subprocess.check_output(["skopeo", "inspect", f"docker://{registry_url}/{repository_name}/{image}:{tag}"], stderr=subprocess.DEVNULL)
            image_info = json.loads(output.decode())
            layers_data = image_info['LayersData']

            for layer in layers_data:
                layer_id = layer['Digest']
                layer_size = layer['Size']
                layers_info[layer_id]['size'] += layer_size
                layers_info[layer_id]['tags'].add(tag)

        return layers_info

    except subprocess.CalledProcessError:
        print(f"Failed to retrieve information for image: {image}")
        print("----------------------------------")
        return None

def show_spinner():
    spinner = "|/-\\"
    for _ in range(10): 
        for char in spinner:
            sys.stdout.write("\rProcessing... " + char)
            sys.stdout.flush()
            time.sleep(0.1)
    sys.stdout.write("\rProcessing... Done!\n")
    sys.stdout.flush()

def merge_tables(image_tables):
    # Merge tables based on layer ID
    merged_table = defaultdict(lambda: {'size': 0, 'tags': set()})

    for table in image_tables:
        for layer_id, info in table.items():
            merged_table[layer_id]['size'] += info['size']
            merged_table[layer_id]['tags'].update(info['tags'])

    return merged_table

def save_to_file(table_data, json_data, output_file):
    # Convert sets to lists in the JSON data
    json_data_copy = json_data.copy()
    for layer_id, info in json_data_copy.items():
        info['tags'] = list(info['tags'])
        # Replace 'size' with 'ind_layer_size' in the JSON data
        info['size'] = '{:.0f}'.format(info['size'] / len(info['tags'])) if len(info['tags']) > 0 else 0

    # Save console table format to txt file
    with open(output_file + ".txt", "w") as file:
        file.write(table_data)

    # Save JSON format to json file
    with open(output_file + ".json", "w") as json_file:
        json.dump(json_data_copy, json_file, indent=2)

def main():
    # Parse command line arguments
    registry_url = ""
    repository_name = ""
    final_output_file = "final-report.json"
    workload_output = "workload-report"
    output_dir = os.getcwd() 
    
    if "--registry-url" in sys.argv:
        index = sys.argv.index("--registry-url") + 1
        if index < len(sys.argv):
            registry_url = sys.argv[index]
        else:
            print("Error: Missing value for --registry-url")
            print_help()
            sys.exit(1)
    
    if "--repository-name" in sys.argv:
        index = sys.argv.index("--repository-name") + 1
        if index < len(sys.argv):
            repository_name = sys.argv[index]
        else:
            print("Error: Missing value for --repository-name")
            print_help()
            sys.exit(1)
    
    if "--registry-url" in sys.argv and "--repository-name" in sys.argv:
        images_start_index = sys.argv.index("--repository-name") + 2
    else:
        print("Error: Both --registry-url and --repository-name are required")
        print_help()
        sys.exit(1)
    
    # Get a list of images from the command line arguments or use default images
    if len(sys.argv) > images_start_index:
        images = sys.argv[images_start_index:]
    else:
        print("No images provided for registry scanning, scanning default Domino images...")
        images = ["environment", "model"]
    
    if "-h" in sys.argv or "--help" in sys.argv:
        print_help()
        sys.exit(0)
    
    inspect_workload("{registry_url}/{repository_name}", "{registry_url}/{repository_name}/", "domino-compute", ["model-", "run-"], workload_output)
    
    print("----------------------------------")
    print(f"   Container registry  scanning")
    print("----------------------------------")
    
    # Loop through each image and get its information
    image_tables = []
    for image in images:
        image_info = get_image_info(registry_url, repository_name, image, output_dir)
        if image_info:
            image_tables.append(image_info)
        print()
    
    # Merge tables based on layer ID
    merged_table = merge_tables(image_tables)
    
    # Sort the merged table based on layer size
    sorted_table = dict(sorted(merged_table.items(), key=lambda x: x[1]['size'], reverse=True))
    
    # Prepare sorted table for tabulate
    headers = ["Layer ID", "Layer size (bytes)", "Total Size (bytes)", "Tag Count", "Tags"]
    rows = []
    
    for layer_id, info in sorted_table.items():
        tag_count = len(info['tags'])
        ind_layer_size = '{:.0f}'.format(info['size'] / tag_count if tag_count > 0 else 0)
        rows.append((layer_id, ind_layer_size, info['size'], tag_count, ', '.join(info['tags'])))
    
    # Print sorted table
    table = tabulate(rows, headers=headers, tablefmt="grid")
    #print(table)
    
    # Save sorted table to a file
    output_file = os.path.join(output_dir, "images-report")
    save_to_file(table, sorted_table, output_file)
    print(f"Merged and sorted results saved to {output_file}.txt and {output_file}.json")

    # Read JSON data from the first file
    with open(f"{output_file}.json", 'r') as file_1:
        images_json_data = json.load(file_1)
    
    # Read JSON data from the second file
    with open(f"{workload_output}.json", 'r') as file_2:
        workload_json_data = json.load(file_2)
    
    # Create a dictionary to map tags to workload counts
    tag_to_workload = {}
    for tag, pod_info in workload_json_data.items():
        if "count" in pod_info:
            tag_to_workload[tag] = {
                "count": pod_info["count"],
                "pods": pod_info.get("pods", [])
            }
    
    # Update pods key in the first JSON with counts from the second JSON
    for tag, info in images_json_data.items():
        info["pods"] = []
        info["workload"] = 0
    
        if "tags" in info:
            for tag_2 in info["tags"]:
                if tag_2 in tag_to_workload:
                    info["pods"].extend(tag_to_workload[tag_2]["pods"])
                    info["workload"] += tag_to_workload[tag_2]["count"]
    
    # Convert the updated data back to JSON
    final_json_data = json.dumps(images_json_data, indent=2)
    
    # Save the updated JSON to the output file
    with open(final_output_file, 'w') as output_file:
        output_file.write(final_json_data)
    
    print(f"Updated JSON data saved to: {final_output_file}")

if __name__ == "__main__":
    main()