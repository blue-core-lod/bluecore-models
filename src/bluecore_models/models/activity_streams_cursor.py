from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bluecore_models.models.base import Base


class ActivityStreamsCursor(Base):
    """Tracks the last processed activity stream date for a named feed consumer."""

    __tablename__ = "activity_streams_cursor"

    cursor_name: Mapped[str] = mapped_column(String(25), primary_key=True)
    cursor: Mapped[str] = mapped_column(Text)

    def __repr__(self):
        return f"<ActivityStreamsCursor {self.cursor_name} at {self.cursor}>"
