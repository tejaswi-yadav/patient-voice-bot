"""Post-call bug and quality analysis using OpenAI."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from openai import OpenAI

from src.scenarios import Scenario
from src.transcription import CallTranscript

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT = """You are a QA engineer evaluating an AI medical office receptionist agent.
Analyze the following phone call transcript between a simulated patient (PATIENT) and the AI agent (AGENT).

Scenario being tested: {scenario_name}
Scenario goals: {goals}

Identify bugs, quality issues, or concerning behaviors in the AGENT's responses. Focus on:
- Incorrect information (wrong hours, dates, policies)
- Failure to verify patient identity when needed
- Scheduling on closed days (weekends/holidays) without pushback
- Ignoring urgent symptoms or failing to triage appropriately
- Poor handling of unclear requests
- Ignoring interruptions or not confirming details
- Hallucinated capabilities or made-up appointment times
- Rude, confusing, or overly verbose responses

For each issue found, provide:
- title: short bug title
- severity: High, Medium, or Low
- timestamp: approximate timestamp from transcript (or "unknown")
- details: what happened and why it's a problem
- expected: what the agent should have done

If no significant issues are found, return an empty issues list but note any minor observations.

Respond in JSON format:
{{
  "summary": "1-2 sentence overall assessment",
  "issues": [
    {{
      "title": "...",
      "severity": "High|Medium|Low",
      "timestamp": "...",
      "details": "...",
      "expected": "..."
    }}
  ],
  "conversation_quality": {{
    "natural_flow": 1-5,
    "task_completion": 1-5,
    "accuracy": 1-5
  }}
}}"""


def analyze_call(
    transcript: CallTranscript,
    scenario: Scenario,
    api_key: str,
    model: str = "gpt-4o-mini",
) -> dict[str, Any]:
    client = OpenAI(api_key=api_key)

    prompt = ANALYSIS_PROMPT.format(
        scenario_name=scenario.name,
        goals=", ".join(scenario.goals),
    )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a precise QA analyst. Return valid JSON only.",
            },
            {
                "role": "user",
                "content": f"{prompt}\n\n--- TRANSCRIPT ---\n{transcript.to_text()}",
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    content = response.choices[0].message.content or "{}"
    return json.loads(content)


def save_analysis(
    call_id: str,
    analysis: dict[str, Any],
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"analysis-{call_id}.json"
    path.write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    return path


def append_to_bug_report(
    call_id: str,
    scenario: Scenario,
    analysis: dict[str, Any],
    bug_report_path: Path,
) -> None:
    """Append findings to the master bug report markdown file."""
    issues = analysis.get("issues", [])
    if not issues:
        return

    lines: list[str] = []
    if not bug_report_path.exists():
        lines.extend(
            [
                "# Bug Report — Athena AI Agent QA",
                "",
                "Issues found during automated patient voice bot testing.",
                "",
            ]
        )

    for issue in issues:
        severity = issue.get("severity", "Medium")
        timestamp = issue.get("timestamp", "unknown")
        lines.extend(
            [
                f"## {issue.get('title', 'Untitled Issue')}",
                "",
                f"**Severity:** {severity}  ",
                f"**Call:** transcript-{call_id}.txt at {timestamp}  ",
                f"**Scenario:** {scenario.name}",
                "",
                f"**Details:** {issue.get('details', '')}",
                "",
                f"**Expected:** {issue.get('expected', '')}",
                "",
                "---",
                "",
            ]
        )

    with bug_report_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
