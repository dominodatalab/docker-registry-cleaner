import os
import time
import json
import subprocess
import sys
import yaml
import argparse
from kubernetes import client, config

file_path = "environments"

try:
    file_size = os.path.getsize(file_path)
    with open(file_path, 'r') as file:
        if file_size == 0:
            print("Environments file is empty.")
            exit(1)
        else:
            print("Reading environments file...")
except FileNotFoundError as e:
    print("Environments file not found")
    exit(1)

# Configuration
docker_registry_name = "docker-registry"
env_to_enable_deletion = "REGISTRY_STORAGE_DELETE_ENABLED"
skopeo_pod_name = "skopeo"
skopeo_namespace = "domino-platform"

class DockerPods:
    def __init__(self):
        self.namespace: str = ""
        self.name = ""


def create_skopeo_pod():
    """Create the Skopeo pod for Docker registry operations"""
    print("Creating Skopeo pod")
    
    # Read the pod.yaml file
    with open("pod.yaml", "r") as f:
        pod_data = yaml.safe_load(f)
    
    # Create the pod using Kubernetes client
    try:
        core_v1_client.create_namespaced_pod(
            namespace=skopeo_namespace,
            body=pod_data
        )
        print(f"Created pod {skopeo_pod_name} in namespace {skopeo_namespace}")
    except client.exceptions.ApiException as e:
        if e.status == 409:  # Already exists
            print(f"Pod {skopeo_pod_name} already exists")
        else:
            raise
    
    # Wait for the pod to be ready
    print(f"Waiting for pod {skopeo_pod_name} to be ready...")
    wait_for_pod_ready(skopeo_namespace, skopeo_pod_name)
    print("Skopeo pod is ready")


def delete_skopeo_pod():
    """Delete the Skopeo pod"""
    print("Deleting Skopeo pod")
    try:
        core_v1_client.delete_namespaced_pod(
            name=skopeo_pod_name,
            namespace=skopeo_namespace
        )
        print(f"Deleted pod {skopeo_pod_name}")
    except client.exceptions.ApiException as e:
        if e.status == 404:  # Not found
            print(f"Pod {skopeo_pod_name} not found")
        else:
            raise


def wait_for_pod_ready(namespace, name, timeout=300, interval=10):
    """Wait for a pod to be ready"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            pod = core_v1_client.read_namespaced_pod(name=name, namespace=namespace)
            if pod.status.phase == "Running":
                # Check if all containers are ready
                ready = all(
                    container.ready for container in pod.status.container_statuses or []
                )
                if ready:
                    print(f"Pod {name} is ready")
                    return
            print(f"Waiting for pod {name} to be ready...")
            time.sleep(interval)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                print(f"Pod {name} not found, waiting...")
                time.sleep(interval)
            else:
                raise
    raise TimeoutError(f"Timeout waiting for pod {name} to be ready")


def get_skopeo_password():
    """Get the password for Skopeo operations"""
    if len(sys.argv) > 1:
        return sys.argv[1]
    else:
        return os.environ.get('SKOPEO_PASSWORD')


def run_skopeo_command(cmd_args):
    """Run a skopeo command in the pod"""
    full_cmd = ["kubectl", "exec", "-i", "-n", skopeo_namespace, skopeo_pod_name, "--", "skopeo"] + cmd_args
    result = subprocess.run(full_cmd, capture_output=True, text=True, check=True)
    return result.stdout


def get_docker_tags(repository, password):
    """Get all Docker tags for a repository"""
    cmd = [
        "list-tags", "--tls-verify=false", 
        f"--creds", f"domino-registry:{password}",
        f"docker://docker-registry:5000/{repository}"
    ]
    
    try:
        output = run_skopeo_command(cmd)
        tags_data = json.loads(output)
        return tags_data.get('Tags', [])
    except subprocess.CalledProcessError as e:
        print(f"Warning: Could not list tags for {repository}: {e}")
        return []


def process_environments_file(password):
    """Process the environments file and determine which tags to delete"""
    print("Processing environments file...")
    
    # Create directories for tag management
    os.makedirs("tags/done", exist_ok=True)
    
    # Get all tags for dominodatalab/environment
    print("Getting tags for dominodatalab/environment...")
    dominodatalab_tags = get_docker_tags("dominodatalab/environment", password)
    
    # Write tags to file for processing
    with open("dominodatalab_environment_tags", "w") as f:
        for tag in dominodatalab_tags:
            f.write(f"{tag}\n")
    
    revisions_to_delete = []
    
    # Process each line in environments file
    with open(file_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
                
            parts = line.split()
            if not parts:
                continue
                
            environment_id = parts[0]
            revisions_to_keep = parts[1:] if len(parts) > 1 else []
            
            print(f"Checking /dominodatalab/environment:{environment_id}-*")
            
            # Find all revisions of this environment
            matching_tags = [tag for tag in dominodatalab_tags if tag.startswith(f"{environment_id}-")]
            revisions_to_delete.extend(matching_tags)
            
            # Remove tags that should be kept
            for revision in revisions_to_keep:
                tag_to_keep = f"{environment_id}-{revision}"
                if tag_to_keep in revisions_to_delete:
                    print(f"Found tag {tag_to_keep} in Environments file - keeping")
                    revisions_to_delete.remove(tag_to_keep)
    
    # Write revisions to delete
    with open("revisions_to_delete", "w") as f:
        for revision in revisions_to_delete:
            f.write(f"{revision}\n")
    
    # Process domino-<environmentId> repositories
    with open(file_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
                
            parts = line.split()
            if not parts:
                continue
                
            environment_id = parts[0]
            revisions_to_keep = parts[1:] if len(parts) > 1 else []
            
            print(f"Checking domino-{environment_id}")
            
            # Get tags for this environment
            domino_tags = get_docker_tags(f"domino-{environment_id}", password)
            
            if domino_tags:
                # Write tags to file
                with open(f"tags/domino-{environment_id}", "w") as f:
                    for tag in domino_tags:
                        f.write(f"{tag}\n")
                
                # Remove tags that should be kept
                for revision in revisions_to_keep:
                    if revision in domino_tags:
                        print(f"{revision} matches a revision to retain - removing from file")
                        # Remove the revision from the file
                        with open(f"tags/domino-{environment_id}", "r") as f:
                            lines = f.readlines()
                        with open(f"tags/domino-{environment_id}", "w") as f:
                            for line in lines:
                                if line.strip() != revision:
                                    f.write(line)
                
                # Remove buildcache tag
                with open(f"tags/domino-{environment_id}", "r") as f:
                    lines = f.readlines()
                with open(f"tags/domino-{environment_id}", "w") as f:
                    for line in lines:
                        if line.strip() != "buildcache":
                            f.write(line)


def delete_images(password, dry_run=False):
    """Delete the images based on the processed files"""
    if dry_run:
        print("DRY RUN MODE: No images will be deleted")
    else:
        print("DELETE MODE: Images will be deleted")
    
    print("Starting image deletion...")
    
    # Delete revisions from dominodatalab/environment
    if os.path.exists("revisions_to_delete"):
        with open("revisions_to_delete", "r") as f:
            for line in f:
                revision = line.strip()
                if revision:
                    if dry_run:
                        print(f"[DRY RUN] Would delete docker-registry:5000/dominodatalab/environment:{revision}")
                    else:
                        print(f"Deleting docker-registry:5000/dominodatalab/environment:{revision}")
                        run_skopeo_command([
                            "delete", "--tls-verify=false",
                            f"--creds", f"domino-registry:{password}",
                            f"docker://docker-registry:5000/dominodatalab/environment:{revision}"
                        ])
    
    # Delete revisions from domino-<environmentId>
    with open(file_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
                
            parts = line.split()
            if not parts:
                continue
                
            environment_id = parts[0]
            tag_file = f"tags/domino-{environment_id}"
            
            if os.path.exists(tag_file):
                with open(tag_file, "r") as tag_f:
                    for tag_line in tag_f:
                        revision = tag_line.strip()
                        if revision:
                            if dry_run:
                                print(f"[DRY RUN] Would delete docker-registry:5000/domino-{environment_id}:{revision}")
                            else:
                                print(f"Deleting docker-registry:5000/domino-{environment_id}:{revision}")
                                run_skopeo_command([
                                    "delete", "--tls-verify=false",
                                    f"--creds", f"domino-registry:{password}",
                                    f"docker://docker-registry:5000/domino-{environment_id}:{revision}"
                                ])
                
                # Move file to done folder
                os.rename(tag_file, f"tags/done/domino-{environment_id}")


def find_docker_registry_pod():
    if sts.namespace == "":
        for ns in core_v1_client.list_namespace(label_selector='domino-platform = true').items:
            sts.namespace = ns.metadata.name
            print(f"Domino platform namespace: {sts.namespace}")
            break
    if sts.namespace == "":
        print("Domino platform namespace not found")
        exit(1)

    for p in core_v1_client.list_namespaced_pod(namespace=sts.namespace, label_selector=f"app.kubernetes.io/name = {docker_registry_name}").items:
        sts.name = p.metadata.name
    if not sts.name:
        print("Docker registry pod not found")
        exit(1)
    print(f"Docker registry pod found: {sts.name}")


def wait_for_docker_ready(namespace, name, timeout=300, interval=10):
    start_time = time.time()
    while time.time() - start_time < timeout:
        pods = core_v1_client.list_namespaced_pod(namespace, name=name).items
        ready = all(
            pod.status.phase == "Running" and
            all(cond.type == "Ready" and cond.status == "True" for cond in pod.status.conditions or [])
            for pod in pods
        )
        if ready:
            print("Pod is ready to proceed")
            return
        print("Waiting for pod to be ready...")
        time.sleep(interval)
    raise TimeoutError(f"Timeout waiting for pod {name} to be ready.")


def enable_deletion_of_docker_images():
    """Enable deletion of Docker images"""
    print("Enabling deletion of Docker images")
    
    # Get the current StatefulSet
    try:
        sts_data = apps_v1_client.read_namespaced_stateful_set(
            name=docker_registry_name, 
            namespace="domino-platform"
        )
    except client.exceptions.ApiException as e:
        print(f"Error reading StatefulSet: {e}")
        return
    
    # Check if the environment variable already exists
    env_exists = False
    for env in sts_data.spec.template.spec.containers[0].env or []:
        if env.name == env_to_enable_deletion:
            env.value = "true"
            env_exists = True
            break
    
    # Add the environment variable if it doesn't exist
    if not env_exists:
        new_env = client.V1EnvVar(name=env_to_enable_deletion, value="true")
        if sts_data.spec.template.spec.containers[0].env is None:
            sts_data.spec.template.spec.containers[0].env = []
        sts_data.spec.template.spec.containers[0].env.append(new_env)
    
    # Update the StatefulSet
    try:
        apps_v1_client.patch_namespaced_stateful_set(
            name=docker_registry_name,
            namespace="domino-platform",
            body=sts_data
        )
        print("Updated StatefulSet with deletion enabled")
    except client.exceptions.ApiException as e:
        print(f"Error updating StatefulSet: {e}")
        return
    
    # Wait for the pod to be ready
    wait_for_pod_ready("domino-platform", "docker-registry-0")


def disable_deletion_of_docker_images():
    """Disable deletion of Docker images"""
    print("Disabling deletion of Docker images")
    
    # Get the current StatefulSet
    try:
        sts_data = apps_v1_client.read_namespaced_stateful_set(
            name=docker_registry_name, 
            namespace="domino-platform"
        )
    except client.exceptions.ApiException as e:
        print(f"Error reading StatefulSet: {e}")
        return
    
    # Remove the environment variable
    if sts_data.spec.template.spec.containers[0].env:
        sts_data.spec.template.spec.containers[0].env = [
            env for env in sts_data.spec.template.spec.containers[0].env 
            if env.name != env_to_enable_deletion
        ]
    
    # Update the StatefulSet
    try:
        apps_v1_client.patch_namespaced_stateful_set(
            name=docker_registry_name,
            namespace="domino-platform",
            body=sts_data
        )
        print("Updated StatefulSet with deletion disabled")
    except client.exceptions.ApiException as e:
        print(f"Error updating StatefulSet: {e}")
        return
    
    # Wait for the pod to be ready
    wait_for_pod_ready("domino-platform", "docker-registry-0")


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="Delete Docker images from registry")
    parser.add_argument("password", nargs="?", help="Password for registry access")
    parser.add_argument("--apply", action="store_true", help="Actually apply changes and delete images (default is dry-run)")
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt when using --apply")
    return parser.parse_args()


def confirm_deletion():
    """Ask for user confirmation before deleting images"""
    print("\n" + "="*60)
    print("⚠️  WARNING: You are about to DELETE Docker images from the registry!")
    print("="*60)
    print("This action cannot be undone.")
    print("Make sure you have reviewed the dry-run output above.")
    print("="*60)
    
    while True:
        response = input("Are you sure you want to proceed with deletion? (yes/no): ").lower().strip()
        if response in ['yes', 'y']:
            return True
        elif response in ['no', 'n']:
            return False
        else:
            print("Please enter 'yes' or 'no'.")


def main():
    # Parse arguments
    args = parse_arguments()
    
    # Get password
    password = args.password or get_skopeo_password()
    if not password:
        print("Error: No password provided. Use SKOPEO_PASSWORD environment variable or provide as argument.")
        exit(1)
    
    # Default to dry-run unless --apply is specified
    dry_run = not args.apply
    
    if dry_run:
        print("🔍 DRY RUN MODE (default)")
        print("Images will NOT be deleted. Use --apply to actually delete images.")
    else:
        print("🗑️  DELETE MODE")
        print("Images WILL be deleted!")
        
        # Require confirmation unless --force is used
        if not args.force:
            if not confirm_deletion():
                print("Deletion cancelled by user.")
                exit(0)
        else:
            print("⚠️  Force mode enabled - skipping confirmation prompt")
    
    try:
        # Create Skopeo pod
        create_skopeo_pod()
        
        # Process environments and determine what to delete
        process_environments_file(password)
        
        # Enable deletion (even in dry-run mode to test the process)
        enable_deletion_of_docker_images()
        
        # Delete images
        delete_images(password, dry_run=dry_run)
        
        # Disable deletion
        disable_deletion_of_docker_images()
        
        if dry_run:
            print("\n✅ DRY RUN COMPLETED")
            print("No images were deleted.")
            print("To actually delete images, run with --apply flag:")
            print("  python delete-image.py <password> --apply")
        else:
            print("\n✅ DELETION COMPLETED")
            print("Images have been deleted from the registry.")
        
    finally:
        # Clean up Skopeo pod
        delete_skopeo_pod()


if __name__ == '__main__':
    config.load_kube_config()
    apps_v1_client = client.AppsV1Api()
    core_v1_client = client.CoreV1Api()

    sts = DockerPods()
    sts.namespace = "domino-platform"

    main()