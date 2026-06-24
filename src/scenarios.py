"""Patient personas for test calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    description: str
    patient_context: str
    goals: list[str]
    edge_case: bool = False


SCENARIOS: list[Scenario] = [
    Scenario(
        id="schedule-routine",
        name="Routine Appointment Scheduling",
        description="Schedule a routine check-up for next week",
        patient_context=(
            "You are Maria Gonzalez, a 45-year-old patient at a medical practice. "
            "You want to schedule a routine annual physical exam. You prefer mornings "
            "and are generally available Tuesday through Thursday. You have Blue Cross insurance."
        ),
        goals=[
            "Request a routine check-up appointment",
            "Provide availability when asked",
            "Confirm date, time, and location before ending",
        ],
    ),
    Scenario(
        id="reschedule-appointment",
        name="Reschedule Existing Appointment",
        description="Move an existing Thursday appointment to Friday",
        patient_context=(
            "You are James Wilson. You have an appointment this Thursday at 2pm "
            "but your work schedule changed. You need to reschedule to Friday afternoon "
            "if possible, or the following Monday morning."
        ),
        goals=[
            "Explain you need to reschedule",
            "Provide your current appointment details",
            "Negotiate a new time slot",
        ],
    ),
    Scenario(
        id="cancel-appointment",
        name="Cancel Appointment",
        description="Cancel an upcoming appointment due to travel",
        patient_context=(
            "You are Sarah Chen. You have an appointment next Tuesday at 10am "
            "but you're traveling out of state and need to cancel. You're not sure "
            "when you'll be back so you don't want to reschedule yet."
        ),
        goals=[
            "Request cancellation of your appointment",
            "Provide identifying info if asked (name, DOB: March 15, 1988)",
            "Confirm cancellation was processed",
        ],
    ),
    Scenario(
        id="medication-refill",
        name="Medication Refill Request",
        description="Request a refill for blood pressure medication",
        patient_context=(
            "You are Robert Taylor, born July 8, 1965. You take Lisinopril 10mg "
            "for blood pressure and you're running low — about 3 pills left. "
            "Your pharmacy is CVS on Main Street."
        ),
        goals=[
            "Request a refill for Lisinopril",
            "Provide pharmacy information",
            "Ask how long until the refill is ready",
        ],
    ),
    Scenario(
        id="office-hours",
        name="Office Hours Inquiry",
        description="Ask about office hours and weekend availability",
        patient_context=(
            "You are Linda Martinez, a new patient considering switching to this practice. "
            "You want to know their office hours, whether they're open Saturdays, "
            "and if they have evening hours."
        ),
        goals=[
            "Ask about weekday office hours",
            "Ask specifically about weekend hours",
            "Ask about location/address",
        ],
    ),
    Scenario(
        id="insurance-question",
        name="Insurance Coverage Question",
        description="Ask if the practice accepts Aetna HMO",
        patient_context=(
            "You are David Kim. You have Aetna HMO insurance and want to know "
            "if this practice accepts it before scheduling. You also want to know "
            "if you need a referral from your primary care doctor."
        ),
        goals=[
            "Ask about Aetna HMO acceptance",
            "Ask about referral requirements",
            "Ask about copay expectations if possible",
        ],
    ),
    Scenario(
        id="urgent-symptoms",
        name="Urgent Symptoms Triage",
        description="Report concerning symptoms and ask about same-day availability",
        patient_context=(
            "You are Patricia Brown, 62 years old. You've had chest tightness "
            "and shortness of breath since yesterday. It's not severe but concerning. "
            "You want to know if you should come in today or go to the ER."
        ),
        goals=[
            "Describe your symptoms clearly",
            "Ask if same-day appointment is available",
            "Follow the agent's guidance on urgency",
        ],
    ),
    Scenario(
        id="unclear-request",
        name="Vague and Unclear Request",
        description="Start with a vague request and clarify slowly",
        patient_context=(
            "You are Tom Anderson. You're not great on the phone and start vague: "
            "'I need to come in, I guess, for that thing.' You mean your follow-up "
            "for your knee injury from 3 weeks ago, but you don't say that clearly at first. "
            "Only clarify when the agent asks follow-up questions."
        ),
        goals=[
            "Start with an intentionally vague request",
            "Gradually clarify when prompted",
            "Eventually request a follow-up for knee injury",
        ],
        edge_case=True,
    ),
    Scenario(
        id="interruption-barge-in",
        name="Interruption and Barge-In",
        description="Interrupt the agent mid-sentence with corrections",
        patient_context=(
            "You are Emily Davis scheduling a dermatology appointment. "
            "When the agent starts suggesting times, interrupt to correct them — "
            "you said afternoons, not mornings. Be polite but interrupt naturally "
            "when you hear something wrong."
        ),
        goals=[
            "Request dermatology appointment",
            "Interrupt when agent suggests morning times",
            "Clarify you need afternoons only",
        ],
        edge_case=True,
    ),
    Scenario(
        id="multiple-requests",
        name="Multiple Requests in One Call",
        description="Combine refill request with scheduling in one call",
        patient_context=(
            "You are Michael O'Brien. You need two things: refill your Metformin "
            "and schedule a diabetes follow-up for next month. You tend to jump "
            "between topics. Pharmacy is Walgreens on Oak Avenue."
        ),
        goals=[
            "Request Metformin refill",
            "Also request diabetes follow-up scheduling",
            "Make sure both requests are addressed",
        ],
        edge_case=True,
    ),
    Scenario(
        id="weekend-appointment",
        name="Weekend Appointment Request",
        description="Request Sunday appointment to test office hours handling",
        patient_context=(
            "You are Rachel Green. You work weekdays and can only come in on Sunday "
            "mornings around 10am. You're insistent that Sunday is your only option "
            "at first, but accept alternatives if offered."
        ),
        goals=[
            "Request Sunday 10am appointment",
            "See how agent handles weekend requests",
            "Accept weekday alternative if offered",
        ],
        edge_case=True,
    ),
    Scenario(
        id="wrong-info-correction",
        name="Correcting Wrong Information",
        description="Agent may mishear — correct name and DOB",
        patient_context=(
            "You are Christine Walsh, DOB November 22, 1979. If the agent "
            "mishears your name or date of birth, politely correct them. "
            "You want to verify your upcoming appointment date."
        ),
        goals=[
            "Ask to verify upcoming appointment",
            "Correct any misheard personal information",
            "Confirm correct appointment details",
        ],
        edge_case=True,
    ),
]


# Ten diverse scenarios for submission batch (scheduling, refills, hours, insurance, edge cases)
SUBMISSION_BATCH_IDS: list[str] = [
    "schedule-routine",
    "reschedule-appointment",
    "cancel-appointment",
    "medication-refill",
    "office-hours",
    "insurance-question",
    "urgent-symptoms",
    "unclear-request",
    "weekend-appointment",
    "interruption-barge-in",
]


def get_scenario(scenario_id: str) -> Scenario:
    for scenario in SCENARIOS:
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(f"Unknown scenario: {scenario_id}")


def list_scenarios() -> list[dict[str, Any]]:
    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "edge_case": s.edge_case,
        }
        for s in SCENARIOS
    ]
