import os
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
TIMEOUT_SECONDS = 120


class ApiError(Exception):
    """Raised whenever the backend can't be reached or returns a non-200 response."""


def _post(path: str, **kwargs: Any) -> dict:
    url = f"{API_BASE_URL}{path}"
    try:
        response = requests.post(url, timeout=TIMEOUT_SECONDS, **kwargs)
    except requests.exceptions.Timeout as exc:
        raise ApiError(
            f"The request to {path} timed out after {TIMEOUT_SECONDS}s. The backend may still "
            "be working on it - try again in a moment."
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise ApiError(f"Could not connect to the backend at {API_BASE_URL}. Is it running?") from exc
    except requests.exceptions.RequestException as exc:
        raise ApiError(f"Request to {path} failed: {exc}") from exc

    if response.status_code != 200:
        raise ApiError(f"Backend returned {response.status_code} for {path}: {response.text}")

    return response.json()


def verify_text(text: str) -> dict:
    return _post("/verify", json={"text": text})


def verify_url(url: str) -> dict:
    return _post("/verify/url", json={"url": url})


def verify_upload(file) -> dict:
    """`file` is a file-like object with `.name`/`.type`/`.getvalue()`, e.g. a
    Streamlit UploadedFile from st.file_uploader()."""
    files = {"file": (file.name, file.getvalue(), file.type or "application/octet-stream")}
    return _post("/verify/upload", files=files, data={"caption": ""})
