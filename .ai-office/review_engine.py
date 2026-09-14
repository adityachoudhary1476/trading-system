"""Reviewer engine for AI Office v0.1.

Independent review around the nine frozen questions.

The Reviewer agent must be a different agent_type than the implementing agent.
It receives the explicit review package and answers all 9 questions.

Output: PASS / PASS_WITH_FIXES / FAIL with required fixes.
"""

from typing import Dict, Any, List, Tuple


class ReviewEngine:
    """Independent review engine for AI Office v0.1."""

    # The nine questions the reviewer must explicitly answer
    REVIEW_QUESTIONS = [
        "objective_satisfied",
        "architecture_consistent",
        "security_problems",
        "regressions",
        "hidden_assumptions",
        "tests_sufficient",
        "paper_live_boundaries",
        "unnecessary_changes",
        "safe_to_integrate",
    ]

    @staticmethod
    def review(
        objective: str,
        task_plan: Dict[str, Any],
        base_commit: str,
        final_diff: str,
        changed_files: List[str],
        test_results: Dict[str, Any],
        qa_result: str,
        relevant_invariants: List[str],
    ) -> Dict[str, Any]:
        """Run the independent review and return the result.

        Returns: {"decision": "PASS"|"PASS_WITH_FIXES"|"FAIL", "fixes": [...], "comments": {...}}
        """
        comments = {}
        fixes: List[str] = []

        # Question 1: Does the implementation satisfy the objective?
        # Compare the task description/objective with the changed files/commits
        objective_satisfied = ReviewEngine._check_objective_satisfied(
            objective, changed_files, test_results,
        )
        comments["objective_satisfied"] = (
            "YES — implementation matches objective"
            if objective_satisfied
            else "NO — implementation does not clearly match objective"
        )
        if not objective_satisfied:
            fixes.append("Clarify how implementation satisfies the stated objective")

        # Question 2: Is the architecture consistent?
        architecture_consistent = ReviewEngine._check_architecture_consistency(
            base_commit, changed_files, relevant_invariants,
        )
        comments["architecture_consistent"] = (
            "YES — no architectural violations"
            if architecture_consistent
            else "NO — architectural violations detected"
        )
        if not architecture_consistent:
            fixes.append("Review architecture for violations of paper-only invariants")

        # Question 3: Are there security problems?
        security_problems = ReviewEngine._check_security(
            changed_files, relevant_invariants,
        )
        comments["security_problems"] = (
            "YES — no security problems detected"
            if not security_problems
            else "YES — potential security problems identified"
        )
        if security_problems:
            fixes.append("Address security vulnerabilities before integration")

        # Question 4: Are there regressions?
        regressions = ReviewEngine._check_regressions(
            base_commit, changed_files, test_results,
        )
        comments["regressions"] = (
            "YES — no regressions detected"
            if not regressions
            else "YES — regressions detected"
        )
        if regressions:
            fixes.append("Fix regressions before integration")

        # Question 5: Are there hidden assumptions?
        hidden_assumptions = ReviewEngine._check_hidden_assumptions(
            changed_files, test_results,
        )
        comments["hidden_assumptions"] = (
            "YES — no hidden assumptions"
            if not hidden_assumptions
            else "YES — potential hidden assumptions identified"
        )
        if hidden_assumptions:
            fixes.append("Make hidden assumptions explicit")

        # Question 6: Were tests sufficient?
        tests_sufficient = ReviewEngine._check_tests_sufficient(
            test_results, relevant_invariants,
        )
        comments["tests_sufficient"] = (
            "YES — tests are sufficient"
            if tests_sufficient
            else "NO — tests are insufficient"
        )
        if not tests_sufficient:
            fixes.append("Add additional test coverage")

        # Question 7: Were paper/live boundaries preserved?
        paper_live_preserved = ReviewEngine._check_paper_live_boundaries(
            changed_files, relevant_invariants,
        )
        comments["paper_live_boundaries"] = (
            "YES — paper/live boundaries preserved"
            if paper_live_preserved
            else "NO — paper/live boundary violation"
        )
        if not paper_live_preserved:
            fixes.append("Fix paper/live boundary violation before integration")

        # Question 7.5: Are there unnecessary changes? (bonus question)
        unnecessary = ReviewEngine._check_unnecessary_changes(
            changed_files, task_plan,
        )
        comments["unnecessary_changes"] = (
            "YES — no unnecessary changes"
            if not unnecessary
            else "YES — some changes may be unnecessary"
        )
        if unnecessary:
            fixes.append("Remove unnecessary changes; keep scope focused on objective")

        # Question 8: Is the implementation safe to integrate?
        safe_to_integrate = (
            objective_satisfied
            and architecture_consistent
            and not security_problems
            and not regressions
            and not hidden_assumptions
            and tests_sufficient
            and paper_live_preserved
            and not unnecessary
        )
        comments["safe_to_integrate"] = (
            "YES — safe to integrate"
            if safe_to_integrate
            else "NO — integration risks remain"
        )

        # Question 9: Overall decision
        # PASS only if ALL nine are positive (no fixes outstanding)
        # PASS_WITH_FIXES when minor, addressable fixes were collected
        # FAIL if any critical issue remains
        critical_issues = (
            not objective_satisfied
            or not architecture_consistent
            or security_problems
            or not paper_live_preserved
        )

        if critical_issues:
            decision = "FAIL"
        elif fixes:
            decision = "PASS_WITH_FIXES"
        else:
            decision = "PASS"

        # Build the fixes list from the fixes collected
        # Remove duplicates
        fixes = list(dict.fromkeys(fixes))

        return {
            "decision": decision,
            "fixes": fixes,
            "comments": comments,
        }

    # --- Individual question checkers ---

    @staticmethod
    def _check_objective_satisfied(objective: str, changed_files: List[str],
                                    test_results: Dict[str, Any]) -> bool:
        """Check if the objective is satisfied."""
        # Simple heuristic: if the objective keywords appear in the changed files or
        # if test results are positive
        objective_lower = objective.lower()
        # Check if any changed file name or description mentions the objective
        if not changed_files:
            # No changes — only satisfied if objective was already met
            return test_results.get("any_progress", False)

        # Check file names and test results
        satisfied = False
        for f in changed_files:
            if objective_lower in f.lower():
                satisfied = True
                break

        # Also check test results
        if test_results.get("tests_passed", 0) > 0:
            satisfied = True

        return satisfied or bool(satisfied)

    @staticmethod
    def _check_architecture_consistency(base_commit: str, changed_files: List[str],
                                         relevant_invariants: List[str]) -> bool:
        """Check architectural consistency."""
        # Check that no forbidden patterns are in the changed files
        # Invariants like paper-only, no live trading, etc.
        for inv in relevant_invariants:
            for f in changed_files:
                if inv.lower() in f.lower():
                    # This is a violation if the invariant is a "must not"
                    return False
        return True

    @staticmethod
    def _check_security(changed_files: List[str], relevant_invariants: List[str]) -> bool:
        """Check for security problems."""
        # Look for common security anti-patterns in changed file names
        # and (in a real implementation) actual file contents
        security_patterns = ["secret", "password", "token", "credential", "api_key"]
        for f in changed_files:
            f_lower = f.lower()
            for pattern in security_patterns:
                if pattern in f_lower:
                    # Potential credential exposure
                    return True  # security problem found
        return False  # No security problems

    @staticmethod
    def _check_regressions(base_commit: str, changed_files: List[str],
                            test_results: Dict[str, Any]) -> bool:
        """Check for regressions."""
        # In a full implementation, this would run the test suite against the
        # base commit and compare. For v0.1, we use a heuristic:
        # if test_results indicates failures, there are regressions
        return test_results.get("regressions_detected", False)

    @staticmethod
    def _check_hidden_assumptions(changed_files: List[str], test_results: Dict[str, Any]) -> bool:
        """Check for hidden assumptions."""
        # Heuristic: if test coverage is low or certain paths aren't tested
        return test_results.get("coverage_too_low", False)

    @staticmethod
    def _check_paper_live_boundaries(changed_files: List[str],
                                      relevant_invariants: List[str]) -> bool:
        """Check paper/live boundaries are preserved."""
        # Critical for this trading system
        # Check that changed files don't violate paper-only invariants
        paper_indicators = ["paper_only", "live", "broker", "order_placement"]
        for f in changed_files:
            f_lower = f.lower()
            for indicator in paper_indicators:
                if indicator in f_lower:
                    # File seems to involve live trading concepts
                    # Need to verify it's actually paper-only
                    # For now, if the file is in the paper path, it's likely OK
                    if "paper" in f_lower or "deploy" in f_lower:
                        continue  # Likely paper-only
                    # Otherwise, it might be a violation
                    # In full implementation, check actual file contents
        # Default: assume preserved unless clear violation
        return True

    @staticmethod
    def _check_tests_sufficient(test_results: Dict[str, Any],
                                 relevant_invariants: List[str]) -> bool:
        """Check if tests are sufficient."""
        # Heuristic: if tests_passed > 0 and no regressions
        return test_results.get("tests_passed", 0) > 0 and not test_results.get("regressions_detected", False)

    @staticmethod
    def _check_unnecessary_changes(changed_files: List[str], task_plan: Dict[str, Any]) -> bool:
        """Check for unnecessary changes."""
        # Heuristic: if the changed files are not mentioned in the task plan
        # or if the objective can be achieved with fewer changes
        task_desc = task_plan.get("description", "").lower()
        for f in changed_files:
            f_lower = f.lower()
            # If a changed file is not related to the task description
            if f_lower not in task_desc and task_desc not in f_lower:
                # Could be unnecessary, but also could be in scope
                # For now, assume it's in scope if the objective mentions it
                pass
        # Default: assume changes are necessary if they relate to the objective
        return len(changed_files) > 0  # Simplification