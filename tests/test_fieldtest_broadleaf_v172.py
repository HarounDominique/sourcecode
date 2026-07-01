"""Regression tests for the v1.72.0 Broadleaf Commerce field-test fixes.

Each test reproduces the EXACT code pattern that produced a false positive /
false negative / inconsistency in the downstream architecture audit, so the
specific defect classes cannot silently reappear.

  BUG 1  modernize cross_module_tangles: real inter-subsystem edges, not a
         re-labelled subsystem list.
  BUG 2  modernize statically_unreferenced: nested-class FQN qualification (no
         collision) + annotation-value static-constant reference edges.
  BUG 4  migrate-check MIG-031: test-scope classification + condition-specific
         explanation text.
  BUG 5  export --integrations: Spring Security LDAP (LdapUserDetailsMapper
         subclass / DirContextOperations) detection.
  BUG 7  spring-audit endpoints reconcile with `endpoints` (FQN-shaped dynamic
         admin paths excluded from the canonical endpoint list).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sourcecode.repository_ir import build_repo_ir, _minimal_class_symbols
from sourcecode.integration_detector import detect_integrations
from sourcecode.migrate_check import (
    MigrationFinding,
    _classify_code_context,
    _scan_xml_file,
)
from sourcecode.canonical_ir import _FQN_PATH_RE


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# BUG 2 — nested-class FQN qualification + annotation-value reference edges
# ---------------------------------------------------------------------------

# Two DIFFERENT outer types in the SAME package, each with a nested `GroupName`
# and `FieldOrder` constant holder — the exact Broadleaf AdminPresentation shape
# that collapsed onto a single colliding FQN and read as zero-caller dead code.
_ADMIN_PRESENTATION_A = """\
package com.example.config.domain;

public interface SystemPropertyAdminPresentation {
    class GroupName {
        public static final String General = "General";
    }
    class FieldOrder {
        public static final int NAME = 1000;
    }
}
"""

_ADMIN_PRESENTATION_B = """\
package com.example.config.domain;

public interface AbstractModuleConfigurationAdminPresentation {
    class GroupName {
        public static final String General = "General";
    }
    class FieldOrder {
        public static final int NAME = 2000;
    }
}
"""

# An entity that implements presentation interface A and references its nested
# constants from an annotation argument (the reference the static graph missed).
_ENTITY_USING_A = """\
package com.example.config.domain;

import javax.persistence.Entity;

@Entity
public class SystemPropertyImpl implements SystemPropertyAdminPresentation {
    @AdminPresentation(friendlyName = "name", group = GroupName.General, order = FieldOrder.NAME)
    protected String name;
}
"""


def _build(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "src/main/java/com/example/config/domain/SystemPropertyAdminPresentation.java", _ADMIN_PRESENTATION_A)
    _write(root, "src/main/java/com/example/config/domain/AbstractModuleConfigurationAdminPresentation.java", _ADMIN_PRESENTATION_B)
    _write(root, "src/main/java/com/example/config/domain/SystemPropertyImpl.java", _ENTITY_USING_A)
    files = [str(p.relative_to(root)) for p in root.rglob("*.java")]
    return root, build_repo_ir(files, root)


def test_nested_class_fqns_do_not_collide(tmp_path):
    root, ir = _build(tmp_path)
    fqns = {n["fqn"] for n in ir["graph"]["nodes"]}
    a = "com.example.config.domain.SystemPropertyAdminPresentation.GroupName"
    b = "com.example.config.domain.AbstractModuleConfigurationAdminPresentation.GroupName"
    assert a in fqns, "nested GroupName must be qualified by its enclosing type"
    assert b in fqns, "the OTHER GroupName must be a distinct qualified FQN"
    # The pre-fix collision FQN (package + bare simple name) must NOT appear.
    assert "com.example.config.domain.GroupName" not in fqns


def test_minimal_class_symbols_qualify_nested_types():
    # Direct unit test of the pre-scan fast path: nested types get Outer.Inner.
    syms = _minimal_class_symbols(_ADMIN_PRESENTATION_A, "com.example.config.domain", "X.java")
    names = {s.symbol for s in syms}
    assert "com.example.config.domain.SystemPropertyAdminPresentation" in names
    assert "com.example.config.domain.SystemPropertyAdminPresentation.GroupName" in names
    assert "com.example.config.domain.SystemPropertyAdminPresentation.FieldOrder" in names
    assert "com.example.config.domain.GroupName" not in names


def test_annotation_value_reference_creates_incoming_edge(tmp_path):
    root, ir = _build(tmp_path)
    nodes = {n["fqn"]: n for n in ir["graph"]["nodes"]}
    gn = nodes["com.example.config.domain.SystemPropertyAdminPresentation.GroupName"]
    fo = nodes["com.example.config.domain.SystemPropertyAdminPresentation.FieldOrder"]
    # `@AdminPresentation(group = GroupName.General, order = FieldOrder.NAME)` on the
    # entity is a real reference — the constant holders must NOT read as zero in-degree.
    assert gn["in_degree"] >= 1, "GroupName referenced from annotation must have callers"
    assert fo["in_degree"] >= 1, "FieldOrder referenced from annotation must have callers"
    # And a `references` edge must exist from the entity to the resolved nested type.
    ref_edges = {
        (e["from"], e["to"]) for e in ir["graph"]["edges"] if e["type"] == "references"
    }
    assert ("com.example.config.domain.SystemPropertyImpl",
            "com.example.config.domain.SystemPropertyAdminPresentation.GroupName") in ref_edges


def test_class_level_fully_qualified_nested_ref_credits_nested_holder(tmp_path):
    # Broadleaf presentation-interface shape: a pre-scan-skipped interface (no Spring
    # markers) whose CLASS-LEVEL annotation references its OWN nested holders in the
    # fully-qualified `Outer.Nested.CONST` form. The nested holder actually read must
    # get the reference edge — not merely the outer type — so it is not false dead code.
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "src/main/java/com/example/cfg/FooAdminPresentation.java", """\
package com.example.cfg;

@AdminPresentationClass(tabs = {
    @AdminTabPresentation(
        name = FooAdminPresentation.TabName.General,
        order = FooAdminPresentation.TabOrder.General)
})
public interface FooAdminPresentation {
    class TabName { public static final String General = "General"; }
    class TabOrder { public static final int General = 1000; }
    class Unused  { public static final String X = "x"; }
}
""")
    files = [str(p.relative_to(root)) for p in root.rglob("*.java")]
    ir = build_repo_ir(files, root)
    nodes = {n["fqn"]: n for n in ir["graph"]["nodes"]}
    assert nodes["com.example.cfg.FooAdminPresentation.TabName"]["in_degree"] >= 1
    assert nodes["com.example.cfg.FooAdminPresentation.TabOrder"]["in_degree"] >= 1
    # A genuinely unreferenced nested holder must still read as zero (no false edge).
    assert nodes["com.example.cfg.FooAdminPresentation.Unused"]["in_degree"] == 0


# ---------------------------------------------------------------------------
# BUG 1 — cross_module_tangles measures real inter-subsystem coupling
# ---------------------------------------------------------------------------

def test_cross_module_tangles_are_real_edges_not_subsystem_echo(tmp_path):
    # Two modules that inject each other → a genuine mutual (cyclic) tangle.
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "src/main/java/com/example/orders/OrderService.java", """\
package com.example.orders;
import com.example.payments.PaymentService;
import org.springframework.stereotype.Service;
@Service
public class OrderService {
    private final PaymentService paymentService;
    public OrderService(PaymentService paymentService) { this.paymentService = paymentService; }
}
""")
    _write(root, "src/main/java/com/example/payments/PaymentService.java", """\
package com.example.payments;
import com.example.orders.OrderService;
import org.springframework.stereotype.Service;
@Service
public class PaymentService {
    private final OrderService orderService;
    public PaymentService(OrderService orderService) { this.orderService = orderService; }
}
""")
    files = [str(p.relative_to(root)) for p in root.rglob("*.java")]
    from typer.testing import CliRunner
    from sourcecode.cli import app
    out = root / "modernize.json"
    res = CliRunner().invoke(app, ["modernize", str(root), "--output", str(out)])
    assert res.exit_code == 0, res.output
    import json
    data = json.loads(out.read_text())
    tangles = data.get("cross_module_tangles", [])
    # The field must carry edge data (from/to package + edge_count), NOT the
    # {label, class_count, member_count} subsystem shape it used to echo.
    assert tangles, "mutual cross-module coupling must be reported"
    t = tangles[0]
    assert "from_package" in t and "to_package" in t
    assert t["from_package"] != t["to_package"]
    assert t["edge_count"] >= 1
    assert t["mutual"] is True, "orders<->payments is a bidirectional tangle"
    # The old echo keys must be gone from the tangle entries.
    assert "member_count" not in t and "class_count" not in t
    # BUG 6: in_degree metric must be self-describing so it is not confused with
    # explain's (distinct-caller) count.
    assert "high_coupling_nodes_note" in data
    assert "raw count of incoming graph edges" in data["high_coupling_nodes_note"]


# ---------------------------------------------------------------------------
# BUG 4 — MIG-031 test-scope + condition-specific explanation
# ---------------------------------------------------------------------------

def test_mig031_test_scoped_xml_classified_as_test():
    # Broadleaf bl-applicationContext-test-security.xml lives under src/main/resources
    # yet is test-only scaffolding — filename `test` marker must bucket it as test so
    # it never inflates blocking_count.
    f = MigrationFinding(
        id="x", rule_id="MIG-031", severity="high", title="t",
        source_file="integration/src/main/resources/bl-applicationContext-test-security.xml",
        first_line=1,
    )
    assert _classify_code_context(f) == "test"


def test_mig031_explanation_only_reports_the_matched_trigger():
    # Only <http auto-config="true"> is present; the schema line is the UNVERSIONED
    # spring-security.xsd. The explanation must NOT claim a versioned legacy schema.
    xml = (
        '<beans xmlns:sec="http://www.springframework.org/schema/security"\n'
        '  xsi:schemaLocation="http://www.springframework.org/schema/security '
        'http://www.springframework.org/schema/security/spring-security.xsd">\n'
        '  <sec:http auto-config="true"></sec:http>\n'
        '</beans>\n'
    )
    findings = _scan_xml_file(xml, "src/main/resources/security.xml")
    mig031 = [f for f in findings if f.rule_id == "MIG-031"]
    assert len(mig031) == 1
    expl = mig031[0].explanation.lower()
    assert "auto-config" in expl
    assert "versioned legacy schema" not in expl, "must not assert an untriggered condition"


def test_mig031_explanation_reports_versioned_schema_when_matched():
    xml = (
        '<beans xsi:schemaLocation="http://www.springframework.org/schema/security '
        'http://www.springframework.org/schema/security/spring-security-3.2.xsd">\n'
        '</beans>\n'
    )
    findings = _scan_xml_file(xml, "src/main/resources/security.xml")
    mig031 = [f for f in findings if f.rule_id == "MIG-031"]
    assert len(mig031) == 1
    assert "versioned legacy schema" in mig031[0].explanation.lower()


# ---------------------------------------------------------------------------
# BUG 5 — Spring Security LDAP integration detection
# ---------------------------------------------------------------------------

def test_ldap_user_details_mapper_subclass_detected(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "src/main/java/com/example/BroadleafActiveDirectoryUserDetailsMapper.java", """\
package com.example;

import org.springframework.ldap.core.DirContextOperations;
import org.springframework.security.ldap.userdetails.LdapUserDetailsMapper;

public class BroadleafActiveDirectoryUserDetailsMapper extends LdapUserDetailsMapper {
    public Object mapUserFromContext(DirContextOperations ctx, String username) {
        return ctx.getStringAttribute("cn");
    }
}
""")
    rels = [str(p.relative_to(root)) for p in root.rglob("*.java")]
    result = detect_integrations(rels, root)
    assert result["by_kind"].get("ldap", 0) >= 1, "LdapUserDetailsMapper/DirContextOperations = LDAP"
    ldap = [r for r in result["integrations"] if r["kind"] == "ldap"]
    clients = {r["client"] for r in ldap}
    assert "spring-security-ldap" in clients or "spring-ldap" in clients


# ---------------------------------------------------------------------------
# BUG 7 — canonical endpoint list excludes FQN-shaped dynamic-admin paths
# ---------------------------------------------------------------------------

def test_fqn_shaped_path_regex_matches_dynamic_admin_route():
    # Broadleaf @AdminSection registers entity class FQNs as URL segments; these are
    # not real REST endpoints and are filtered by BOTH the `endpoints` command and
    # the canonical IR now (so spring-audit endpoints_analyzed reconciles).
    assert _FQN_PATH_RE.search("/org.broadleafcommerce.core.catalog.domain.Product")
    assert not _FQN_PATH_RE.search("/api/v1/products")
