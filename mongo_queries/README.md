# Metadata Extraction Tool for MongoDB

## Overview
This project consists of a Python script and JavaScript files designed to extract metadata from MongoDB within a Kubernetes environment. It handles different types of MongoDB scripts, focusing on model and workspace data extraction.

## Files
- `extract-metadata.py`: The main Python script that orchestrates the execution of MongoDB scripts.
- `model_env_usage.js`: JavaScript file for extracting model-related metadata from MongoDB.
- `workspace_env_usage.js`: JavaScript file for extracting workspace-related metadata from MongoDB.
- `mongo.js`: General-purpose MongoDB script for additional data extraction tasks.

## Features
- Supports different types of MongoDB extractions (model and workspace).
- Enhanced error handling and logging for better debugging and output clarity.
- Compatibility with Kubernetes environments for executing MongoDB scripts.

## Requirements
- Python 3.x
- Access to a Kubernetes cluster running Domino.
- Necessary permissions to execute `kubectl` commands and access MongoDB.

## Setup and Usage
1. **Setting up the Environment**: Ensure that Python 3 is installed on your system and you have access to the Kubernetes cluster where MongoDB is hosted.

2. **Running the Script**:
   - To extract model-related data:
     ```
     python3 extract-metadata.py model
     ```
   - To extract workspace-related data:
     ```
     python3 extract-metadata.py workspace
     ```
   - To run your own query:
     ```
     python3 extract-metadata.py yourfile.js
     ```
   - To extract both types of data:
     ```
     python3 extract-metadata.py
     ```


## Output
The script generates JSON files with extracted data:
- `model_output.json` for model-related data.
- `workspace_output.json` for workspace-related data.

## Customization
You can modify the JavaScript files (`model_env_usage.js` and `workspace_env_usage.js`) to adjust the data extraction queries as per your requirements.

## Contribution
Contributions to enhance the functionality or efficiency of this tool are welcome. Please follow the standard Git workflow for contributions.

## Contact
ben.wolstenholme@dominodatalab.com

