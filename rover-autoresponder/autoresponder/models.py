"""Data structures shared across the pipeline."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class ParsedMessage:
    gmail_msg_id: str          # Gmail message id — dedupe key
    thread_key: str            # Gmail thread id — stable across a stay's conversation
    owner_name: Optional[str]
    pet_name: Optional[str]
    stay_start: Optional[str]  # MM/DD/YYYY as it appears in the email
    stay_end: Optional[str]
    message_text: Optional[str]
    raw_subject: str = ""
    recognized: bool = True    # False → email format didn't match; stored for review
