import subprocess
import sys
import unittest
import os
import tempfile
from pathlib import Path


class DeploymentAuditTests(unittest.TestCase):
    def test_static_deployment_gate_skips_binary_assets(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "scripts/audit_deployment.py"], cwd=root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DEPLOYMENT_GATE_PASS", result.stdout)

    def test_docker_secret_backend_fails_closed_for_missing_file(self):
        from quant_lab.trade_coach import DockerSecretCredentialBackend
        old = os.environ.get("QQBOT_APP_SECRET_FILE")
        try:
            os.environ["QQBOT_APP_SECRET_FILE"] = "/definitely/missing/qqbot-secret"
            backend = DockerSecretCredentialBackend()
            self.assertIsNone(backend.read())
            self.assertNotEqual(backend.secure_store_status()[0], "READY")
            with self.assertRaises(PermissionError):
                backend.write("must-not-write")
        finally:
            if old is None: os.environ.pop("QQBOT_APP_SECRET_FILE", None)
            else: os.environ["QQBOT_APP_SECRET_FILE"] = old
