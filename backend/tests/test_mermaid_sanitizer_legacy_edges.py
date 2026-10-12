"""Regression tests for legacy edge text normalization (Step 0a).

BUG fix: `A -- "label" --> B` syntax was misidentified as bare nodes
by the old parser, creating garbage nodes + topology corruption.
New `_LEGACY_EDGE_TEXT_RE` rewrites it to canonical `A -->|"label"| B`.

Run: cd backend && pytest tests/test_mermaid_sanitizer_legacy_edges.py -v
"""

from app.services.ai.mermaid_sanitizer import sanitize_mermaid_code


class TestLegacyEdgeTextNormalization:
    """Step 0a: legacy edge text normalization tests."""

    def test_legacy_edge_text_with_quotes(self):
        """A -- "label" --> B should be normalized to pipe format."""
        result = sanitize_mermaid_code(
            "flowchart TD\nA[\"Project Start\"] -- \"Pass\" --> B[\"Completion\"]"
        )
        assert "--> |" in result

    def test_legacy_edge_text_plain(self):
        """A -- label --> B (no quotes) should be normalized."""
        result = sanitize_mermaid_code(
            "flowchart TD\nA[\"A\"] -- Pass --> B[\"B\"]"
        )
        assert "--> |" in result

    def test_arrow_chain_not_matched_as_legacy(self):
        """A --> B --> C should NOT be matched as legacy edge."""
        result = sanitize_mermaid_code(
            "flowchart TD\nA[\"Start\"] --> B[\"Middle\"] --> C[\"End\"]"
        )
        # Should NOT produce legacy edge artifacts - edges are split per line
        # but all three nodes and both edges should be preserved
        assert 'A["Start"]' in result
        assert 'B["Middle"]' in result
        assert 'C["End"]' in result
        # Should have 2 edges (not 0 or 3+)
        assert result.count("-->") == 2

    def test_bracket_label_with_dash_not_matched_as_edge(self):
        """-- inside bracket label should NOT be treated as legacy edge."""
        result = sanitize_mermaid_code(
            "flowchart TD\nN1[\"Node 2026-09-22 -- 2026-12-15\"] --> N2[\"Key Node\"]"
        )
        assert "2026-09-22 -- 2026-12-15" in result

    def test_full_chart_with_multiple_legacy_edges(self):
        """Full chart with multiple legacy edges should be normalized."""
        result = sanitize_mermaid_code(
            "flowchart TD\n"
            "N1[\"Project Start\"] --> Check{\"BIM Check\"}\n"
            "Check -- \"Pass\" --> N2[\"Concrete\"]\n"
            "Check -- \"Fail\" --> N3[\"Optimize\"]"
        )
        assert "--> |" in result

    def test_canonical_pipe_syntax_preserved(self):
        """Canonical pipe syntax should not be modified."""
        result = sanitize_mermaid_code(
            "flowchart TD\nA[\"A\"] -->|Pass| B[\"B\"]"
        )
        assert "--> |" in result

    def test_decision_node_legacy_edge(self):
        """Decision node with legacy edge text should work correctly."""
        result = sanitize_mermaid_code(
            "flowchart TD\n"
            "Start[\"Start\"] --> D1{\"Decision\"}\n"
            "D1 -- \"Yes\" --> Y[\"Yes Path\"]\n"
            "D1 -- \"No\" --> N[\"No Path\"]"
        )
        assert "--> |" in result
        # Both edges should have pipe-delimited labels
        assert result.count("--> |") >= 2
