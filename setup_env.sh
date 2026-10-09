python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip certifi
python -m pip install -r requirements.txt
export SSL_CERT_FILE="$(python - <<'PY'
import certifi
print(certifi.where())
PY
)"
export SSL_CERT_DIR=""