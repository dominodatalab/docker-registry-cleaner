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

def get_image_info(registry_url, repository_name, image, output_dir):
    print(f"Report for {image} images:")
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

        print("\r----------------------------------")

        sorted_layers = sorted(layers_info.items(), key=lambda x: x[1]['size'], reverse=True)
        
        headers = ["Layer ID", "Layer size (bytes)", "Total Size (bytes)", "Tag Count", "Tags"]
        rows = []
        
        for layer_id, info in sorted_layers:
            layer_info = layers_info[layer_id]
            tag_count = len(info['tags'])

            ind_layer_size = '{:.0f}'.format(layer_info['size'] / tag_count if tag_count > 0 else 0)
    
            rows.append((layer_id, ind_layer_size, info['size'], tag_count, ', '.join(info['tags'])))
        
        print(tabulate(rows, headers=headers, tablefmt="grid"))
        
        # Save to a file
        output_file = os.path.join(output_dir, f"{image}_report.txt")
        with open(output_file, "w") as file:
            file.write(tabulate(rows, headers=headers, tablefmt="grid"))

    except subprocess.CalledProcessError:
        print(f"Failed to retrieve information for image: {image}")
        print("----------------------------------")

def show_spinner():
    spinner = "|/-\\"
    for _ in range(10): 
        for char in spinner:
            sys.stdout.write("\rProcessing... " + char)
            sys.stdout.flush()
            time.sleep(0.1)
    sys.stdout.write("\rProcessing... Done!\n")
    sys.stdout.flush()

registry_url = ""
repository_name = ""
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
    #images_start_index = 1
    print("Error: Both --registry-url and --repository-name are required")
    print_help()
    sys.exit(1)

if len(sys.argv) > images_start_index:
    images = sys.argv[images_start_index:]
else:
    print("No images provided, scanning default images...")
    images = ["environment", "model"]

if "-h" in sys.argv or "--help" in sys.argv:
    print_help()
    sys.exit(0)

for image in images:
    get_image_info(registry_url, repository_name, image, output_dir)
    print()  

print("All images processed.")
