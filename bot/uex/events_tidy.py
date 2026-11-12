"""Keeping #events tidy (bot/cogs/event_cards.py does the posting and deleting).

- **The next op at the bottom.** Upcoming cards read furthest-out at the top down to the next op
  at the bottom, where the channel opens. Discord can't move a message, so when a new card lands
  below a sooner one, the sooner ones are posted again under it (sign-ups are kept, and a repost
  doesn't ping Org Events again).
- **Cleared once an op is over.** TIDY_AFTER after a card is ended or cancelled, the card and the
  bot's reminder, start and end replies for it are deleted. Attendance and Ops credit are kept in
  the database, not in the messages.
- **Other posts** (members', staff's, other bots') go once every op that was upcoming when they
  were posted has been cleared - or, posted when none was, once the next op posted after them
  has. Pinned messages are never touched.
"""
from __future__ import annotations

from datetime import datetime, timezone

TIDY_AFTER = 6 * 3600     # seconds after an op is over that its messages are cleared
CLOSE_AFTER = 12 * 3600   # as bot/cogs/event_cards.py: a card nobody ended closes this long after its start
UPCOMING = ("open", "started")
OVER = ("ended", "cancelled")


def created(card: dict) -> int:
    """When a card was made (its created_at, UTC text), in Unix seconds."""
    return int(datetime.strptime(card["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


def over_at(card: dict) -> int | None:
    """When a card's op was over; for one closed before this was kept, when it would have closed."""
    if card["state"] not in OVER:
        return None
    return card["over_at"] if card.get("over_at") else card["starts_at"] + CLOSE_AFTER


def due_to_clear(card: dict, now: int) -> bool:
    over = over_at(card)
    return over is not None and not card.get("cleaned_at") and now >= over + TIDY_AFTER


def to_repost(cards: list[tuple[int, int, int]]) -> list[int]:
    """Upcoming cards as (card_id, starts_at, message_id): the ones to post again, in the order to
    post them, so the channel reads furthest-out at the top and the next op at the bottom. The
    first card out of place and every sooner one after it - nothing when they're already in order."""
    wanted = sorted(cards, key=lambda c: (-c[1], c[0]))  # top to bottom
    for index in range(1, len(wanted)):
        if wanted[index][2] < wanted[index - 1][2]:
            return [card_id for card_id, _, _ in wanted[index:]]
    return []


def other_post_due(posted_at: int, cards: list[dict]) -> bool:
    """Whether a post that isn't one of the bot's can go: every op upcoming when it was posted has
    been cleared away - or, if none was, the next one posted after it has. Never before an op
    it could be about has passed."""
    live = [c for c in cards if created(c) <= posted_at and ((o := over_at(c)) is None or o > posted_at)]
    if not live:
        later = [c for c in cards if created(c) > posted_at]
        live = [min(later, key=created)] if later else []
    return bool(live) and all(c.get("cleaned_at") for c in live)
