"""Tests for fqn_utils — FQN-01..FQN-14."""
import pytest
from sourcecode.fqn_utils import is_member_fqn, is_type_fqn, normalize_owner_fqn


# ---------------------------------------------------------------------------
# FQN-01..06  normalize_owner_fqn
# ---------------------------------------------------------------------------

class TestNormalizeOwnerFqn:
    def test_fqn01_plain_class(self) -> None:
        assert normalize_owner_fqn("PatientServiceImpl") == "PatientServiceImpl"

    def test_fqn02_package_class(self) -> None:
        assert normalize_owner_fqn("org.openmrs.api.impl.PatientServiceImpl") == (
            "org.openmrs.api.impl.PatientServiceImpl"
        )

    def test_fqn03_method(self) -> None:
        assert normalize_owner_fqn("org.openmrs.api.impl.PatientServiceImpl#savePatient") == (
            "org.openmrs.api.impl.PatientServiceImpl"
        )

    def test_fqn04_constructor(self) -> None:
        assert normalize_owner_fqn("org.openmrs.api.impl.PatientServiceImpl#<init>") == (
            "org.openmrs.api.impl.PatientServiceImpl"
        )

    def test_fqn05_field_dot_format(self) -> None:
        assert normalize_owner_fqn("org.openmrs.api.impl.PatientServiceImpl.dao") == (
            "org.openmrs.api.impl.PatientServiceImpl"
        )

    def test_fqn06_inner_class_unchanged(self) -> None:
        # Inner class: last segment is PascalCase → not a field → unchanged
        assert normalize_owner_fqn("org.openmrs.api.impl.PatientServiceImpl.Builder") == (
            "org.openmrs.api.impl.PatientServiceImpl.Builder"
        )

    def test_fqn07_no_package(self) -> None:
        assert normalize_owner_fqn("MyService#doWork") == "MyService"

    def test_fqn08_field_no_package(self) -> None:
        assert normalize_owner_fqn("MyService.myField") == "MyService"


# ---------------------------------------------------------------------------
# FQN-09..12  is_member_fqn
# ---------------------------------------------------------------------------

class TestIsMemberFqn:
    def test_fqn09_class_is_not_member(self) -> None:
        assert is_member_fqn("org.foo.PatientServiceImpl") is False

    def test_fqn10_method_is_member(self) -> None:
        assert is_member_fqn("org.foo.PatientServiceImpl#savePatient") is True

    def test_fqn11_constructor_is_member(self) -> None:
        assert is_member_fqn("org.foo.PatientServiceImpl#<init>") is True

    def test_fqn12_field_is_member(self) -> None:
        assert is_member_fqn("org.foo.PatientServiceImpl.dao") is True

    def test_fqn13_inner_class_is_not_member(self) -> None:
        assert is_member_fqn("org.foo.PatientServiceImpl.Builder") is False

    def test_fqn14_plain_name_is_not_member(self) -> None:
        assert is_member_fqn("PatientServiceImpl") is False


# ---------------------------------------------------------------------------
# FQN-15  is_type_fqn is inverse of is_member_fqn
# ---------------------------------------------------------------------------

class TestIsTypeFqn:
    @pytest.mark.parametrize("fqn", [
        "org.foo.PatientServiceImpl",
        "org.foo.PatientServiceImpl.Builder",
        "PatientServiceImpl",
    ])
    def test_type_fqns(self, fqn: str) -> None:
        assert is_type_fqn(fqn) is True

    @pytest.mark.parametrize("fqn", [
        "org.foo.PatientServiceImpl#save",
        "org.foo.PatientServiceImpl#<init>",
        "org.foo.PatientServiceImpl.dao",
    ])
    def test_member_fqns(self, fqn: str) -> None:
        assert is_type_fqn(fqn) is False
