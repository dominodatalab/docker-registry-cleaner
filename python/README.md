# Container Image and Workload Analysis

This Python script is designed to analyze container images from a specified container registry and generate a report on the layers and their sizes. Additionally, it fetches workload information from a Kubernetes cluster and combines it with the image analysis for a comprehensive report.

## Requirements

Make sure you have the following dependencies installed:

- Python 3.10
- Skopeo
- Kubernetes Cluster Access

Install Python dependencies using:

```bash
pip install -r requirements.txt
```
## Usage

```bash
python image-data-analysis.py [options] [image1 image2 ...]
```

### Options

    --registry-url URL: Container registry URL (required).
    --repository-name NAME: Container repository name (required).
    -h, --help: Display help message.

### Example

```bash
python3.10 image-data-analysis.py --registry-url $registry_url --repository-name $repository_name
No images provided for registry scanning, scanning default Domino images...
----------------------------------
   Kubernetes cluster scanning
----------------------------------
|_ Analyzing Pod model-659d755348824d102d3ec6e0-6d67f98765-9rxnl...
Processing... Done!
  |_ Analyzing Image 659d755348824d102d3ec6df-v1-202419163323_HfGeD8Ia...
Processing... Done!
|_ Analyzing Pod run-659ead94e63dbc67d664a553-8jngh...
Processing... Done!
  |_ Analyzing Image 659c516f24a4aa48d5365374-1...
Processing... Done!
|_ Analyzing Pod run-659eaef1e63dbc67d664a567-c2fzf...
Processing... Done!
  |_ Analyzing Image 659c516f24a4aa48d5365374-1...
Processing... Done!
|_ Analyzing Pod run-659ecaf8e63dbc67d664a57c-kvjmn...
Processing... Done!
  |_ Analyzing Image 659c7bce85f6381e675bb205-1...
Processing... Done!
|_ Analyzing Pod model-659d755348824d102d3ec6e0-6d67f98765-9rxnl...
Processing... Done!
  |_ Analyzing Image 659d755348824d102d3ec6df-v1-202419163323_HfGeD8Ia...
Processing... Done!
|_ Analyzing Pod run-659ead94e63dbc67d664a553-8jngh...
Processing... Done!
  |_ Analyzing Image 659c516f24a4aa48d5365374-1...
Processing... Done!
|_ Analyzing Pod run-659eaef1e63dbc67d664a567-c2fzf...
Processing... Done!
  |_ Analyzing Image 659c516f24a4aa48d5365374-1...
Processing... Done!
|_ Analyzing Pod run-659ecaf8e63dbc67d664a57c-kvjmn...
Processing... Done!
  |_ Analyzing Image 659c7bce85f6381e675bb205-1...
Processing... Done!
Results saved to workload-report.txt and workload-report.json
----------------------------------
   Container registry  scanning
----------------------------------
Analyzing Tag 659c690185f6381e675bb1f1-2...
Processing... Done!
Analyzing Tag 659c516f24a4aa48d5365377-1...
Processing... Done!
Analyzing Tag 659ceb3048824d102d3ec6ce-2...
Processing... Done!
Analyzing Tag 659ceb1348824d102d3ec6c9-1...
Processing... Done!
Analyzing Tag 659d75d748824d102d3ec6f3-1...
Processing... Done!
Analyzing Tag 659c68c885f6381e675bb1ec-1...
Processing... Done!
Analyzing Tag 659c7f6885f6381e675bb20f-1...
Processing... Done!
Analyzing Tag 659c67d485f6381e675bb1df-2...
Processing... Done!
Analyzing Tag 659c516f24a4aa48d5365375-1...
Processing... Done!
Analyzing Tag 659d75ac48824d102d3ec6ee-1...
Processing... Done!
Analyzing Tag 659c797b85f6381e675bb1f8-4...
Processing... Done!
Analyzing Tag 659c7bce85f6381e675bb205-1...
Processing... Done!
Analyzing Tag 659c516f24a4aa48d5365374-1...
Processing... Done!
Analyzing Tag 659c7f4985f6381e675bb20a-1...
Processing... Done!
Analyzing Tag 659c516f24a4aa48d5365376-1...
Processing... Done!

Analyzing Tag 659d755348824d102d3ec6df-v1-202419163323_HfGeD8Ia...
Processing... Done!

Merged and sorted results saved to /Users/yassine.maachi/Documents/domino-internal/hackathon-24/docker-registry-cleaner/images-report.txt and /Users/yassine.maachi/Documents/domino-internal/hackathon-24/docker-registry-cleaner/images-report.json
Updated JSON data saved to: final-report.json
```

### Example of final-report.json output

```json
{
  "sha256:2bd74ca1764422f7716e11debae14904cd95706e1b856ecd4a9e169853c460da": {
    "size": "615515076",
    "tags": [
      "659ceb3048824d102d3ec6ce-2",
      "659c690185f6381e675bb1f1-2",
      "659c67d485f6381e675bb1df-2",
      "659c516f24a4aa48d5365374-1"
    ],
    "pods": [
      "run-659ead94e63dbc67d664a553-8jngh",
      "run-659eaef1e63dbc67d664a567-c2fzf"
    ],
    "workload": 2
  },
  "sha256:f5e22a82c7fe473246531877d1964d333859730419da094f4ab43a759af30c05": {
    "size": "586742350",
    "tags": [
      "659ceb3048824d102d3ec6ce-2",
      "659c690185f6381e675bb1f1-2",
      "659c67d485f6381e675bb1df-2",
      "659c516f24a4aa48d5365374-1"
    ],
    "pods": [
      "run-659ead94e63dbc67d664a553-8jngh",
      "run-659eaef1e63dbc67d664a567-c2fzf"
    ],
    "workload": 2
  },
  "sha256:5911bbbd670e6f41a8a926f7896d4b3f408088b6240fe1bb98125a844abcffd4": {
    "size": "1904480214",
    "tags": [
      "659c68c885f6381e675bb1ec-1"
    ],
    "pods": [],
    "workload": 0
  },
  "sha256:d3495407ffa1407909cd37d5b1f950b0ab10cbd1ae98b8bfda50888836c7c1ec": {
    "size": "1821017881",
    "tags": [
      "659c516f24a4aa48d5365376-1"
    ],
    "pods": [],
    "workload": 0
  },
  "sha256:62051b400ea83ce89418724bc97bae6a5cbc8bef804f29cc1ecabcec8a87d3f5": {
    "size": "728625256",
    "tags": [
      "659c797b85f6381e675bb1f8-4",
      "659d755348824d102d3ec6df-v1-202419163323_HfGeD8Ia"
    ],
    "pods": [
      "model-659d755348824d102d3ec6e0-6d67f98765-9rxnl"
    ],
    "workload": 1
  }
}
```

## Workflow

### Workload Inspection:
    The script starts by inspecting the workload information from the Kubernetes cluster using the inspect-workload.py script.

### Image Analysis:
    It then proceeds to analyze each specified container image from the given registry. The Skopeo tool is used to gather layer information, including sizes and associated tags.

### Merging and Sorting:
    The script merges the obtained image data based on layer IDs and sorts the results based on layer size.

### Report Generation:
    The final results are presented in a tabulated format, and both console and JSON reports are saved to files.

### Combining between Images and Workload Information:
    The script reads the workload information obtained earlier and combines it with the image analysis results. The merged data is saved in a final JSON file.

## Output

    Console Report: images-report.txt and workload-report.txt
    JSON Report: images-report.json and workload-report.json
    Final Report (combination of images and workload): final-report.json
