"""fqn_utils.py — FQN normalization utilities (single source of truth).

All code that needs to distinguish class FQNs from member FQNs (methods, fields,
constructors) must use these functions. No direct `.split("#")`, `.rsplit(".")`,
or lowercase-heuristic checks elsewhere.

Symbol FQN conventions in the CIR:
  Class/Interface/Enum: pkg.ClassName               (no # or lowercase-last-seg)
  Method:               pkg.ClassName#methodName    (hash separator)
  Constructor:          pkg.ClassName#<init>        (hash, angle-bracket name)
  Field:                pkg.ClassName.fieldName     (dot separator, lowercase last segment)
  Inner class:          pkg.ClassName.InnerClass    (dot separator, uppercase last segment)
"""
from __future__ import annotations


def normalize_owner_fqn(fqn: str) -> str:
    """Extract the owning class FQN from any symbol FQN.

    Examples:
        PatientServiceImpl                     -> PatientServiceImpl
        org.foo.PatientServiceImpl             -> org.foo.PatientServiceImpl
        org.foo.PatientServiceImpl#savePatient -> org.foo.PatientServiceImpl
        org.foo.PatientServiceImpl#<init>      -> org.foo.PatientServiceImpl
        org.foo.PatientServiceImpl.dao         -> org.foo.PatientServiceImpl
        org.foo.PatientServiceImpl.InnerClass  -> org.foo.PatientServiceImpl.InnerClass (unchanged)
    """
    if "#" in fqn:
        return fqn.rsplit("#", 1)[0]
    if "." in fqn:
        last_seg = fqn.rsplit(".", 1)[1]
        if last_seg and last_seg[0].islower():
            return fqn.rsplit(".", 1)[0]
    return fqn


def is_member_fqn(fqn: str) -> bool:
    """Return True for method/field/constructor FQNs; False for type FQNs.

    True:  pkg.Class#method, pkg.Class#<init>, pkg.Class.fieldName
    False: pkg.Class, pkg.outer.InnerClass, simple.Name
    """
    if "#" in fqn:
        return True
    if "." in fqn:
        last_seg = fqn.rsplit(".", 1)[1]
        return bool(last_seg and last_seg[0].islower())
    return False


def is_type_fqn(fqn: str) -> bool:
    """Return True for class/interface/enum/record FQNs; False for member FQNs."""
    return not is_member_fqn(fqn)
