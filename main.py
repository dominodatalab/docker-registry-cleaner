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
        "delete_image": "Delete Docker images from registry based on environments file",
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

def validate_script_requirements(script_keyword, args):
    """Validate required arguments for specific scripts"""
    
    if script_keyword == "image_data_analysis":
        has_registry_url = any('--registry-url' in arg for arg in args)
        has_repository_name = any('--repository-name' in arg for arg in args)
        
        if not has_registry_url or not has_repository_name:
            logging.error("Both --registry-url and --repository-name are required for image_data_analysis.")
            logging.error("Example: main.py image_data_analysis --registry-url <url> --repository-name <name>")
            sys.exit(1)
    
    elif script_keyword == "inspect_workload":
        has_registry_url = any('--registry-url' in arg for arg in args)
        has_prefix_to_remove = any('--prefix-to-remove' in arg for arg in args)
        
        if not has_registry_url or not has_prefix_to_remove:
            logging.error("Both --registry-url and --prefix-to-remove are required for inspect_workload.")
            logging.error("Example: main.py inspect_workload --registry-url <url> --prefix-to-remove <prefix>")
            sys.exit(1)
    
    elif script_keyword == "delete_image":
        # Check if password is provided (either as argument or environment variable)
        has_password_arg = any(arg.startswith('--password') or not arg.startswith('-') for arg in args)
        has_env_password = os.environ.get('SKOPEO_PASSWORD') is not None
        
        if not has_password_arg and not has_env_password:
            logging.warning("No password provided for delete_image. Use SKOPEO_PASSWORD environment variable or provide password as first argument.")
            logging.warning("Example: main.py delete_image <password>")
            logging.warning("Or: export SKOPEO_PASSWORD=<password> && main.py delete_image")

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

Examples:
  # Analyze images
  python main.py image_data_analysis --registry-url registry.example.com --repository-name my-repo

  # Inspect workload
  python main.py inspect_workload --registry-url registry.example.com --prefix-to-remove registry.example.com/

  # Delete images (dry run - default, safe)
  python main.py delete_image mypassword

  # Delete images (actual deletion - requires confirmation)
  python main.py delete_image mypassword --apply

  # Delete images (force deletion - no confirmation)
  python main.py delete_image mypassword --apply --force

  # Security scanning
  python main.py security_scanning --clair-url http://clair:6060 --layers-file layers.json

Safety Notes:
  - delete_image runs in dry-run mode by default for safety
  - Use --apply to actually delete images
  - Use --force to skip confirmation prompt
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
        'additional_args', 
        nargs=argparse.REMAINDER, 
        help="Additional arguments for the script"
    )
    
    args = parser.parse_args()

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

if __name__ == "__main__":
    main()
