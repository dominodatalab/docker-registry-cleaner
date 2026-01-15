"""
Error message utilities for providing actionable guidance to users.

This module provides functions to create helpful error messages with
suggested fixes and troubleshooting steps.
"""

from typing import List, Optional, Dict, Any
from enum import Enum


class ErrorCategory(Enum):
    """Categories of errors for better error handling"""
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    CONFIGURATION = "configuration"
    PERMISSION = "permission"
    RESOURCE = "resource"
    NETWORK = "network"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class ActionableError(Exception):
    """Exception with actionable guidance for users"""
    
    def __init__(self, message: str, category: ErrorCategory = ErrorCategory.UNKNOWN,
                 suggestions: Optional[List[str]] = None, details: Optional[Dict[str, Any]] = None):
        """Initialize actionable error
        
        Args:
            message: Primary error message
            category: Error category for classification
            suggestions: List of suggested fixes
            details: Additional context information
        """
        self.message = message
        self.category = category
        self.suggestions = suggestions or []
        self.details = details or {}
        super().__init__(self.format_message())
    
    def format_message(self) -> str:
        """Format the complete error message with suggestions"""
        lines = [f"❌ {self.message}"]
        
        if self.suggestions:
            lines.append("\n💡 Suggested fixes:")
            for i, suggestion in enumerate(self.suggestions, 1):
                lines.append(f"   {i}. {suggestion}")
        
        if self.details:
            lines.append("\n📋 Additional details:")
            for key, value in self.details.items():
                lines.append(f"   {key}: {value}")
        
        return "\n".join(lines)


def create_registry_connection_error(registry_url: str, error: Exception) -> ActionableError:
    """Create actionable error for registry connection failures"""
    error_str = str(error).lower()
    
    suggestions = [
        f"Verify the registry URL is correct: {registry_url}",
        "Check network connectivity to the registry",
        "Verify firewall rules allow access to the registry",
        "Check if the registry service is running",
        "For in-cluster registries, verify Kubernetes service discovery"
    ]
    
    if "timeout" in error_str or "timed out" in error_str:
        suggestions.insert(1, "Check if the registry is experiencing high load")
        suggestions.insert(2, "Verify network latency is acceptable")
    
    if "name resolution" in error_str or "dns" in error_str:
        suggestions.insert(1, "Verify DNS resolution for the registry hostname")
        suggestions.insert(2, "Check /etc/hosts if using local hostnames")
    
    return ActionableError(
        message=f"Failed to connect to Docker registry at {registry_url}",
        category=ErrorCategory.CONNECTION,
        suggestions=suggestions,
        details={
            "registry_url": registry_url,
            "error_type": type(error).__name__,
            "error_message": str(error)
        }
    )


def create_registry_auth_error(registry_url: str, error: Exception) -> ActionableError:
    """Create actionable error for registry authentication failures"""
    error_str = str(error).lower()
    
    suggestions = [
        "Verify REGISTRY_PASSWORD environment variable is set correctly",
        "Check config.yaml for registry.password field",
        "For ECR registries, ensure AWS credentials are configured",
        "Verify the password hasn't expired or been rotated",
        "Check if the registry requires different authentication (e.g., OAuth)"
    ]
    
    if "amazonaws.com" in registry_url:
        suggestions.insert(0, "Run 'aws ecr get-login-password' to test ECR authentication")
        suggestions.insert(1, "Verify AWS credentials are configured (aws configure)")
        suggestions.insert(2, "Check AWS IAM permissions for ECR access")
    
    return ActionableError(
        message=f"Failed to authenticate with Docker registry at {registry_url}",
        category=ErrorCategory.AUTHENTICATION,
        suggestions=suggestions,
        details={
            "registry_url": registry_url,
            "error_type": type(error).__name__,
            "error_message": str(error)
        }
    )


def create_mongodb_connection_error(host: str, port: int, error: Exception) -> ActionableError:
    """Create actionable error for MongoDB connection failures"""
    error_str = str(error).lower()
    
    suggestions = [
        f"Verify MongoDB is running at {host}:{port}",
        "Check network connectivity to MongoDB",
        "Verify firewall rules allow access to MongoDB",
        "Check MongoDB connection string in config.yaml",
        "Verify MONGODB_PASSWORD environment variable is set (if required)"
    ]
    
    if "authentication" in error_str or "auth" in error_str:
        suggestions.insert(0, "Verify MongoDB username and password are correct")
        suggestions.insert(1, "Check if MongoDB credentials are in Kubernetes secret 'mongodb-replicaset-admin'")
    
    if "timeout" in error_str:
        suggestions.insert(1, "Check if MongoDB is experiencing high load")
        suggestions.insert(2, "Verify network latency is acceptable")
    
    return ActionableError(
        message=f"Failed to connect to MongoDB at {host}:{port}",
        category=ErrorCategory.CONNECTION,
        suggestions=suggestions,
        details={
            "host": host,
            "port": port,
            "error_type": type(error).__name__,
            "error_message": str(error)
        }
    )


def create_kubernetes_error(operation: str, error: Exception) -> ActionableError:
    """Create actionable error for Kubernetes API failures"""
    error_str = str(error).lower()
    
    suggestions = [
        "Verify Kubernetes cluster access (kubectl cluster-info)",
        "Check if running in-cluster or using kubeconfig",
        "Verify RBAC permissions for the operation",
        "Check if the namespace exists and is accessible"
    ]
    
    if "403" in error_str or "forbidden" in error_str:
        suggestions.insert(0, "Check Kubernetes RBAC permissions")
        suggestions.insert(1, "Verify service account has required permissions")
    
    if "404" in error_str or "not found" in error_str:
        suggestions.insert(0, "Verify the resource exists in the namespace")
        suggestions.insert(1, "Check if the namespace name is correct")
    
    return ActionableError(
        message=f"Kubernetes operation failed: {operation}",
        category=ErrorCategory.PERMISSION if "403" in error_str or "forbidden" in error_str else ErrorCategory.RESOURCE,
        suggestions=suggestions,
        details={
            "operation": operation,
            "error_type": type(error).__name__,
            "error_message": str(error)
        }
    )


def create_s3_error(operation: str, bucket: str, error: Exception) -> ActionableError:
    """Create actionable error for S3 operation failures"""
    error_str = str(error).lower()
    
    suggestions = [
        f"Verify S3 bucket '{bucket}' exists and is accessible",
        "Check AWS credentials are configured (aws configure)",
        "Verify IAM permissions for S3 access",
        "Check if the bucket is in the correct region"
    ]
    
    if "credentials" in error_str or "no credentials" in error_str:
        suggestions.insert(0, "Configure AWS credentials: aws configure")
        suggestions.insert(1, "Or set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables")
    
    if "403" in error_str or "forbidden" in error_str:
        suggestions.insert(0, "Check IAM policy allows S3 operations on this bucket")
        suggestions.insert(1, "Verify bucket policy allows your AWS account access")
    
    if "404" in error_str or "not found" in error_str:
        suggestions.insert(0, f"Verify bucket '{bucket}' exists in the specified region")
        suggestions.insert(1, "Check bucket name spelling and region")
    
    return ActionableError(
        message=f"S3 operation failed: {operation} on bucket {bucket}",
        category=ErrorCategory.PERMISSION if "403" in error_str or "forbidden" in error_str else ErrorCategory.RESOURCE,
        suggestions=suggestions,
        details={
            "operation": operation,
            "bucket": bucket,
            "error_type": type(error).__name__,
            "error_message": str(error)
        }
    )


def create_config_error(field: str, value: Any, reason: str) -> ActionableError:
    """Create actionable error for configuration validation failures"""
    suggestions = [
        f"Check the '{field}' value in config.yaml",
        "Verify the value matches the expected format",
        "Check the config-example.yaml for correct format",
        "Review the configuration validation error message above"
    ]
    
    if "port" in field.lower():
        suggestions.insert(1, "Port must be an integer between 1 and 65535")
    elif "url" in field.lower():
        suggestions.insert(1, "URL should be in format: hostname[:port]")
    elif "timeout" in field.lower() or "delay" in field.lower():
        suggestions.insert(1, "Time values must be positive numbers")
    
    return ActionableError(
        message=f"Configuration error: Invalid value for '{field}'",
        category=ErrorCategory.CONFIGURATION,
        suggestions=suggestions,
        details={
            "field": field,
            "value": value,
            "reason": reason
        }
    )


def create_rate_limit_error(operation: str, retry_after: Optional[float] = None) -> ActionableError:
    """Create actionable error for rate limiting"""
    suggestions = [
        "Reduce the number of parallel workers (--max-workers)",
        "Increase rate limiting delay in config.yaml",
        "Wait before retrying the operation",
        "Contact registry administrator to increase rate limits"
    ]
    
    if retry_after:
        suggestions.insert(0, f"Wait {retry_after:.1f} seconds before retrying")
    
    return ActionableError(
        message=f"Rate limit exceeded for operation: {operation}",
        category=ErrorCategory.NETWORK,
        suggestions=suggestions,
        details={
            "operation": operation,
            "retry_after": retry_after
        }
    )
