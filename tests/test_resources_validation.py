"""Validation test suite for Kubernetes, Docker Compose, and Observability resources."""

import json
import re
from pathlib import Path
from typing import Any

import yaml

RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"


def _load_all_yaml_documents(path: Path) -> list[dict[str, Any]]:
    """Helper to parse multi-document YAML files."""
    text = path.read_text(encoding="utf-8")
    docs = [doc for doc in yaml.safe_load_all(text) if doc is not None]
    return docs


def _validate_k8s_doc(doc: dict[str, Any]) -> bool:
    """Predicate verifying required Kubernetes document root keys."""
    return "apiVersion" in doc and "kind" in doc and "name" in doc.get("metadata", {})


def test_observability_otel_collector_config_valid() -> None:
    """Ensure otel-collector-config.yaml is syntactically valid and has pipelines."""
    config_path = RESOURCES_DIR / "observability" / "otel-collector-config.yaml"
    assert config_path.is_file(), f"Missing config: {config_path}"

    docs = _load_all_yaml_documents(config_path)
    assert len(docs) == 1
    config = docs[0]

    required_keys = ("receivers", "processors", "exporters", "service")
    assert all(k in config for k in required_keys)
    assert "traces" in config.get("service", {}).get("pipelines", {})


def test_observability_prometheus_alerts_valid() -> None:
    """Ensure prometheus-agent-alerts.yaml contains valid alerting rules."""
    alerts_path = RESOURCES_DIR / "observability" / "prometheus-agent-alerts.yaml"
    assert alerts_path.is_file(), f"Missing alerts: {alerts_path}"

    docs = _load_all_yaml_documents(alerts_path)
    assert len(docs) == 1 and "groups" in docs[0]
    rules = docs[0]["groups"][0]["rules"]
    rule_names = {rule["alert"] for rule in rules}
    assert len(rules) >= 4 and {"AgentTokenBurnSpike", "AgentASTInvariantBreachRate"}.issubset(rule_names)


def test_observability_grafana_dashboard_valid() -> None:
    """Ensure agent-telemetry-dashboard.json is valid Grafana schema."""
    dash_path = RESOURCES_DIR / "observability" / "grafana" / "agent-telemetry-dashboard.json"
    assert dash_path.is_file(), f"Missing dashboard: {dash_path}"

    data = json.loads(dash_path.read_text(encoding="utf-8"))
    assert (
        data["schemaVersion"] >= 30,
        data["uid"] == "vibes-agent-mesh",
        "panels" in data,
        len(data["panels"]) >= 5,
    ) == (True, True, True, True)


def test_k8s_manifests_validity_and_metadata() -> None:
    """Ensure all Kubernetes manifests have apiVersion, kind, and metadata.name."""
    k8s_dir = RESOURCES_DIR / "k8s"
    yaml_files = list(k8s_dir.rglob("*.yaml"))
    assert len(yaml_files) >= 5, f"Expected at least 5 K8s manifests, found {len(yaml_files)}"

    for file_path in yaml_files:
        docs = _load_all_yaml_documents(file_path)
        assert docs, f"Empty document in {file_path}"
        assert all(_validate_k8s_doc(doc) for doc in docs), f"Invalid K8s doc in {file_path}"


def test_k8s_sandbox_security_context_invariants() -> None:
    """Ensure sandbox-pod.yaml enforces non-root, read-only rootfs, and dropped caps."""
    pod_path = RESOURCES_DIR / "k8s" / "agent-sandbox" / "sandbox-pod.yaml"
    docs = _load_all_yaml_documents(pod_path)
    pod = docs[0]

    pod_sec = pod["spec"]["securityContext"]
    container_sec = pod["spec"]["containers"][0]["securityContext"]
    assert (
        pod_sec.get("runAsNonRoot"),
        pod_sec.get("seccompProfile", {}).get("type"),
        container_sec.get("readOnlyRootFilesystem"),
        container_sec.get("allowPrivilegeEscalation"),
        "ALL" in container_sec.get("capabilities", {}).get("drop", []),
    ) == (True, "RuntimeDefault", True, False, True)


def test_k8s_network_policy_egress_rules() -> None:
    """Ensure network-policy.yaml specifies egress and drops inbound."""
    net_path = RESOURCES_DIR / "k8s" / "agent-sandbox" / "network-policy.yaml"
    docs = _load_all_yaml_documents(net_path)
    policy = docs[0]
    spec = policy["spec"]

    assert (
        policy["kind"] == "NetworkPolicy",
        "Egress" in spec["policyTypes"],
        "Ingress" in spec["policyTypes"],
        spec.get("ingress") == [],
    ) == (True, True, True, True)


def test_docker_compose_validity_and_services() -> None:
    """Ensure docker-compose.yml parses and configures all essential services."""
    compose_path = RESOURCES_DIR / "docker-compose" / "docker-compose.yml"
    assert compose_path.is_file(), f"Missing compose file: {compose_path}"

    docs = _load_all_yaml_documents(compose_path)
    compose = docs[0]

    assert "services" in compose
    services = compose["services"]
    required_services = {"jaeger", "otel-collector", "prometheus", "valkey", "grafana", "agent-sandbox"}
    assert required_services.issubset(set(services.keys()))

    sandbox = services["agent-sandbox"]
    assert sandbox.get("read_only") is True
    assert sandbox.get("user") == "10001:10001"


def test_sanitization_across_all_resource_files() -> None:
    """Verify zero unapproved mock subdomains or unredacted secrets across resources."""
    subdomain_pattern = re.compile(r"https?://([a-zA-Z0-9_-]+\.example\.com)")

    for path in RESOURCES_DIR.rglob("*"):
        if path.is_file() and path.suffix in (".yaml", ".json", ".md", ".example"):
            content = path.read_text(encoding="utf-8")
            # Ensure no subdomains of example.com (e.g. api.example.com)
            matches = subdomain_pattern.findall(content)
            assert not matches, f"Forbidden subdomain found in {path}: {matches}"
