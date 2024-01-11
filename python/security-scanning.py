from pyclair import Clair
import json

def scan_image_layers(clair_url, image_layers_json):
    # Load image layers information from the JSON file
    with open(image_layers_json, 'r') as json_file:
        image_layers = json.load(json_file)

    # Connect to the Clair server
    clair = Clair(clair_url)

    # Iterate through each image layer and perform vulnerability scanning
    for layer_id, info in image_layers.items():
        print(f"Scanning Layer ID: {layer_id}")
        try:
            # Scan the layer using Clair
            vulnerabilities = clair.get_vulnerabilities(info['sha256'])
            
            # Print or process the vulnerabilities as needed
            print("Vulnerabilities found:")
            for vulnerability in vulnerabilities:
                print(f" - {vulnerability['Name']}")
            
            print("\n")
        
        except Exception as e:
            print(f"Error scanning layer {layer_id}: {e}")
            print("\n")

# Example usage
clair_url = "http://clair-server-url:6060"  # Replace with the actual Clair server URL
image_layers_json_file = "path/to/image-layers.json"  # Replace with the actual path to your image layers JSON file

scan_image_layers(clair_url, image_layers_json_file)
