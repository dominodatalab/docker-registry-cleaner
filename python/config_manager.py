#!/usr/bin/env python3
"""
Configuration Manager for Docker Registry Cleaner

This module handles loading and managing configuration from config.yaml
and environment variables.
"""

import os
import yaml
import logging
from typing import Dict, Any, Optional, List
import subprocess
import json
import base64
from kubernetes import client


class ConfigManager:
    """Manages configuration for the Docker registry cleaner project"""
    
    def __init__(self, config_file: str = "../config.yaml"):
        self.config_file = config_file
        self.config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file with defaults"""
        default_config = {
            'registry': {
                'url': 'docker-registry:5000',
                'repository': 'dominodatalab'
            },
            'kubernetes': {
                'platform_namespace': 'domino-platform',
                'compute_namespace': 'domino-compute',
                'pod_prefixes': ['model-', 'run-']
            },
            'mongo': {
                'host': 'mongodb-replicaset',
                'port': 27017,
                'replicaset': 'rs0',
                'db': 'domino',
                'collection': 'environment_revisions'
            },
            'analysis': {
                'max_workers': 4,
                'timeout': 300,
                'output_dir': 'reports'
            },
            'reports': {
                'archived_tags': 'archived-tags.json',
                'deletion_analysis': 'deletion-analysis.json',
                'filtered_layers': 'filtered-layers.json',
                'image_analysis': 'final-report.json',
                'images_report': 'images-report',
                'layers_and_sizes': 'layers-and-sizes.json',
                'tags_per_layer': 'tags-per-layer.json',
                'tag_sums': 'tag-sums.json',
                'workload_report': 'workload-report.json'
            },
            'security': {
                'dry_run_by_default': True,
                'require_confirmation': True
            }
        }
        
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    user_config = yaml.safe_load(f) or {}
                return self._merge_config(default_config, user_config)
            else:
                logging.warning(f"Config file {self.config_file} not found, using defaults")
                return default_config
        except Exception as e:
            logging.error(f"Error loading config file: {e}")
            return default_config
    
    def _merge_config(self, default: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively merge user config with defaults"""
        result = default.copy()
        for key, value in user.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
        return result
    
    # Registry configuration
    def get_registry_url(self) -> str:
        """Get registry URL from environment or config"""
        return os.environ.get('REGISTRY_URL') or self.config['registry']['url']
    
    def get_repository(self) -> str:
        """Get canonical repository value.
        Priority: env REPOSITORY -> config.registry.repository -> config.registry.repository_name
        """
        return (
            os.environ.get('REPOSITORY')
            or self.config['registry']['repository']
        )
    
    def get_registry_password(self) -> Optional[str]:
        """Get registry password from environment or handle ECR authentication"""
        password = os.environ.get('REGISTRY_PASSWORD')
        
        # If password is provided, return it
        if password:
            return password
        
        # If no password and registry is ECR, handle authentication
        registry_url = self.get_registry_url()
        if "amazonaws.com" in registry_url:
            self._authenticate_ecr(registry_url)
            # After authentication, we don't need to return a password
            # as skopeo will use the authenticated session
            return None
        
        return None
    
    def _authenticate_ecr(self, registry_url: str) -> None:
        """Authenticate with ECR using AWS CLI"""
        try:
            # Extract region from registry URL
            # ECR URLs are typically: account.dkr.ecr.region.amazonaws.com
            parts = registry_url.split('.')
            if len(parts) >= 4 and parts[-2] == 'amazonaws' and parts[-1] == 'com':
                region = parts[-3]  # Extract region from URL
            else:
                # Fallback: try to get region from AWS_DEFAULT_REGION env var
                region = os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
            
            logging.info(f"Authenticating with ECR in region: {region}")
            
            # Run ECR authentication command
            subprocess.run(
                [
                    "bash",
                    "-c",
                    f"aws ecr get-login-password --region {region} | "
                    f"skopeo login --username AWS --password-stdin {registry_url}",
                ],
                check=True,
            )
            logging.info("ECR authentication successful")
            
        except subprocess.CalledProcessError as e:
            logging.error(f"ECR authentication failed: {e}")
            raise
        except Exception as e:
            logging.error(f"Unexpected error during ECR authentication: {e}")
            raise
    
    # Kubernetes configuration
    def get_platform_namespace(self) -> str:
        """Get Domino Platform namespace from environment or config"""
        return os.environ.get('PLATFORM_NAMESPACE') or self.config['kubernetes']['platform_namespace']
    
    def get_compute_namespace(self) -> str:
        """Get Domino Compute namespace from environment or config"""
        return os.environ.get('COMPUTE_NAMESPACE') or self.config['kubernetes']['compute_namespace']
    
    def get_pod_prefixes(self) -> List[str]:
        """Get pod prefixes from config"""
        return self.config['kubernetes']['pod_prefixes']
    
    # Analysis configuration
    def get_max_workers(self) -> int:
        """Get max workers from config"""
        return self.config['analysis']['max_workers']
    
    def get_timeout(self) -> int:
        """Get timeout from config"""
        return self.config['analysis']['timeout']
    
    def get_output_dir(self) -> str:
        """Get output directory from config"""
        return self.config['analysis']['output_dir']
    
    # Report configuration
    def _resolve_report_path(self, path: str) -> str:
        """Resolve report file path under the configured output_dir unless absolute or already a path.
        If the value is just a filename, prefix it with output_dir.
        """
        # If absolute or contains a directory component, return as is
        if os.path.isabs(path) or os.path.basename(path) != path:
            return path
        return os.path.join(self.get_output_dir(), path)

    def get_workload_report_path(self) -> str:
        """Get workload report path from config"""
        return self._resolve_report_path(self.config['reports']['workload_report'])
    
    def get_image_analysis_path(self) -> str:
        """Get image analysis path from config"""
        return self._resolve_report_path(self.config['reports']['image_analysis'])
    
    def get_deletion_analysis_path(self) -> str:
        """Get deletion analysis path from config"""
        return self._resolve_report_path(self.config['reports']['deletion_analysis'])
    
    def get_tags_per_layer_path(self) -> str:
        """Get tags per layer report path from config"""
        return self._resolve_report_path(self.config['reports']['tags_per_layer'])
    
    def get_layers_and_sizes_path(self) -> str:
        """Get layers and sizes report path from config"""
        return self._resolve_report_path(self.config['reports']['layers_and_sizes'])
    
    def get_filtered_layers_path(self) -> str:
        """Get filtered layers report path from config"""
        return self._resolve_report_path(self.config['reports']['filtered_layers'])
    
    def get_tag_sums_path(self) -> str:
        """Get tag sums report path from config"""
        return self._resolve_report_path(self.config['reports']['tag_sums'])
    
    def get_images_report_path(self) -> str:
        """Get images report path from config"""
        # images_report is a base name used to save both .txt and .json
        base = self.config['reports']['images_report']
        # If it's just a filename, prefix with output_dir
        if os.path.isabs(base) or os.path.basename(base) != base:
            return base
        return os.path.join(self.get_output_dir(), base)

    def get_archived_tags_report_path(self) -> str:
        """Get archived tags report path from config"""
        return self._resolve_report_path(self.config['reports']['archived_tags'])
    
    # Security configuration
    def is_dry_run_by_default(self) -> bool:
        """Get dry run default from config"""
        return self.config['security']['dry_run_by_default']
    
    def requires_confirmation(self) -> bool:
        """Get confirmation requirement from config"""
        return self.config['security']['require_confirmation']
    
    # Mongo configuration
    def get_mongo_host(self) -> str:
        return self.config['mongo']['host']

    def get_mongo_port(self) -> int:
        return int(self.config['mongo']['port'])

    def get_mongo_replicaset(self) -> str:
        return self.config['mongo']['replicaset']

    def get_mongo_db(self) -> str:
        return self.config['mongo']['db']

    def get_mongo_collection(self) -> str:
        return self.config['mongo']['collection']

    def get_mongo_auth(self) -> str:
        username = os.environ.get('MONGODB_USERNAME', 'admin')
        password = os.environ.get('MONGODB_PASSWORD')

        if password:
            return f"{username}:{password}"

        # Fallback: read credentials from Kubernetes secret mongodb-replicaset-admin
        try:
            # Load kube config (in-cluster if available, else local)
            try:
                from kubernetes import config as k8s_config
                # Detect in-cluster via env or SA token
                if os.environ.get("KUBERNETES_SERVICE_HOST") or os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token"):
                    k8s_config.load_incluster_config()
                else:
                    k8s_config.load_kube_config()
            except Exception:
                # Last attempt: try in-cluster
                from kubernetes import config as k8s_config
                k8s_config.load_incluster_config()

            core = client.CoreV1Api()
            namespace = self.get_platform_namespace()
            secret = core.read_namespaced_secret(name="mongodb-replicaset-admin", namespace=namespace)
            data = secret.data or {}
            secret_user_b64 = data.get("user")
            secret_pass_b64 = data.get("password")

            if secret_pass_b64:
                decoded_password = base64.b64decode(secret_pass_b64).decode("utf-8")
                decoded_username = username
                if secret_user_b64:
                    decoded_username = base64.b64decode(secret_user_b64).decode("utf-8")
                return f"{decoded_username}:{decoded_password}"

            raise RuntimeError("Mongo password not found in secret 'mongodb-replicaset-admin'")
        except Exception as e:
            raise RuntimeError(
                "Mongo password is not set and failed to load from Kubernetes secret 'mongodb-replicaset-admin'"
            ) from e

    def get_mongo_connection_string(self) -> str:
        auth = self.get_mongo_auth()
        host = self.get_mongo_host()
        port = self.get_mongo_port()
        rs = self.get_mongo_replicaset()
        return f"mongodb://{auth}@{host}:{port}/?replicaSet={rs}"
    
    def print_config(self):
        """Print current configuration"""
        print("Current Configuration:")
        print(f"  Registry URL: {self.get_registry_url()}")
        print(f"  Repository Name: {self.get_repository()}")
        print(f"  Platform Namespace: {self.get_platform_namespace()}")
        print(f"  Compute Namespace: {self.get_compute_namespace()}")
        print(f"  Max Workers: {self.get_max_workers()}")
        print(f"  Timeout: {self.get_timeout()}")
        print(f"  Output Directory: {self.get_output_dir()}")
        print(f"  Dry Run Default: {self.is_dry_run_by_default()}")
        print(f"  Require Confirmation: {self.requires_confirmation()}")
        
        password = self.get_registry_password()
        if password:
            print(f"  Registry Password: {'*' * len(password)}")
        else:
            print("  Registry Password: Not set")


class SkopeoClient:
    """Standardized Skopeo client for registry operations"""
    
    def __init__(self, config_manager: ConfigManager, use_pod: bool = False, namespace: str = None):
        self.config_manager = config_manager
        self.use_pod = use_pod
        self.namespace = namespace or config_manager.get_platform_namespace()
        self.registry_url = config_manager.get_registry_url()
        self.repository = config_manager.get_repository()
        self.password = config_manager.get_registry_password()
        self._logged_in = False
        
        if use_pod:
            # Initialize Kubernetes client for pod operations
            try:
                from kubernetes import config
                config.load_kube_config()
                self.core_v1_client = client.CoreV1Api()
            except Exception as e:
                raise RuntimeError(f"Failed to initialize Kubernetes client: {e}")
        
        # Ensure we're logged in before any operations
        self._ensure_logged_in()
    
    def _ensure_logged_in(self):
        """Ensure skopeo is logged in to the registry before operations"""
        if self._logged_in:
            return
        
        try:
            # For ECR, authentication is handled by get_registry_password()
            # For other registries, we'll try to login if we have credentials
            if self.password and "amazonaws.com" not in self.registry_url:
                logging.info(f"Logging in to registry: {self.registry_url}")
                self._login_to_registry()
            
            self._logged_in = True
            logging.info("Skopeo authentication ready")
            
        except Exception as e:
            logging.warning(f"Failed to authenticate with registry: {e}")
            # Continue without authentication - some operations might work unauthenticated
    
    def _login_to_registry(self):
        """Login to the registry using skopeo login"""
        try:
            if self.use_pod:
                # Login in pod
                cmd = [
                    "skopeo", "login", 
                    "--username", "domino-registry",
                    "--password", self.password,
                    "--tls-verify=false",
                    self.registry_url
                ]
                
                response = self.core_v1_client.connect_get_namespaced_pod_exec(
                    name="skopeo",
                    namespace=self.namespace,
                    command=cmd,
                    container="skopeo",
                    stderr=True,
                    stdout=True,
                    stdin=False,
                    tty=False
                )
                
                if "Login Succeeded" not in response:
                    raise RuntimeError(f"Login failed: {response}")
                    
            else:
                # Login locally
                cmd = [
                    "skopeo", "login", 
                    "--username", "domino-registry",
                    "--password", self.password,
                    "--tls-verify=false",
                    self.registry_url
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                if "Login Succeeded" not in result.stdout:
                    raise RuntimeError(f"Login failed: {result.stdout}")
                    
        except Exception as e:
            logging.error(f"Registry login failed: {e}")
            raise
    
    def _get_auth_args(self) -> List[str]:
        """Get authentication arguments for Skopeo commands"""
        base = ["--tls-verify=false"]
        # Allow unauthenticated operations when password is not set
        if self.password:
            return base + ["--creds", f"domino-registry:{self.password}"]
        return base
    
    def _build_skopeo_command(self, subcommand: str, args: List[str]) -> List[str]:
        """Build a complete Skopeo command with authentication"""
        return ["skopeo", subcommand] + self._get_auth_args() + args
    
    def run_skopeo_command(self, subcommand: str, args: List[str]) -> Optional[str]:
        """Run a Skopeo command with standardized configuration"""
        # Ensure we're logged in before any operation
        self._ensure_logged_in()
        
        if self.use_pod:
            return self._run_skopeo_in_pod(subcommand, args)
        else:
            return self._run_skopeo_local(subcommand, args)
    
    def _run_skopeo_local(self, subcommand: str, args: List[str]) -> Optional[str]:
        """Run Skopeo command locally using subprocess"""
        try:
            cmd = self._build_skopeo_command(subcommand, args)
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout
        except subprocess.CalledProcessError as e:
            logging.error(f"Skopeo command failed: {' '.join(cmd)}")
            logging.error(f"Error: {e.stderr}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error running Skopeo: {e}")
            return None
    
    def _run_skopeo_in_pod(self, subcommand: str, args: List[str]) -> Optional[str]:
        """Run Skopeo command in Kubernetes pod"""
        try:
            cmd = self._build_skopeo_command(subcommand, args)
            
            response = self.core_v1_client.connect_get_namespaced_pod_exec(
                name="skopeo",
                namespace=self.namespace,
                command=cmd,
                container="skopeo",
                stderr=True,
                stdout=True,
                stdin=False,
                tty=False
            )
            
            if response:
                return response
            else:
                logging.error(f"Empty response from skopeo command: {' '.join(cmd)}")
                return None
                
        except client.exceptions.ApiException as e:
            if e.status == 404:
                logging.error(f"Skopeo pod not found in namespace {self.namespace}")
            else:
                logging.error(f"API error executing skopeo command: {e}")
            return None
        except Exception as e:
            logging.error(f"Unexpected error executing skopeo command: {e}")
            return None
    
    def list_tags(self, repository: Optional[str] = None) -> List[str]:
        """List all tags for a repository.
        If repository is provided, it should be the full path under the registry
        (e.g., "dominodatalab/environment"). Otherwise uses the default from config.
        """
        repo_path = repository or self.repository
        args = [f"docker://{self.registry_url}/{repo_path}"]
        
        output = self.run_skopeo_command("list-tags", args)
        if output:
            try:
                tags_data = json.loads(output)
                return tags_data.get('Tags', [])
            except json.JSONDecodeError:
                logging.error(f"Failed to parse tags for {self.repository}")
                return []
        return []
    
    def inspect_image(self, repository: Optional[str], tag: str) -> Optional[Dict]:
        """Inspect a specific image tag.
        repository should be the full path under the registry (e.g., "dominodatalab/environment").
        If None, falls back to the default repository from config.
        """
        repo_path = repository or self.repository
        args = [f"docker://{self.registry_url}/{repo_path}:{tag}"]
        
        output = self.run_skopeo_command("inspect", args)
        if output:
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse image inspection for {self.repository}:{tag}")
                return None
        return None
    
    def delete_image(self, repository: Optional[str], tag: str) -> bool:
        """Delete a specific image tag.
        repository should be the full path under the registry (e.g., "dominodatalab/environment").
        If None, falls back to the default repository from config.
        """
        repo_path = repository or self.repository
        args = [f"docker://{self.registry_url}/{repo_path}:{tag}"]
        
        output = self.run_skopeo_command("delete", args)
        return output is not None


# Global config manager instance
config_manager = ConfigManager() 