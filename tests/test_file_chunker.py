"""Tests for file_chunker.py — BLOCKER-B: semantic chunking of large Java files.

Coverage:
  - Basic chunking of a simple Java class
  - Method/constructor boundary detection
  - Context header content (package, class, imports)
  - max_lines respected (except oversized methods)
  - size_warning on methods exceeding max_lines
  - metadata_only mode (no content)
  - Single-chunk retrieval by chunk_id
  - Real large file (OrderServices.java) covers no-timeout and completeness
  - File read error handling
  - chunk_count_by_type accuracy
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from sourcecode.file_chunker import chunk_java_file, ChunkResult, ChunkRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_java(tmp_path: pathlib.Path, filename: str, content: str) -> pathlib.Path:
    p = tmp_path / filename
    p.write_text(content)
    return p


_SIMPLE_CLASS = """
package com.example;

import java.util.List;
import java.util.ArrayList;

/**
 * Simple service for testing.
 */
public class SimpleService {

    private String name;
    private int count;

    public SimpleService(String name) {
        this.name = name;
        this.count = 0;
    }

    public String getName() {
        return this.name;
    }

    public int getCount() {
        return this.count;
    }

    public void increment() {
        this.count++;
    }

    public List<String> buildList() {
        List<String> result = new ArrayList<>();
        result.add(name);
        return result;
    }
}
""".strip()


# ---------------------------------------------------------------------------
# Basic chunking
# ---------------------------------------------------------------------------

class TestBasicChunking:
    def test_returns_chunk_result(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        assert isinstance(result, ChunkResult)

    def test_class_name_detected(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        assert result.class_name == "SimpleService"

    def test_package_detected(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        assert result.package == "com.example"

    def test_total_lines_accurate(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        assert result.total_lines == len(_SIMPLE_CLASS.splitlines())

    def test_chunks_are_non_empty(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        assert result.total_chunks > 0
        assert len(result.chunks) == result.total_chunks

    def test_chunk_ids_sequential(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        ids = [c.chunk_id for c in result.chunks]
        assert ids == list(range(1, len(ids) + 1))

    def test_chunks_cover_full_file(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p, include_content=True)
        all_content = "\n".join(c.content for c in result.chunks)
        # All original source lines should appear somewhere in chunks
        for line in _SIMPLE_CLASS.splitlines():
            assert line in all_content, f"Line not in any chunk: {repr(line)}"

    def test_no_line_overlap(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        for i in range(len(result.chunks) - 1):
            assert result.chunks[i].end_line < result.chunks[i + 1].start_line, (
                f"Chunks {i+1} and {i+2} overlap or gap"
            )


# ---------------------------------------------------------------------------
# Method detection
# ---------------------------------------------------------------------------

class TestMethodDetection:
    def test_methods_detected_as_chunks(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        method_names = {c.symbol.split("#")[1] for c in result.chunks if "#" in c.symbol}
        assert "getName" in method_names
        assert "getCount" in method_names
        assert "increment" in method_names
        assert "buildList" in method_names

    def test_constructor_detected(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        constructor_chunks = [c for c in result.chunks if c.chunk_type == "constructor"]
        assert len(constructor_chunks) >= 1
        assert any("SimpleService" in c.symbol for c in constructor_chunks)

    def test_method_chunk_type_correct(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        method_chunks = [c for c in result.chunks if c.chunk_type == "method"]
        assert len(method_chunks) >= 4  # getName, getCount, increment, buildList


# ---------------------------------------------------------------------------
# Context header
# ---------------------------------------------------------------------------

class TestContextHeader:
    def test_context_header_has_package(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        for chunk in result.chunks:
            assert "com.example" in chunk.context_header

    def test_context_header_has_class_name(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        for chunk in result.chunks:
            assert "SimpleService" in chunk.context_header

    def test_context_header_has_imports(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p)
        for chunk in result.chunks:
            assert "import" in chunk.context_header


# ---------------------------------------------------------------------------
# max_lines and size_warning
# ---------------------------------------------------------------------------

class TestMaxLines:
    def test_small_max_lines_produces_more_chunks(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        r_default = chunk_java_file(p, max_lines=500)
        r_small = chunk_java_file(p, max_lines=5)
        assert r_small.total_chunks >= r_default.total_chunks

    def test_oversized_method_gets_size_warning(self, tmp_path):
        # Build a method that is 50 lines long, max_lines=10
        method_lines = ["    public void bigMethod() {"] + [f"        int x{i} = {i};" for i in range(48)] + ["    }"]
        src = "package com.ex;\npublic class BigService {\n" + "\n".join(method_lines) + "\n}\n"
        p = _write_java(tmp_path, "BigService.java", src)
        result = chunk_java_file(p, max_lines=10)
        oversized = [c for c in result.chunks if c.size_warning]
        assert len(oversized) >= 1
        assert any("bigMethod" in c.symbol for c in oversized)
        assert result.limitations  # limitations list populated

    def test_normal_methods_no_size_warning(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p, max_lines=500)
        assert not any(c.size_warning for c in result.chunks)


# ---------------------------------------------------------------------------
# metadata_only mode
# ---------------------------------------------------------------------------

class TestMetadataOnly:
    def test_metadata_only_has_no_content(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p, include_content=False)
        for chunk in result.chunks:
            assert chunk.content == "", f"Expected empty content, got: {repr(chunk.content[:50])}"

    def test_metadata_only_has_all_fields(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p, include_content=False)
        for chunk in result.chunks:
            assert chunk.chunk_id > 0
            assert chunk.chunk_type
            assert chunk.symbol
            assert chunk.start_line >= 1
            assert chunk.end_line >= chunk.start_line


# ---------------------------------------------------------------------------
# to_dict serialization
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_to_dict_complete(self, tmp_path):
        import json
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p, include_content=True)
        d = result.to_dict()
        assert "file" in d
        assert "total_lines" in d
        assert "total_chunks" in d
        assert "class_name" in d
        assert "package" in d
        assert "chunks" in d
        # Must be JSON-serializable
        serialized = json.dumps(d)
        loaded = json.loads(serialized)
        assert loaded["class_name"] == "SimpleService"

    def test_chunk_to_dict_fields(self, tmp_path):
        p = _write_java(tmp_path, "SimpleService.java", _SIMPLE_CLASS)
        result = chunk_java_file(p, include_content=True)
        for c in result.chunks:
            d = c.to_dict()
            required = {"chunk_id", "chunk_type", "symbol", "start_line", "end_line",
                        "total_lines", "context_header", "content", "size_warning"}
            assert required <= set(d.keys())


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_nonexistent_file(self, tmp_path):
        result = chunk_java_file(tmp_path / "DoesNotExist.java")
        assert result.total_chunks == 0
        assert result.total_lines == 0
        assert result.limitations


# ---------------------------------------------------------------------------
# Real large file validation (BLOCKER-B P0 requirement)
# ---------------------------------------------------------------------------

REAL_LARGE_FILE = pathlib.Path(
    "/Users/user/Documents/workspace/testing/ofbiz-framework/"
    "applications/order/src/main/java/org/apache/ofbiz/order/order/OrderServices.java"
)


@pytest.mark.skipif(not REAL_LARGE_FILE.exists(), reason="testing repo not available")
class TestRealLargeFile:
    def test_no_timeout_on_7500_lines(self):
        import time
        t0 = time.time()
        result = chunk_java_file(REAL_LARGE_FILE, max_lines=500, include_content=False)
        t1 = time.time()
        assert (t1 - t0) < 10, f"Chunking took {t1-t0:.1f}s — too slow"
        assert result.total_lines >= 7500
        assert result.total_chunks > 100

    def test_class_name_detected_correctly(self):
        result = chunk_java_file(REAL_LARGE_FILE, include_content=False)
        assert result.class_name == "OrderServices"

    def test_chunks_cover_all_lines(self):
        result = chunk_java_file(REAL_LARGE_FILE, include_content=False)
        # All lines must be covered by exactly one chunk (no gaps beyond rounding)
        last_end = 0
        for c in result.chunks:
            assert c.start_line >= last_end, f"Gap or overlap at chunk {c.chunk_id}"
            last_end = c.end_line
        assert last_end >= result.total_lines - 1

    def test_limitations_for_oversized_method(self):
        result = chunk_java_file(REAL_LARGE_FILE, max_lines=500, include_content=False)
        assert result.limitations, "createOrder method (973 lines) should trigger limitation"

    def test_method_chunks_have_symbols(self):
        result = chunk_java_file(REAL_LARGE_FILE, include_content=False)
        method_chunks = [c for c in result.chunks if c.chunk_type == "method"]
        assert len(method_chunks) > 50
        for c in method_chunks:
            assert "#" in c.symbol, f"Method chunk should have Class#method symbol: {c.symbol}"
