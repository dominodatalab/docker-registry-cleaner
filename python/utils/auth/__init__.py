"""
Authentication providers for Docker registries.

This module provides authentication helpers for various registry types:
- AWS ECR (Elastic Container Registry)
- Azure ACR (Azure Container Registry)
- Kubernetes secrets (for in-cluster and external registries)
"""

from utils.auth.providers import authenticate_acr, authenticate_ecr, get_credentials_from_k8s_secret

__all__ = [
    "authenticate_ecr",
    "authenticate_acr",
    "get_credentials_from_k8s_secret",
]
