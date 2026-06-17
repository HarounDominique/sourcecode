"""Tests Fase 17 — outbound integration detection (C4INT-01/02/03/04)."""

import json

from typer.testing import CliRunner

from sourcecode.cli import app
from sourcecode.integration_detector import detect_integrations

runner = CliRunner()

_REST = """\
package com.x;
import org.springframework.web.client.RestTemplate;
public class RestCaller {
    private final RestTemplate rt = new RestTemplate();
    public String call() {
        return rt.getForObject("https://api.example.com/v1/foo", String.class);
    }
}
"""

_FEIGN = """\
package com.x;
import org.springframework.cloud.openfeign.FeignClient;
@FeignClient(name = "billing", url = "https://billing.example.com")
public interface BillingClient {
    String charge();
}
"""

_LDAP_JMS = """\
package com.x;
import org.springframework.ldap.core.LdapTemplate;
import org.springframework.jms.core.JmsTemplate;
public class Mixed {
    private LdapTemplate ldap;
    private JmsTemplate jms;
    public void send() { jms.convertAndSend("queue.orders", "x"); }
}
"""


def _write_repo(tmp_path):
    pkg = tmp_path / "src" / "main" / "java" / "com" / "x"
    pkg.mkdir(parents=True)
    (pkg / "RestCaller.java").write_text(_REST, encoding="utf-8")
    (pkg / "BillingClient.java").write_text(_FEIGN, encoding="utf-8")
    (pkg / "Mixed.java").write_text(_LDAP_JMS, encoding="utf-8")
    return tmp_path


def test_detect_rest_feign_ldap_jms(tmp_path):
    repo = _write_repo(tmp_path)
    rels = ["src/main/java/com/x/" + f for f in ("RestCaller.java", "BillingClient.java", "Mixed.java")]
    out = detect_integrations(rels, repo)
    kinds = out["by_kind"]
    assert kinds.get("http", 0) >= 2  # resttemplate + feign
    assert kinds.get("ldap", 0) >= 1
    assert kinds.get("jms", 0) >= 1
    clients = {r["client"] for r in out["integrations"]}
    assert {"resttemplate", "feign", "ldaptemplate", "jmstemplate"} <= clients, clients


def test_feign_target_url_captured(tmp_path):
    repo = _write_repo(tmp_path)
    out = detect_integrations(["src/main/java/com/x/BillingClient.java"], repo)
    feign = [r for r in out["integrations"] if r["client"] == "feign"]
    assert feign, out
    assert feign[0]["target"] == "https://billing.example.com"


def test_rest_url_and_evidence(tmp_path):
    repo = _write_repo(tmp_path)
    out = detect_integrations(["src/main/java/com/x/RestCaller.java"], repo)
    rest = [r for r in out["integrations"] if r["client"] == "resttemplate"]
    assert rest, out
    # at least one hit carries the literal URL and a file:line anchor
    assert any(r["target"] == "https://api.example.com/v1/foo" for r in rest), rest
    assert all(":" in r["evidence"] for r in rest)


def test_import_lines_not_counted(tmp_path):
    # A file that only imports RestTemplate but never uses it → no integration.
    pkg = tmp_path / "src" / "main" / "java" / "com" / "x"
    pkg.mkdir(parents=True)
    (pkg / "OnlyImport.java").write_text(
        "package com.x;\nimport org.springframework.web.client.RestTemplate;\n"
        "public class OnlyImport {}\n",
        encoding="utf-8",
    )
    out = detect_integrations(["src/main/java/com/x/OnlyImport.java"], tmp_path)
    assert out["count"] == 0, out


def test_export_integrations_via_cli(tmp_path):
    repo = _write_repo(tmp_path)
    res = runner.invoke(app, ["export", str(repo), "--integrations", "--format", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert "integrations" in data
    assert data["integrations"]["count"] >= 4
