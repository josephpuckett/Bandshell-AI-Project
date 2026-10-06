from dataclasses import dataclass, field


# Global defaults that apply to every event. These are supposed to change
# rarely and are intended to be hard-coded into the scripts the agent will
# (later) generate, so they live here instead of in the per-event form.
@dataclass
class Settings:
    venue: str = "Recreation Park Bandshell"
    location: str = "701-939 Federation Drive, Long Beach, California 90804"
    open_to_public: bool = True
    event_cost: float = 0.0
    website_url: str = "https://www.liveattheshell.org/"
    organizer: str = "Live at The Shell"
    organizer_first_name: str = "Steve"
    organizer_last_name: str = "Pruitt"
    organizer_phone_number: str = "562-522-9285"
    organizer_email_address: str = "info@liveattheshell.org"
    categories: list[str] = field(default_factory=lambda: ["Music", "Family", "Outdoor"])
    # Eventbrite support
    timezone: str = "America/Los_Angeles"  # IANA tz used when publishing to Eventbrite
    eventbrite_organization_id: str = ""   # optional; empty = auto-resolve first org from token
    eventbrite_categories: dict[str, str] = field(default_factory=dict)  # our category -> Eventbrite category id
