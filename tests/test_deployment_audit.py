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
            self.assertEqual(
                backend.secure_store_status(),
                ("DEPLOYMENT_SECRET_UNSET", "QQBOT_DEPLOYMENT_SECRET_NOT_CONFIGURED"),
            )
            with self.assertRaises(PermissionError):
                backend.write("must-not-write")
        finally:
            if old is None: os.environ.pop("QQBOT_APP_SECRET_FILE", None)
            else: os.environ["QQBOT_APP_SECRET_FILE"] = old

    def test_docker_secret_backend_accepts_read_only_and_rejects_writable(self):
        from quant_lab.trade_coach import DockerSecretCredentialBackend
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("top-secret", encoding="utf-8")
            old = os.environ.get("QQBOT_APP_SECRET_FILE")
            try:
                os.environ["QQBOT_APP_SECRET_FILE"] = str(path)
                path.chmod(0o444)
                self.assertEqual(DockerSecretCredentialBackend().read(), "top-secret")
                path.chmod(0o666)
                if os.name != "nt":
                    self.assertIsNone(DockerSecretCredentialBackend().read())
            finally:
                if old is None: os.environ.pop("QQBOT_APP_SECRET_FILE", None)
                else: os.environ["QQBOT_APP_SECRET_FILE"] = old

    @unittest.skipUnless(os.name != "nt", "Docker secret mode is Linux-only")
    def test_docker_secret_service_wires_status_without_echoing_secret(self):
        from quant_lab.trade_coach import TradeCoachService
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); files = {}
            for name, value in (("id", "app-123"), ("secret", "secret-do-not-echo"), ("openid", "openid-456")):
                path = root / name; path.write_text(value, encoding="utf-8"); path.chmod(0o444); files[name] = str(path)
            old = {key: os.environ.get(key) for key in ("QQBOT_APP_ID_FILE", "QQBOT_APP_SECRET_FILE", "QQBOT_OPENID_FILE")}
            try:
                os.environ.update({"QQBOT_APP_ID_FILE": files["id"], "QQBOT_APP_SECRET_FILE": files["secret"], "QQBOT_OPENID_FILE": files["openid"]})
                service = TradeCoachService(root / "coach.sqlite3", project_root=root, bootstrap=True)
                status = service._qqbot_status(); serialized = str(status) + str(service.summary())
                self.assertTrue(status["deployment_managed"] and status["has_secret"] and status["openid_bound"])
                self.assertEqual(service.notifications.adapter.status()["status"], "READY")
                self.assertEqual(service.notifications.adapter.status()["target_host"], "api.sgroup.qq.com")
                self.assertNotIn("secret-do-not-echo", serialized)
                with self.assertRaises(PermissionError): service._save_qqbot({"app_id": "new", "app_secret": "new"})
                with self.assertRaises(PermissionError): service.bind_qqbot_openid("new-openid")
                with self.assertRaises(PermissionError): service.clear_qqbot_binding()
            finally:
                for key, value in old.items():
                    if value is None: os.environ.pop(key, None)
                    else: os.environ[key] = value
