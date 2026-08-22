"""The expansion budget that keeps `expanded_expression` from growing multiplicatively.

Migrated from the integration repository, which held the only tests for `ExpansionBudget` and
`EXPANSION_MAX_CHARS` while Core had none -- Core could have retuned or removed the cap and found
out from a downstream test.

The class is self-contained: its SQL and schema fixtures are synthetic and travel with it.
"""

from __future__ import annotations

from scope_lineage import parse_scope_lineage


class TestExpansionBudget:
    """`expanded_expression` must not grow multiplicatively with reference count and depth.

    Each upstream field's expanded text is inlined at every reference, so an expression used
    N times is copied N times and every extra scope layer multiplies again. On a moderately
    sized statement that produced a string and a lineage.json three orders of magnitude
    larger (PERF-001).
    """

    # a large CASE over window fields, then another window layer over that CASE, then a final
    # CASE referencing all of them many times — the shape that multiplies
    SQL = """
    WITH base AS (
      SELECT id, amt, grp, dt FROM ods.src
    ), w1 AS (
      SELECT id, amt, grp, dt,
             ROW_NUMBER() OVER (PARTITION BY grp ORDER BY amt) AS rn,
             RAND(7) AS rnd
      FROM base
    ), c1 AS (
      SELECT id, grp, rn, rnd, amt, dt,
             CASE WHEN rn = 1 AND rnd < 0.1 THEN 'a' WHEN rn = 2 AND rnd < 0.2 THEN 'b'
                  WHEN rn = 3 AND rnd < 0.3 THEN 'c' WHEN rn = 4 AND rnd < 0.4 THEN 'd'
                  WHEN rn = 5 AND rnd < 0.5 THEN 'e' WHEN rn = 6 AND rnd < 0.6 THEN 'f'
                  WHEN rn = 7 AND rnd < 0.7 THEN 'g' ELSE 'z' END AS bucket
      FROM w1
    ), w2 AS (
      SELECT id, grp, amt, dt, bucket,
             ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY amt) AS rn2,
             RAND(9) AS rnd2
      FROM c1
    ), c2 AS (
      SELECT id, grp, amt, dt, bucket, rn2, rnd2,
             CASE WHEN bucket = 'a' AND rn2 = 1 AND rnd2 < 0.1 THEN CONCAT(bucket, '1')
                  WHEN bucket = 'b' AND rn2 = 2 AND rnd2 < 0.2 THEN CONCAT(bucket, '2')
                  WHEN bucket = 'c' AND rn2 = 3 AND rnd2 < 0.3 THEN CONCAT(bucket, '3')
                  WHEN bucket = 'd' AND rn2 = 4 AND rnd2 < 0.4 THEN CONCAT(bucket, '4')
                  WHEN bucket = 'e' AND rn2 = 5 AND rnd2 < 0.5 THEN CONCAT(bucket, '5')
                  WHEN bucket = 'f' AND rn2 = 6 AND rnd2 < 0.6 THEN CONCAT(bucket, '6')
                  WHEN bucket = 'g' AND rn2 = 7 AND rnd2 < 0.7 THEN CONCAT(bucket, '7')
                  ELSE bucket END AS bucket2
      FROM w2
    ), w3 AS (
      SELECT id, grp, amt, dt, bucket2,
             ROW_NUMBER() OVER (PARTITION BY bucket2 ORDER BY amt) AS rn3,
             RAND(11) AS rnd3
      FROM c2
    )
    INSERT INTO dwd.t
    SELECT id,
           CASE WHEN bucket2 = 'a1' AND rn3 = 1 AND rnd3 < 0.1 THEN CONCAT(bucket2, 'A')
                WHEN bucket2 = 'b2' AND rn3 = 2 AND rnd3 < 0.2 THEN CONCAT(bucket2, 'B')
                WHEN bucket2 = 'c3' AND rn3 = 3 AND rnd3 < 0.3 THEN CONCAT(bucket2, 'C')
                WHEN bucket2 = 'd4' AND rn3 = 4 AND rnd3 < 0.4 THEN CONCAT(bucket2, 'D')
                WHEN bucket2 = 'e5' AND rn3 = 5 AND rnd3 < 0.5 THEN CONCAT(bucket2, 'E')
                WHEN bucket2 = 'f6' AND rn3 = 6 AND rnd3 < 0.6 THEN CONCAT(bucket2, 'F')
                WHEN bucket2 = 'g7' AND rn3 = 7 AND rnd3 < 0.7 THEN CONCAT(bucket2, 'G')
                ELSE bucket2 END AS final_bucket
    FROM w3
    """
    SCHEMA = {"ods.src": ["id", "amt", "grp", "dt"]}

    def _root_outputs(self):
        result = parse_scope_lineage(self.SQL, "expansion_budget", schema=self.SCHEMA)
        return {output.name: output for output in result.scopes["ROOT"].outputs}

    def test_no_expression_exceeds_the_budget(self):
        from scope_lineage.scope.expansion_budget import EXPANSION_MAX_CHARS

        result = parse_scope_lineage(self.SQL, "expansion_budget", schema=self.SCHEMA)
        oversized = [
            (scope_id, output.name, len(output.expanded_expression or ""))
            for scope_id, scope in result.scopes.items()
            for output in scope.outputs
            if len(output.expanded_expression or "") > EXPANSION_MAX_CHARS
        ]
        assert not oversized, f"超出展开上限: {oversized}"

    def _largest_expansion(self):
        result = parse_scope_lineage(self.SQL, "expansion_budget", schema=self.SCHEMA)
        return max(
            len(output.expanded_expression or "")
            for scope in result.scopes.values()
            for output in scope.outputs
        )

    def test_the_budget_is_what_keeps_this_case_in_check(self, monkeypatch):
        """Differential, not a tuned threshold. Raising the limits must make THIS sql blow
        past the cap — proving the fixture still reproduces the defect — and the default
        limits must then bring it back under. A fixed ratio would only pin whatever number
        today's code happens to produce."""
        from scope_lineage.scope import expansion_budget

        cap = expansion_budget.EXPANSION_MAX_CHARS
        monkeypatch.setattr(expansion_budget, "EXPANSION_MAX_CHARS", 10 ** 12)
        monkeypatch.setattr(expansion_budget, "EXPANSION_MAX_SUBSTITUTIONS", 10 ** 9)
        unbounded = self._largest_expansion()
        assert unbounded > cap, (
            f"用例已不再复现膨胀(无上限时仅 {unbounded:,} 字符,上限 {cap:,}),firepower 失效"
        )

        monkeypatch.undo()
        bounded = self._largest_expansion()
        assert bounded <= cap
        assert bounded < unbounded / 2, (
            f"预算几乎没起作用: 无上限 {unbounded:,} → 有上限 {bounded:,}"
        )

    def test_a_bounded_expansion_says_so_and_names_what_it_skipped(self):
        """Silent truncation is the failure mode this must not have: a consumer reading a
        shortened expression with no marker would take it for the whole logic."""
        from scope_lineage.scope.expansion_budget import ExpansionBudget

        budget = ExpansionBudget(max_chars=40)
        out = budget.substitute(
            "CASE WHEN x THEN a.big END", "y" * 100,
            lambda expr, repl: expr.replace("a.big", repl),
            ref="a.big", scope_id="cte:up", field="big",
        )
        assert out == "CASE WHEN x THEN a.big END", "超限时必须保留原引用,而不是截断文本"
        assert budget.status == "bounded"
        assert budget.stop_reason == "max_chars"
        assert budget.skipped_refs == [
            {"ref": "a.big", "reason": "max_chars", "scope_id": "cte:up", "field": "big"}
        ]

    def test_bounding_never_drops_a_source_fact(self):
        """The whole point of declining rather than truncating: physical sources, generated
        sources and transform stay complete even where the text does not."""
        outputs = self._root_outputs()
        final = outputs["final_bucket"]
        resolution = final.expression_resolution
        tables = {item["table"] for item in resolution["physical_source_fields"]}
        assert tables == {"ods.src"}, f"物理来源丢失: {resolution['physical_source_fields']}"
        assert final.transform
        assert resolution["status"] in {"resolved", "partially_resolved"}
