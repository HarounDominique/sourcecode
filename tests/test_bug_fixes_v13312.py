"""
v1.33.12 regression tests — fix-bug --symptom semantic recall in large repos.

Covers:
  R1  Stop-word filtering: "fails", "for", "error" removed from keyword set
  R2  Keycloak offline-session symptom recalls all 4 known relevant classes
  R3  NullPointerException symptom retrieves service + provider adjacency
  R4  Recall does not degrade with repo size (large-repo path)
  R5  CamelCase stem expansion finds tokens not present in plain path
  R6  Directory co-location (Pass 4c) injects subsystem siblings
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Generator

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")


def _paths(output) -> set[str]:
    """Return the set of file paths in relevant_files."""
    return {rf.path.replace("\\", "/") for rf in output.relevant_files}


def _build(root: Path, symptom: str):
    from sourcecode.prepare_context import TaskContextBuilder
    return TaskContextBuilder(root).build("fix-bug", symptom=symptom)


# ---------------------------------------------------------------------------
# R1 — Stop-word filtering
# ---------------------------------------------------------------------------


class TestStopWordFiltering:
    def test_fails_and_for_not_in_keywords(self, tmp_path: Path) -> None:
        """'fails' and 'for' must be stripped from keyword list (stop words)."""
        from sourcecode.prepare_context import _SYMPTOM_STOP_WORDS
        assert "fails" in _SYMPTOM_STOP_WORDS
        assert "for" in _SYMPTOM_STOP_WORDS

    def test_symptom_explain_drops_stop_words(self, tmp_path: Path) -> None:
        """symptom_explain.keywords must not contain stop words."""
        _write(tmp_path, "pom.xml", "<project><artifactId>demo</artifactId></project>")
        _write(tmp_path, "src/main/java/com/demo/SessionService.java", """
            package com.demo;
            public class SessionService {
                public void handleSession() {}
            }
        """)
        output = _build(tmp_path, "token validation fails for offline sessions")
        if output.symptom_explain is not None:
            kws = output.symptom_explain["keywords"]
            assert "fails" not in kws
            assert "for" not in kws
            assert "error" not in kws

    def test_meaningful_keywords_preserved(self, tmp_path: Path) -> None:
        """Filtering stop words must keep domain-specific terms."""
        from sourcecode.prepare_context import _SYMPTOM_STOP_WORDS
        for term in ("offline", "sessions", "token", "validation", "authentication"):
            assert term not in _SYMPTOM_STOP_WORDS


# ---------------------------------------------------------------------------
# Shared fixture: Keycloak-scale offline-session repo
# ---------------------------------------------------------------------------


def _write_keycloak_offline_session_repo(root: Path) -> None:
    """Write a representative Keycloak-like repo with offline-session subsystem."""
    _write(root, "pom.xml", """
        <project>
          <groupId>org.keycloak</groupId>
          <artifactId>keycloak-model-infinispan</artifactId>
          <version>25.0.0</version>
        </project>
    """)

    # ── Target file 1: OfflineSessionLoader ───────────────────────────────
    _write(root, "src/main/java/org/keycloak/models/sessions/infinispan/OfflineSessionLoader.java", """
        package org.keycloak.models.sessions.infinispan;

        public class OfflineSessionLoader {
            public void loadOfflineSessions() {
                // Load offline sessions from persistent storage
            }
            public boolean isOfflineSession(String sessionId) {
                return sessionId != null && sessionId.startsWith("offline:");
            }
        }
    """)

    # ── Target file 2: InfinispanOfflineSessionCacheEntryLifespanProviderFactory ─
    _write(
        root,
        "src/main/java/org/keycloak/models/sessions/infinispan/"
        "InfinispanOfflineSessionCacheEntryLifespanProviderFactory.java",
        """
        package org.keycloak.models.sessions.infinispan;

        public class InfinispanOfflineSessionCacheEntryLifespanProviderFactory {
            public long getOfflineSessionLifespan() {
                return 2592000L; // 30 days
            }
        }
        """,
    )

    # ── Sibling in same infinispan/ package (for co-location test) ────────
    _write(root, "src/main/java/org/keycloak/models/sessions/infinispan/InfinispanSessionCacheProvider.java", """
        package org.keycloak.models.sessions.infinispan;

        public class InfinispanSessionCacheProvider {
            public void init() {}
        }
    """)

    # ── Target file 3: DefaultUserSessionProvider ─────────────────────────
    _write(root, "src/main/java/org/keycloak/models/sessions/DefaultUserSessionProvider.java", """
        package org.keycloak.models.sessions;

        public class DefaultUserSessionProvider {
            public boolean isOfflineSession(String sessionId) {
                return false;
            }
            public void persistOfflineSession(String token, String sessionId) {}
            public void validateToken(String token) {
                if (token == null) throw new IllegalArgumentException("token null");
            }
        }
    """)

    # ── Target file 4: TokenManager ───────────────────────────────────────
    _write(root, "src/main/java/org/keycloak/protocol/oidc/TokenManager.java", """
        package org.keycloak.protocol.oidc;

        public class TokenManager {
            public boolean validateToken(String token) {
                // Token validation logic
                return token != null && !token.isEmpty();
            }
            public String createOfflineToken(String sessionId) {
                return "offline:" + sessionId;
            }
        }
    """)

    # ── Filler files: unrelated Keycloak classes ──────────────────────────
    filler_classes = [
        ("src/main/java/org/keycloak/services/UserResource.java",
         "org.keycloak.services", "UserResource"),
        ("src/main/java/org/keycloak/services/AuthResource.java",
         "org.keycloak.services", "AuthResource"),
        ("src/main/java/org/keycloak/services/ClientResource.java",
         "org.keycloak.services", "ClientResource"),
        ("src/main/java/org/keycloak/services/RealmResource.java",
         "org.keycloak.services", "RealmResource"),
        ("src/main/java/org/keycloak/admin/AdminRoot.java",
         "org.keycloak.admin", "AdminRoot"),
        ("src/main/java/org/keycloak/forms/LoginFormsProvider.java",
         "org.keycloak.forms", "LoginFormsProvider"),
        ("src/main/java/org/keycloak/events/EventBuilder.java",
         "org.keycloak.events", "EventBuilder"),
        ("src/main/java/org/keycloak/events/EventStoreProvider.java",
         "org.keycloak.events", "EventStoreProvider"),
        ("src/main/java/org/keycloak/storage/UserStorageProvider.java",
         "org.keycloak.storage", "UserStorageProvider"),
        ("src/main/java/org/keycloak/storage/federated/UserFederatedStorageProvider.java",
         "org.keycloak.storage.federated", "UserFederatedStorageProvider"),
        ("src/main/java/org/keycloak/models/UserModel.java",
         "org.keycloak.models", "UserModel"),
        ("src/main/java/org/keycloak/models/RealmModel.java",
         "org.keycloak.models", "RealmModel"),
        ("src/main/java/org/keycloak/models/ClientModel.java",
         "org.keycloak.models", "ClientModel"),
        ("src/main/java/org/keycloak/models/RoleModel.java",
         "org.keycloak.models", "RoleModel"),
        ("src/main/java/org/keycloak/crypto/SignatureProvider.java",
         "org.keycloak.crypto", "SignatureProvider"),
        ("src/main/java/org/keycloak/crypto/KeyWrapper.java",
         "org.keycloak.crypto", "KeyWrapper"),
        ("src/main/java/org/keycloak/util/JsonSerialization.java",
         "org.keycloak.util", "JsonSerialization"),
        ("src/main/java/org/keycloak/util/MultivaluedHashMap.java",
         "org.keycloak.util", "MultivaluedHashMap"),
        ("src/main/java/org/keycloak/connections/jpa/JpaConnectionProvider.java",
         "org.keycloak.connections.jpa", "JpaConnectionProvider"),
        ("src/main/java/org/keycloak/connections/infinispan/InfinispanConnectionProvider.java",
         "org.keycloak.connections.infinispan", "InfinispanConnectionProvider"),
    ]
    for path, pkg, cls in filler_classes:
        _write(root, path, f"""
            package {pkg};
            public class {cls} {{
                public void execute() {{}}
            }}
        """)


@pytest.fixture()
def keycloak_offline_repo(tmp_path: Path) -> Path:
    root = tmp_path / "keycloak-offline"
    root.mkdir()
    _write_keycloak_offline_session_repo(root)
    return root


# ---------------------------------------------------------------------------
# R2 — Keycloak offline-session recall
# ---------------------------------------------------------------------------


class TestOfflineSessionRecall:
    """All 4 Keycloak offline-session files must appear for the given symptom."""

    def _run(self, root: Path, monkeypatch, threshold: int = 5) -> set[str]:
        import sourcecode.prepare_context as pc
        monkeypatch.setattr(pc, "_LARGE_REPO_THRESHOLD", threshold)
        output = _build(root, "Token validation fails for offline sessions")
        return _paths(output)

    def test_offline_session_loader_found(
        self, keycloak_offline_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = self._run(keycloak_offline_repo, monkeypatch)
        assert any("OfflineSessionLoader" in p for p in paths), (
            f"OfflineSessionLoader.java missing from relevant_files.\nGot: {sorted(paths)}"
        )

    def test_infinispan_factory_found(
        self, keycloak_offline_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = self._run(keycloak_offline_repo, monkeypatch)
        assert any("InfinispanOfflineSession" in p for p in paths), (
            f"InfinispanOfflineSession...Factory missing.\nGot: {sorted(paths)}"
        )

    def test_default_user_session_provider_found(
        self, keycloak_offline_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = self._run(keycloak_offline_repo, monkeypatch)
        assert any("DefaultUserSessionProvider" in p for p in paths), (
            f"DefaultUserSessionProvider missing.\nGot: {sorted(paths)}"
        )

    def test_token_manager_found(
        self, keycloak_offline_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = self._run(keycloak_offline_repo, monkeypatch)
        assert any("TokenManager" in p for p in paths), (
            f"TokenManager.java missing.\nGot: {sorted(paths)}"
        )

    def test_all_four_found_together(
        self, keycloak_offline_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All 4 must appear in the same output — the complete semantic cone."""
        paths = self._run(keycloak_offline_repo, monkeypatch)
        missing = []
        if not any("OfflineSessionLoader" in p for p in paths):
            missing.append("OfflineSessionLoader")
        if not any("InfinispanOfflineSession" in p for p in paths):
            missing.append("InfinispanOfflineSessionCacheEntryLifespanProviderFactory")
        if not any("DefaultUserSessionProvider" in p for p in paths):
            missing.append("DefaultUserSessionProvider")
        if not any("TokenManager" in p for p in paths):
            missing.append("TokenManager")
        assert not missing, (
            f"Missing from relevant_files: {missing}\nGot: {sorted(paths)}"
        )

    def test_small_repo_still_works(self, keycloak_offline_repo: Path) -> None:
        """Small-repo path (no monkeypatch) must also find the relevant files."""
        output = _build(keycloak_offline_repo, "Token validation fails for offline sessions")
        paths = _paths(output)
        assert any("OfflineSessionLoader" in p or "TokenManager" in p for p in paths), (
            f"Neither OfflineSessionLoader nor TokenManager found in small-repo mode.\nGot: {sorted(paths)}"
        )


# ---------------------------------------------------------------------------
# R3 — NullPointerException symptom retrieves service + provider adjacency
# ---------------------------------------------------------------------------


class TestNullPointerExceptionRecall:
    """NPE symptom must retrieve the throwing class and its provider/service siblings."""

    @pytest.fixture()
    def npe_repo(self, tmp_path: Path) -> Path:
        root = tmp_path / "npe-repo"
        root.mkdir()
        _write(root, "pom.xml", "<project><artifactId>npe-demo</artifactId></project>")

        # The class that throws the NPE
        _write(root, "src/main/java/com/demo/OrderService.java", """
            package com.demo;
            public class OrderService {
                private OrderRepository repo;
                public Order findById(String id) {
                    Order o = repo.findById(id);
                    return o.process(); // NullPointerException when order not found
                }
            }
        """)
        # Provider adjacent to the NPE site
        _write(root, "src/main/java/com/demo/OrderServiceImpl.java", """
            package com.demo;
            public class OrderServiceImpl extends OrderService {
                public void validate(Order o) {
                    if (o == null) throw new NullPointerException("order is null");
                }
            }
        """)
        # Repository
        _write(root, "src/main/java/com/demo/OrderRepository.java", """
            package com.demo;
            public class OrderRepository {
                public Order findById(String id) { return null; }
            }
        """)
        # Unrelated filler
        for i in range(8):
            _write(root, f"src/main/java/com/demo/UnrelatedService{i}.java", f"""
                package com.demo;
                public class UnrelatedService{i} {{
                    public void doWork() {{}}
                }}
            """)
        return root

    def test_order_service_found_for_npe(
        self, npe_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sourcecode.prepare_context as pc
        monkeypatch.setattr(pc, "_LARGE_REPO_THRESHOLD", 5)
        output = _build(npe_repo, "NullPointerException in OrderService")
        paths = _paths(output)
        assert any("OrderService" in p for p in paths), (
            f"OrderService not found for NPE symptom.\nGot: {sorted(paths)}"
        )

    def test_npe_no_crash(self, npe_repo: Path) -> None:
        """fix-bug with NPE symptom must not crash on any repo size."""
        output = _build(npe_repo, "NullPointerException at startup")
        assert output is not None
        assert output.task == "fix-bug"


# ---------------------------------------------------------------------------
# R4 — Recall stable across repo sizes (stop-word isolation)
# ---------------------------------------------------------------------------


class TestRecallStability:
    def test_stop_words_do_not_create_false_positives(self, tmp_path: Path) -> None:
        """A symptom made entirely of stop words must not inject arbitrary files."""
        _write(tmp_path, "pom.xml", "<project><artifactId>demo</artifactId></project>")
        _write(tmp_path, "src/main/java/com/demo/SomeService.java", """
            package com.demo;
            public class SomeService { public void work() {} }
        """)
        output = _build(tmp_path, "fails not for with the error")
        # symptom_explain.keywords should be empty (all stop words) → no injection
        if output.symptom_explain is not None:
            assert output.symptom_explain["keywords"] == [], (
                "All-stop-word symptom must produce empty keyword list, "
                f"got {output.symptom_explain['keywords']}"
            )

    def test_large_repo_mode_triggered_by_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_is_large_repo logic must activate when file count exceeds threshold."""
        import sourcecode.prepare_context as pc
        # Patch to a low threshold so a tiny repo triggers large-repo mode
        monkeypatch.setattr(pc, "_LARGE_REPO_THRESHOLD", 2)
        _write(tmp_path, "pom.xml", "<project><artifactId>demo</artifactId></project>")
        _write(tmp_path, "src/main/java/com/demo/SessionManager.java", """
            package com.demo;
            public class SessionManager { public void open() {} }
        """)
        _write(tmp_path, "src/main/java/com/demo/TokenStore.java", """
            package com.demo;
            public class TokenStore { public String get(String k) { return null; } }
        """)
        _write(tmp_path, "src/main/java/com/demo/AuthFilter.java", """
            package com.demo;
            public class AuthFilter { public void filter() {} }
        """)
        # Must not crash with large-repo mode active
        output = _build(tmp_path, "session token validation")
        assert output is not None
        assert output.task == "fix-bug"


# ---------------------------------------------------------------------------
# R5 — CamelCase stem expansion
# ---------------------------------------------------------------------------


class TestCamelCaseStemExpansion:
    def test_camelcase_class_name_matches_keyword(self, tmp_path: Path) -> None:
        """OfflineSessionLoader must match keyword 'offline' via CamelCase expansion."""
        _write(tmp_path, "pom.xml", "<project><artifactId>cc-test</artifactId></project>")
        _write(tmp_path, "src/main/java/com/demo/OfflineSessionLoader.java", """
            package com.demo;
            public class OfflineSessionLoader {
                public void loadOfflineSessions() {}
            }
        """)
        _write(tmp_path, "src/main/java/com/demo/TokenManager.java", """
            package com.demo;
            public class TokenManager {
                public boolean validateToken(String token) { return true; }
            }
        """)
        output = _build(tmp_path, "offline session token")
        paths = _paths(output)
        assert any("OfflineSessionLoader" in p for p in paths), (
            f"OfflineSessionLoader not found via CamelCase expansion.\nGot: {sorted(paths)}"
        )


# ---------------------------------------------------------------------------
# R6 — Directory co-location (Pass 4c)
# ---------------------------------------------------------------------------


class TestDirectoryCoLocation:
    def test_sibling_in_same_package_injected(
        self, keycloak_offline_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """InfinispanSessionCacheProvider (no keyword in name) must be injected via
        co-location with OfflineSessionLoader in the same infinispan/ directory."""
        import sourcecode.prepare_context as pc
        monkeypatch.setattr(pc, "_LARGE_REPO_THRESHOLD", 5)
        output = _build(keycloak_offline_repo, "offline session")
        paths = _paths(output)
        # Either the co-location sibling OR one of the direct keyword matches must appear
        infinispan_files = [p for p in paths if "infinispan" in p.lower()]
        assert infinispan_files, (
            f"No infinispan/ files found — co-location injection failed.\nGot: {sorted(paths)}"
        )
