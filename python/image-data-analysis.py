import subprocess
import json
import argparse
import sys
import time
import os
from collections import defaultdict
from tabulate import tabulate
from typing import List, Optional, Set
from config_manager import config_manager, SkopeoClient

def print_help():
    print("Usage: python image-data-analysis.py [options] [image1 image2 ...]")
    print("Get layer information for Container images in the specified repository.")
    print("Options:")
    print("  --registry-url URL     Container registry URL")
    print("  --repository-name NAME Container repository name")
    print("  --file FILE            File containing ObjectIDs (first column) to filter images")
    print("  -h, --help              Display this help message")
    print("If no images are provided, the default images will be used.")
    print("Example: python image-data-analysis.py --registry-url <registry_url> --repository-name <repository_name> --file environments environment model")

def add_environments(original_json):
    transformed_json = {}

    for sha256, data in original_json.items():
        transformed_json[sha256] = {
            "size": data["size"],
            "tags": data["tags"],
            "environments": [tag.split('-')[0] for tag in data["tags"]]
        }

    return transformed_json

def read_object_ids_from_file(file_path: str) -> List[str]:
    """Read ObjectIDs from a file, extracting the first column"""
    object_ids = []
    try:
        with open(file_path, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith('#'):  # Skip empty lines and comments
                    parts = line.split()
                    if parts:
                        obj_id = parts[0]  # First column is the ObjectID
                        if len(obj_id) == 24:
                            try:
                                int(obj_id, 16)  # Validate hexadecimal
                                object_ids.append(obj_id)
                            except ValueError:
                                print(f"Warning: Invalid ObjectID '{obj_id}' on line {line_num}")
                        else:
                            print(f"Warning: ObjectID '{obj_id}' on line {line_num} is not 24 characters")
        return object_ids
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found")
        return []
    except Exception as e:
        print(f"Error reading file '{file_path}': {e}")
        return []

def filter_tags_by_object_ids(tags: List[str], object_ids: Optional[List[str]] = None) -> List[str]:
    """Filter tags to only include those that start with one of the provided ObjectIDs"""
    if not object_ids:
        return tags
    
    filtered_tags = []
    for tag in tags:
        # Check if the tag starts with any of the provided ObjectIDs
        for obj_id in object_ids:
            if tag.startswith(obj_id):
                filtered_tags.append(tag)
                break
    
    return filtered_tags

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

def get_image_info(registry_url, repository_name, image, output_dir, object_ids: Optional[List[str]] = None):
    layers_info = defaultdict(lambda: {'size': 0, 'tags': set()})

    try:
        # Use standardized SkopeoClient
        skopeo_client = SkopeoClient(config_manager, use_pod=False)
        
        # Get tags using standardized client
        tags = skopeo_client.list_tags(f"{repository_name}/{image}")
        
        # Filter tags by ObjectIDs if provided
        if object_ids:
            original_count = len(tags)
            tags = filter_tags_by_object_ids(tags, object_ids)
            filtered_count = len(tags)
            print(f"Filtered tags: {filtered_count}/{original_count} tags match the provided ObjectIDs")
            
            if filtered_count == 0:
                print(f"No tags found matching the provided ObjectIDs for image: {image}")
                return None

        for tag in tags:
            print(f"Analyzing Tag {tag}...")
            show_spinner()
            
            # Inspect image using standardized client
            image_info = skopeo_client.inspect_image(f"{repository_name}/{image}", tag)
            if not image_info:
                print(f"Failed to inspect image {image}:{tag}")
                continue
                
            layers_data = image_info['LayersData']

            for layer in layers_data:
                layer_id = layer['Digest']
                layer_size = layer['Size']
                layers_info[layer_id]['size'] += layer_size
                layers_info[layer_id]['tags'].add(tag)

        return layers_info

    except Exception as e:
        print(f"Failed to retrieve information for image: {image}")
        print(f"Error: {e}")
        print("----------------------------------")
        return None

def count_tags_per_layer(original_json):
    tags_per_layer = {}

    for sha256, data in original_json.items():
        tags_per_layer[sha256] = len(data["tags"])

    return tags_per_layer

def extract_layers_and_sizes(original_json):
    layers_and_sizes = {}

    for sha256, data in original_json.items():
        layers_and_sizes[sha256] = int(data["size"])

    return layers_and_sizes

def filter_layers_by_single_tag(original_json):
    filtered_layers = {sha256: data for sha256, data in original_json.items() if len(data["tags"]) == 1}
    return filtered_layers

def sum_layers_by_tag(filtered_layers):
    tag_sum_dict = {}
    for sha256, data in filtered_layers.items():
        for tag in data["tags"]:
            if tag in tag_sum_dict:
                tag_sum_dict[tag]["size"] += int(data["size"])
            else:
                tag_sum_dict[tag] = {"size": int(data["size"]), "environments": data["environments"]}

    return tag_sum_dict

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

def save_to_json(data, output_file):
    with open(output_file, 'w') as json_file:
        json.dump(data, json_file, indent=2)

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Analyze Docker registry images and extract layer information",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python image-data-analysis.py --registry-url docker-registry:5000 --repository-name dominodatalab environment model
  python image-data-analysis.py --registry-url docker-registry:5000 --repository-name dominodatalab --file environments environment model
        """
    )
    
    parser.add_argument("--registry-url", required=True, help="Container registry URL")
    parser.add_argument("--repository-name", required=True, help="Container repository name")
    parser.add_argument("--file", help="File containing ObjectIDs (first column) to filter images")
    parser.add_argument("images", nargs="*", help="Images to analyze (default: environment, model)")
    
    args = parser.parse_args()
    
    # Parse ObjectIDs from file if provided
    object_ids = None
    if args.file:
        object_ids = read_object_ids_from_file(args.file)
        if not object_ids:
            print(f"Error: No valid ObjectIDs found in file '{args.file}'")
            sys.exit(1)
        print(f"Filtering images by ObjectIDs from file '{args.file}': {object_ids}")
    
    # Get a list of images from the command line arguments or use default images
    if args.images:
        images = args.images
    else:
        print("No images provided for registry scanning, scanning default Domino images...")
        images = ["environment", "model"]
    
    registry_url = args.registry_url
    repository_name = args.repository_name
    final_output_file = "final-report.json"
    tags_per_layer_output_file = "tags-per-layer.json"
    layers_and_sizes_output_file = "layers-and-sizes.json"
    filtered_layers_output_file = "filtered-layers.json"
    tag_sums_output_file = "tag-sums.json"
    workload_output = "workload-report"
    output_dir = os.getcwd() 
    
    print("----------------------------------")
    print(f"   Container registry  scanning")
    if object_ids:
        print(f"   Filtering by ObjectIDs: {', '.join(object_ids)}")
    print("----------------------------------")
    
    # Loop through each image and get its information
    image_tables = []
    for image in images:
        image_info = get_image_info(registry_url, repository_name, image, output_dir, object_ids)
        if image_info:
            image_tables.append(image_info)
        print()
    
    if not image_tables:
        print("No image data found. Check your ObjectID filters or registry access.")
        sys.exit(1)
    
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

    # Read JSON data from the final output file
    with open(f"{output_file}.json", 'r') as final_file:
        final_json_data = json.load(final_file)

    transformed_json = add_environments(final_json_data)

    # Save the updated JSON with environments to the output file
    save_to_json(transformed_json, final_output_file)
    print(f"Updated JSON data saved to: {final_output_file}")

    # Get number of tags per layer
    tags_per_layer = count_tags_per_layer(final_json_data)
    save_to_json(tags_per_layer, tags_per_layer_output_file)
    print(f"Tags per layer count saved to: {tags_per_layer_output_file}")

    # Get size per layer
    layers_and_sizes = extract_layers_and_sizes(final_json_data)
    save_to_json(layers_and_sizes, layers_and_sizes_output_file)
    print(f"Layers and sizes saved to: {layers_and_sizes_output_file}")

    # Get filtered layers by single tag
    filtered_layers = filter_layers_by_single_tag(final_json_data)
    save_to_json(filtered_layers, filtered_layers_output_file)
    print(f"Filtered layers saved to: {filtered_layers_output_file}")

    # Get tag sums
    tag_sums = sum_layers_by_tag(filtered_layers)
    save_to_json(tag_sums, tag_sums_output_file)
    print(f"Tag sums saved to: {tag_sums_output_file}")

    print("Analysis complete!")

if __name__ == "__main__":
    main()