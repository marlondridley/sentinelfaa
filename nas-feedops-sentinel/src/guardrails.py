"""
Programmatic Alignment Guardrails — Layer 3.

Implements the article's "Programmatic Alignment Guardrails" primitive:
independent application firewall decoupled from the LLM's system prompt.

Two interception points, as described in the article:
  InputGuardrail  — sanitizes raw operator intent before it reaches the model
  OutputGuardrail — verifies structured proposals before they reach the gate

The LLM is treated as an untrusted reasoning component. Guardrails do NOT
call the model; they apply deterministic pattern matching and structural
validation so that alignment logic cannot be bypassed via prompt manipulation.

NAS-specific scope: queries must be about operational status, feed health,
alerts, RVR conditions, or recommended actions for PCT TRACON. Queries
attempting to extract credentials, execute system commands, or access
data outside the aviation operational domain are blocked.
"""
from __future__ import annotations

import re
from typing import Any

from src.models import GuardrailResult

# ── Injection patterns (Layer 3 input interception) ───────────────────────────
# Patterns that indicate an attempt to override system instructions or
# escape the agent's operational scope.

_INJECTION_PATTERNS: list[tuple[str, float]] = [
    # Classic instruction override attempts
    (r"ignore\s+(\w+\s+)*(previous|above|prior|all)\s+instructions", 1.0),
    (r"disregard\s+(your|all)\s+(instructions|rules|constraints)", 1.0),
    (r"forget\s+(your|all)\s+(instructions|training|rules)", 1.0),
    (r"you\s+are\s+now\s+", 0.9),
    (r"act\s+as\s+if\s+you\s+(are|were|have)", 0.9),
    (r"pretend\s+(you|that)\s+", 0.8),
    # System prompt extraction
    (r"(print|repeat|show|reveal|output)\s+(your\s+)?(system\s+prompt|instructions)", 0.9),
    (r"what\s+(are|were)\s+your\s+(instructions|system\s+prompt)", 0.8),
    # Template injection markers
    (r"\{\{.*\}\}", 0.7),
    (r"<\|system\|>", 1.0),
    (r"\[INST\]", 0.9),
    (r"###\s*system", 0.8),
    # Jailbreak signals
    (r"jailbreak", 0.9),
    (r"DAN\s+mode", 1.0),
    (r"do\s+anything\s+now", 0.9),
]

# Out-of-scope patterns for NAS operational context
_OUT_OF_SCOPE_PATTERNS: list[tuple[str, str]] = [
    (r"\b(password|passwd|credentials?|api[_\s]keys?|secret[_\s]keys?|access[_\s]token)\b", "credential_access"),
    (r"\b(ssh|shell|bash|powershell|cmd\.exe)\b", "system_command"),
    (r"\b(drop\s+table|delete\s+from|truncate\s+table)\b", "database_mutation"),
    (r"\b(rm\s+-rf|format\s+c:|del\s+/f)\b", "destructive_command"),
    (r"\b(bitcoin|cryptocurrency|wire\s+transfer|bank\s+account)\b", "financial_exfiltration"),
    (r"\b(social\s+security|ssn|passport\s+number|credit\s+card)\b", "pii_exfiltration"),
]

# Minimum signal that the query is aviation/NAS-operational in scope
_NAS_SCOPE_SIGNALS: list[str] = [
    "status", "alert", "airport", "tracon", "flight", "runway", "rvr",
    "feed", "ismc", "smes", "swim", "kdca", "kiad", "kbwi", "kadw",
    "pct", "latency", "operational", "manager", "duty", "tower",
    "approach", "departure", "weather", "visibility", "happening",
    "concern", "issue", "problem", "health", "current", "what",
]

_MIN_SCOPE_SIGNAL_THRESHOLD = 1   # at least 1 NAS signal required


# ── Input Guardrail ───────────────────────────────────────────────────────────

class InputGuardrail:
    """
    Sanitizes raw operator input before it reaches the LLM.

    Checks (in order):
      1. Prompt injection detection via pattern scoring
      2. Out-of-scope command detection
      3. NAS operational scope validation
    Returns a GuardrailResult; caller blocks the pipeline if not is_compliant.
    """

    def verify(self, raw_input: str) -> GuardrailResult:
        if not raw_input or not raw_input.strip():
            return GuardrailResult(
                is_compliant=False,
                violation_type="EMPTY_INPUT",
                detail="Input is empty.",
            )

        lower = raw_input.lower()

        # 1. Injection scoring
        injection_score = 0.0
        for pattern, weight in _INJECTION_PATTERNS:
            if re.search(pattern, lower, re.IGNORECASE):
                injection_score = max(injection_score, weight)

        if injection_score >= 0.7:
            return GuardrailResult(
                is_compliant=False,
                violation_type="INJECTION",
                injection_score=injection_score,
                detail=f"Prompt injection detected (score={injection_score:.2f}).",
            )

        # 2. Out-of-scope command detection
        for pattern, scope_type in _OUT_OF_SCOPE_PATTERNS:
            if re.search(pattern, lower, re.IGNORECASE):
                return GuardrailResult(
                    is_compliant=False,
                    violation_type="SCOPE_VIOLATION",
                    injection_score=injection_score,
                    detail=f"Query contains out-of-scope content: {scope_type}.",
                )

        # 3. NAS scope validation
        signal_count = sum(1 for sig in _NAS_SCOPE_SIGNALS if sig in lower)
        if signal_count < _MIN_SCOPE_SIGNAL_THRESHOLD:
            return GuardrailResult(
                is_compliant=False,
                violation_type="OUT_OF_SCOPE",
                injection_score=injection_score,
                detail=(
                    "Query does not appear to be an NAS operational query. "
                    "This system answers questions about PCT TRACON feed health, "
                    "alerts, RVR conditions, and operational status only."
                ),
            )

        # Clean — return sanitized version (strip leading/trailing whitespace,
        # collapse internal whitespace)
        sanitized = re.sub(r"\s+", " ", raw_input.strip())
        return GuardrailResult(
            is_compliant=True,
            injection_score=injection_score,
            sanitized_input=sanitized,
            detail="Input passed all guardrail checks.",
        )


# ── Output Guardrail ──────────────────────────────────────────────────────────

_VALID_PATTERNS = {
    "CASCADE_FAILURE", "ISOLATED_FAULT", "WEATHER_CORRELATED",
    "DATA_ANOMALY", "TRANSIENT", "UNKNOWN",
}

_VALID_CRITIC_PATTERNS = _VALID_PATTERNS


class OutputGuardrail:
    """
    Verifies structured LLM output proposals before they reach the execution gate.

    The LLM output (CriticResult or PlannerResult dict) is treated as an
    unverified proposal. This guardrail confirms:
      1. Required fields are present and non-null
      2. Confidence score is within valid bounds [0.0, 1.0]
      3. Pattern classification is a valid enum value
      4. Reasoning is substantive (not empty, not suspiciously short)
      5. No schema injection artifacts in string fields
    """
    @staticmethod
    def guard(
        raw_narrative: RawLLMNarrative,
        context: dict[str, Any],
        signals: dict[str, Any],
        *,
        llm_used: bool,
    ) -> WatchOfficerBrief:
        # Overwrite all critical state with control plane facts
        posture = _derive_posture(signals, context)
        trust = _derive_trust(signals, context)
        affected = _affected_airports(signals, context)
        evidence = _build_deterministic_evidence(context, signals)

        limitations = list(raw_narrative.limitations)
        limitations.extend([
            "Deterministic control plane establishes posture.",
            "Deterministic control plane establishes trust assessment.",
            "Deterministic control plane establishes affected airports.",
            "Operational evidence is generated from system state, not LLM claims.",
            "Human approval is required before any staged action can proceed.",
        ])

        return WatchOfficerBrief(
            posture=posture,
            trust_assessment=trust,
            headline=raw_narrative.headline,
            what_changed=raw_narrative.what_changed,
            why_it_matters=raw_narrative.why_it_matters,
            recommended_human_review=raw_narrative.recommended_human_review,
            evidence=evidence,
            pending_actions_count=context.get("pending_actions_count", 0),
            critical_alert_count=context.get("critical_alert_count", 0),
            affected_airports=affected,
            confidence=raw_narrative.confidence,
            llm_used=llm_used,
            limitations=limitations,
        )
        
    def verify_critic_output(self, output: dict[str, Any]) -> GuardrailResult:
        return self._verify_output(
            output=output,
            required_fields=["confidence", "reasoning", "pattern", "escalate", "citations"],
            valid_patterns=_VALID_CRITIC_PATTERNS,
            context="critic",
        )

    def verify_planner_output(self, output: dict[str, Any]) -> GuardrailResult:
        return self._verify_output(
            output=output,
            required_fields=["answer", "confidence", "recommended_actions", "sub_queries"],
            valid_patterns=None,  # planner has no pattern field
            context="planner",
        )

    def _verify_output(
        self,
        output: dict[str, Any],
        required_fields: list[str],
        valid_patterns: set[str] | None,
        context: str,
    ) -> GuardrailResult:
        # 1. Required field presence
        missing = [f for f in required_fields if f not in output or output[f] is None]
        if missing:
            return GuardrailResult(
                is_compliant=False,
                violation_type="STRUCTURAL",
                detail=f"{context} output missing required fields: {missing}.",
            )

        # 2. Confidence bounds
        conf = output.get("confidence")
        if conf is not None:
            try:
                conf_f = float(conf)
                if not (0.0 <= conf_f <= 1.0):
                    return GuardrailResult(
                        is_compliant=False,
                        violation_type="BOUNDS",
                        detail=f"Confidence {conf_f} is outside [0.0, 1.0].",
                    )
            except (TypeError, ValueError):
                return GuardrailResult(
                    is_compliant=False,
                    violation_type="STRUCTURAL",
                    detail=f"Confidence field is not numeric: {conf!r}.",
                )

        # 3. Pattern validation (critic only)
        if valid_patterns is not None:
            pattern = output.get("pattern", "")
            if pattern not in valid_patterns:
                return GuardrailResult(
                    is_compliant=False,
                    violation_type="STRUCTURAL",
                    detail=f"Pattern '{pattern}' is not a valid enum value.",
                )

        # 4. Reasoning / answer substantiveness
        text_field = "reasoning" if context == "critic" else "answer"
        text_val = str(output.get(text_field, ""))
        if len(text_val.strip()) < 10:
            return GuardrailResult(
                is_compliant=False,
                violation_type="STRUCTURAL",
                detail=f"{text_field} is too short to be substantive (len={len(text_val)}).",
            )

        # 5. Injection artifacts in string fields (model trying to escape)
        for field in required_fields:
            val = str(output.get(field, ""))
            for pattern, _ in _INJECTION_PATTERNS[:4]:  # check top-severity patterns only
                if re.search(pattern, val, re.IGNORECASE):
                    return GuardrailResult(
                        is_compliant=False,
                        violation_type="INJECTION",
                        detail=f"Injection artifact detected in output field '{field}'.",
                    )

        return GuardrailResult(
            is_compliant=True,
            detail=f"{context} output passed all structural guardrail checks.",
        )