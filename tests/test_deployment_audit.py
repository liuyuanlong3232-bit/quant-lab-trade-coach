import subprocess
import sys
import unittest
from pathlib import Path


class DeploymentAuditTests(unittest.TestCase):
    def test_static_deployment_gate_skips_binary_assets(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "scripts/audit_deployment.py"], cwd=root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DEPLOYMENT_GATE_PASS", result.stdout)
