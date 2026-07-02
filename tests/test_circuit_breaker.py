"""
tests/test_circuit_breaker.py
------------------------------
Unit tests for the circuit breaker in retrieve_similar_cases_batch.

Tests verify:
- Normal operation (all succeed) returns correct results
- When >50% of tasks fail, circuit breaker triggers
- Partial results are returned with error flags
- Results are sorted by idx

Run: python -m pytest tests/test_circuit_breaker.py -v
"""
import sys
import pathlib
from unittest.mock import patch, MagicMock
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


class TestCircuitBreaker:
    """Tests for circuit breaker in retrieve_similar_cases_batch."""

    def _make_items(self, n):
        """Create n test items for batch processing."""
        return [
            {"idx": i, "pain_point": f"pain point {i}", "solution": "Ariba Sourcing"}
            for i in range(n)
        ]

    @patch("server.recommend.retrieve_similar_cases")
    def test_all_succeed(self, mock_retrieve):
        """When all tasks succeed, all results are returned."""
        from server.recommend import retrieve_similar_cases_batch

        mock_retrieve.return_value = [
            {"similar_pain_point": "test", "category": "Innovation",
             "effort": "Low", "timeline": "Quick Win", "impact": "N/A",
             "solution_area": None, "similarity_score": 0.8}
        ]

        items = self._make_items(5)
        results = retrieve_similar_cases_batch(items, top_k=3)

        assert len(results) == 5
        assert all("cases" in r for r in results)
        assert all(len(r["cases"]) == 1 for r in results)
        # Verify sorted by idx
        idxs = [r["idx"] for r in results]
        assert idxs == sorted(idxs)

    @patch("server.recommend.retrieve_similar_cases")
    def test_circuit_breaker_triggers(self, mock_retrieve):
        """When >50% of tasks fail, circuit breaker triggers and returns partial results."""
        from server.recommend import retrieve_similar_cases_batch

        call_count = {"n": 0}

        def _side_effect(pain_point, solution, area=None, top_k=3):
            call_count["n"] += 1
            # First 2 succeed, rest fail (thread execution order is non-deterministic)
            if call_count["n"] <= 2:
                return [{"similar_pain_point": "ok", "category": "Innovation",
                         "effort": "Low", "timeline": "Quick Win", "impact": "N/A",
                         "solution_area": None, "similarity_score": 0.7}]
            raise TimeoutError("HANA connection timeout")

        mock_retrieve.side_effect = _side_effect

        items = self._make_items(10)
        results = retrieve_similar_cases_batch(items, top_k=3)

        # Should have results for all 10 items (some with errors)
        assert len(results) == 10

        # Check that some have error flags
        errored = [r for r in results if "error" in r]

        # With >50% failures, circuit breaker should have triggered
        assert len(errored) >= 5, f"Expected at least 5 errors, got {len(errored)}"

        # Verify error entries have empty cases
        for r in errored:
            assert r["cases"] == []

        # Verify at least some have circuit_breaker_triggered
        cb_errors = [r for r in results if r.get("error") == "circuit_breaker_triggered"]
        assert len(cb_errors) >= 1, "Circuit breaker should mark some items"

        # Verify results are sorted by idx
        idxs = [r["idx"] for r in results]
        assert idxs == sorted(idxs)

    @patch("server.recommend.retrieve_similar_cases")
    def test_partial_failures_no_circuit_break(self, mock_retrieve):
        """When <50% fail, no circuit breaker — all are processed."""
        from server.recommend import retrieve_similar_cases_batch

        call_count = {"n": 0}

        def _side_effect(pain_point, solution, area=None, top_k=3):
            call_count["n"] += 1
            # Only 1 out of 5 fails (20% < 50%)
            if call_count["n"] == 3:
                raise ValueError("embed failed")
            return [{"similar_pain_point": "ok", "category": "Q&A",
                     "effort": "Medium", "timeline": "Short Term", "impact": "N/A",
                     "solution_area": None, "similarity_score": 0.65}]

        mock_retrieve.side_effect = _side_effect

        items = self._make_items(5)
        results = retrieve_similar_cases_batch(items, top_k=3)

        assert len(results) == 5

        # Exactly 1 should have error
        errored = [r for r in results if "error" in r]
        successful = [r for r in results if "error" not in r]
        assert len(errored) == 1
        assert len(successful) == 4

    @patch("server.recommend.retrieve_similar_cases")
    def test_all_fail_circuit_breaker(self, mock_retrieve):
        """When all tasks fail immediately, circuit breaker triggers early."""
        from server.recommend import retrieve_similar_cases_batch

        mock_retrieve.side_effect = ConnectionError("HANA unavailable")

        items = self._make_items(6)
        results = retrieve_similar_cases_batch(items, top_k=3)

        # All should have results (with errors)
        assert len(results) == 6
        errored = [r for r in results if "error" in r]
        assert len(errored) == 6

        # All should have empty cases
        for r in results:
            assert r["cases"] == []

        # Verify circuit_breaker_triggered appears in some errors
        cb_errors = [r for r in results if r.get("error") == "circuit_breaker_triggered"]
        assert len(cb_errors) >= 1, "Circuit breaker should mark remaining items"

    @patch("server.recommend.retrieve_similar_cases")
    def test_empty_items_list(self, mock_retrieve):
        """Empty items list should return empty results without error."""
        from server.recommend import retrieve_similar_cases_batch

        results = retrieve_similar_cases_batch([], top_k=3)
        assert results == []
        mock_retrieve.assert_not_called()
