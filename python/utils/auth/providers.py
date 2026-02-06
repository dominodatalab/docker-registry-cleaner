"""
Authentication provider implementations for Docker registries.

This module contains the actual authentication logic for various registry types.
"""

import base64
import json
import logging
import os
import subprocess
import urllib.parse
import urllib.request
from typing import Optional, Tuple


def _load_kubernetes_config():
    """Helper function to load Kubernetes configuration.

    Tries in-cluster config first, then falls back to local kubeconfig.

    Raises:
        Exception if both methods fail
    """
    try:
        from kubernetes.config import load_incluster_config

        load_incluster_config()
    except Exception:
        from kubernetes.config import load_kube_config

        load_kube_config()


def _get_kubernetes_core_client():
    """Helper function to get Kubernetes CoreV1Api client.

    Returns:
        CoreV1Api instance

    Raises:
        ImportError if kubernetes package is not available
    """
    from kubernetes import client as k8s_client

    _load_kubernetes_config()
    return k8s_client.CoreV1Api()


def get_credentials_from_k8s_secret(
    secret_name: str,
    namespace: str,
    registry_url: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Get Docker registry username and password from Kubernetes secret.

    Attempts to read credentials from a Kubernetes secret containing
    .dockerconfigjson with Docker registry credentials.

    Args:
        secret_name: Name of the secret to read (e.g., 'domino-registry').
        namespace: Kubernetes namespace containing the secret.
        registry_url: Registry URL to match in the dockerconfigjson.

    Returns:
        Tuple of (username, password) - either or both may be None if not found
    """
    try:
        from kubernetes.client.rest import ApiException

        core_v1 = _get_kubernetes_core_client()

        logging.debug(f"Attempting to read {secret_name} secret from namespace {namespace}")

        try:
            secret = core_v1.read_namespaced_secret(name=secret_name, namespace=namespace)
        except ApiException as e:
            if e.status == 404:
                logging.debug(f"Secret {secret_name} not found in namespace {namespace}")
            else:
                logging.debug(f"Error reading secret {secret_name}: {e}")
            return None, None

        # Read .dockerconfigjson from the secret
        if not secret.data or ".dockerconfigjson" not in secret.data:
            logging.debug(f"Secret {secret_name} does not contain .dockerconfigjson")
            return None, None

        # Decode the dockerconfigjson
        dockerconfig_b64 = secret.data[".dockerconfigjson"]
        dockerconfig_json = base64.b64decode(dockerconfig_b64).decode("utf-8")
        dockerconfig = json.loads(dockerconfig_json)

        # Normalize registry URL for matching (remove port, protocol, etc.)
        registry_host = registry_url.split(":")[0].split("/")[0]

        if "auths" not in dockerconfig:
            logging.debug("No 'auths' section in dockerconfigjson")
            return None, None

        # Try to find matching registry in auths
        for auth_url, auth_data in dockerconfig["auths"].items():
            # Check if this auth entry matches our registry
            auth_host = auth_url.split(":")[0].split("/")[0]
            if auth_host == registry_host or registry_host in auth_host:
                username = auth_data.get("username")
                password = auth_data.get("password")

                # If username/password not directly available, decode from 'auth' field
                if (not username or not password) and "auth" in auth_data:
                    auth_decoded = base64.b64decode(auth_data["auth"]).decode("utf-8")
                    if ":" in auth_decoded:
                        decoded_user, decoded_pass = auth_decoded.split(":", 1)
                        username = username or decoded_user
                        password = password or decoded_pass

                if username or password:
                    logging.info(f"Found registry credentials in {secret_name} secret")
                    return username, password

        logging.debug(f"No matching registry credentials found in {secret_name} secret for {registry_url}")
        return None, None

    except Exception as e:
        # Log but don't fail - fall back to other auth methods
        logging.debug(f"Could not read credentials from Kubernetes secret: {e}")
        return None, None


def authenticate_ecr(registry_url: str, auth_file: str) -> None:
    """Authenticate with AWS ECR using boto3.

    Uses boto3 to get an ECR authorization token and logs in via skopeo.

    Args:
        registry_url: ECR registry URL (e.g., '123456789.dkr.ecr.us-west-2.amazonaws.com')
        auth_file: Path to skopeo auth file for storing credentials

    Raises:
        subprocess.CalledProcessError: If skopeo login fails
        Exception: For other authentication errors
    """
    try:
        # Extract region from registry URL
        # ECR URLs are typically: account.dkr.ecr.region.amazonaws.com
        parts = registry_url.split(".")
        if len(parts) >= 4 and parts[-2] == "amazonaws" and parts[-1] == "com":
            region = parts[-3]  # Extract region from URL
        else:
            region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

        logging.info(f"Authenticating with ECR in region: {region}")

        # Get ECR login password via boto3 (no aws CLI or shell needed)
        import boto3

        client = boto3.client("ecr", region_name=region)
        response = client.get_authorization_token()
        token_b64 = response["authorizationData"][0]["authorizationToken"]
        token = base64.b64decode(token_b64).decode("utf-8")
        _, password = token.split(":", 1)

        # Run skopeo login with password on stdin (no shell)
        subprocess.run(
            [
                "skopeo",
                "login",
                "--authfile",
                auth_file,
                "--username",
                "AWS",
                "--password-stdin",
                registry_url,
            ],
            input=password,
            capture_output=True,
            text=True,
            check=True,
        )
        logging.info("ECR authentication successful")

    except subprocess.CalledProcessError as e:
        logging.error(f"ECR authentication failed: {e}")
        if e.stderr:
            logging.error(f"  stderr: {e.stderr}")
        raise
    except Exception as e:
        logging.error(f"Unexpected error during ECR authentication: {e}")
        raise


def authenticate_acr(registry_url: str, auth_file: str) -> None:
    """Authenticate with Azure Container Registry using managed identity.

    Uses Azure Identity SDK to get an access token and exchanges it for
    an ACR refresh token via the OAuth2 exchange endpoint.

    Args:
        registry_url: ACR registry URL (e.g., 'myregistry.azurecr.io')
        auth_file: Path to skopeo auth file for storing credentials

    Raises:
        subprocess.CalledProcessError: If skopeo login fails
        Exception: For other authentication errors

    Environment Variables:
        AZURE_CLIENT_ID: Client ID of the managed identity (required when
                        multiple user-assigned identities exist on the cluster)
        AZURE_TENANT_ID: Azure AD tenant ID (optional, for troubleshooting)
    """
    try:
        logging.info(f"Authenticating with ACR: {registry_url}")

        # Get Azure AD access token using managed identity
        # If AZURE_CLIENT_ID is set, use ManagedIdentityCredential directly
        # (required when multiple user-assigned identities exist on the AKS cluster)
        client_id = os.environ.get("AZURE_CLIENT_ID")
        if client_id:
            from azure.identity import ManagedIdentityCredential

            logging.info(f"Using managed identity with client ID: {client_id}")
            credential = ManagedIdentityCredential(client_id=client_id)
        else:
            from azure.identity import DefaultAzureCredential

            logging.info("Using DefaultAzureCredential (no AZURE_CLIENT_ID specified)")
            credential = DefaultAzureCredential()

        # Scope for ACR is the Azure management endpoint
        token = credential.get_token("https://management.azure.com/.default")
        access_token = token.token

        # Exchange the AAD token for an ACR refresh token
        exchange_url = f"https://{registry_url}/oauth2/exchange"
        data = urllib.parse.urlencode(
            {
                "grant_type": "access_token",
                "service": registry_url,
                "access_token": access_token,
            }
        ).encode("utf-8")

        req = urllib.request.Request(exchange_url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            refresh_token = result["refresh_token"]

        # Run skopeo login with the refresh token as password
        # ACR uses a placeholder GUID as the username when using refresh tokens
        subprocess.run(
            [
                "skopeo",
                "login",
                "--authfile",
                auth_file,
                "--username",
                "00000000-0000-0000-0000-000000000000",
                "--password-stdin",
                registry_url,
            ],
            input=refresh_token,
            capture_output=True,
            text=True,
            check=True,
        )
        logging.info("ACR authentication successful")

    except subprocess.CalledProcessError as e:
        logging.error(f"ACR authentication failed (skopeo login): {e}")
        if e.stderr:
            logging.error(f"  stderr: {e.stderr}")
        raise
    except urllib.error.HTTPError as e:
        client_id = os.environ.get("AZURE_CLIENT_ID", "not set")
        logging.error(f"ACR token exchange failed: {e}")
        logging.error(f"  AZURE_CLIENT_ID: {client_id}")
        logging.error("  Troubleshooting steps:")
        logging.error("    1. Verify the managed identity has AcrPull role on the ACR")
        logging.error("    2. Verify the managed identity is assigned to the AKS node pool VMSS")
        logging.error("    3. Check AZURE_CLIENT_ID matches the identity's client ID")
        raise
    except Exception as e:
        client_id = os.environ.get("AZURE_CLIENT_ID", "not set")
        logging.error(f"Unexpected error during ACR authentication: {e}")
        logging.error(f"  AZURE_CLIENT_ID: {client_id}")
        if "Identity not found" in str(e):
            logging.error("  The managed identity was not found. This usually means:")
            logging.error("    - AZURE_CLIENT_ID is incorrect")
            logging.error("    - The identity is not assigned to the AKS node pool VMSS")
            logging.error("  Run: az vmss identity show -g <node-rg> -n <vmss-name>")
        raise
