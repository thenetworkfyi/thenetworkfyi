"""Contracts for the local Prometheus and Alertmanager stack."""

import pathlib

import yaml


_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_COMPOSE_TEXT = (_REPO_ROOT / "docker-compose.yml").read_text()
_COMPOSE_CONFIG = yaml.safe_load(_COMPOSE_TEXT)
_PROMETHEUS_CONFIG = yaml.safe_load((_REPO_ROOT / "prometheus.yml").read_text())
_ALERT_RULES = yaml.safe_load((_REPO_ROOT / "prometheus-alert-rules.yml").read_text())


def _rendered_alertmanager_config() -> dict:
    template = _COMPOSE_CONFIG["configs"]["alertmanager-config"]["content"]
    values = {
        "${ALERTMANAGER_SMTP_SMARTHOST:-}": "smtp.invalid:587",
        "${ALERTMANAGER_SMTP_FROM:-}": "monitoring@validation.invalid",
        "${ALERTMANAGER_SMTP_USERNAME:-}": "",
        "${ALERTMANAGER_SMTP_REQUIRE_TLS:-true}": "false",
        "${ALERTMANAGER_GROUP_WAIT:-30s}": "30s",
        "${ALERTMANAGER_GROUP_INTERVAL:-5m}": "5m",
        "${ALERTMANAGER_WARNING_REPEAT_INTERVAL:-12h}": "12h",
        "${ALERTMANAGER_EVENT_GROUP_INTERVAL:-1m}": "1m",
        "${ALERTMANAGER_EVENT_REPEAT_INTERVAL:-24h}": "24h",
        "${ALERTMANAGER_CRITICAL_GROUP_WAIT:-10s}": "10s",
        "${ALERTMANAGER_CRITICAL_REPEAT_INTERVAL:-1h}": "1h",
        "${ALERTMANAGER_OPERATOR_EMAIL:-}": "operator@validation.invalid",
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    assert "${" not in template
    return yaml.safe_load(template)


def _rules_by_name() -> dict[str, dict]:
    groups = _ALERT_RULES["groups"]
    assert [group["name"] for group in groups] == ["thenetwork-operational"]
    return {rule["alert"]: rule for rule in groups[0]["rules"]}


def test_alert_catalog_covers_worker_collector_and_event_failures():
    rules = _rules_by_name()
    assert set(rules) == {
        "ProducerPollingStale",
        "WorkerBacklogSustained",
        "AutomatedPrimaryIntakePaused",
        "OutboundEmailFailuresRepeated",
        "AgentFailureRateElevated",
        "MessageRejectionsSpiking",
        "CollectorUnavailable",
        "CollectorPipelineDegraded",
        "SystemControlActionObserved",
        "AgentUsageLimitExceeded",
        "ProcessEmailJobExhausted",
    }

    immediate = {
        "SystemControlActionObserved",
        "AgentUsageLimitExceeded",
        "ProcessEmailJobExhausted",
    }
    for name, rule in rules.items():
        assert rule["labels"]["severity"] in {"warning", "critical"}
        assert set(rule["labels"]) == {
            "severity",
            "service",
            "scope",
            "category",
        }
        assert set(rule["annotations"]) == {"summary", "runbook"}
        assert rule["annotations"]["runbook"].startswith("docs/monitoring.md#")
        assert "{{" not in yaml.safe_dump(rule["annotations"])
        if name in immediate:
            assert "for" not in rule
        else:
            assert rule["for"] not in {"0m", "0s"}


def test_rules_filter_admin_controls_and_low_traffic_noise():
    rules = _rules_by_name()
    control_expr = rules["SystemControlActionObserved"]["expr"]
    assert 'actor="system"' in control_expr
    assert 'action=~"pause|ban"' in control_expr
    assert "admin" not in control_expr

    intake_expr = rules["AutomatedPrimaryIntakePaused"]["expr"]
    assert 'reason=~"new_sender_burst|coordinated_abuse"' in intake_expr
    assert "admin" not in intake_expr

    agent_expr = rules["AgentFailureRateElevated"]["expr"]
    assert 'outcome="error"' in agent_expr
    assert ">= 5" in agent_expr
    assert "> 0.25" in agent_expr

    exhausted_expr = rules["ProcessEmailJobExhausted"]["expr"]
    assert "thenetwork_jobs_exhausted_total" in exhausted_expr


def test_alert_content_is_static_and_seal_safe():
    serialized = yaml.safe_dump(_ALERT_RULES).lower()
    prohibited = {
        "trace_id",
        "run_id",
        "sender_id_hash",
        "person_id",
        "event_id",
        "message_content",
        "task_arguments",
        "error_text",
    }
    for value in prohibited:
        assert value not in serialized


def test_prometheus_sends_rules_to_internal_alertmanager():
    assert _PROMETHEUS_CONFIG["alerting"] == {
        "alertmanagers": [{"static_configs": [{"targets": ["alertmanager:9093"]}]}]
    }
    assert _PROMETHEUS_CONFIG["rule_files"] == ["/etc/prometheus/rules/*.yml"]

    prometheus = _COMPOSE_CONFIG["services"]["prometheus"]
    assert (
        "./prometheus-alert-rules.yml:/etc/prometheus/rules/thenetwork.yml:ro"
        in prometheus["volumes"]
    )
    assert prometheus["depends_on"]["alertmanager"]["condition"] == ("service_started")


def test_alertmanager_is_pinned_private_persistent_and_file_secreted():
    alertmanager = _COMPOSE_CONFIG["services"]["alertmanager"]
    assert alertmanager["image"] == "prom/alertmanager:v0.32.1"
    assert alertmanager["ports"] == ["127.0.0.1:9093:9093"]
    assert "alertmanager-data:/alertmanager" in alertmanager["volumes"]
    assert "alertmanager-data" in _COMPOSE_CONFIG["volumes"]
    assert alertmanager["configs"] == [
        {
            "source": "alertmanager-config",
            "target": "/etc/alertmanager/alertmanager.yml",
        }
    ]
    assert alertmanager["secrets"] == [
        {
            "source": "alertmanager-smtp-password",
            "target": "alertmanager-smtp-password",
        }
    ]
    assert _COMPOSE_CONFIG["secrets"]["alertmanager-smtp-password"] == {
        "file": "${ALERTMANAGER_SMTP_PASSWORD_FILE:-/dev/null}"
    }
    assert "ALERTMANAGER_OPERATOR_EMAIL" in _COMPOSE_TEXT
    assert "@example.com" not in _COMPOSE_TEXT


def test_alertmanager_groups_routes_resolves_and_inhibits_safely():
    config = _rendered_alertmanager_config()
    route = config["route"]
    assert route["group_by"] == ["alertname", "severity", "category"]
    assert route["group_wait"] == "30s"
    assert route["group_interval"] == "5m"
    assert route["repeat_interval"] == "12h"

    children = {child["matchers"][0]: child for child in route["routes"]}
    assert children['category="event"'] == {
        "receiver": "operator-email",
        "matchers": ['category="event"'],
        "group_wait": "0s",
        "group_interval": "1m",
        "repeat_interval": "24h",
    }
    assert children['severity="critical"']["repeat_interval"] == "1h"
    assert children['severity="warning"']["repeat_interval"] == "12h"

    assert config["inhibit_rules"] == [
        {
            "source_matchers": ['alertname="CollectorUnavailable"'],
            "target_matchers": ['scope="worker"'],
            "equal": ["service"],
        }
    ]

    email = config["receivers"][0]["email_configs"][0]
    assert email["to"] == "operator@validation.invalid"
    assert email["send_resolved"] is True
    assert config["global"]["smtp_auth_password_file"] == (
        "/run/secrets/alertmanager-smtp-password"
    )
    assert "smtp_auth_password" not in config["global"]
    for field in ("alertname", "severity", "action", "reason"):
        assert f".Labels.{field} }}" in email["text"]
    for field in ("summary", "runbook"):
        assert f".Annotations.{field} }}" in email["text"]
    for value in ("trace_id", "run_id", "sender_id_hash", "error"):
        assert value not in email["text"]


def test_operator_address_is_deployment_provided_not_committed():
    example = (_REPO_ROOT / ".env.example").read_text()
    assert "ALERTMANAGER_OPERATOR_EMAIL=" in example
    line = next(
        line
        for line in example.splitlines()
        if line.startswith("ALERTMANAGER_OPERATOR_EMAIL=")
    )
    assert line == "ALERTMANAGER_OPERATOR_EMAIL="


def test_promtool_fixture_covers_pending_firing_and_resolution():
    fixture = yaml.safe_load(
        (_REPO_ROOT / "tests/fixtures/prometheus-alert-rules.test.yml").read_text()
    )
    assert fixture["rule_files"] == ["../../prometheus-alert-rules.yml"]
    names = {test["name"] for test in fixture["tests"]}
    assert names == {
        "backlog moves from pending to firing and resolves",
        "administrator pause is not an automated incident",
        "automated pause fires and resolves",
        "low traffic does not trigger agent failure rate",
        "agent failure rate requires traffic and stays elevated",
        "system controls notify without a for delay",
    }
