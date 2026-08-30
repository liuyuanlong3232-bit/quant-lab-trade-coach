"""Static deployment safety gate; no network, Docker, or filesystem writes."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
text = "\n".join(p.read_text(encoding="utf-8") for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "node_modules" not in p.parts)
required = ["127.0.0.1:${QUANT_LAB_PORT:-8080}:8080", "read_only: true", "cap_drop: [ALL]", "healthcheck:", "quant_lab_data:", "quant_lab_logs:", "tailscale"]
for marker in required:
    if marker not in text:
        raise SystemExit(f"DEPLOYMENT_GATE_FAIL missing: {marker}")
for env_file in (ROOT / ".env", ROOT / "deploy" / ".env"):
    if env_file.exists() and re.search(r"(?m)^[A-Z0-9_]+=(?!\s*$).+", env_file.read_text(encoding="utf-8")):
        raise SystemExit(f"DEPLOYMENT_GATE_FAIL non-empty local secret file: {env_file}")
if re.search(r"(?i)(sk-[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{20,})", text):
    raise SystemExit("DEPLOYMENT_GATE_FAIL possible hard-coded secret")
# Comments may document intentionally unsupported integrations. Look only for
# executable import/call patterns that would create a broker/order capability.
prohibited_code = [p for p in (ROOT / "quant_lab").rglob("*.py") if re.search(r"(?m)^\s*(from|import)\s+(alpaca|ib_insync|joinquant)\b|\b(place_order|submit_order)\s*\(", p.read_text(encoding="utf-8"))]
if prohibited_code:
    raise SystemExit("DEPLOYMENT_GATE_FAIL prohibited broker/order integration in runtime")
print("DEPLOYMENT_GATE_PASS: loopback publish, external volumes, healthcheck, no hard-coded secret/order integration")
