"""Domain constants shared across the app. Change them here, nowhere else."""

CATEGORIES = ("water", "roads")

# Complaint lifecycle. ACTIVE statuses are the only ones that count toward
# hotspots, scores, rollup counts and the AI complaint sample.
COMPLAINT_STATUSES = ("open", "in_progress", "resolved", "rejected")
ACTIVE_STATUSES = ("open", "in_progress")

URGENCIES = ("low", "medium", "high")
CHANNELS = ("web", "voice", "messaging")

# Defaults used when Person 1's AI webhook fails in Flow A.
FALLBACK_SEVERITY = 3
FALLBACK_URGENCY = "medium"

# Location resolution confidence
CONFIDENCE_VILLAGE = 1.0
CONFIDENCE_DISTRICT = 0.5

PILOT_STATE = "Rajasthan"
