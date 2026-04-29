from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional

from sourcecode.detectors.parsers import load_json_file, load_toml_file
from sourcecode.schema import EntryPoint, GraphEdge, GraphNode, ModuleGraph, ModuleGraphSummary
from sourcecode.tree_utils import flatten_file_tree

GraphDetail = Literal["high", "medium", "full"]
GraphImportance = Literal["high", "medium", "low"]


# Node.js stdlib modules — omit from graph unless --graph-detail full
_NODE_STDLIB: frozenset[str] = frozenset({
    "fs", "fs/promises", "path", "os", "child_process", "process",
    "net", "http", "https", "http2", "stream", "events", "util",
    "crypto", "buffer", "url", "querystring", "string_decoder",
    "readline", "repl", "assert", "dns", "dgram", "vm",
    "worker_threads", "cluster", "module", "v8", "perf_hooks",
    "async_hooks", "inspector", "tty", "zlib", "domain",
    "timers", "timers/promises", "console", "constants",
    "node:fs", "node:path", "node:os", "node:crypto", "node:buffer",
    "node:stream", "node:events", "node:util", "node:http", "node:https",
    "node:child_process", "node:process", "node:worker_threads",
})

# Python stdlib modules — omit from graph unless --graph-detail full
_PYTHON_STDLIB: frozenset[str] = frozenset({
    "os", "sys", "re", "io", "abc", "ast", "copy", "csv",
    "enum", "functools", "itertools", "json", "logging", "math",
    "pathlib", "random", "shutil", "string", "subprocess",
    "threading", "time", "typing", "uuid", "warnings",
    "collections", "contextlib", "dataclasses", "datetime",
    "decimal", "difflib", "email", "glob", "hashlib", "hmac",
    "http", "importlib", "inspect", "operator", "pickle",
    "platform", "pprint", "queue", "signal", "socket",
    "sqlite3", "stat", "struct", "tempfile", "textwrap",
    "traceback", "unicodedata", "unittest", "urllib", "weakref",
    "xml", "zipfile", "zlib",
})

# External packages that add noise without structural signal
_NODE_NOISE_PACKAGES: frozenset[str] = frozenset({
    "lodash", "moment", "date-fns", "uuid", "chalk", "debug",
    "yargs", "commander", "dotenv", "cross-env",
    "eslint", "prettier", "typescript", "ts-node",
    "@types/node", "@types/react",
})


class GraphAnalyzer:
    """Construye un grafo estructural parcial y seguro del proyecto."""

    _PYTHON_EXTENSIONS = {".py"}
    _NODE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
    _GO_EXTENSIONS = {".go"}
    _JVM_EXTENSIONS = {".java", ".kt", ".scala"}
    _DOTNET_PROJECT_EXTENSIONS = {".csproj", ".fsproj", ".vbproj"}
    _SUPPORTED_EXTENSIONS = _PYTHON_EXTENSIONS | _NODE_EXTENSIONS | _GO_EXTENSIONS | _JVM_EXTENSIONS
    _DEFAULT_EDGE_KINDS: dict[GraphDetail, tuple[str, ...]] = {
        "high": ("imports",),
        "medium": ("imports", "calls"),
        "full": ("imports", "calls", "contains", "extends"),
    }
    _DEFAULT_MAX_NODES: dict[GraphDetail, Optional[int]] = {
        "high": 80,
        "medium": 160,
        "full": None,
    }

    def __init__(
        self,
        *,
        max_files: int = 200,
        max_file_size: int = 200_000,
        max_nodes: int = 1_000,
        max_edges: int = 2_000,
    ) -> None:
        self.max_files = max_files
        self.max_file_size = max_file_size
        self.max_nodes = max_nodes
        self.max_edges = max_edges

    def analyze(
        self,
        root: Path,
        file_tree: dict[str, Any],
        *,
        workspace: str | None = None,
        detail: GraphDetail = "full",
        edge_kinds: set[str] | None = None,
        max_nodes: int | None = None,
        entry_points: list[EntryPoint] | None = None,
    ) -> ModuleGraph:
        full_graph = self._analyze_full(root, file_tree, workspace=workspace)
        return self._project_graph(
            full_graph,
            detail=detail,
            edge_kinds=edge_kinds,
            max_nodes=max_nodes,
            entry_points=entry_points or [],
        )

    def merge_graphs(
        self,
        graphs: list[ModuleGraph],
        *,
        detail: GraphDetail = "full",
        edge_kinds: set[str] | None = None,
        max_nodes: int | None = None,
        entry_points: list[EntryPoint] | None = None,
    ) -> ModuleGraph:
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        limitations: list[str] = []
        node_ids: set[str] = set()
        edge_keys: set[tuple[str, str, str, str]] = set()
        for graph in graphs:
            for node in graph.nodes:
                self._append_node(nodes, node_ids, node)
            for edge in graph.edges:
                self._append_edge(edges, edge_keys, edge)
            limitations.extend(graph.summary.limitations)
        merged = ModuleGraph(
            nodes=nodes,
            edges=edges,
            summary=ModuleGraphSummary(
                requested=bool(graphs),
                node_count=len(nodes),
                edge_count=len(edges),
                languages=sorted({node.language for node in nodes}),
                methods=sorted({edge.method for edge in edges}),
                limitations=self._unique(limitations),
            ),
        )
        return self._project_graph(
            merged,
            detail=detail,
            edge_kinds=edge_kinds,
            max_nodes=max_nodes,
            entry_points=entry_points or [],
        )

    def prefix_graph(self, graph: ModuleGraph, prefix: str, workspace: str) -> ModuleGraph:
        """Reescribe ids y paths de un grafo de workspace al espacio de paths del repo."""
        node_id_map: dict[str, str] = {}
        prefixed_nodes: list[GraphNode] = []
        clean_prefix = prefix.strip("/")
        for node in graph.nodes:
            prefixed_path = f"{clean_prefix}/{node.path}".strip("/")
            new_id = self._node_id(node.kind, prefixed_path, node.symbol)
            node_id_map[node.id] = new_id
            prefixed_nodes.append(
                replace(
                    node,
                    id=new_id,
                    path=prefixed_path,
                    workspace=workspace,
                )
            )
        prefixed_edges: list[GraphEdge] = []
        for edge in graph.edges:
            prefixed_edges.append(
                replace(
                    edge,
                    source=node_id_map.get(edge.source, edge.source),
                    target=node_id_map.get(edge.target, edge.target),
                )
            )
        return ModuleGraph(
            nodes=prefixed_nodes,
            edges=prefixed_edges,
            summary=replace(graph.summary),
        )

    def _analyze_full(
        self,
        root: Path,
        file_tree: dict[str, Any],
        *,
        workspace: str | None,
    ) -> ModuleGraph:
        source_files = [
            path
            for path in flatten_file_tree(file_tree)
            if Path(path).suffix in self._SUPPORTED_EXTENSIONS and (root / path).is_file()
        ]
        limitations: list[str] = []
        if len(source_files) > self.max_files:
            limitations.append(f"max_files_reached:{len(source_files)}>{self.max_files}")
            source_files = source_files[: self.max_files]

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        node_ids: set[str] = set()
        edge_keys: set[tuple[str, str, str, str]] = set()

        python_modules = self._build_python_module_map(source_files)
        node_modules = {
            path: path for path in source_files if Path(path).suffix in self._NODE_EXTENSIONS
        }
        go_imports = self._build_go_import_map(root, source_files)
        jvm_types = self._build_jvm_type_map(root, source_files)

        # .NET: project-level graph from .csproj/.fsproj/.vbproj manifests
        all_tree_paths = [p for p in flatten_file_tree(file_tree)]
        dotnet_project_paths = [
            p for p in all_tree_paths
            if Path(p).suffix.lower() in self._DOTNET_PROJECT_EXTENSIONS
            and (root / p).is_file()
        ]
        if dotnet_project_paths:
            dn_nodes, dn_edges, dn_lims = self._analyze_dotnet_projects(
                root, dotnet_project_paths, workspace
            )
            for node in dn_nodes:
                self._append_node(nodes, node_ids, node)
            for edge in dn_edges:
                self._append_edge(edges, edge_keys, edge)
            limitations.extend(dn_lims)

        # Rust workspace crate graph
        if (root / "Cargo.toml").is_file():
            rw_nodes, rw_edges, rw_lims = self._analyze_rust_workspace(root, workspace)
            for node in rw_nodes:
                self._append_node(nodes, node_ids, node)
            for edge in rw_edges:
                self._append_edge(edges, edge_keys, edge)
            limitations.extend(rw_lims)

        # Go workspace module graph
        if (root / "go.work").is_file():
            gw_nodes, gw_edges, gw_lims = self._analyze_go_workspace(root, workspace)
            for node in gw_nodes:
                self._append_node(nodes, node_ids, node)
            for edge in gw_edges:
                self._append_edge(edges, edge_keys, edge)
            limitations.extend(gw_lims)

        # Node monorepo workspace graph (only for confirmed workspaces)
        if self._is_node_workspace(root, file_tree):
            nw_nodes, nw_edges, nw_lims = self._analyze_node_workspace(root, file_tree, workspace)
            for node in nw_nodes:
                self._append_node(nodes, node_ids, node)
            for edge in nw_edges:
                self._append_edge(edges, edge_keys, edge)
            limitations.extend(nw_lims)

        for relative_path in source_files:
            absolute_path = root / relative_path
            try:
                if absolute_path.stat().st_size > self.max_file_size:
                    limitations.append(f"file_too_large:{relative_path}")
                    continue
                content = absolute_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                limitations.append(f"read_error:{relative_path}")
                continue

            suffix = absolute_path.suffix
            module_node = GraphNode(
                id=f"module:{relative_path}",
                kind="module",
                language=self._language_for_suffix(suffix),
                path=relative_path,
                display_name=relative_path,
                workspace=workspace,
            )
            self._append_node(nodes, node_ids, module_node)

            file_nodes: list[GraphNode] = []
            file_edges: list[GraphEdge] = []
            file_limitations: list[str] = []

            if suffix in self._PYTHON_EXTENSIONS:
                file_nodes, file_edges, file_limitations = self._analyze_python_file(
                    relative_path, content, python_modules, workspace
                )
            elif suffix in self._NODE_EXTENSIONS:
                file_nodes, file_edges, file_limitations = self._analyze_node_file(
                    relative_path, content, node_modules, workspace
                )
            elif suffix in self._GO_EXTENSIONS:
                file_nodes, file_edges, file_limitations = self._analyze_go_file(
                    relative_path, content, go_imports, workspace
                )
            elif suffix in self._JVM_EXTENSIONS:
                file_nodes, file_edges, file_limitations = self._analyze_jvm_file(
                    relative_path, content, jvm_types, workspace
                )

            limitations.extend(file_limitations)
            for node in file_nodes:
                self._append_node(nodes, node_ids, node)
            for edge in file_edges:
                self._append_edge(edges, edge_keys, edge)
            if len(nodes) >= self.max_nodes or len(edges) >= self.max_edges:
                limitations.append("graph_budget_reached")
                break

        nodes = nodes[: self.max_nodes]
        edges = edges[: self.max_edges]
        return ModuleGraph(
            nodes=nodes,
            edges=edges,
            summary=ModuleGraphSummary(
                requested=True,
                node_count=len(nodes),
                edge_count=len(edges),
                languages=sorted({node.language for node in nodes}),
                methods=sorted({edge.method for edge in edges}),
                detail="full",
                edge_kinds=sorted({edge.kind for edge in edges}),
                limitations=self._unique(limitations),
            ),
        )

    def _project_graph(
        self,
        graph: ModuleGraph,
        *,
        detail: GraphDetail,
        edge_kinds: set[str] | None,
        max_nodes: int | None,
        entry_points: list[EntryPoint],
    ) -> ModuleGraph:
        active_edge_kinds = edge_kinds or set(self._DEFAULT_EDGE_KINDS[detail])
        budget = (
            max_nodes if detail != "full" and max_nodes is not None else self._DEFAULT_MAX_NODES[detail]
        )
        ranked_nodes = self._rank_nodes(graph, entry_points)
        importance_by_id = {node.id: node.importance for node in ranked_nodes}
        if detail == "full":
            projected_nodes = ranked_nodes
            projected_edges = [
                edge for edge in graph.edges if edge.kind in active_edge_kinds
            ]
            truncated = False
            base_limitations = list(graph.summary.limitations)
        elif detail == "medium":
            projected_nodes = self._select_medium_nodes(ranked_nodes, entry_points)
            projected_nodes = self._expand_medium_call_targets(projected_nodes, ranked_nodes, graph.edges)
            projected_edges = [
                edge
                for edge in graph.edges
                if edge.kind in active_edge_kinds
                and edge.source in {node.id for node in projected_nodes}
                and edge.target in {node.id for node in projected_nodes}
            ]
            projected_nodes, projected_edges, truncated, base_limitations = self._apply_budget(
                projected_nodes,
                projected_edges,
                importance_by_id,
                budget,
                list(graph.summary.limitations),
            )
        else:
            collapsed_nodes, collapsed_edges = self._collapse_high_graph(
                ranked_nodes,
                graph.edges,
                entry_points,
            )
            projected_nodes, projected_edges, truncated, base_limitations = self._apply_budget(
                collapsed_nodes,
                [edge for edge in collapsed_edges if edge.kind in active_edge_kinds],
                {node.id: node.importance for node in collapsed_nodes},
                budget,
                list(graph.summary.limitations),
            )

        summary = self._build_summary(
            projected_nodes,
            projected_edges,
            detail=detail,
            edge_kinds=active_edge_kinds,
            budget=budget,
            entry_points=entry_points,
            limitations=base_limitations,
            truncated=truncated,
        )
        return ModuleGraph(nodes=projected_nodes, edges=projected_edges, summary=summary)

    def _rank_nodes(self, graph: ModuleGraph, entry_points: list[EntryPoint]) -> list[GraphNode]:
        module_entry_ids = self._entry_point_module_ids(entry_points, graph.nodes)
        incoming_imports = Counter(
            edge.target for edge in graph.edges if edge.kind == "imports"
        )
        incoming_calls = Counter(
            edge.target for edge in graph.edges if edge.kind == "calls"
        )
        outgoing_calls = Counter(
            edge.source for edge in graph.edges if edge.kind == "calls"
        )

        scored_nodes: list[tuple[int, GraphNode]] = []
        for node in graph.nodes:
            score = 0
            if node.id in module_entry_ids:
                score += 100
            if node.kind == "module":
                score += 20
                score += incoming_imports[node.id] * 10
            elif node.kind == "function":
                score += outgoing_calls[node.id] * 20
                score += incoming_calls[node.id] * 8
            elif node.kind == "class":
                score += incoming_imports[node.id] * 6

            if node.path.endswith(("main.py", "index.ts", "index.js", "__main__.py")):
                score += 20

            importance = self._importance_from_score(score, node.kind)
            scored_nodes.append((score, replace(node, importance=importance)))

        scored_nodes.sort(
            key=lambda item: (
                -item[0],
                0 if item[1].kind == "module" else 1,
                item[1].path,
                item[1].symbol or "",
            )
        )
        return [node for _score, node in scored_nodes]

    def _importance_from_score(self, score: int, kind: str) -> GraphImportance:
        if score >= 40:
            return "high"
        if kind == "module" and score >= 20:
            return "medium"
        if score >= 12:
            return "medium"
        return "low"

    def _select_medium_nodes(
        self,
        ranked_nodes: list[GraphNode],
        entry_points: list[EntryPoint],
    ) -> list[GraphNode]:
        entry_module_ids = self._entry_point_module_ids(entry_points, ranked_nodes)
        selected: list[GraphNode] = []
        seen: set[str] = set()
        for node in ranked_nodes:
            include = node.kind == "module" or node.importance == "high"
            if node.id in entry_module_ids:
                include = True
            if include and node.id not in seen:
                seen.add(node.id)
                selected.append(node)
        return selected

    def _expand_medium_call_targets(
        self,
        selected_nodes: list[GraphNode],
        ranked_nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> list[GraphNode]:
        node_map = {node.id: node for node in ranked_nodes}
        selected_ids = {node.id for node in selected_nodes}
        expanded = list(selected_nodes)
        for edge in edges:
            if edge.kind != "calls":
                continue
            if edge.source in selected_ids and edge.target in node_map and edge.target not in selected_ids:
                selected_ids.add(edge.target)
                expanded.append(node_map[edge.target])
        return expanded

    def _collapse_high_graph(
        self,
        ranked_nodes: list[GraphNode],
        edges: list[GraphEdge],
        entry_points: list[EntryPoint],
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        module_nodes = [node for node in ranked_nodes if node.kind == "module"]
        entry_module_ids = self._entry_point_module_ids(entry_points, module_nodes)
        group_map = self._build_high_group_map(module_nodes, entry_module_ids)
        grouped_nodes: dict[str, GraphNode] = {}
        for node in module_nodes:
            group_path = group_map[node.id]
            if group_path not in grouped_nodes:
                importance: GraphImportance = "high" if node.id in entry_module_ids else "medium"
                grouped_nodes[group_path] = GraphNode(
                    id=f"module:{group_path}",
                    kind="module",
                    language=node.language,
                    path=group_path,
                    display_name=group_path,
                    workspace=node.workspace,
                    importance=importance,
                )
            elif node.importance == "high":
                grouped_nodes[group_path] = replace(
                    grouped_nodes[group_path],
                    importance="high",
                )

        grouped_edges: list[GraphEdge] = []
        edge_keys: set[tuple[str, str, str, str]] = set()
        for edge in edges:
            if edge.kind != "imports":
                continue
            source_group = group_map.get(edge.source)
            target_group = group_map.get(edge.target)
            if source_group is None or target_group is None or source_group == target_group:
                continue
            grouped_edge = GraphEdge(
                source=f"module:{source_group}",
                target=f"module:{target_group}",
                kind="imports",
                confidence=edge.confidence,
                method=edge.method,
            )
            self._append_edge(grouped_edges, edge_keys, grouped_edge)

        return list(grouped_nodes.values()), grouped_edges

    def _build_high_group_map(
        self,
        module_nodes: list[GraphNode],
        entry_module_ids: set[str],
    ) -> dict[str, str]:
        modules_by_parent: dict[str, list[GraphNode]] = defaultdict(list)
        for node in module_nodes:
            parent = str(PurePosixPath(node.path).parent)
            modules_by_parent[parent].append(node)

        group_map: dict[str, str] = {}
        for node in module_nodes:
            parent = str(PurePosixPath(node.path).parent)
            if parent not in {".", ""} and len(modules_by_parent[parent]) > 1 and node.id not in entry_module_ids:
                group_map[node.id] = parent
            else:
                stem_path = str(PurePosixPath(node.path).with_suffix(""))
                if stem_path.endswith("/__init__"):
                    stem_path = stem_path[: -len("/__init__")]
                group_map[node.id] = stem_path or node.path
        return group_map

    def _apply_budget(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        importance_by_id: dict[str, GraphImportance | None],
        budget: int | None,
        limitations: list[str],
    ) -> tuple[list[GraphNode], list[GraphEdge], bool, list[str]]:
        if budget is None or len(nodes) <= budget:
            return nodes, edges, False, limitations

        priority = {"high": 0, "medium": 1, "low": 2, None: 3}
        trimmed_nodes = sorted(
            nodes,
            key=lambda node: (
                priority[importance_by_id.get(node.id)],
                0 if node.kind == "module" else 1,
                node.path,
                node.symbol or "",
            ),
        )[:budget]
        kept_ids = {node.id for node in trimmed_nodes}
        trimmed_edges = [
            edge
            for edge in edges
            if edge.source in kept_ids and edge.target in kept_ids
        ]
        new_limitations = list(limitations)
        new_limitations.append(f"node_budget_applied:{len(nodes)}->{budget}")
        return trimmed_nodes, trimmed_edges, True, self._unique(new_limitations)

    def _build_summary(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        *,
        detail: GraphDetail,
        edge_kinds: set[str],
        budget: int | None,
        entry_points: list[EntryPoint],
        limitations: list[str],
        truncated: bool,
    ) -> ModuleGraphSummary:
        return ModuleGraphSummary(
            requested=True,
            node_count=len(nodes),
            edge_count=len(edges),
            languages=sorted({node.language for node in nodes}),
            methods=sorted({edge.method for edge in edges}),
            main_flows=self._derive_main_flows(nodes, edges, entry_points),
            layers=self._infer_layers(nodes),
            entry_points_count=len(entry_points),
            truncated=truncated,
            detail=detail,
            max_nodes_applied=budget,
            edge_kinds=sorted(edge_kinds),
            limitations=self._unique(limitations),
            hubs=self._find_hubs(nodes, edges),
            orphans=self._find_orphans(nodes, edges),
            cycle_count=self._count_import_cycles(nodes, edges),
        )

    def _derive_main_flows(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        entry_points: list[EntryPoint],
    ) -> list[str]:
        node_map = {node.id: node for node in nodes}
        adjacency: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            if edge.kind not in {"imports", "calls"}:
                continue
            adjacency[edge.source].append(edge.target)

        flows: list[str] = []
        entry_module_ids = self._entry_point_module_ids(entry_points, nodes)
        for source_id in sorted(entry_module_ids):
            if source_id not in adjacency and source_id not in node_map:
                continue
            walk = [self._node_label(node_map[source_id])]
            current = source_id
            visited = {source_id}
            for _ in range(3):
                candidates = [
                    target for target in adjacency.get(current, [])
                    if target in node_map and target not in visited
                ]
                if not candidates:
                    break
                next_target = sorted(
                    candidates,
                    key=lambda node_id: (
                        0 if (node_map[node_id].importance == "high") else 1,
                        0 if node_map[node_id].kind == "module" else 1,
                        node_map[node_id].path,
                    ),
                )[0]
                visited.add(next_target)
                walk.append(self._node_label(node_map[next_target]))
                current = next_target
            if len(walk) > 1:
                flows.append(" -> ".join(walk))
            if len(flows) >= 3:
                break
        return flows

    def _infer_layers(self, nodes: list[GraphNode]) -> list[str]:
        layers: list[str] = []
        for node in nodes:
            if node.kind != "module":
                continue
            parts = PurePosixPath(node.path).parts
            if not parts:
                continue
            layer = parts[0] if len(parts) > 1 else PurePosixPath(node.path).stem
            if layer not in layers:
                layers.append(layer)
        return layers[:10]

    def _entry_point_module_ids(
        self,
        entry_points: list[EntryPoint],
        nodes: list[GraphNode],
    ) -> set[str]:
        module_ids = {node.id for node in nodes if node.kind == "module"}
        resolved: set[str] = set()
        for entry_point in entry_points:
            entry_path = entry_point.path.strip("/")
            candidates = [f"module:{entry_path}"]
            if "/" in entry_path:
                parent = entry_path.rsplit("/", 1)[0]
                candidates.extend(
                    [
                        f"module:{parent}/main.py",
                        f"module:{parent}/src/main.py",
                        f"module:{parent}/index.ts",
                        f"module:{parent}/src/index.ts",
                        f"module:{parent}/index.js",
                        f"module:{parent}/src/index.js",
                    ]
                )
            for candidate in candidates:
                if candidate in module_ids:
                    resolved.add(candidate)
                    break
        return resolved

    def _node_label(self, node: GraphNode) -> str:
        return node.display_name or node.symbol or node.path

    def _analyze_python_file(
        self,
        relative_path: str,
        content: str,
        module_map: dict[str, str],
        workspace: str | None,
    ) -> tuple[list[GraphNode], list[GraphEdge], list[str]]:
        limitations: list[str] = []
        try:
            tree = ast.parse(content, filename=relative_path)
        except SyntaxError:
            return [], [], [f"python_parse_error:{relative_path}"]

        module_node_id = f"module:{relative_path}"
        current_module = module_map.get(relative_path)
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        function_node_ids: dict[str, str] = {}
        class_node_ids: dict[str, str] = {}
        imported_function_node_ids: dict[str, str] = {}
        imported_class_node_ids: dict[str, str] = {}

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_node_id = f"function:{relative_path}:{node.name}"
                function_node_ids[node.name] = function_node_id
                nodes.append(
                    GraphNode(
                        id=function_node_id,
                        kind="function",
                        language="python",
                        path=relative_path,
                        symbol=node.name,
                        display_name=node.name,
                        workspace=workspace,
                    )
                )
                edges.append(
                    GraphEdge(
                        source=module_node_id,
                        target=function_node_id,
                        kind="contains",
                        confidence="high",
                        method="ast",
                    )
                )
            elif isinstance(node, ast.ClassDef):
                class_node_id = f"class:{relative_path}:{node.name}"
                class_node_ids[node.name] = class_node_id
                nodes.append(
                    GraphNode(
                        id=class_node_id,
                        kind="class",
                        language="python",
                        path=relative_path,
                        symbol=node.name,
                        display_name=node.name,
                        workspace=workspace,
                    )
                )
                edges.append(
                    GraphEdge(
                        source=module_node_id,
                        target=class_node_id,
                        kind="contains",
                        confidence="high",
                        method="ast",
                    )
                )

        for ast_node in ast.walk(tree):
            if isinstance(ast_node, ast.Import):
                for alias in ast_node.names:
                    target_path = self._resolve_python_import(alias.name, module_map)
                    if target_path is None:
                        continue
                    edges.append(
                        GraphEdge(
                            source=module_node_id,
                            target=f"module:{target_path}",
                            kind="imports",
                            confidence="high",
                            method="ast",
                        )
                    )
            elif isinstance(ast_node, ast.ImportFrom):
                target_path = self._resolve_python_from_import(
                    ast_node, current_module, module_map
                )
                if target_path is None:
                    continue
                edges.append(
                    GraphEdge(
                        source=module_node_id,
                        target=f"module:{target_path}",
                        kind="imports",
                        confidence="high",
                        method="ast",
                    )
                )
                for alias in ast_node.names:
                    local_name = alias.asname or alias.name
                    imported_function_node_ids[local_name] = f"function:{target_path}:{alias.name}"
                    imported_class_node_ids[local_name] = f"class:{target_path}:{alias.name}"

        for top_level in tree.body:
            if isinstance(top_level, (ast.FunctionDef, ast.AsyncFunctionDef)):
                source_id = function_node_ids.get(top_level.name)
                if source_id is None:
                    continue
                for nested in ast.walk(top_level):
                    if isinstance(nested, ast.Call) and isinstance(nested.func, ast.Name):
                        target_id = function_node_ids.get(nested.func.id) or imported_function_node_ids.get(
                            nested.func.id
                        )
                        if target_id is not None and target_id != source_id:
                            edges.append(
                                GraphEdge(
                                    source=source_id,
                                    target=target_id,
                                    kind="calls",
                                    confidence="medium",
                                    method="ast",
                                )
                            )
            elif isinstance(top_level, ast.ClassDef):
                source_id = class_node_ids.get(top_level.name)
                if source_id is None:
                    continue
                for base in top_level.bases:
                    if isinstance(base, ast.Name):
                        target_id = class_node_ids.get(base.id) or imported_class_node_ids.get(base.id)
                        if target_id is not None:
                            edges.append(
                                GraphEdge(
                                    source=source_id,
                                    target=target_id,
                                    kind="extends",
                                    confidence="medium",
                                    method="ast",
                                )
                            )
        return nodes, edges, limitations

    def _analyze_node_file(
        self,
        relative_path: str,
        content: str,
        module_map: dict[str, str],
        workspace: str | None,
    ) -> tuple[list[GraphNode], list[GraphEdge], list[str]]:
        module_node_id = f"module:{relative_path}"
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        limitations: list[str] = []

        function_pattern = re.compile(
            r"^\s*(?:export\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)",
            re.MULTILINE,
        )
        class_pattern = re.compile(
            r"^\s*(?:export\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+extends\s+([A-Za-z_][A-Za-z0-9_]*))?",
            re.MULTILINE,
        )
        function_node_ids: dict[str, str] = {}
        class_node_ids: dict[str, str] = {}

        for match in function_pattern.finditer(content):
            name = match.group(1)
            node_id = f"function:{relative_path}:{name}"
            function_node_ids[name] = node_id
            nodes.append(
                GraphNode(
                    id=node_id,
                    kind="function",
                    language="nodejs",
                    path=relative_path,
                    symbol=name,
                    display_name=name,
                    workspace=workspace,
                )
            )
            edges.append(
                GraphEdge(
                    source=module_node_id,
                    target=node_id,
                    kind="contains",
                    confidence="medium",
                    method="heuristic",
                )
            )

        for match in class_pattern.finditer(content):
            name = match.group(1)
            base = match.group(2)
            node_id = f"class:{relative_path}:{name}"
            class_node_ids[name] = node_id
            nodes.append(
                GraphNode(
                    id=node_id,
                    kind="class",
                    language="nodejs",
                    path=relative_path,
                    symbol=name,
                    display_name=name,
                    workspace=workspace,
                )
            )
            edges.append(
                GraphEdge(
                    source=module_node_id,
                    target=node_id,
                    kind="contains",
                    confidence="medium",
                    method="heuristic",
                )
            )
            if base and base in class_node_ids:
                edges.append(
                    GraphEdge(
                        source=node_id,
                        target=class_node_ids[base],
                        kind="extends",
                        confidence="low",
                        method="heuristic",
                    )
                )

        patterns = [
            re.compile(r"""import\s+(?:[^'"]+?\s+from\s+)?['"]([^'"]+)['"]"""),
            re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),
        ]
        found_specs: set[str] = set()
        for pattern in patterns:
            for match in pattern.finditer(content):
                spec = match.group(1)
                if spec in found_specs:
                    continue
                found_specs.add(spec)
                if spec.startswith("."):
                    target_path = self._resolve_node_import(relative_path, spec, module_map)
                    if target_path is None:
                        limitations.append(f"node_unresolved:{relative_path}:{spec}")
                        continue
                    edges.append(
                        GraphEdge(
                            source=module_node_id,
                            target=f"module:{target_path}",
                            kind="imports",
                            confidence="medium",
                            method="heuristic",
                        )
                    )
                else:
                    # Silently skip stdlib and noise packages — they add no structural signal
                    bare = spec.removeprefix("node:")
                    root_pkg = bare.split("/")[0]
                    if (spec not in _NODE_STDLIB
                            and bare not in _NODE_STDLIB
                            and root_pkg not in _NODE_STDLIB
                            and root_pkg not in _NODE_NOISE_PACKAGES):
                        limitations.append(f"node_external:{relative_path}:{spec}")

        return nodes, edges, limitations

    def _analyze_go_file(
        self,
        relative_path: str,
        content: str,
        import_map: dict[str, str],
        workspace: str | None,
    ) -> tuple[list[GraphNode], list[GraphEdge], list[str]]:
        module_node_id = f"module:{relative_path}"
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        limitations: list[str] = []

        for match in re.finditer(r"(?m)^func\s+([A-ZA-Za-z_][A-Za-z0-9_]*)\s*\(", content):
            name = match.group(1)
            node_id = f"function:{relative_path}:{name}"
            nodes.append(
                GraphNode(
                    id=node_id,
                    kind="function",
                    language="go",
                    path=relative_path,
                    symbol=name,
                    display_name=name,
                    workspace=workspace,
                )
            )
            edges.append(
                GraphEdge(
                    source=module_node_id,
                    target=node_id,
                    kind="contains",
                    confidence="medium",
                    method="heuristic",
                )
            )

        import_specs = re.findall(r'"([^"]+)"', self._extract_go_import_block(content))
        for spec in import_specs:
            target_path = import_map.get(spec)
            if target_path is None:
                continue
            edges.append(
                GraphEdge(
                    source=module_node_id,
                    target=f"module:{target_path}",
                    kind="imports",
                    confidence="medium",
                    method="heuristic",
                )
            )
        return nodes, edges, limitations

    def _analyze_jvm_file(
        self,
        relative_path: str,
        content: str,
        type_map: dict[str, tuple[str, str]],
        workspace: str | None,
    ) -> tuple[list[GraphNode], list[GraphEdge], list[str]]:
        module_node_id = f"module:{relative_path}"
        language = self._language_for_suffix(Path(relative_path).suffix)
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        limitations: list[str] = []
        imported_type_map = {
            imported.split(".")[-1]: imported
            for imported in re.findall(r"(?m)^\s*import\s+([A-Za-z0-9_.]+)", content)
        }

        class_pattern = re.compile(
            r"(?m)^\s*(?:public\s+|private\s+|protected\s+|internal\s+)?"
            r"(?:class|interface|object|trait)\s+([A-Za-z_][A-Za-z0-9_]*)"
            r"(?:\s+extends\s+([A-Za-z_][A-Za-z0-9_.]*))?"
        )
        local_class_ids: dict[str, str] = {}
        for match in class_pattern.finditer(content):
            name = match.group(1)
            base = match.group(2)
            node_id = f"class:{relative_path}:{name}"
            local_class_ids[name] = node_id
            nodes.append(
                GraphNode(
                    id=node_id,
                    kind="class",
                    language=language,
                    path=relative_path,
                    symbol=name,
                    display_name=name,
                    workspace=workspace,
                )
            )
            edges.append(
                GraphEdge(
                    source=module_node_id,
                    target=node_id,
                    kind="contains",
                    confidence="medium",
                    method="heuristic",
                )
            )
            if base:
                base_short = base.split(".")[-1]
                if base_short in local_class_ids:
                    edges.append(
                        GraphEdge(
                            source=node_id,
                            target=local_class_ids[base_short],
                            kind="extends",
                            confidence="low",
                            method="heuristic",
                        )
                    )
                else:
                    imported_base = imported_type_map.get(base_short)
                    fqcn = imported_base or base
                    mapping = type_map.get(fqcn)
                    if mapping is None:
                        continue
                    base_path, _base_name = mapping
                    edges.append(
                        GraphEdge(
                            source=node_id,
                            target=f"class:{base_path}:{base_short}",
                            kind="extends",
                            confidence="low",
                            method="heuristic",
                        )
                    )

        for imported in imported_type_map.values():
            mapping = type_map.get(imported)
            if mapping is None:
                limitations.append(f"jvm_unresolved:{relative_path}:{imported}")
                continue
            target_path, _symbol = mapping
            edges.append(
                GraphEdge(
                    source=module_node_id,
                    target=f"module:{target_path}",
                    kind="imports",
                    confidence="medium",
                    method="heuristic",
                )
            )
        return nodes, edges, limitations

    def _build_python_module_map(self, source_files: list[str]) -> dict[str, str]:
        module_map: dict[str, str] = {}
        for relative_path in source_files:
            path = PurePosixPath(relative_path)
            if path.suffix not in self._PYTHON_EXTENSIONS:
                continue
            if path.name == "__init__.py":
                module_name = ".".join(path.parts[:-1])
            else:
                module_name = ".".join(path.with_suffix("").parts)
            if module_name:
                module_map[relative_path] = module_name
        return module_map

    def _build_go_import_map(self, root: Path, source_files: list[str]) -> dict[str, str]:
        go_mod = root / "go.mod"
        if not go_mod.exists():
            return {}
        module_name = ""
        for line in go_mod.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("module "):
                module_name = stripped.removeprefix("module ").strip()
                break
        if not module_name:
            return {}
        mapping: dict[str, str] = {}
        for relative_path in source_files:
            path = PurePosixPath(relative_path)
            if path.suffix not in self._GO_EXTENSIONS:
                continue
            directory = "." if len(path.parts) == 1 else "/".join(path.parts[:-1])
            import_path = module_name if directory == "." else f"{module_name}/{directory}"
            mapping[import_path] = relative_path
        return mapping

    def _build_jvm_type_map(self, root: Path, source_files: list[str]) -> dict[str, tuple[str, str]]:
        mapping: dict[str, tuple[str, str]] = {}
        package_pattern = re.compile(r"(?m)^\s*package\s+([A-Za-z0-9_.]+)")
        type_pattern = re.compile(
            r"(?m)^\s*(?:public\s+|private\s+|protected\s+|internal\s+)?"
            r"(?:class|interface|object|trait)\s+([A-Za-z_][A-Za-z0-9_]*)"
        )
        for relative_path in source_files:
            if Path(relative_path).suffix not in self._JVM_EXTENSIONS:
                continue
            try:
                content = (root / relative_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            package_match = package_pattern.search(content)
            type_match = type_pattern.search(content)
            if type_match is None:
                continue
            package_name = package_match.group(1) if package_match else ""
            symbol = type_match.group(1)
            fqcn = f"{package_name}.{symbol}" if package_name else symbol
            mapping[fqcn] = (relative_path, symbol)
        return mapping

    def _resolve_python_import(
        self, module_name: str, module_map: dict[str, str]
    ) -> Optional[str]:
        # Skip stdlib modules — they add no structural signal to the project graph
        root_module = module_name.split(".")[0]
        if root_module in _PYTHON_STDLIB:
            return None
        for path, candidate in module_map.items():
            if candidate == module_name:
                return path
        return None

    def _resolve_python_from_import(
        self,
        node: ast.ImportFrom,
        current_module: str | None,
        module_map: dict[str, str],
    ) -> Optional[str]:
        if node.level and current_module:
            package_parts = current_module.split(".")
            base_parts = package_parts[:-1]
            if node.level > 1:
                base_parts = base_parts[: max(0, len(base_parts) - (node.level - 1))]
            target_module = ".".join(base_parts + ([node.module] if node.module else []))
        else:
            target_module = node.module or ""
        if not target_module:
            return None
        return self._resolve_python_import(target_module, module_map)

    def _resolve_node_import(
        self, source_path: str, spec: str, module_map: dict[str, str]
    ) -> Optional[str]:
        base_dir = PurePosixPath(source_path).parent
        spec_path = PurePosixPath(spec)
        candidate_base = (base_dir / spec_path).as_posix()
        candidates = [
            candidate_base,
            f"{candidate_base}.js",
            f"{candidate_base}.jsx",
            f"{candidate_base}.ts",
            f"{candidate_base}.tsx",
            f"{candidate_base}/index.js",
            f"{candidate_base}/index.ts",
            f"{candidate_base}/index.tsx",
        ]
        for candidate in candidates:
            normalized = PurePosixPath(candidate).as_posix()
            if normalized in module_map:
                return normalized
        return None

    def _extract_go_import_block(self, content: str) -> str:
        block_match = re.search(r"import\s*\((.*?)\)", content, re.DOTALL)
        if block_match is not None:
            return block_match.group(1)
        single_imports = re.findall(r'(?m)^\s*import\s+"([^"]+)"', content)
        return "\n".join(f'"{item}"' for item in single_imports)

    def _append_node(self, nodes: list[GraphNode], node_ids: set[str], node: GraphNode) -> None:
        if len(nodes) >= self.max_nodes or node.id in node_ids:
            return
        node_ids.add(node.id)
        nodes.append(node)

    def _append_edge(
        self,
        edges: list[GraphEdge],
        edge_keys: set[tuple[str, str, str, str]],
        edge: GraphEdge,
    ) -> None:
        if len(edges) >= self.max_edges:
            return
        key = (edge.source, edge.target, edge.kind, edge.method)
        if key in edge_keys:
            return
        edge_keys.add(key)
        edges.append(edge)

    # ── workspace graph methods ───────────────────────────────────────────────

    def _is_node_workspace(self, root: Path, file_tree: dict[str, Any]) -> bool:
        for marker in ("turbo.json", "nx.json", "pnpm-workspace.yaml"):
            if (root / marker).is_file():
                return True
        pkg = load_json_file(root / "package.json")
        return isinstance(pkg, dict) and isinstance(pkg.get("workspaces"), (list, dict))

    def _analyze_node_workspace(
        self,
        root: Path,
        file_tree: dict[str, Any],
        workspace: str | None,
    ) -> tuple[list[GraphNode], list[GraphEdge], list[str]]:
        _SKIP = {"node_modules/", ".venv/", "dist/", "build/", ".git/", ".turbo/"}
        pkg_paths = [
            p for p in flatten_file_tree(file_tree)
            if p != "package.json"
            and p.endswith("/package.json")
            and not any(s in p for s in _SKIP)
            and (root / p).is_file()
        ]
        if not pkg_paths:
            return [], [], []

        name_to_dir: dict[str, str] = {}
        pkg_data: list[tuple[str, dict[str, Any]]] = []
        for rel_path in pkg_paths:
            data = load_json_file(root / rel_path)
            if not isinstance(data, dict):
                continue
            pkg_dir = str(PurePosixPath(rel_path).parent)
            name = data.get("name", "")
            if isinstance(name, str) and name:
                name_to_dir[name] = pkg_dir
                pkg_data.append((pkg_dir, data))

        if not name_to_dir:
            return [], [], []

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        dir_to_id: dict[str, str] = {}
        for pkg_dir, data in pkg_data:
            pkg_name = str(data.get("name", pkg_dir.split("/")[-1]))
            node_id = f"module:{pkg_dir}"
            dir_to_id[pkg_dir] = node_id
            nodes.append(GraphNode(
                id=node_id, kind="module", language="nodejs",
                path=pkg_dir, display_name=pkg_name, workspace=workspace,
            ))

        for pkg_dir, data in pkg_data:
            source_id = dir_to_id.get(pkg_dir)
            if not source_id:
                continue
            all_deps: set[str] = set()
            for field in ("dependencies", "devDependencies", "peerDependencies"):
                raw = data.get(field, {})
                if isinstance(raw, dict):
                    all_deps.update(str(k) for k in raw)
            for dep in all_deps:
                target_dir = name_to_dir.get(dep)
                if target_dir and target_dir != pkg_dir:
                    edges.append(GraphEdge(
                        source=source_id,
                        target=dir_to_id.get(target_dir, f"module:{target_dir}"),
                        kind="imports", confidence="high", method="heuristic",
                    ))
        return nodes, edges, []

    def _analyze_rust_workspace(
        self,
        root: Path,
        workspace: str | None,
    ) -> tuple[list[GraphNode], list[GraphEdge], list[str]]:
        cargo = load_toml_file(root / "Cargo.toml")
        if not cargo:
            return [], [], []
        ws = cargo.get("workspace", {})
        if not isinstance(ws, dict):
            return [], [], []
        members_raw = ws.get("members", [])
        if not isinstance(members_raw, list) or not members_raw:
            return [], [], []

        member_dirs = self._expand_cargo_members(root, [str(m) for m in members_raw])
        if not member_dirs:
            return [], [], []

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        limitations: list[str] = []
        name_to_dir: dict[str, str] = {}
        member_data: list[tuple[str, dict[str, Any]]] = []

        for member_dir in member_dirs:
            mc = load_toml_file(root / member_dir / "Cargo.toml")
            if not mc:
                limitations.append(f"rust_workspace_missing:{member_dir}")
                continue
            pkg = mc.get("package", {})
            name = str(pkg.get("name", member_dir.split("/")[-1])) if isinstance(pkg, dict) else member_dir.split("/")[-1]
            name_to_dir[name] = member_dir
            member_data.append((member_dir, mc))

        for member_dir, mc in member_data:
            pkg = mc.get("package", {})
            name = str(pkg.get("name", member_dir.split("/")[-1])) if isinstance(pkg, dict) else member_dir.split("/")[-1]
            is_bin = (root / member_dir / "src" / "main.rs").is_file()
            nodes.append(GraphNode(
                id=f"module:{member_dir}", kind="module", language="rust",
                path=member_dir,
                display_name=f"{name} ({'bin' if is_bin else 'lib'})",
                workspace=workspace,
            ))

        for member_dir, mc in member_data:
            source_id = f"module:{member_dir}"
            deps = mc.get("dependencies", {})
            if isinstance(deps, dict):
                for dep_name in deps:
                    target_dir = name_to_dir.get(str(dep_name))
                    if target_dir and target_dir != member_dir:
                        edges.append(GraphEdge(
                            source=source_id, target=f"module:{target_dir}",
                            kind="imports", confidence="high", method="heuristic",
                        ))
        return nodes, edges, limitations

    def _expand_cargo_members(self, root: Path, members: list[str]) -> list[str]:
        result: list[str] = []
        for member in members:
            if "*" in member or "?" in member:
                for expanded in root.glob(member):
                    if expanded.is_dir():
                        result.append(str(expanded.relative_to(root)).replace("\\", "/"))
            else:
                cleaned = member.replace("\\", "/")
                if (root / cleaned).is_dir():
                    result.append(cleaned)
        return result

    def _analyze_go_workspace(
        self,
        root: Path,
        workspace: str | None,
    ) -> tuple[list[GraphNode], list[GraphEdge], list[str]]:
        use_paths = self._parse_go_work_uses(root / "go.work")
        if not use_paths:
            return [], [], []

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        mod_name_to_dir: dict[str, str] = {}
        dir_list: list[str] = []

        for raw_path in use_paths:
            mod_dir = raw_path.lstrip("./").replace("\\", "/") or "."
            go_mod = root / (mod_dir if mod_dir != "." else "") / "go.mod"
            if not go_mod.is_file():
                go_mod = root / "go.mod" if mod_dir == "." else root / mod_dir / "go.mod"
            mod_name = self._read_go_module_name(go_mod)
            display = mod_name.split("/")[-1] if mod_name else (mod_dir.split("/")[-1] or "root")
            if mod_name:
                mod_name_to_dir[mod_name] = mod_dir
            dir_list.append(mod_dir)
            nodes.append(GraphNode(
                id=f"module:{mod_dir}", kind="module", language="go",
                path=mod_dir, display_name=display, workspace=workspace,
            ))

        for mod_dir in dir_list:
            go_mod_path = root / "go.mod" if mod_dir == "." else root / mod_dir / "go.mod"
            if not go_mod_path.is_file():
                continue
            try:
                content = go_mod_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            source_id = f"module:{mod_dir}"
            for other_name, other_dir in mod_name_to_dir.items():
                if other_dir != mod_dir and other_name in content:
                    edges.append(GraphEdge(
                        source=source_id, target=f"module:{other_dir}",
                        kind="imports", confidence="medium", method="heuristic",
                    ))
        return nodes, edges, []

    def _parse_go_work_uses(self, go_work: Path) -> list[str]:
        try:
            content = go_work.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        uses: list[str] = []
        in_block = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("use ("):
                in_block = True
            elif in_block:
                if stripped == ")":
                    in_block = False
                elif stripped and not stripped.startswith("//"):
                    uses.append(stripped)
            elif stripped.startswith("use ") and not stripped.startswith("use ("):
                path = stripped[4:].strip()
                if path and not path.startswith("//"):
                    uses.append(path)
        return uses

    def _read_go_module_name(self, go_mod: Path) -> str:
        try:
            for line in go_mod.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s.startswith("module "):
                    return s[7:].strip()
        except OSError:
            pass
        return ""

    # ── graph analytics ───────────────────────────────────────────────────────

    def _find_hubs(self, nodes: list[GraphNode], edges: list[GraphEdge]) -> list[str]:
        module_ids = {n.id for n in nodes if n.kind == "module"}
        incoming: Counter[str] = Counter(
            e.target for e in edges if e.kind == "imports" and e.target in module_ids
        )
        return [nid for nid, _ in incoming.most_common(5) if incoming[nid] >= 2]

    def _find_orphans(self, nodes: list[GraphNode], edges: list[GraphEdge]) -> list[str]:
        module_ids = {n.id for n in nodes if n.kind == "module"}
        connected: set[str] = set()
        for e in edges:
            if e.kind == "imports":
                connected.add(e.source)
                connected.add(e.target)
        return sorted(module_ids - connected)[:10]

    def _count_import_cycles(self, nodes: list[GraphNode], edges: list[GraphEdge]) -> int:
        """Count strongly connected components of size >= 2 via Kosaraju's algorithm."""
        module_ids = {n.id for n in nodes if n.kind == "module"}
        if not module_ids:
            return 0
        adj: dict[str, list[str]] = {nid: [] for nid in module_ids}
        radj: dict[str, list[str]] = {nid: [] for nid in module_ids}
        for e in edges:
            if e.kind == "imports" and e.source in adj and e.target in adj:
                adj[e.source].append(e.target)
                radj[e.target].append(e.source)

        visited: set[str] = set()
        order: list[str] = []

        def dfs1(start: str) -> None:
            stack: list[tuple[str, bool]] = [(start, False)]
            while stack:
                v, done = stack.pop()
                if done:
                    order.append(v)
                    continue
                if v in visited:
                    continue
                visited.add(v)
                stack.append((v, True))
                for nb in adj[v]:
                    if nb not in visited:
                        stack.append((nb, False))

        for nid in module_ids:
            if nid not in visited:
                dfs1(nid)

        visited2: set[str] = set()
        cycle_count = 0
        for nid in reversed(order):
            if nid in visited2:
                continue
            comp: list[str] = []
            stack2 = [nid]
            while stack2:
                v = stack2.pop()
                if v in visited2:
                    continue
                visited2.add(v)
                comp.append(v)
                for nb in radj[v]:
                    if nb not in visited2:
                        stack2.append(nb)
            if len(comp) >= 2:
                cycle_count += 1
        return cycle_count

    def _analyze_dotnet_projects(
        self,
        root: Path,
        csproj_paths: list[str],
        workspace: str | None,
    ) -> tuple[list[GraphNode], list[GraphEdge], list[str]]:
        from sourcecode.detectors.csproj_parser import parse_csproj

        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        limitations: list[str] = []

        projects = []
        node_id_map: dict[str, str] = {}  # csproj_path → node_id

        for rel_path in csproj_paths:
            project = parse_csproj(root / rel_path, rel_path)
            if project is None:
                limitations.append(f"dotnet_parse_error:{rel_path}")
                continue
            projects.append(project)
            node_path = project.project_dir if project.project_dir else project.name
            node_id = f"module:{node_path}"
            node_id_map[project.path] = node_id
            nodes.append(
                GraphNode(
                    id=node_id,
                    kind="module",
                    language=project.language,
                    path=node_path,
                    display_name=project.name,
                    workspace=workspace,
                )
            )

        for project in projects:
            source_id = node_id_map.get(project.path)
            if source_id is None:
                continue
            for ref_path in project.project_references:
                target_id = node_id_map.get(ref_path)
                if target_id is None:
                    limitations.append(f"dotnet_unresolved_ref:{project.path}:{ref_path}")
                    continue
                edges.append(
                    GraphEdge(
                        source=source_id,
                        target=target_id,
                        kind="imports",
                        confidence="high",
                        method="heuristic",
                    )
                )

        return nodes, edges, limitations

    def _language_for_suffix(self, suffix: str) -> str:
        if suffix in self._PYTHON_EXTENSIONS:
            return "python"
        if suffix in self._NODE_EXTENSIONS:
            return "nodejs"
        if suffix in self._GO_EXTENSIONS:
            return "go"
        if suffix == ".java":
            return "java"
        if suffix == ".kt":
            return "kotlin"
        if suffix == ".scala":
            return "scala"
        if suffix == ".cs":
            return "csharp"
        if suffix == ".fs":
            return "fsharp"
        if suffix == ".vb":
            return "vbnet"
        return "unknown"

    def _node_id(self, kind: str, path: str, symbol: str | None = None) -> str:
        if symbol:
            return f"{kind}:{path}:{symbol}"
        return f"{kind}:{path}"

    def _unique(self, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            if value not in result:
                result.append(value)
        return result
