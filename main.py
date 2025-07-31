import argparse
import subprocess
import sys
import logging
import os
import json
from python.config_manager import config_manager
from typing import List

def setup_logging():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_script_paths():
    return {
        "extract_metadata": "metadata_extraction/extract-metadata.py",
        "image_data_analysis": "python/image-data-analysis.py",
        "inspect_workload": "python/inspect-workload.py",
        "security_scanning": "python/security-scanning.py",
        "delete_image": "python/delete-image.py",
        "reports": "python/reports.py"
    }

def get_script_descriptions():
    return {
        "extract_metadata": "Extract metadata from MongoDB",
        "image_data_analysis": "Analyze container images and generate reports",
        "inspect_workload": "Inspect Kubernetes workload and pod information",
        "security_scanning": "Scan container images for security vulnerabilities using Clair",
        "delete_image": "Delete Docker images from registry (default: dry-run)",
        "reports": "Generate reports from analysis data"
    }

def run_script(script_path, args, dry_run=False):
    """Run a script with the given arguments"""
    
    # Special handling for delete_image script
    if script_path.endswith('delete-image.py'):
        if dry_run:
            logging.info("Running delete-image.py in DRY RUN mode (default) - no images will be deleted")
            # Remove any --apply flags and ensure dry-run behavior
            args = [arg for arg in args if arg != '--apply' and arg != '--force']
        else:
            logging.info("Running delete-image.py in DELETE mode - images will be deleted")
            # Ensure --apply flag is present
            if '--apply' not in args:
                args.append('--apply')
    
    try:
        # Check if script exists
        if not os.path.exists(script_path):
            logging.error(f"Script not found: {script_path}")
            sys.exit(1)
            
        logging.info(f"Running script: {script_path}")
        logging.info(f"Arguments: {args}")
        
        subprocess.run([sys.executable, script_path] + args, check=True)
        
    except subprocess.CalledProcessError as e:
        logging.error(f"Error running script {script_path}: {e}")
        sys.exit(1)
    except FileNotFoundError as e:
        logging.error(f"Script not found: {script_path}")
        sys.exit(1)

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
                                logging.warning(f"Invalid ObjectID '{obj_id}' on line {line_num}")
                        else:
                            logging.warning(f"ObjectID '{obj_id}' on line {line_num} is not 24 characters")
        return object_ids
    except FileNotFoundError:
        logging.error(f"File '{file_path}' not found")
        return []
    except Exception as e:
        logging.error(f"Error reading file '{file_path}': {e}")
        return []

def validate_script_requirements(script_keyword, args):
    """Validate required arguments for specific scripts"""
    
    if script_keyword == "image_data_analysis":
        # Check if registry URL and repository name are provided, otherwise use config defaults
        has_registry_url = any('--registry-url' in arg for arg in args)
        has_repository_name = any('--repository-name' in arg for arg in args)
        
        if not has_registry_url and not has_repository_name:
            # Use config defaults
            registry_url = config_manager.get_registry_url()
            repository_name = config_manager.get_repository_name()
            args.extend(['--registry-url', registry_url, '--repository-name', repository_name])
            logging.info(f"Using config defaults: registry-url={registry_url}, repository-name={repository_name}")
        elif not has_registry_url or not has_repository_name:
            logging.error("Both --registry-url and --repository-name are required for image_data_analysis.")
            logging.error("Example: main.py image_data_analysis --registry-url <url> --repository-name <name>")
            logging.error("Or configure defaults in config.yaml")
            sys.exit(1)
    
    elif script_keyword == "inspect_workload":
        # Check if registry URL and prefix are provided, otherwise use config defaults
        has_registry_url = any('--registry-url' in arg for arg in args)
        has_prefix_to_remove = any('--prefix-to-remove' in arg for arg in args)
        
        if not has_registry_url and not has_prefix_to_remove:
            # Use config defaults
            registry_url = config_manager.get_registry_url()
            args.extend(['--registry-url', registry_url, '--prefix-to-remove', f"{registry_url}/"])
            logging.info(f"Using config defaults: registry-url={registry_url}")
        elif not has_registry_url or not has_prefix_to_remove:
            logging.error("Both --registry-url and --prefix-to-remove are required for inspect_workload.")
            logging.error("Example: main.py inspect_workload --registry-url <url> --prefix-to-remove <prefix>")
            logging.error("Or configure defaults in config.yaml")
            sys.exit(1)
    
    elif script_keyword == "delete_image":
        # Check if password is provided (either as argument or environment variable)
        has_password_arg = any(arg.startswith('--password') or not arg.startswith('-') for arg in args)
        has_env_password = config_manager.get_registry_password() is not None
        
        if not has_password_arg and not has_env_password:
            logging.warning("No password provided for delete_image. Use SKOPEO_PASSWORD environment variable or provide password as first argument.")
            logging.warning("Example: main.py delete_image <password>")
            logging.warning("Or: export SKOPEO_PASSWORD=<password> && main.py delete_image")
    
    # Validate ObjectID file if provided for supported scripts
    if script_keyword in ["image_data_analysis", "inspect_workload", "delete_image"]:
        file_arg = None
        for i, arg in enumerate(args):
            if arg == '--file' and i + 1 < len(args):
                file_arg = args[i + 1]
                break
        
        if file_arg:
            object_ids = read_object_ids_from_file(file_arg)
            if not object_ids:
                logging.error(f"No valid ObjectIDs found in file '{file_arg}'")
                sys.exit(1)
            logging.info(f"Validated ObjectIDs from file '{file_arg}': {object_ids}")

def main():
    setup_logging()
    script_paths = load_script_paths()
    script_descriptions = get_script_descriptions()

    parser = argparse.ArgumentParser(
        description="Unified entrypoint for Docker registry cleaner scripts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Available scripts:
  extract_metadata      - Extract metadata from MongoDB
  image_data_analysis   - Analyze container images and generate reports
  inspect_workload      - Inspect Kubernetes workload and pod information
  security_scanning     - Scan container images for security vulnerabilities
  delete_image          - Delete Docker images from registry (default: dry-run)
  reports               - Generate reports from analysis data

Configuration:
  The tool uses config.yaml for default settings. You can also use environment variables:
  - REGISTRY_URL: Docker registry URL
  - REPOSITORY_NAME: Repository name
  - SKOPEO_PASSWORD: Registry password
  - KUBERNETES_NAMESPACE: Kubernetes namespace
  - COMPUTE_NAMESPACE: Compute namespace

Examples:
  # Basic usage (uses config.yaml defaults)
  python main.py image_data_analysis
  python main.py inspect_workload
  python main.py delete_image mypassword

  # Override defaults
  python main.py image_data_analysis --registry-url registry.example.com --repository-name my-repo

  # Inspect workload with custom settings
  python main.py inspect_workload --registry-url registry.example.com --prefix-to-remove registry.example.com/

  # Delete images (dry run - default, safe)
  python main.py delete_image mypassword

  # Delete images (actual deletion - requires confirmation)
  python main.py delete_image mypassword --apply

  # Delete images (force deletion - no confirmation)
  python main.py delete_image mypassword --apply --force

  # Filter by ObjectIDs from file (first column contains ObjectIDs)
  python main.py image_data_analysis --file environments
  python main.py inspect_workload --file environments
  python main.py delete_image mypassword --file environments

  # Security scanning
  python main.py security_scanning --clair-url http://clair:6060 --layers-file layers.json

Safety Notes:
  - delete_image runs in dry-run mode by default for safety
  - Use --apply to actually delete images
  - Use --force to skip confirmation prompt
  - Configure defaults in config.yaml to avoid repeating common parameters
  - ObjectID filtering helps target specific models/environments for analysis/deletion
        """
    )
    
    parser.add_argument(
        'script_keyword', 
        choices=script_paths.keys(), 
        help="Script to run"
    )
    
    parser.add_argument(
        '--apply', 
        action='store_true',
        help="For delete_image: Actually delete images (default is dry-run)"
    )
    
    parser.add_argument(
        '--force', 
        action='store_true',
        help="For delete_image: Skip confirmation prompt when using --apply"
    )
    
    parser.add_argument(
        '--file',
        help="File containing ObjectIDs (first column) to filter images (for image_data_analysis, inspect_workload, delete_image)"
    )
    
    parser.add_argument(
        '--config', 
        action='store_true',
        help="Show current configuration and exit"
    )
    
    parser.add_argument(
        'additional_args', 
        nargs=argparse.REMAINDER, 
        help="Additional arguments for the script"
    )
    
    args = parser.parse_args()

    # Show configuration if requested
    if args.config:
        config_manager.print_config()
        sys.exit(0)

    # Validate script requirements
    validate_script_requirements(args.script_keyword, args.additional_args)
    
    # Get script path
    script_path = script_paths.get(args.script_keyword)
    
    if not script_path:
        logging.error(f"Unknown script: {args.script_keyword}")
        logging.error(f"Available scripts: {list(script_paths.keys())}")
        sys.exit(1)
    
    # Determine if we're in dry-run mode for delete_image
    dry_run = True  # Default to dry-run for safety
    if args.script_keyword == "delete_image":
        if args.apply:
            dry_run = False
            # Add --apply flag to additional args if not already present
            if '--apply' not in args.additional_args:
                args.additional_args.append('--apply')
            if args.force and '--force' not in args.additional_args:
                args.additional_args.append('--force')
    
    # Run the script
    run_script(script_path, args.additional_args, dry_run=dry_run)

if __name__ == '__main__':
    main()
