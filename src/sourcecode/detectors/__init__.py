"""Infraestructura de deteccion para sourcecode."""

from sourcecode.detectors.base import (
    AbstractDetector,
    DetectionContext,
    EntryPoint,
    FrameworkDetection,
    StackDetection,
)
from sourcecode.detectors.dart import DartDetector
from sourcecode.detectors.dotnet import DotnetDetector
from sourcecode.detectors.elixir import ElixirDetector
from sourcecode.detectors.go import GoDetector
from sourcecode.detectors.heuristic import HeuristicDetector
from sourcecode.detectors.java import JavaDetector
from sourcecode.detectors.jvm_ext import JvmExtDetector
from sourcecode.detectors.nodejs import NodejsDetector
from sourcecode.detectors.php import PhpDetector
from sourcecode.detectors.project import ProjectDetector
from sourcecode.detectors.python import PythonDetector
from sourcecode.detectors.ruby import RubyDetector
from sourcecode.detectors.rust import RustDetector
from sourcecode.detectors.systems import SystemsDetector
from sourcecode.detectors.terraform import TerraformDetector


def build_default_detectors() -> list[AbstractDetector]:
    """Registro por defecto de detectores para la CLI."""
    return [
        NodejsDetector(),
        PythonDetector(),
        GoDetector(),
        RustDetector(),
        JavaDetector(),
        DotnetDetector(),
        ElixirDetector(),
        JvmExtDetector(),
        PhpDetector(),
        RubyDetector(),
        DartDetector(),
        TerraformDetector(),
        SystemsDetector(),
        HeuristicDetector(),
    ]


__all__ = [
    "AbstractDetector",
    "build_default_detectors",
    "DartDetector",
    "DetectionContext",
    "DotnetDetector",
    "ElixirDetector",
    "EntryPoint",
    "FrameworkDetection",
    "GoDetector",
    "HeuristicDetector",
    "JavaDetector",
    "JvmExtDetector",
    "NodejsDetector",
    "PhpDetector",
    "PythonDetector",
    "ProjectDetector",
    "RubyDetector",
    "RustDetector",
    "StackDetection",
    "SystemsDetector",
    "TerraformDetector",
]
