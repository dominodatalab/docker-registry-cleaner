"""Unit tests for utils/auth/providers.py"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_python_dir = Path(__file__).parent.parent / "python"
if str(_python_dir.absolute()) not in sys.path:
    sys.path.insert(0, str(_python_dir.absolute()))

from utils.auth.providers import (
    _get_kubernetes_core_client,
    _load_kubernetes_config,
    authenticate_acr,
    authenticate_ecr,
    get_credentials_from_k8s_secret,
)


class TestLoadKubernetesConfig:
    """Tests for _load_kubernetes_config helper function"""

    def test_loads_incluster_config_first(self):
        """Test that in-cluster config is tried first"""
        with patch("kubernetes.config.load_incluster_config") as mock_incluster:
            _load_kubernetes_config()
            mock_incluster.assert_called_once()

    def test_falls_back_to_kubeconfig(self):
        """Test that local kubeconfig is used when in-cluster fails"""
        with patch(
            "kubernetes.config.load_incluster_config", side_effect=Exception("Not in cluster")
        ) as mock_incluster:
            with patch("kubernetes.config.load_kube_config") as mock_kubeconfig:
                _load_kubernetes_config()
                mock_incluster.assert_called_once()
                mock_kubeconfig.assert_called_once()


class TestGetKubernetesCoreClient:
    """Tests for _get_kubernetes_core_client helper function"""

    def test_returns_core_v1_client(self):
        """Test that CoreV1Api client is returned"""
        mock_core_v1 = MagicMock()
        with patch("utils.auth.providers._load_kubernetes_config"):
            with patch("kubernetes.client.CoreV1Api", return_value=mock_core_v1):
                result = _get_kubernetes_core_client()
                assert result == mock_core_v1


class TestGetCredentialsFromK8sSecret:
    """Tests for get_credentials_from_k8s_secret function"""

    def test_returns_none_when_secret_not_found(self):
        """Test that (None, None) is returned when secret doesn't exist"""
        from kubernetes.client.rest import ApiException

        mock_core_v1 = MagicMock()
        error = ApiException(status=404)
        mock_core_v1.read_namespaced_secret.side_effect = error

        with patch("utils.auth.providers._get_kubernetes_core_client", return_value=mock_core_v1):
            username, password = get_credentials_from_k8s_secret("my-secret", "my-namespace", "registry.example.com")
            assert username is None
            assert password is None

    def test_returns_none_when_no_dockerconfigjson(self):
        """Test that (None, None) is returned when secret has no .dockerconfigjson"""
        mock_secret = MagicMock()
        mock_secret.data = {"other-key": "value"}

        mock_core_v1 = MagicMock()
        mock_core_v1.read_namespaced_secret.return_value = mock_secret

        with patch("utils.auth.providers._get_kubernetes_core_client", return_value=mock_core_v1):
            username, password = get_credentials_from_k8s_secret("my-secret", "my-namespace", "registry.example.com")
            assert username is None
            assert password is None

    def test_extracts_credentials_from_dockerconfigjson(self):
        """Test successful credential extraction from .dockerconfigjson"""
        # Create a dockerconfigjson structure
        dockerconfig = {
            "auths": {
                "registry.example.com": {
                    "username": "myuser",
                    "password": "mypassword",
                }
            }
        }
        dockerconfig_b64 = base64.b64encode(json.dumps(dockerconfig).encode()).decode()

        mock_secret = MagicMock()
        mock_secret.data = {".dockerconfigjson": dockerconfig_b64}

        mock_core_v1 = MagicMock()
        mock_core_v1.read_namespaced_secret.return_value = mock_secret

        with patch("utils.auth.providers._get_kubernetes_core_client", return_value=mock_core_v1):
            username, password = get_credentials_from_k8s_secret("my-secret", "my-namespace", "registry.example.com")
            assert username == "myuser"
            assert password == "mypassword"

    def test_extracts_credentials_from_auth_field(self):
        """Test credential extraction from base64 auth field"""
        # Create a dockerconfigjson with auth field (base64 encoded username:password)
        auth_b64 = base64.b64encode("myuser:mypassword".encode()).decode()
        dockerconfig = {
            "auths": {
                "registry.example.com": {
                    "auth": auth_b64,
                }
            }
        }
        dockerconfig_b64 = base64.b64encode(json.dumps(dockerconfig).encode()).decode()

        mock_secret = MagicMock()
        mock_secret.data = {".dockerconfigjson": dockerconfig_b64}

        mock_core_v1 = MagicMock()
        mock_core_v1.read_namespaced_secret.return_value = mock_secret

        with patch("utils.auth.providers._get_kubernetes_core_client", return_value=mock_core_v1):
            username, password = get_credentials_from_k8s_secret("my-secret", "my-namespace", "registry.example.com")
            assert username == "myuser"
            assert password == "mypassword"

    def test_matches_registry_by_host(self):
        """Test that registry is matched by hostname"""
        dockerconfig = {
            "auths": {
                "registry.example.com:5000": {
                    "username": "myuser",
                    "password": "mypassword",
                }
            }
        }
        dockerconfig_b64 = base64.b64encode(json.dumps(dockerconfig).encode()).decode()

        mock_secret = MagicMock()
        mock_secret.data = {".dockerconfigjson": dockerconfig_b64}

        mock_core_v1 = MagicMock()
        mock_core_v1.read_namespaced_secret.return_value = mock_secret

        with patch("utils.auth.providers._get_kubernetes_core_client", return_value=mock_core_v1):
            # Should match even without the port
            username, password = get_credentials_from_k8s_secret("my-secret", "my-namespace", "registry.example.com")
            assert username == "myuser"
            assert password == "mypassword"

    def test_returns_none_when_no_matching_registry(self):
        """Test that (None, None) is returned when no matching registry in auths"""
        dockerconfig = {
            "auths": {
                "other-registry.example.com": {
                    "username": "myuser",
                    "password": "mypassword",
                }
            }
        }
        dockerconfig_b64 = base64.b64encode(json.dumps(dockerconfig).encode()).decode()

        mock_secret = MagicMock()
        mock_secret.data = {".dockerconfigjson": dockerconfig_b64}

        mock_core_v1 = MagicMock()
        mock_core_v1.read_namespaced_secret.return_value = mock_secret

        with patch("utils.auth.providers._get_kubernetes_core_client", return_value=mock_core_v1):
            username, password = get_credentials_from_k8s_secret("my-secret", "my-namespace", "my-registry.example.com")
            assert username is None
            assert password is None

    def test_handles_exception_gracefully(self):
        """Test that exceptions are caught and (None, None) is returned"""
        with patch("utils.auth.providers._get_kubernetes_core_client", side_effect=Exception("Connection error")):
            username, password = get_credentials_from_k8s_secret("my-secret", "my-namespace", "registry.example.com")
            assert username is None
            assert password is None


class TestAuthenticateECR:
    """Tests for authenticate_ecr function"""

    def test_extracts_region_from_registry_url(self):
        """Test that region is extracted from ECR registry URL"""
        mock_ecr_client = MagicMock()
        mock_ecr_client.get_authorization_token.return_value = {
            "authorizationData": [{"authorizationToken": base64.b64encode(b"AWS:mytoken").decode()}]
        }

        with patch("boto3.client", return_value=mock_ecr_client) as mock_boto3:
            with patch("subprocess.run") as mock_subprocess:
                mock_subprocess.return_value = MagicMock()
                authenticate_ecr("123456789.dkr.ecr.us-west-2.amazonaws.com", "/tmp/auth.json")

                mock_boto3.assert_called_once_with("ecr", region_name="us-west-2")

    def test_uses_default_region_for_non_standard_url(self):
        """Test that default region is used for non-standard ECR URLs"""
        mock_ecr_client = MagicMock()
        mock_ecr_client.get_authorization_token.return_value = {
            "authorizationData": [{"authorizationToken": base64.b64encode(b"AWS:mytoken").decode()}]
        }

        with patch("boto3.client", return_value=mock_ecr_client) as mock_boto3:
            with patch("subprocess.run") as mock_subprocess:
                mock_subprocess.return_value = MagicMock()
                with patch.dict(os.environ, {"AWS_DEFAULT_REGION": "eu-west-1"}):
                    authenticate_ecr("custom-ecr-url", "/tmp/auth.json")

                    mock_boto3.assert_called_once_with("ecr", region_name="eu-west-1")

    def test_calls_skopeo_login_with_token(self):
        """Test that skopeo login is called with the ECR token"""
        mock_ecr_client = MagicMock()
        mock_ecr_client.get_authorization_token.return_value = {
            "authorizationData": [{"authorizationToken": base64.b64encode(b"AWS:mytoken").decode()}]
        }

        with patch("boto3.client", return_value=mock_ecr_client):
            with patch("subprocess.run") as mock_subprocess:
                mock_subprocess.return_value = MagicMock()
                authenticate_ecr("123456789.dkr.ecr.us-west-2.amazonaws.com", "/tmp/auth.json")

                mock_subprocess.assert_called_once()
                call_args = mock_subprocess.call_args
                cmd = call_args[0][0]
                assert "skopeo" in cmd
                assert "login" in cmd
                assert "--authfile" in cmd
                assert "/tmp/auth.json" in cmd
                assert "--username" in cmd
                assert "AWS" in cmd
                assert call_args[1]["input"] == "mytoken"

    def test_raises_on_skopeo_login_failure(self):
        """Test that CalledProcessError is raised when skopeo login fails"""
        mock_ecr_client = MagicMock()
        mock_ecr_client.get_authorization_token.return_value = {
            "authorizationData": [{"authorizationToken": base64.b64encode(b"AWS:mytoken").decode()}]
        }

        with patch("boto3.client", return_value=mock_ecr_client):
            with patch("subprocess.run") as mock_subprocess:
                mock_subprocess.side_effect = subprocess.CalledProcessError(1, "skopeo", stderr="Login failed")
                with pytest.raises(subprocess.CalledProcessError):
                    authenticate_ecr("123456789.dkr.ecr.us-west-2.amazonaws.com", "/tmp/auth.json")


class TestAuthenticateACR:
    """Tests for authenticate_acr function"""

    @pytest.fixture(autouse=True)
    def check_azure_available(self):
        """Skip ACR tests if azure-identity is not installed"""
        pytest.importorskip("azure.identity", reason="azure-identity not installed")

    def test_uses_managed_identity_when_client_id_set(self):
        """Test that ManagedIdentityCredential is used when AZURE_CLIENT_ID is set"""
        mock_credential = MagicMock()
        mock_credential.get_token.return_value = MagicMock(token="access_token")

        with patch.dict(os.environ, {"AZURE_CLIENT_ID": "my-client-id"}):
            with patch("azure.identity.ManagedIdentityCredential", return_value=mock_credential) as mock_mic:
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.read.return_value = json.dumps({"refresh_token": "acr_token"}).encode()
                    mock_response.__enter__ = MagicMock(return_value=mock_response)
                    mock_response.__exit__ = MagicMock(return_value=False)
                    mock_urlopen.return_value = mock_response

                    with patch("subprocess.run") as mock_subprocess:
                        mock_subprocess.return_value = MagicMock()
                        authenticate_acr("myregistry.azurecr.io", "/tmp/auth.json")

                        mock_mic.assert_called_once_with(client_id="my-client-id")

    def test_uses_default_credential_when_no_client_id(self):
        """Test that DefaultAzureCredential is used when AZURE_CLIENT_ID is not set"""
        mock_credential = MagicMock()
        mock_credential.get_token.return_value = MagicMock(token="access_token")

        with patch.dict(os.environ, {}, clear=True):
            # Ensure AZURE_CLIENT_ID is not set
            os.environ.pop("AZURE_CLIENT_ID", None)
            with patch("azure.identity.DefaultAzureCredential", return_value=mock_credential) as mock_dac:
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.read.return_value = json.dumps({"refresh_token": "acr_token"}).encode()
                    mock_response.__enter__ = MagicMock(return_value=mock_response)
                    mock_response.__exit__ = MagicMock(return_value=False)
                    mock_urlopen.return_value = mock_response

                    with patch("subprocess.run") as mock_subprocess:
                        mock_subprocess.return_value = MagicMock()
                        authenticate_acr("myregistry.azurecr.io", "/tmp/auth.json")

                        mock_dac.assert_called_once()

    def test_exchanges_aad_token_for_acr_token(self):
        """Test that AAD token is exchanged for ACR refresh token"""
        mock_credential = MagicMock()
        mock_credential.get_token.return_value = MagicMock(token="access_token")

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AZURE_CLIENT_ID", None)
            with patch("azure.identity.DefaultAzureCredential", return_value=mock_credential):
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.read.return_value = json.dumps({"refresh_token": "acr_token"}).encode()
                    mock_response.__enter__ = MagicMock(return_value=mock_response)
                    mock_response.__exit__ = MagicMock(return_value=False)
                    mock_urlopen.return_value = mock_response

                    with patch("subprocess.run") as mock_subprocess:
                        mock_subprocess.return_value = MagicMock()
                        authenticate_acr("myregistry.azurecr.io", "/tmp/auth.json")

                        # Verify the exchange URL was called
                        mock_urlopen.assert_called_once()
                        call_args = mock_urlopen.call_args
                        request = call_args[0][0]
                        assert "myregistry.azurecr.io/oauth2/exchange" in request.full_url

    def test_calls_skopeo_login_with_refresh_token(self):
        """Test that skopeo login is called with ACR refresh token"""
        mock_credential = MagicMock()
        mock_credential.get_token.return_value = MagicMock(token="access_token")

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AZURE_CLIENT_ID", None)
            with patch("azure.identity.DefaultAzureCredential", return_value=mock_credential):
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.read.return_value = json.dumps({"refresh_token": "acr_refresh_token"}).encode()
                    mock_response.__enter__ = MagicMock(return_value=mock_response)
                    mock_response.__exit__ = MagicMock(return_value=False)
                    mock_urlopen.return_value = mock_response

                    with patch("subprocess.run") as mock_subprocess:
                        mock_subprocess.return_value = MagicMock()
                        authenticate_acr("myregistry.azurecr.io", "/tmp/auth.json")

                        mock_subprocess.assert_called_once()
                        call_args = mock_subprocess.call_args
                        cmd = call_args[0][0]
                        assert "skopeo" in cmd
                        assert "login" in cmd
                        assert "--username" in cmd
                        assert "00000000-0000-0000-0000-000000000000" in cmd
                        assert call_args[1]["input"] == "acr_refresh_token"

    def test_raises_on_skopeo_login_failure(self):
        """Test that CalledProcessError is raised when skopeo login fails"""
        mock_credential = MagicMock()
        mock_credential.get_token.return_value = MagicMock(token="access_token")

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("AZURE_CLIENT_ID", None)
            with patch("azure.identity.DefaultAzureCredential", return_value=mock_credential):
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.read.return_value = json.dumps({"refresh_token": "acr_token"}).encode()
                    mock_response.__enter__ = MagicMock(return_value=mock_response)
                    mock_response.__exit__ = MagicMock(return_value=False)
                    mock_urlopen.return_value = mock_response

                    with patch("subprocess.run") as mock_subprocess:
                        mock_subprocess.side_effect = subprocess.CalledProcessError(1, "skopeo", stderr="Login failed")
                        with pytest.raises(subprocess.CalledProcessError):
                            authenticate_acr("myregistry.azurecr.io", "/tmp/auth.json")
