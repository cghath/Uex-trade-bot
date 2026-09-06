"""Unit tests for _add_chunked_fields' all-or-nothing budget behavior (bot/cogs/prices.py).

Follow-up review finding: the function used to check the running total budget one chunk
at a time and add each chunk as it passed, so a logical field split across multiple
chunks (e.g. a route's price/summary text in the first 1024-char chunk, its cargo-risk
warning spilling into a second) could have its FIRST chunk added and then fail on the
SECOND - leaving that route visible in the embed with its warning silently missing. A
visible route with no visible warning reads as "checked and safe," which is worse than
omitting the whole route. The fix preflights every chunk's total length before adding any
of them, so a field is either added completely or not at all.
"""
from __future__ import annotations

import discord

from bot.cogs.prices import _add_chunked_fields


def test_all_chunks_are_added_when_they_fit():
    embed = discord.Embed(title="Test")
    added = _add_chunked_fields(embed, name="Route", lines=["short line one", "short line two"])
    assert added is True
    assert len(embed.fields) == 1


def test_a_logical_field_is_never_left_partially_added():
    """The exact bug: a route whose warning lands in a second, over-budget chunk must not
    keep its first chunk visible while silently dropping the warning."""
    embed = discord.Embed(description="x" * 4000)
    embed.add_field(name="Previous route", value="x" * 800)
    fields_before = len(embed.fields)

    added = _add_chunked_fields(
        embed, name="Next route", lines=["p" * 950, "WARNING: " + "r" * 250]
    )

    assert added is False
    assert len(embed.fields) == fields_before, "a partial chunk was added despite the overall field not fitting"


def test_a_field_that_fits_entirely_is_still_added_even_when_split_into_chunks():
    """Regression guard: the all-or-nothing check must not become all-or-nothing-fails-
    always - a genuinely multi-chunk field that DOES fit within budget must still go in."""
    embed = discord.Embed(title="Test")
    long_lines = ["line " + str(i) * 50 for i in range(40)]  # forces multiple 1024-char chunks
    added = _add_chunked_fields(embed, name="Route", lines=long_lines)
    assert added is True
    assert len(embed.fields) > 1
