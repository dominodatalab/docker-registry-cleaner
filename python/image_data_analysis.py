import subprocess
import json
import argparse
import sys
import time
import os
from collections import defaultdict
from tabulate import tabulate
from typing import List, Optional
from config_manager import config_manager, SkopeoClient
from logging_utils import setup_logging, get_logger
from object_id_utils import read_object_ids_from_file
from report_utils import save_table_and_json, save_json

logger = get_logger(__name__)

def print_help():
    print("Usage: python image_data_analysis.py [options] [image1 image2 ...]")
    print("Get layer information for Container images in the specified repository.")
    print("Options:")
    print("  --registry-url URL     Container registry URL")
    print("  --repository NAME      Container repository name")
    print("  --file FILE            File containing ObjectIDs (first column) to filter images")
    print("  -h, --help             Display this help message")
    print("If no images are provided, the default images will be used.")
    print("Example: python image_data_analysis.py --registry-url <registry_url> --repository <repository> --file environments environment model")

def add_environments(original_json):
    transformed_json = {}

    for sha256, data in original_json.items():
        transformed_json[sha256] = {
            "size": data["size"],
            "tags": data["tags"],
            "environments": [tag.split('-')[0] for tag in data["tags"]]
        }

    return transformed_json

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
        "inspect_workload.py",
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

def get_image_info(repository, image, object_ids: Optional[List[str]] = None):
    layers_info = defaultdict(lambda: {'size': 0, 'tags': set()})

    try:
        # Use standardized SkopeoClient
        skopeo_client = SkopeoClient(config_manager, use_pod=False)
        
        # Get tags using standardized client
        tags = skopeo_client.list_tags(f"{repository}/{image}")
        
        # Filter tags by ObjectIDs if provided
        if object_ids:
            original_count = len(tags)
            tags = filter_tags_by_object_ids(tags, object_ids)
            filtered_count = len(tags)
            logger.info(f"Filtered tags: {filtered_count}/{original_count} tags match the provided ObjectIDs")
            
            if filtered_count == 0:
                logger.warning(f"No tags found matching the provided ObjectIDs for image: {image}")
                return None

        for tag in tags:
            logger.info(f"Analyzing Tag {tag}...")
            show_spinner()
            
            # Inspect image using standardized client
            image_info = skopeo_client.inspect_image(f"{repository}/{image}", tag)
            if not image_info:
                logger.error(f"Failed to inspect image {image}:{tag}")
                continue
                
            layers_data = image_info['LayersData']

            for layer in layers_data:
                layer_id = layer['Digest']
                layer_size = layer['Size']
                layers_info[layer_id]['size'] += layer_size
                layers_info[layer_id]['tags'].add(tag)

        return layers_info

    except Exception as e:
        logger.error(f"Failed to retrieve information for image: {image}")
        logger.error(f"Error: {e}")
        logger.error("----------------------------------")
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

def main():
    setup_logging()
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Analyze Docker registry images and extract layer information",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python image_data_analysis.py --registry-url docker-registry:5000 --repository dominodatalab environment model
  python image_data_analysis.py --registry-url docker-registry:5000 --repository dominodatalab --file environments environment model
        """
    )
    
    parser.add_argument("--registry-url", required=True, help="Container registry URL")
    parser.add_argument("--repository", required=True, help="Container repository name")
    parser.add_argument("--file", help="File containing ObjectIDs (first column) to filter images")
    parser.add_argument("images", nargs="*", help="Images to analyze (default: environment, model)")
    
    args = parser.parse_args()
    
    # Parse ObjectIDs from file if provided
    object_ids = None
    if args.file:
        object_ids = read_object_ids_from_file(args.file)
        if not object_ids:
            logger.error(f"No valid ObjectIDs found in file '{args.file}'")
            sys.exit(1)
        logger.info(f"Filtering images by ObjectIDs from file '{args.file}': {object_ids}")
    
    # Get a list of images from the command line arguments or use default images
    if args.images:
        images = args.images
    else:
        logger.info("No images provided for registry scanning, scanning default Domino images...")
        images = ["environment", "model"]
    
    registry_url = args.registry_url
    repository = args.repository
    
    # Get output file paths from ConfigManager
    final_output_file = config_manager.get_image_analysis_path()
    tags_per_layer_output_file = config_manager.get_tags_per_layer_path()
    layers_and_sizes_output_file = config_manager.get_layers_and_sizes_path()
    filtered_layers_output_file = config_manager.get_filtered_layers_path()
    tag_sums_output_file = config_manager.get_tag_sums_path()
    images_report_output_file = config_manager.get_images_report_path()
    output_dir = config_manager.get_output_dir()
    
    logger.info("----------------------------------")
    logger.info(f"   Container registry  scanning")
    if object_ids:
        logger.info(f"   Filtering by ObjectIDs: {', '.join(object_ids)}")
    logger.info("----------------------------------")
    
    # Loop through each image and get its information
    image_tables = []
    for image in images:
        image_info = get_image_info(repository, image, object_ids)
        if image_info:
            image_tables.append(image_info)
        logger.info("")
    
    if not image_tables:
        logger.error("No image data found. Check your ObjectID filters or registry access.")
        sys.exit(1)
    
    # Merge tables based on layer ID
    merged_table = merge_tables(image_tables)
    
    # Sort the merged table based on layer size
    sorted_table = dict(sorted(merged_table.items(), key=lambda x: x[1]['size'], reverse=True))
    
    # Convert sets to lists for JSON serialization
    json_serializable_table = {}
    for layer_id, info in sorted_table.items():
        json_serializable_table[layer_id] = {
            'size': info['size'],
            'tags': list(info['tags'])  # Convert set to list for JSON serialization
        }
    
    # Prepare sorted table for tabulate
    headers = ["Layer ID", "Layer size (bytes)", "Total Size (bytes)", "Tag Count", "Tags"]
    rows = []
    
    for layer_id, info in sorted_table.items():
        tag_count = len(info['tags'])
        ind_layer_size = '{:.0f}'.format(info['size'] / tag_count if tag_count > 0 else 0)
        rows.append((layer_id, ind_layer_size, info['size'], tag_count, ', '.join(info['tags'])))
    
    # Print sorted table
    table = tabulate(rows, headers=headers, tablefmt="grid")
    #logger.info(table)
    
    # Save sorted table to a file
    output_file = images_report_output_file
    save_table_and_json(output_file, table, json_serializable_table)
    logger.info(f"Merged and sorted results saved to {output_file}.txt and {output_file}.json")

    # Read JSON data from the final output file
    with open(f"{output_file}.json", 'r') as final_file:
        final_json_data = json.load(final_file)

    transformed_json = add_environments(final_json_data)

    # Save the updated JSON with environments to the output file
    save_json(final_output_file, transformed_json)
    logger.info(f"Updated JSON data saved to: {final_output_file}")

    # Get number of tags per layer
    tags_per_layer = count_tags_per_layer(transformed_json)
    save_json(tags_per_layer_output_file, tags_per_layer)
    logger.info(f"Tags per layer count saved to: {tags_per_layer_output_file}")

    # Get size per layer
    layers_and_sizes = extract_layers_and_sizes(transformed_json)
    save_json(layers_and_sizes_output_file, layers_and_sizes)
    logger.info(f"Layers and sizes saved to: {layers_and_sizes_output_file}")

    # Get filtered layers by single tag
    filtered_layers = filter_layers_by_single_tag(transformed_json)
    save_json(filtered_layers_output_file, filtered_layers)
    logger.info(f"Filtered layers saved to: {filtered_layers_output_file}")

    # Get tag sums
    tag_sums = sum_layers_by_tag(filtered_layers)
    save_json(tag_sums_output_file, tag_sums)
    logger.info(f"Tag sums saved to: {tag_sums_output_file}")

    logger.info("Analysis complete!")

if __name__ == "__main__":
    main()