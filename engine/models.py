"""Scan result type, cancellation exception and the dashboard's status messages."""

from dataclasses import dataclass, field, asdict

NO_RESULTS_MESSAGE = "No relevant trends found for this topic."

LOW_VOLUME_MESSAGE = ("Not enough relevant tech data found for this term. Please search "
                      "for a more specific technology, language, or trend.")

INSUFFICIENT_PLATFORMS_MESSAGE = ("Not enough data to analyze a trend. This topic yielded "
                                  "valid results in fewer than 3 communities.")

CANCELLED_MESSAGE = "Search cancelled."


class ScanCancelled(Exception):
    """Raised inside a cycle when its cancel event is set; becomes status 'cancelled'."""


@dataclass
class ScanResult:
    """
    Outcome of one scan.

    Attributes:
        topic: The validated topic.
        plan_source: "keywords" or "fallback" -- how the plan was produced.
        fetched: Posts that cleared the quality gate across all platforms.
        saved: Rows newly inserted into the database.
        per_platform: Accepted post count per platform.
        volume: Per-platform volume/gate report (BaseCollector.volume_report).
        duration_s: Cycle duration in seconds.
        replaced_previous_topic: True when the scan swapped the stored corpus.
        status: ok | invalid | no_results | busy | cancelled | failed.
        error: Displayable sentence when status is not "ok".
    """
    topic: str
    plan_source: str = ""
    fetched: int = 0
    saved: int = 0
    per_platform: dict = field(default_factory=dict)
    volume: dict = field(default_factory=dict)
    duration_s: float = 0.0
    replaced_previous_topic: bool = False
    status: str = "ok"
    error: str = ""

    def to_dict(self):
        """Returns the result as a plain dict (safe for Streamlit session state)."""
        return asdict(self)
