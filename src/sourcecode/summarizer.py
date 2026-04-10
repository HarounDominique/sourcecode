"""Generador deterministico de resumen en lenguaje natural del proyecto.

Sin llamadas a API — solo templates aplicados sobre SourceMap.
"""
from __future__ import annotations

from sourcecode.schema import SourceMap


class ProjectSummarizer:
    """Genera project_summary: string NL deterministica desde SourceMap."""

    def generate(self, sm: SourceMap) -> str:
        """Retorna descripcion NL del proyecto. Nunca lanza excepcion."""
        try:
            return self._build_summary(sm)
        except Exception:
            return "Proyecto analizado."

    def _build_summary(self, sm: SourceMap) -> str:
        # Determinar stack primario
        primary_stacks = [s for s in sm.stacks if s.primary]
        all_stacks = primary_stacks if primary_stacks else sm.stacks

        if not all_stacks:
            return "Proyecto sin stack detectado."

        # Tipo de proyecto
        project_type = sm.project_type or "Proyecto"
        type_label = {
            "webapp": "Aplicacion web",
            "api": "API",
            "library": "Libreria",
            "cli": "CLI",
            "monorepo": "Monorepo",
            "fullstack": "Proyecto fullstack",
            "unknown": "Proyecto",
        }.get(project_type, project_type.capitalize())

        # Stacks y frameworks
        primary = all_stacks[0]
        stack_name = primary.stack.capitalize()
        frameworks = [f.name for f in primary.frameworks]
        fw_part = f" ({', '.join(frameworks[:3])})" if frameworks else ""

        # Entry points (max 3)
        ep_paths = [ep.path for ep in sm.entry_points[:3]]
        ep_part = f" Entry points: {', '.join(ep_paths)}." if ep_paths else ""

        # Dependencias
        dep_part = ""
        if sm.dependency_summary and sm.dependency_summary.total_count > 0:
            ds = sm.dependency_summary
            ecosystems = ", ".join(ds.ecosystems[:3])
            dep_part = f" {ds.total_count} dependencias ({ecosystems})."
        elif sm.dependency_summary is None:
            dep_part = ""
        else:
            dep_part = " Sin dependencias detectadas."

        # Monorepo: variante especial
        if project_type == "monorepo":
            stacks_desc = ", ".join(sorted({s.stack.capitalize() for s in sm.stacks}))
            n_ws = len({s.workspace for s in sm.stacks if s.workspace})
            ws_part = f" con {n_ws} workspaces" if n_ws > 0 else ""
            return f"Monorepo{ws_part} en {stacks_desc}.{ep_part}{dep_part}"

        return f"{type_label} en {stack_name}{fw_part}.{ep_part}{dep_part}"
