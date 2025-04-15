import argparse
import subprocess
import sys
import logging
import os
import json

def setup_logging():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_script_paths():
    return {
        "extract_metadata": "metadata_extraction/extract-metadata.py",
        "image_data_analysis": "python/image-data-analysis.py",
        "inspect_workload": "python/inspect-workload.py",
        "security_scanning": "python/security-scanning.py"
    }

def run_script(script_path, args):
    if script_path.endswith('image-data-analysis.py') and ('--registry-url' not in args or '--repository-name' not in args):
        logging.error("Both --registry-url and --repository-name are required for image_data_analysis.")
        sys.exit(1)
    try:
        subprocess.run([sys.executable, script_path] + args, check=True)
    except subprocess.CalledProcessError as e:
        logging.error(f"Error running script {script_path}: {e}", exc_info=True)
        sys.exit(1)

def main():
    setup_logging()
    script_paths = load_script_paths()

    parser = argparse.ArgumentParser(description="Script runner for various tasks")
    parser.add_argument('script_keyword', choices=script_paths.keys(), help="Script to run")
    parser.add_argument('additional_args', nargs=argparse.REMAINDER, help="Additional arguments for the script")
    args = parser.parse_args()

    script_path = script_paths.get(args.script_keyword)
    run_script(script_path, args.additional_args)

if __name__ == "__main__":
    main()
