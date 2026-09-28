"""MOCK of Person 1's n8n webhooks. Used by default until AI_PROCESS_URL / AI_NARRATE_URL
point at the real ones. Responses are fixed and clearly labelled ("mock": true), and
recommendations generated from them are returned with "is_mock": true.

Contracts are identical to the real ones (master doc Sections 5 and 5.1).
"""
from fastapi import APIRouter

from ..schemas import MockNarrateIn, MockProcessIn

router = APIRouter(prefix="/mock/ai", tags=["mock-ai"])


@router.post("/process-complaint")
async def mock_process_complaint(body: MockProcessIn):
    text = (body.text or "").strip()
    if not text:
        text = "mock transcript" if body.input_type == "voice" else "mock text"
    return {"text": text, "severity": 3, "urgency": "medium", "mock": True}


@router.post("/narrate")
async def mock_narrate(body: MockNarrateIn):
    n = len(body.sample_descriptions or [])
    return {
        "summary": f"[MOCK AI] Summary of {n} recent active complaint(s). Replace with Person 1's n8n narration.",
        "recommended_intervention": "[MOCK AI] Placeholder recommendation - prioritise this location based on the "
                                    "evidence scores.",
        "mock": True,
    }
