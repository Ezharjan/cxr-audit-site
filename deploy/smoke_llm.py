"""Run inside the API container: synthetic structured-output check, no patient data."""
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/app/cxr_audit")
from openai import OpenAI
from pydantic import BaseModel
from helpers import getLLMJSON


class Probe(BaseModel):
    status: str


base_url = os.environ["OLLAMA_BASE_URL"].rstrip("/")
try:
    urllib.request.urlopen(base_url + "/models", timeout=10)
except urllib.error.HTTPError as exc:
    assert exc.code == 401, "Unexpected unauthenticated response"
else:
    raise AssertionError("Inference endpoint accepted an unauthenticated request")

client = OpenAI(base_url=base_url,
                api_key=os.environ["OLLAMA_API_KEY"], timeout=120, max_retries=0)
model = os.environ["OLLAMA_MODEL"]
assert model in {item.id for item in client.models.list().data}, "Configured model is not served"
result = getLLMJSON(
    [{"role": "user", "content": 'Return a JSON object with status exactly "ok".'}],
    client, model, Probe,
)
assert result == {"status": "ok"}, "Synthetic structured-output check failed"
print("Authentication, model discovery and application JSON parsing passed (synthetic input only).")
