#!/usr/bin/env python3
"""
Configuration Manager for Docker Registry Cleaner

This module handles loading and managing configuration from config.yaml
and environment variables.
"""

import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional


class ConfigManager:
    """Manages configuration for the Docker Registry Cleaner"""
    
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = Path(config_path)
        self.config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from config.yaml"""
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
        
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r') as f:
                    file_config = yaml.safe_load(f)
                    # Merge with defaults
                    return self._merge_config(default_config, file_config)
            except Exception as e:
                print(f"Warning: Could not load config file {self.config_path}: {e}")
                print("Using default configuration")
                return default_config
        else:
            print(f"Config file {self.config_path} not found, using default configuration")
            return default_config
    
    def _merge_config(self, default: Dict, user: Dict) -> Dict:
        """Merge user configuration with defaults"""
        result = default.copy()
        for key, value in user.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
        return result
    
    def get_registry_url(self) -> str:
        """Get registry URL from config or environment"""
        return os.environ.get('REGISTRY_URL', self.config['registry']['url'])
    
    def get_repository_name(self) -> str:
        """Get repository name from config or environment"""
        return os.environ.get('REPOSITORY_NAME', self.config['registry']['repository_name'])
    
    def get_registry_password(self) -> Optional[str]:
        """Get registry password from environment"""
        password_var = self.config['registry']['password_env_var']
        return os.environ.get(password_var)
    
    def get_kubernetes_namespace(self) -> str:
        """Get Kubernetes namespace from config or environment"""
        return os.environ.get('KUBERNETES_NAMESPACE', self.config['kubernetes']['namespace'])
    
    def get_compute_namespace(self) -> str:
        """Get compute namespace from config or environment"""
        return os.environ.get('COMPUTE_NAMESPACE', self.config['kubernetes']['compute_namespace'])
    
    def get_pod_prefixes(self) -> list:
        """Get pod prefixes from config or environment"""
        prefixes = os.environ.get('POD_PREFIXES')
        if prefixes:
            return prefixes.split(',')
        return self.config['kubernetes']['pod_prefixes']
    
    def get_max_workers(self) -> int:
        """Get max workers from config or environment"""
        return int(os.environ.get('MAX_WORKERS', self.config['analysis']['max_workers']))
    
    def get_timeout(self) -> int:
        """Get timeout from config or environment"""
        return int(os.environ.get('TIMEOUT', self.config['analysis']['timeout']))
    
    def get_output_dir(self) -> str:
        """Get output directory from config or environment"""
        return os.environ.get('OUTPUT_DIR', self.config['analysis']['output_dir'])
    
    def get_workload_report_path(self) -> str:
        """Get workload report path from config or environment"""
        return os.environ.get('WORKLOAD_REPORT', self.config['reports']['workload_report'])
    
    def get_image_analysis_path(self) -> str:
        """Get image analysis path from config or environment"""
        return os.environ.get('IMAGE_ANALYSIS', self.config['reports']['image_analysis'])
    
    def get_deletion_analysis_path(self) -> str:
        """Get deletion analysis path from config or environment"""
        return os.environ.get('DELETION_ANALYSIS', self.config['reports']['deletion_analysis'])
    
    def is_dry_run_by_default(self) -> bool:
        """Check if dry run is enabled by default"""
        return self.config['security']['dry_run_by_default']
    
    def require_confirmation(self) -> bool:
        """Check if confirmation is required"""
        return self.config['security']['require_confirmation']
    
    def get_all_config(self) -> Dict[str, Any]:
        """Get all configuration as a dictionary"""
        return {
            'registry_url': self.get_registry_url(),
            'repository_name': self.get_repository_name(),
            'kubernetes_namespace': self.get_kubernetes_namespace(),
            'compute_namespace': self.get_compute_namespace(),
            'pod_prefixes': self.get_pod_prefixes(),
            'max_workers': self.get_max_workers(),
            'timeout': self.get_timeout(),
            'output_dir': self.get_output_dir(),
            'workload_report': self.get_workload_report_path(),
            'image_analysis': self.get_image_analysis_path(),
            'deletion_analysis': self.get_deletion_analysis_path(),
            'dry_run_by_default': self.is_dry_run_by_default(),
            'require_confirmation': self.require_confirmation()
        }
    
    def print_config(self):
        """Print current configuration"""
        print("📋 Current Configuration:")
        config = self.get_all_config()
        for key, value in config.items():
            print(f"   {key}: {value}")


# Global config manager instance
config_manager = ConfigManager() 