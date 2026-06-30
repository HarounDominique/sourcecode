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


# ── v1.68.0 regression: BUG #1 / BUG #2 (Netflix Eureka field test) ──────────

_SMTP_LOG_FP = """\
package com.netflix.discovery;
public class DiscoveryClient {
    public void start() {
        try {
            init();
        } catch (Exception e) {
            logger.warn("Transport initialization failure", e);
        }
    }
}
"""

_REAL_JAVAMAIL = """\
package com.x;
import javax.mail.Transport;
import javax.mail.internet.MimeMessage;
public class Mailer {
    public void send(MimeMessage msg) throws Exception {
        Transport.send(msg);
    }
}
"""

_DNS_DIRCONTEXT = """\
package com.netflix.discovery.endpoint;
import javax.naming.directory.DirContext;
import javax.naming.directory.InitialDirContext;
public class DnsResolver {
    private static final String DNS_NAMING_FACTORY = "com.sun.jndi.dns.DnsContextFactory";
    public static DirContext getDirContext() throws Exception {
        java.util.Hashtable<String, String> env = new java.util.Hashtable<>();
        env.put("java.naming.factory.initial", DNS_NAMING_FACTORY);
        return new InitialDirContext(env);
    }
}
"""

_UNKNOWN_DIRCONTEXT = """\
package com.x;
import javax.naming.directory.InitialDirContext;
public class Resolver {
    public Object lookup() throws Exception {
        return new InitialDirContext(env);
    }
}
"""


def test_smtp_log_literal_not_detected(tmp_path):
    # BUG #1: the word "Transport" inside a log string with NO JavaMail import must
    # NOT be reported as an SMTP integration.
    p = tmp_path / "DiscoveryClient.java"
    p.write_text(_SMTP_LOG_FP, encoding="utf-8")
    out = detect_integrations(["DiscoveryClient.java"], tmp_path)
    smtp = [r for r in out["integrations"] if r["kind"] == "smtp"]
    assert smtp == [], out


def test_real_javamail_still_detected(tmp_path):
    # The gate must not suppress genuine JavaMail usage.
    p = tmp_path / "Mailer.java"
    p.write_text(_REAL_JAVAMAIL, encoding="utf-8")
    out = detect_integrations(["Mailer.java"], tmp_path)
    smtp = [r for r in out["integrations"] if r["kind"] == "smtp"]
    assert smtp, out
    assert all(r["client"] == "javamail" for r in smtp)


def test_jndi_dns_not_mislabeled_ldap(tmp_path):
    # BUG #2: DirContext with DnsContextFactory is DNS, never LDAP.
    p = tmp_path / "DnsResolver.java"
    p.write_text(_DNS_DIRCONTEXT, encoding="utf-8")
    out = detect_integrations(["DnsResolver.java"], tmp_path)
    kinds = {r["kind"] for r in out["integrations"]}
    assert "dns" in kinds, out
    assert "ldap" not in kinds, out
    dns = [r for r in out["integrations"] if r["kind"] == "dns"]
    assert dns[0]["client"] == "jndi-dns"


def test_jndi_unknown_factory_low_confidence(tmp_path):
    # BUG #2: DirContext with no resolvable factory → explicit low-confidence
    # unknown, never an assumed LDAP.
    p = tmp_path / "Resolver.java"
    p.write_text(_UNKNOWN_DIRCONTEXT, encoding="utf-8")
    out = detect_integrations(["Resolver.java"], tmp_path)
    kinds = {r["kind"] for r in out["integrations"]}
    assert "ldap" not in kinds, out
    unk = [r for r in out["integrations"] if r["kind"] == "naming-directory-unknown"]
    assert unk, out
    assert unk[0]["confidence"] == "low"


def test_export_integrations_via_cli(tmp_path):
    repo = _write_repo(tmp_path)
    res = runner.invoke(app, ["export", str(repo), "--integrations", "--format", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert "integrations" in data
    assert data["integrations"]["count"] >= 4
