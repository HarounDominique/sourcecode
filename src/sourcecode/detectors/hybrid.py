from __future__ import annotations

"""Lightweight hybrid inference: scan source files for framework import/usage patterns.

Complements manifest-based detection with code-level evidence.
Budget: max 20 files, 8 KB each. Pure string matching — no AST, no regex overhead.
"""

from pathlib import Path
from typing import Any

from sourcecode.schema import FrameworkDetection
from sourcecode.tree_utils import flatten_file_tree

_MAX_FILES = 20
_MAX_BYTES = 8_192

# Per-ecosystem import/usage patterns → framework name
# Each entry: (pattern_string, framework_name, evidence_label)
_PYTHON_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("from fastapi import", "FastAPI", "from fastapi import"),
    ("import fastapi", "FastAPI", "import fastapi"),
    ("FastAPI()", "FastAPI", "FastAPI() call"),
    ("from flask import Flask", "Flask", "from flask import Flask"),
    ("Flask(__name__)", "Flask", "Flask(__name__) call"),
    ("from django", "Django", "from django import"),
    ("import django", "Django", "import django"),
    ("from celery import Celery", "Celery", "from celery import Celery"),
    ("Celery(", "Celery", "Celery() call"),
    ("import typer", "Typer", "import typer"),
    ("typer.Typer()", "Typer", "typer.Typer() call"),
    ("import click", "Click", "import click"),
    ("@click.command", "Click", "@click.command decorator"),
    ("from starlette", "Starlette", "from starlette import"),
    ("from tornado", "Tornado", "from tornado import"),
    ("from sanic import", "Sanic", "from sanic import"),
    ("from litestar import", "Litestar", "from litestar import"),
)

_NODE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("require('express')", "Express", "require('express')"),
    ('require("express")', "Express", 'require("express")'),
    ("from 'express'", "Express", "import from 'express'"),
    ('from "express"', "Express", 'import from "express"'),
    ("NestFactory", "NestJS", "NestFactory usage"),
    ("@nestjs/core", "NestJS", "@nestjs/core import"),
    ("from 'next'", "Next.js", "import from 'next'"),
    ("next/", "Next.js", "next/* import"),
    ("useRouter", "Next.js", "useRouter hook"),
    ("from 'fastify'", "Fastify", "import from 'fastify'"),
    ("require('fastify')", "Fastify", "require('fastify')"),
    ("from 'hono'", "Hono", "import from 'hono'"),
    ("from '@remix-run", "Remix", "import from @remix-run"),
    ("from 'astro'", "Astro", "import from 'astro'"),
    ("import { createApp } from 'vue'", "Vue", "createApp from vue"),
    ("from 'react'", "React", "import from 'react'"),
    ("from '@trpc/server'", "tRPC", "import from @trpc/server"),
    ("gql`", "GraphQL", "gql template literal"),
    ("ApolloServer", "Apollo", "ApolloServer usage"),
)

_GO_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ('"github.com/gin-gonic/gin"', "Gin", "gin import"),
    ("gin.Default()", "Gin", "gin.Default() call"),
    ("gin.New()", "Gin", "gin.New() call"),
    ('"github.com/labstack/echo"', "Echo", "echo import"),
    ("echo.New()", "Echo", "echo.New() call"),
    ('"github.com/gofiber/fiber"', "Fiber", "fiber import"),
    ("fiber.New()", "Fiber", "fiber.New() call"),
    ('"github.com/go-chi/chi"', "chi", "chi import"),
    ('"github.com/spf13/cobra"', "Cobra", "cobra import"),
    ("cobra.Command", "Cobra", "cobra.Command usage"),
    ('"google.golang.org/grpc"', "gRPC", "grpc import"),
)

_RUST_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("use axum", "Axum", "use axum"),
    ("axum::Router", "Axum", "axum::Router"),
    ("use actix_web", "Actix Web", "use actix_web"),
    ("actix_web::main", "Actix Web", "actix_web::main"),
    ("use rocket", "Rocket", "use rocket"),
    ("#[rocket::launch]", "Rocket", "#[rocket::launch]"),
    ("#[tokio::main]", "Tokio", "#[tokio::main]"),
    ("use tokio", "Tokio", "use tokio"),
    ("use tonic", "tonic/gRPC", "use tonic"),
    ("use clap", "Clap", "use clap"),
    ("#[derive(Parser)]", "Clap", "#[derive(Parser)]"),
    ("use tauri", "Tauri", "use tauri"),
    ("use warp", "Warp", "use warp"),
    ("use sqlx", "sqlx", "use sqlx"),
    ("use diesel", "Diesel", "use diesel"),
)

_DOTNET_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("WebApplication.CreateBuilder", "ASP.NET Core (Minimal API)", "WebApplication.CreateBuilder"),
    ("app.MapGet(", "ASP.NET Core (Minimal API)", "app.MapGet routes"),
    ("app.MapPost(", "ASP.NET Core (Minimal API)", "app.MapPost routes"),
    ("ControllerBase", "ASP.NET Core (MVC)", "ControllerBase"),
    ("[ApiController]", "ASP.NET Core (MVC)", "[ApiController] attribute"),
    ("BackgroundService", "Worker Service", "BackgroundService"),
    ("IHostedService", "Worker Service", "IHostedService"),
    ("using Microsoft.AspNetCore", "ASP.NET Core", "using Microsoft.AspNetCore"),
    ("SignalR", "SignalR", "SignalR usage"),
    ("GraphQL", "GraphQL", "GraphQL usage"),
)

_EXTENSIONS_BY_STACK: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "nodejs": (".ts", ".tsx", ".js", ".jsx", ".mjs"),
    "go": (".go",),
    "rust": (".rs",),
    "dotnet": (".cs", ".fs"),
}

_PATTERNS_BY_STACK: dict[str, tuple[tuple[str, str, str], ...]] = {
    "python": _PYTHON_PATTERNS,
    "nodejs": _NODE_PATTERNS,
    "go": _GO_PATTERNS,  # type: ignore[dict-item]
    "rust": _RUST_PATTERNS,
    "dotnet": _DOTNET_PATTERNS,
}


def scan_for_frameworks(
    root: Path,
    file_tree: dict[str, Any],
    stack: str,
    *,
    priority_paths: list[str] | None = None,
) -> list[FrameworkDetection]:
    """Scan source files for framework usage patterns.

    Returns FrameworkDetection list with confidence="medium" and detected_via evidence.
    Deduplicates by framework name, merging evidence from multiple files.
    """
    patterns = _PATTERNS_BY_STACK.get(stack)
    if not patterns:
        return []

    extensions = _EXTENSIONS_BY_STACK.get(stack, ())
    candidates = _rank_candidates(file_tree, extensions, priority_paths)

    evidence: dict[str, list[str]] = {}  # framework_name → evidence strings
    files_scanned = 0

    for rel_path in candidates:
        if files_scanned >= _MAX_FILES:
            break
        abs_path = root / rel_path
        try:
            content = abs_path.read_bytes()[:_MAX_BYTES].decode("utf-8", errors="replace")
        except OSError:
            continue
        files_scanned += 1

        for pattern, fw_name, label in patterns:
            if pattern in content:
                ev = evidence.setdefault(fw_name, [])
                entry = f"{label} ({rel_path})"
                if entry not in ev:
                    ev.append(entry)

    return [
        FrameworkDetection(
            name=fw_name,
            source="imports",
            confidence="medium",
            detected_via=ev,
        )
        for fw_name, ev in evidence.items()
    ]


def merge_framework_detections(
    manifest_detections: list[FrameworkDetection],
    import_detections: list[FrameworkDetection],
) -> list[FrameworkDetection]:
    """Merge manifest + import detections. Manifest detection is promoted to high confidence
    when confirmed by imports. New import-only detections are added with medium confidence."""
    result: dict[str, FrameworkDetection] = {}

    for fw in manifest_detections:
        result[fw.name] = FrameworkDetection(
            name=fw.name,
            source=fw.source,
            confidence="high",
            detected_via=list(fw.detected_via) + [f"manifest:{fw.source}"],
        )

    for fw in import_detections:
        existing = result.get(fw.name)
        if existing:
            # Confirmed by both manifest and imports → high confidence
            merged_via = existing.detected_via + [v for v in fw.detected_via if v not in existing.detected_via]
            result[fw.name] = FrameworkDetection(
                name=existing.name,
                source=existing.source,
                confidence="high",
                detected_via=merged_via,
            )
        else:
            # Import-only detection
            result[fw.name] = fw

    return list(result.values())


def _rank_candidates(
    file_tree: dict[str, Any],
    extensions: tuple[str, ...],
    priority_paths: list[str] | None,
) -> list[str]:
    """Return source file paths ranked by likely relevance: entry points first, then src/, then rest."""
    all_paths = [
        p for p in flatten_file_tree(file_tree)
        if any(p.endswith(ext) for ext in extensions)
        and not any(skip in p for skip in (
            "node_modules/", ".venv/", "venv/", "__pycache__/",
            "dist/", "build/", ".git/", "test", "spec",
        ))
    ]

    _ENTRY_NAMES = {"main", "app", "server", "index", "cli", "program", "startup"}

    def rank(path: str) -> tuple[int, int, str]:
        stem = Path(path).stem.lower()
        if priority_paths and path in priority_paths:
            return (0, 0, path)
        if stem in _ENTRY_NAMES:
            return (1, 0, path)
        if path.startswith("src/"):
            return (2, 0, path)
        return (3, 0, path)

    return sorted(all_paths, key=rank)[:_MAX_FILES]
