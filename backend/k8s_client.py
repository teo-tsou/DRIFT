"""Lightweight K8s API client using kubeconfig credentials."""

import base64
import logging
import os
import tempfile
from typing import Optional

import requests
import yaml

logger = logging.getLogger(__name__)


def _k8s_get(kubeconfig_path: str, path: str) -> Optional[dict]:
    try:
        with open(kubeconfig_path) as f:
            kc = yaml.safe_load(f)
        cluster_info = kc["clusters"][0]["cluster"]
        server = cluster_info["server"]
        user_info = kc["users"][0]["user"]
        cert = user_info.get("client-certificate-data")
        key = user_info.get("client-key-data")

        cert_file = key_file = ca_file = None
        if cert and key:
            ct = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
            ct.write(base64.b64decode(cert)); ct.flush(); cert_file = ct.name
            kt = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
            kt.write(base64.b64decode(key)); kt.flush(); key_file = kt.name
        ca_data = cluster_info.get("certificate-authority-data")
        if ca_data:
            cat = tempfile.NamedTemporaryFile(delete=False, suffix=".ca")
            cat.write(base64.b64decode(ca_data)); cat.flush(); ca_file = cat.name

        resp = requests.get(
            f"{server}{path}",
            cert=(cert_file, key_file) if cert_file else None,
            verify=ca_file if ca_file else False,
            timeout=10,
        )
        for f in [cert_file, key_file, ca_file]:
            if f:
                try: os.unlink(f)
                except Exception: pass
        return resp.json() if resp.status_code == 200 else None
    except Exception as e:
        logger.debug(f"K8s GET failed ({path}): {e}")
        return None


def _k8s_post(kubeconfig_path: str, path: str, body: dict, _err: list = None) -> Optional[dict]:
    try:
        with open(kubeconfig_path) as f:
            kc = yaml.safe_load(f)
        cluster_info = kc["clusters"][0]["cluster"]
        server = cluster_info["server"]
        user_info = kc["users"][0]["user"]
        cert = user_info.get("client-certificate-data")
        key = user_info.get("client-key-data")

        cert_file = key_file = ca_file = None
        if cert and key:
            ct = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
            ct.write(base64.b64decode(cert)); ct.flush(); cert_file = ct.name
            kt = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
            kt.write(base64.b64decode(key)); kt.flush(); key_file = kt.name
        ca_data = cluster_info.get("certificate-authority-data")
        if ca_data:
            cat = tempfile.NamedTemporaryFile(delete=False, suffix=".ca")
            cat.write(base64.b64decode(ca_data)); cat.flush(); ca_file = cat.name

        resp = requests.post(
            f"{server}{path}",
            json=body,
            headers={"Content-Type": "application/json"},
            cert=(cert_file, key_file) if cert_file else None,
            verify=ca_file if ca_file else False,
            timeout=15,
        )
        for f in [cert_file, key_file, ca_file]:
            if f:
                try: os.unlink(f)
                except Exception: pass
        if resp.status_code in (200, 201):
            return resp.json()
        try:
            msg = resp.json().get("message", resp.text[:300]) if resp.text else f"HTTP {resp.status_code}"
        except ValueError:   # non-JSON body (e.g. plaintext "404 page not found")
            msg = f"HTTP {resp.status_code}: {(resp.text or '').strip()[:300]}"
        logger.warning(f"K8s POST {path} returned {resp.status_code}: {msg}")
        if _err is not None:
            _err.append(msg)
        return None
    except Exception as e:
        logger.debug(f"K8s POST failed ({path}): {e}")
        if _err is not None:
            _err.append(str(e))
        return None


def _k8s_delete(kubeconfig_path: str, path: str) -> bool:
    try:
        with open(kubeconfig_path) as f:
            kc = yaml.safe_load(f)
        cluster_info = kc["clusters"][0]["cluster"]
        server = cluster_info["server"]
        user_info = kc["users"][0]["user"]
        cert = user_info.get("client-certificate-data")
        key = user_info.get("client-key-data")

        cert_file = key_file = ca_file = None
        if cert and key:
            ct = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
            ct.write(base64.b64decode(cert)); ct.flush(); cert_file = ct.name
            kt = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
            kt.write(base64.b64decode(key)); kt.flush(); key_file = kt.name
        ca_data = cluster_info.get("certificate-authority-data")
        if ca_data:
            cat = tempfile.NamedTemporaryFile(delete=False, suffix=".ca")
            cat.write(base64.b64decode(ca_data)); cat.flush(); ca_file = cat.name

        resp = requests.delete(
            f"{server}{path}",
            cert=(cert_file, key_file) if cert_file else None,
            verify=ca_file if ca_file else False,
            timeout=15,
        )
        for f in [cert_file, key_file, ca_file]:
            if f:
                try: os.unlink(f)
                except Exception: pass
        return resp.status_code in (200, 202, 204)
    except Exception as e:
        logger.debug(f"K8s DELETE failed ({path}): {e}")
        return False


def _k8s_patch(kubeconfig_path: str, path: str, body: dict, _err: list = None) -> Optional[dict]:
    """Merge PATCH (application/merge-patch+json)."""
    try:
        with open(kubeconfig_path) as f:
            kc = yaml.safe_load(f)
        cluster_info = kc["clusters"][0]["cluster"]
        server = cluster_info["server"]
        user_info = kc["users"][0]["user"]
        cert = user_info.get("client-certificate-data")
        key = user_info.get("client-key-data")

        cert_file = key_file = ca_file = None
        if cert and key:
            ct = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
            ct.write(base64.b64decode(cert)); ct.flush(); cert_file = ct.name
            kt = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
            kt.write(base64.b64decode(key)); kt.flush(); key_file = kt.name
        ca_data = cluster_info.get("certificate-authority-data")
        if ca_data:
            cat = tempfile.NamedTemporaryFile(delete=False, suffix=".ca")
            cat.write(base64.b64decode(ca_data)); cat.flush(); ca_file = cat.name

        resp = requests.patch(
            f"{server}{path}",
            json=body,
            headers={"Content-Type": "application/merge-patch+json"},
            cert=(cert_file, key_file) if cert_file else None,
            verify=ca_file if ca_file else False,
            timeout=15,
        )
        for f in [cert_file, key_file, ca_file]:
            if f:
                try: os.unlink(f)
                except Exception: pass
        if resp.status_code in (200, 201):
            return resp.json()
        try:
            msg = resp.json().get("message", resp.text[:300]) if resp.text else f"HTTP {resp.status_code}"
        except ValueError:   # non-JSON body (e.g. plaintext "404 page not found")
            msg = f"HTTP {resp.status_code}: {(resp.text or '').strip()[:300]}"
        logger.warning(f"K8s PATCH {path} returned {resp.status_code}: {msg}")
        if _err is not None:
            _err.append(msg)
        return None
    except Exception as e:
        logger.debug(f"K8s PATCH failed ({path}): {e}")
        if _err is not None:
            _err.append(str(e))
        return None
