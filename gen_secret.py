#!/usr/bin/env python3
"""Generate a Kubernetes Secret manifest for Coinbase API credentials."""
from __future__ import annotations

import argparse
import base64
from pathlib import Path
from typing import Dict, Optional

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException

DEFAULT_KEYS = ["api_key", "api_secret"]


class SecretBuilder:
    def __init__(self, name: str, namespace: str | None) -> None:
        self.name = name
        self.namespace = namespace
        self.data: Dict[str, str] = {}

    def add_file(self, key: str, path: Path) -> None:
        content = self._load_file(path)
        self.data[key] = self._encode(content)

    @staticmethod
    def _load_file(path: Path) -> bytes:
        if not path.is_file():
            raise FileNotFoundError(f"Credential file '{path}' does not exist")
        data = path.read_bytes()
        if not data:
            raise ValueError(f"Credential file '{path}' is empty")
        return data

    @staticmethod
    def _encode(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    def to_manifest(self) -> str:
        lines = ["apiVersion: v1", "kind: Secret", "metadata:", f"  name: {self.name}"]
        if self.namespace:
            lines.append(f"  namespace: {self.namespace}")
        lines.extend(["  labels:", "    app.kubernetes.io/managed-by: coinbase-local-tools"])
        lines.append("type: Opaque")
        lines.append("data:")
        for key, value in sorted(self.data.items()):
            lines.append(f"  {key}: {value}")
        return "\n".join(lines) + "\n"

    def to_k8s_object(self) -> client.V1Secret:
        metadata = client.V1ObjectMeta(name=self.name, namespace=self.namespace)
        return client.V1Secret(
            api_version="v1",
            kind="Secret",
            metadata=metadata,
            type="Opaque",
            data=self.data,
        )


def detect_default_namespace() -> str:
    try:
        _, active_context = config.list_kube_config_contexts()
    except ConfigException:
        try:
            config.load_kube_config()
            _, active_context = config.list_kube_config_contexts()
        except ConfigException:
            return "default"
    if not active_context:
        return "default"
    context_details = active_context.get("context", {})
    return context_details.get("namespace", "default")


def ensure_namespace(namespace: Optional[str], submit: bool) -> Optional[str]:
    if submit and not namespace:
        namespace = detect_default_namespace()
    return namespace


def load_credentials(args: argparse.Namespace) -> Dict[str, Path]:
    file_map: Dict[str, Path] = {}
    for key in DEFAULT_KEYS:
        path_value = getattr(args, f"{key}_file", None)
        if path_value:
            file_map[key] = Path(path_value)
    if not file_map:
        raise SystemExit("error: at least one credential file must be provided")
    return file_map


def submit_secret(secret: client.V1Secret, force: bool) -> None:
    try:
        config.load_kube_config()
    except ConfigException as exc:
        raise SystemExit(f"error: failed to load kubeconfig: {exc}")

    api = client.CoreV1Api()
    namespace = secret.metadata.namespace or "default"
    name = secret.metadata.name

    try:
        api.read_namespaced_secret(name=name, namespace=namespace)
        exists = True
    except client.ApiException as exc:
        if exc.status == 404:
            exists = False
        else:
            raise SystemExit(f"error: Kubernetes API error {exc.status}: {exc.reason}")

    if exists and not force:
        print(f"Secret '{name}' already exists in namespace '{namespace}'. Use --force to overwrite.")
        return

    body = secret
    if exists:
        api.replace_namespaced_secret(name=name, namespace=namespace, body=body)
        print(f"Updated secret '{name}' in namespace '{namespace}'")
    else:
        api.create_namespaced_secret(namespace=namespace, body=body)
        print(f"Created secret '{name}' in namespace '{namespace}'")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a Kubernetes Secret manifest for Coinbase credentials")
    parser.add_argument("--name", required=True, help="Name of the Kubernetes Secret")
    parser.add_argument("--namespace", "-n", help="Namespace for the Secret")
    parser.add_argument("--api-key-file", "-k", dest="api_key_file", help="Path to the API key file")
    parser.add_argument("--api-secret-file", "-s", dest="api_secret_file", help="Path to the API secret file")
    parser.add_argument("--output", "-o", help="Write manifest to this file instead of stdout")
    parser.add_argument("--force", action="store_true", help="Overwrite the output file if it exists")
    parser.add_argument("--submit", action="store_true", help="Apply the secret to the current Kubernetes cluster")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    file_map = load_credentials(args)
    namespace = ensure_namespace(args.namespace, args.submit)

    builder = SecretBuilder(args.name, namespace)
    for key, path in file_map.items():
        builder.add_file(key, path)

    if args.submit:
        secret_obj = builder.to_k8s_object()
        submit_secret(secret_obj, args.force)

    output_requested = args.output is not None
    if output_requested:
        output_path = Path(args.output)
        if output_path.exists() and not args.force:
            raise SystemExit(f"error: output file '{output_path}' already exists (use --force to overwrite)")
        output_path.write_text(builder.to_manifest(), encoding="utf-8")
        print(f"Wrote {output_path}")
    elif not args.submit:
        print(builder.to_manifest(), end="")


if __name__ == "__main__":
    main()
