from dataclasses import dataclass
from datetime import datetime

@dataclass
class Event:
    title: str
    short_summary: str
    long_summary: str
    categories: list[str] # from settings (moved out of the per-event form)
    image: str # Google drive url?
    start_date: datetime
    end_date: datetime

    venue: str
    location: str
    open_to_public: bool
    event_cost: float
    website_url: str
    organizer: str
    organizer_first_name: str
    organizer_last_name: str
    organizer_phone_number: str
    organizer_email_address: str
