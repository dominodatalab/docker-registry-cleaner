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
from kubernetes import client


class ConfigManager:
    """Manages configuration for the Docker registry cleaner project"""
    
    def __init__(self, config_file: str = "config.yaml"):
        self.config_file = config_file
        self.config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file with defaults"""
        default_config = {
            'registry': {
                'url': 'docker-registry:5000',
                'repository_name': 'dominodatalab',
                'password_env_var': 'SKOPEO_PASSWORD'
            },
            'kubernetes': {
                'namespace': 'domino-platform',
                'compute_namespace': 'domino-compute',
                'pod_prefixes': ['model-', 'run-']
            },
            'analysis': {
                'max_workers': 4,
                'timeout': 300,
                'output_dir': '.'
            },
            'reports': {
                'workload_report': 'workload-report.json',
                'image_analysis': 'final-report.json',
                'deletion_analysis': 'deletion-analysis.json'
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
    
    def get_repository_name(self) -> str:
        """Get repository name from environment or config"""
        return os.environ.get('REPOSITORY_NAME') or self.config['registry']['repository_name']
    
    def get_registry_password(self) -> Optional[str]:
        """Get registry password from environment"""
        password_env_var = self.config['registry']['password_env_var']
        return os.environ.get(password_env_var)
    
    # Kubernetes configuration
    def get_kubernetes_namespace(self) -> str:
        """Get Kubernetes namespace from environment or config"""
        return os.environ.get('KUBERNETES_NAMESPACE') or self.config['kubernetes']['namespace']
    
    def get_compute_namespace(self) -> str:
        """Get compute namespace from environment or config"""
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
    def get_workload_report_path(self) -> str:
        """Get workload report path from config"""
        return self.config['reports']['workload_report']
    
    def get_image_analysis_path(self) -> str:
        """Get image analysis path from config"""
        return self.config['reports']['image_analysis']
    
    def get_deletion_analysis_path(self) -> str:
        """Get deletion analysis path from config"""
        return self.config['reports']['deletion_analysis']
    
    # Security configuration
    def is_dry_run_by_default(self) -> bool:
        """Get dry run default from config"""
        return self.config['security']['dry_run_by_default']
    
    def requires_confirmation(self) -> bool:
        """Get confirmation requirement from config"""
        return self.config['security']['require_confirmation']
    
    def print_config(self):
        """Print current configuration"""
        print("Current Configuration:")
        print(f"  Registry URL: {self.get_registry_url()}")
        print(f"  Repository Name: {self.get_repository_name()}")
        print(f"  Kubernetes Namespace: {self.get_kubernetes_namespace()}")
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
        self.namespace = namespace or config_manager.get_kubernetes_namespace()
        self.registry_url = config_manager.get_registry_url()
        self.repository_name = config_manager.get_repository_name()
        self.password = config_manager.get_registry_password()
        
        if use_pod:
            # Initialize Kubernetes client for pod operations
            try:
                from kubernetes import config
                config.load_kube_config()
                self.core_v1_client = client.CoreV1Api()
            except Exception as e:
                raise RuntimeError(f"Failed to initialize Kubernetes client: {e}")
    
    def _get_auth_args(self) -> List[str]:
        """Get authentication arguments for Skopeo commands"""
        if not self.password:
            raise ValueError("Registry password not configured. Set SKOPEO_PASSWORD environment variable.")
        
        return [
            "--tls-verify=false",
            "--creds", f"domino-registry:{self.password}"
        ]
    
    def _build_skopeo_command(self, subcommand: str, args: List[str]) -> List[str]:
        """Build a complete Skopeo command with authentication"""
        return ["skopeo", subcommand] + self._get_auth_args() + args
    
    def run_skopeo_command(self, subcommand: str, args: List[str]) -> Optional[str]:
        """Run a Skopeo command with standardized configuration"""
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
            cmd = ["skopeo", subcommand] + self._get_auth_args() + args
            
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
    
    def list_tags(self, repository: str = None) -> List[str]:
        """List all tags for a repository"""
        repo = repository or self.repository_name
        args = [f"docker://{self.registry_url}/{repo}"]
        
        output = self.run_skopeo_command("list-tags", args)
        if output:
            try:
                tags_data = json.loads(output)
                return tags_data.get('Tags', [])
            except json.JSONDecodeError:
                logging.error(f"Failed to parse tags for {repo}")
                return []
        return []
    
    def inspect_image(self, repository: str, tag: str) -> Optional[Dict]:
        """Inspect a specific image tag"""
        args = [f"docker://{self.registry_url}/{repository}:{tag}"]
        
        output = self.run_skopeo_command("inspect", args)
        if output:
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse image inspection for {repository}:{tag}")
                return None
        return None
    
    def delete_image(self, repository: str, tag: str) -> bool:
        """Delete a specific image tag"""
        args = [f"docker://{self.registry_url}/{repository}:{tag}"]
        
        output = self.run_skopeo_command("delete", args)
        return output is not None


# Global config manager instance
config_manager = ConfigManager() 