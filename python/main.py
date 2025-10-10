import argparse
import logging
import os
import subprocess
import sys

from typing import List

from config_manager import config_manager
from logging_utils import setup_logging


def load_script_paths():
    return {
        "delete_all_unused_environments": None,  # Special: runs multiple scripts
        "delete_archived_tags": "delete_archived_tags.py",
        "delete_image": "delete_image.py",
        "delete_unused_environments": "delete_unused_environments.py",
        "delete_unused_private_environments": "delete_unused_private_environments.py",
        "delete_unused_references": "delete_unused_references.py",
        "extract_metadata": "extract_metadata.py",
        "image_data_analysis": "image_data_analysis.py",
        "inspect_workload": "inspect_workload.py",
        "mongo_cleanup": "mongo_cleanup.py",
        "reports": "reports.py",
    }

def get_script_descriptions():
    return {
        "delete_all_unused_environments": "Run comprehensive unused environment cleanup (unused environments + deactivated user private environments)",
        "delete_archived_tags": "Find and optionally delete Docker tags associated with archived environments and/or models",
        "delete_image": "Delete Docker images from registry (default: dry-run)",
        "delete_unused_environments": "Find and optionally delete environments not used in workspaces, models, or project defaults (auto-generates reports)",
        "delete_unused_private_environments": "Find and optionally delete private environments owned by deactivated Keycloak users",
        "delete_unused_references": "Find and optionally delete MongoDB references to non-existent Docker images",
        "extract_metadata": "Extract metadata from MongoDB",
        "image_data_analysis": "Analyze container images and generate reports",
        "inspect_workload": "Inspect Kubernetes workload and pod information",
        "mongo_cleanup": "Simple tag/ObjectID-based Mongo cleanup (consider using delete_unused_references for advanced features)",
        "reports": "Generate tag usage reports from analysis data (auto-generates metadata)",
    }

def run_script(script_path, args, dry_run=True):
    """Run a script with the given arguments"""
    
    # Special handling for delete_image script
    if script_path.endswith('delete_image.py'):
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
    """Read ObjectIDs from a file, one per line (comments starting with # are ignored)."""
    object_ids = []
    try:
        with open(file_path, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith('#'):  # Skip empty lines and comments
                    obj_id = line
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
        has_repository = any('--repository' in arg for arg in args)
        
        if not has_registry_url and not has_repository:
            # Use config defaults
            registry_url = config_manager.get_registry_url()
            repository = config_manager.get_repository()
            args.extend(['--registry-url', registry_url, '--repository', repository])
            logging.info(f"Using config defaults: registry-url={registry_url}, repository={repository}")
        elif not has_registry_url or not has_repository:
            logging.error("Both --registry-url and --repository are required for image_data_analysis.")
            logging.error("Example: main.py image_data_analysis --registry-url <url> --repository <name>")
            logging.error("Or configure defaults in config.yaml")
            sys.exit(1)
    
    elif script_keyword == "inspect_workload":
        # inspect_workload.py now handles config defaults internally
        # No validation needed - it will use config values if args not provided
        pass
    
    elif script_keyword == "mongo_cleanup":
        # Check if required arguments are provided
        has_file = any('--file' in arg for arg in args)
        
        if not has_file:
            logging.error("mongo_cleanup requires --file argument with path to tags/ObjectIDs file")
            logging.error("Example: main.py mongo_cleanup --file environments")
            logging.error("         main.py mongo_cleanup --apply --file environments")
            sys.exit(1)
    
    elif script_keyword == "delete_image":
        # Check if password is provided (argument, env var, or config)
        has_password_arg = any(arg.startswith('--password') or not arg.startswith('-') for arg in args)
        has_password = config_manager.get_registry_password() is not None
        
        # Check if using ECR (which doesn't need a password)
        registry_url = config_manager.get_registry_url()
        is_ecr = '.amazonaws.com' in registry_url if registry_url else False
        
        if not has_password_arg and not has_password and not is_ecr:
            logging.warning("No password provided for delete_image.")
            logging.warning("Options:")
            logging.warning("  1. Add to config.yaml: registry.password: <password>")
            logging.warning("  2. Set REGISTRY_PASSWORD environment variable: export REGISTRY_PASSWORD=<password>")
            logging.warning("  3. Provide password as argument: main.py delete_image <password>")
            logging.warning("  4. For ECR registries, authentication is automatic (no password needed)")
    
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
  delete_all_unused_environments     - Run comprehensive unused environment cleanup (unused environments + deactivated user private environments)
  delete_archived_tags               - Find and optionally delete Docker tags associated with archived environments and/or models
  delete_image                       - Delete Docker images from registry (default: dry-run)
  delete_unused_environments         - Find and optionally delete environments not used in workspaces, models, or project defaults (auto-generates reports)
  delete_unused_private_environments - Find and optionally delete private environments owned by deactivated Keycloak users
  delete_unused_references           - Find and optionally delete MongoDB references to non-existent Docker images
  extract_metadata                   - Extract metadata from MongoDB
  image_data_analysis                - Analyze container images and generate reports
  inspect_workload                   - Inspect Kubernetes workload and pod information
  mongo_cleanup                      - Simple tag/ObjectID-based Mongo cleanup
  reports                            - Generate tag usage reports from analysis data (auto-generates metadata)

Configuration:
  The tool uses config.yaml for default settings. You can also use environment variables:
  - REGISTRY_URL: Docker registry URL
  - REPOSITORY: Repository name
  - REGISTRY_PASSWORD: Registry password
  - PLATFORM_NAMESPACE: Domino platform namespace
  - COMPUTE_NAMESPACE: Compute namespace

Examples:
  # Basic usage (uses config.yaml defaults)
  python main.py image_data_analysis
  python main.py inspect_workload
  python main.py delete_image [password]  # password optional if REGISTRY_PASSWORD is set
  python main.py delete_archived_tags --environment --output archived-tags.json

  # Override defaults
  python main.py image_data_analysis --registry-url registry.example.com --repository my-repo
  python main.py inspect_workload --registry-url registry.example.com

  # Delete images (dry run - default, safe) - password optional
  python main.py delete_image

  # Delete images (actual deletion - requires confirmation)
  python main.py delete_image --apply

  # Delete images (force deletion - no confirmation) - explicit password
  python main.py delete_image <password> --apply --force

  # Filter by ObjectIDs from file (first column contains ObjectIDs)
  python main.py image_data_analysis --file environments
  python main.py inspect_workload --file environments
  python main.py delete_image --file environments

  # Mongo cleanup
  python main.py mongo_cleanup --file environments
  python main.py mongo_cleanup --apply --file environments --collection environment_revisions
  
  # Generate tag usage reports (auto-generates metadata if missing)
  python main.py reports
  
  # Force regeneration of metadata before generating reports
  python main.py reports --generate-reports
  
  # Find archived environment tags (dry-run)
  python main.py delete_archived_tags --environment --output archived-tags.json
  
  # Find archived model tags (dry-run)
  python main.py delete_archived_tags --model --output archived-model-tags.json
  
  # Find both archived environments and models
  python main.py delete_archived_tags --environment --model --output archived-tags.json
  
  # Delete archived environment tags
  python main.py delete_archived_tags --environment --apply
  
  # Delete archived model tags
  python main.py delete_archived_tags --model --apply
  
  # Delete both with backup to S3
  python main.py delete_archived_tags --environment --model --apply --backup --s3-bucket my-bucket

  # Find unused references (MongoDB references to non-existent Docker images)
  python main.py delete_unused_references --output unused-refs.json

  # Delete unused references (requires --apply flag)
  python main.py delete_unused_references --apply

  # Delete unused references from pre-generated file
  python main.py delete_unused_references --apply --input unused-refs.json

  # Find private environments owned by deactivated Keycloak users (dry-run)
   python main.py delete_unused_private_environments --output deactivated-user-envs.json

  # Delete private environments owned by deactivated Keycloak users
   python main.py delete_unused_private_environments --apply

  # Delete from pre-generated file
   python main.py delete_unused_private_environments --apply --input deactivated-user-envs.json

  # Find unused environments (auto-generates required reports if missing)
   python main.py delete_unused_environments
   
  # Force regeneration of metadata reports before analysis
   python main.py delete_unused_environments --generate-reports

  # Delete unused environments (with confirmation)
   python main.py delete_unused_environments --apply

  # Full workflow: generate reports and delete
   python main.py delete_unused_environments --generate-reports --apply
   
  # Delete from pre-generated file
   python main.py delete_unused_environments --apply --input unused-envs.json
  
  # Comprehensive unused environment cleanup - analyze (dry-run)
   python main.py delete_all_unused_environments
  
  # Comprehensive cleanup - delete (requires --apply)
   python main.py delete_all_unused_environments --apply
  
  # Comprehensive cleanup with backup
   python main.py delete_all_unused_environments --apply --backup --s3-bucket my-backup-bucket

Backup Examples (all delete scripts support backup to S3 before deletion):
  # Backup images to S3 before deleting archived tags
  python main.py delete_archived_tags --environment --apply --backup --s3-bucket my-backup-bucket
   
  # Backup with custom region
  python main.py delete_archived_tags --model --apply --backup --s3-bucket my-backup-bucket --region us-east-1
   
  # Backup before deleting unused environments
  python main.py delete_unused_environments --apply --backup --s3-bucket my-backup-bucket
   
  # Backup before deleting private environments of deactivated users
  python main.py delete_unused_private_environments --apply --backup --s3-bucket my-backup-bucket
   
  # Backup before deleting unused Docker images
  python main.py delete_image --apply --backup --s3-bucket my-backup-bucket

Safety Notes:
  - delete_image runs in dry-run mode by default for safety
  - Use --apply to actually delete images
  - Use --force to skip confirmation prompt
  - Use --backup and --s3-bucket to backup images to S3 before deletion (all delete scripts)
  - Backup will abort deletion if backup fails to prevent data loss
  - Configure defaults in config.yaml to avoid repeating common parameters
  - ObjectID filtering helps target specific models/environments for analysis/deletion
        """
    )
    
    parser.add_argument(
        'script_keyword', 
        nargs='?',
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
        help="File containing ObjectIDs (one per line) to filter images (for image_data_analysis, inspect_workload, delete_image)"
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

    # If no script determined, show help
    if not args.script_keyword:
        parser.print_help()
        sys.exit(1)

    # Special handling for delete_all_unused_environments (runs multiple scripts)
    if args.script_keyword == 'delete_all_unused_environments':
        # Run both unused environment scripts sequentially
        is_apply = '--apply' in args.additional_args
        mode = "deletion" if is_apply else "analysis (dry-run)"
        logging.info(f"Running comprehensive unused environment cleanup in {mode} mode (2 scripts)...")
        
        # Build script paths
        script_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Run delete_unused_environments first
        logging.info("\n" + "="*60)
        logging.info("Step 1/2: Unused environments (not in workspaces/models/defaults)")
        logging.info("="*60)
        unused_envs_script = os.path.join(script_dir, 'delete_unused_environments.py')
        run_script(unused_envs_script, args.additional_args, dry_run=True)
        
        # Run delete_unused_private_environments second
        logging.info("\n" + "="*60)
        logging.info("Step 2/2: Private environments of deactivated users")
        logging.info("="*60)
        private_envs_script = os.path.join(script_dir, 'delete_unused_private_environments.py')
        run_script(private_envs_script, args.additional_args, dry_run=True)
        
        logging.info("\n" + "="*60)
        if is_apply:
            logging.info("✅ Comprehensive unused environment cleanup completed!")
        else:
            logging.info("✅ Comprehensive unused environment analysis completed!")
            logging.info("   Use --apply to actually delete the identified environments")
        logging.info("="*60)
        sys.exit(0)

    # Validate script requirements
    validate_script_requirements(args.script_keyword, args.additional_args)
    
    # Get script path
    script_filename = script_paths.get(args.script_keyword)
    
    if not script_filename:
        logging.error(f"Unknown script: {args.script_keyword}")
        logging.error(f"Available scripts: {list(script_paths.keys())}")
        sys.exit(1)
    
    # Build full path to script (in same directory as main.py)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, script_filename)
    
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
