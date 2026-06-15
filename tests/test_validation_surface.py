"""Unit + integration tests for the validation surface (Phase 20).

Covers custom-validator discovery (@Constraint + ConstraintValidator) and the
per-endpoint validation surface that combines OpenAPI-declared constraints with
those custom validators, linked via x-field-extra-annotation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sourcecode.openapi_surface import build_openapi_surface
from sourcecode.validation_surface import (
    build_validation_surface,
    discover_custom_validators,
)

# ── Fixtures ────────────────────────────────────────────────────────────────

_CONTROLLER = """\
package com.example.rest;

import org.springframework.web.bind.annotation.RestController;

@RestController
public class PetRestController implements PetsApi {
    // mappings on the generated PetsApi interface
}
"""

_VALIDATION_ANN = """\
package com.example.rest.validation;

import jakarta.validation.Constraint;
import jakarta.validation.Payload;
import java.lang.annotation.*;

@Target({ ElementType.FIELD })
@Retention(RetentionPolicy.RUNTIME)
@Constraint(validatedBy = PetAgeValidator.class)
@Documented
public @interface PetAgeValidation {
    String message() default "Birth date must not be in the future or older than 50 years";
    Class<?>[] groups() default {};
    Class<? extends Payload>[] payload() default {};
}
"""

_VALIDATOR_IMPL = """\
package com.example.rest.validation;

import java.time.LocalDate;
import jakarta.validation.ConstraintValidator;
import jakarta.validation.ConstraintValidatorContext;

public class PetAgeValidator implements ConstraintValidator<PetAgeValidation, LocalDate> {
    @Override
    public boolean isValid(LocalDate birthDate, ConstraintValidatorContext context) {
        return true;
    }
}
"""

_SPEC = """\
openapi: 3.0.1
info:
  title: Demo
  version: '1.0'
paths:
  /pets:
    post:
      tags: [pets]
      operationId: addPet
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/PetFields'
      responses:
        '201':
          description: created
  /pets/{id}:
    put:
      tags: [pets]
      operationId: updatePet
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/Bare'
      responses:
        '200':
          description: ok
components:
  schemas:
    PetFields:
      type: object
      properties:
        name:
          type: string
          minLength: 1
          maxLength: 30
          pattern: "^[A-Za-z].*"
        birthDate:
          type: string
          format: date
          x-field-extra-annotation: "@com.example.rest.validation.PetAgeValidation"
      required:
        - name
        - birthDate
    Bare:
      type: object
      properties:
        note:
          type: string
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    java = tmp_path / "src" / "main" / "java" / "com" / "example" / "rest"
    java.mkdir(parents=True)
    (java / "PetRestController.java").write_text(_CONTROLLER)
    val = java / "validation"
    val.mkdir()
    (val / "PetAgeValidation.java").write_text(_VALIDATION_ANN)
    (val / "PetAgeValidator.java").write_text(_VALIDATOR_IMPL)
    res = tmp_path / "src" / "main" / "resources"
    res.mkdir(parents=True)
    (res / "openapi.yml").write_text(_SPEC)
    (tmp_path / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>com.example</groupId><artifactId>demo</artifactId>"
        "<version>0</version></project>"
    )
    return tmp_path


# ── Custom-validator discovery ──────────────────────────────────────────────

class TestDiscoverCustomValidators:
    def test_finds_constraint_annotation(self, repo: Path):
        cat = discover_custom_validators(repo)
        assert "PetAgeValidation" in cat
        cc = cat["PetAgeValidation"]
        assert "PetAgeValidator" in cc.validators
        assert cc.message and "future" in cc.message
        assert cc.validated_types == ["LocalDate"]
        assert cc.targets == ["FIELD"]
        assert cc.source_file and cc.source_file.endswith("PetAgeValidation.java")

    def test_no_validators_when_absent(self, tmp_path: Path):
        java = tmp_path / "src" / "main" / "java" / "com" / "x"
        java.mkdir(parents=True)
        (java / "Plain.java").write_text("package com.x;\npublic class Plain {}\n")
        assert discover_custom_validators(tmp_path) == {}

    def test_skips_test_sources(self, tmp_path: Path):
        test = tmp_path / "src" / "test" / "java" / "com" / "x"
        test.mkdir(parents=True)
        (test / "FooValidator.java").write_text(
            "package com.x;\nimport jakarta.validation.ConstraintValidator;\n"
            "public class FooValidator implements ConstraintValidator<FooAnn, String> {}\n"
        )
        assert discover_custom_validators(tmp_path) == {}

    def test_validator_in_separate_file(self, tmp_path: Path):
        java = tmp_path / "src" / "main" / "java" / "com" / "x"
        java.mkdir(parents=True)
        (java / "Ann.java").write_text(
            "package com.x;\nimport jakarta.validation.Constraint;\n"
            "@Constraint(validatedBy = SepValidator.class)\n"
            "public @interface SepAnn { String message() default \"bad\"; }\n"
        )
        (java / "SepValidator.java").write_text(
            "package com.x;\nimport jakarta.validation.ConstraintValidator;\n"
            "public class SepValidator implements ConstraintValidator<SepAnn, Integer> {}\n"
        )
        cat = discover_custom_validators(tmp_path)
        assert cat["SepAnn"].validated_types == ["Integer"]
        assert "SepValidator" in cat["SepAnn"].validators


# ── x-field-extra-annotation parsing in the spec surface ───────────────────

class TestExtraAnnotationParsing:
    def test_extra_annotation_on_field(self, repo: Path):
        surface = build_openapi_surface(repo)
        assert surface is not None
        pet = surface.schemas["PetFields"]
        bd = next(f for f in pet.fields if f.name == "birthDate")
        assert bd.extra_annotations == ["PetAgeValidation"]
        assert bd.to_dict()["extraAnnotations"] == ["PetAgeValidation"]


# ── Per-endpoint validation surface ─────────────────────────────────────────

class TestBuildValidationSurface:
    def test_endpoint_validated_fields(self, repo: Path):
        surf = build_validation_surface(repo)
        post = next(e for e in surf["endpoints"] if e["handler"] == "addPet")
        assert post["schema"] == "PetFields"
        by_name = {f["name"]: f for f in post["validatedFields"]}
        # name: builtin rules
        name_rules = {r["kind"] for r in by_name["name"]["rules"]}
        assert {"required", "pattern", "minLength", "maxLength"} <= name_rules
        # birthDate: custom validator linked + resolved
        cust = by_name["birthDate"]["customValidators"][0]
        assert cust["annotation"] == "PetAgeValidation"
        assert cust["resolved"] is True
        assert "PetAgeValidator" in cust["validators"]

    def test_summary_counts(self, repo: Path):
        surf = build_validation_surface(repo)
        s = surf["summary"]
        assert s["custom_validators_declared"] == 1
        assert s["custom_validators_linked"] == 1
        assert s["endpoints_with_body"] >= 2

    def test_gap_detected_for_unconstrained_body(self, repo: Path):
        surf = build_validation_surface(repo)
        # PUT /pets/{id} body "Bare" has only an unconstrained "note" -> a gap.
        gaps = {(g.get("method"), g.get("schema")) for g in surf["gaps"]}
        assert ("PUT", "Bare") in gaps

    def test_unresolved_custom_annotation(self, tmp_path: Path):
        # Spec references a custom annotation whose validator is NOT in source.
        java = tmp_path / "src" / "main" / "java" / "com" / "x"
        java.mkdir(parents=True)
        (java / "WidgetController.java").write_text(
            "package com.x;\n"
            "import org.springframework.web.bind.annotation.RestController;\n"
            "@RestController\npublic class WidgetController implements WidgetsApi {}\n"
        )
        res = tmp_path / "src" / "main" / "resources"
        res.mkdir(parents=True)
        (res / "openapi.yml").write_text(
            "openapi: 3.0.1\ninfo:\n  title: d\n  version: '1'\n"
            "paths:\n  /widgets:\n    post:\n      tags: [widgets]\n"
            "      operationId: addWidget\n      requestBody:\n        content:\n"
            "          application/json:\n            schema:\n"
            "              $ref: '#/components/schemas/WidgetFields'\n"
            "      responses:\n        '201':\n          description: ok\n"
            "components:\n  schemas:\n    WidgetFields:\n      type: object\n"
            "      properties:\n        code:\n          type: string\n"
            "          x-field-extra-annotation: \"@com.x.MysteryValidation\"\n"
            "      required: [code]\n"
        )
        surf = build_validation_surface(tmp_path)
        post = next(e for e in surf["endpoints"] if e["handler"] == "addWidget")
        cust = next(f for f in post["validatedFields"] if f["name"] == "code")
        entry = cust["customValidators"][0]
        assert entry["annotation"] == "MysteryValidation"
        assert entry["resolved"] is False

    def test_no_spec_empty_surface(self, tmp_path: Path):
        java = tmp_path / "src" / "main" / "java" / "com" / "x"
        java.mkdir(parents=True)
        (java / "Plain.java").write_text("package com.x;\npublic class Plain {}\n")
        surf = build_validation_surface(tmp_path)
        assert surf["endpoints"] == []
        assert surf["gaps"] == []
        assert surf["summary"]["endpoints_with_body"] == 0
