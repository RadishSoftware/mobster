"""The two records a run keeps: Turn, what the turn loop and its actions share, and RunState, what the run has done."""

import dataclasses
import threading
from collections import deque
from typing import Any

from ..effect_ledger import EffectLedger
from ..harness_api import InitialState
from ..state import Element, Snapshot
from .actions import Action
from .screen import Provenance, _Verify


# The turn loop's locals that the per-action step shares; slots turn a misspelt name into an AttributeError.
@dataclasses.dataclass(slots=True, kw_only=True)
class Turn:
    request: str
    started: float
    recent: deque
    session: Any  # a longform session; the engine does not import longform
    data_json: bool
    stopped: dict
    acted: bool = False
    acted_since: bool = False
    actions: int = 0
    after: str | None = None
    aliases: dict[str, Element] = dataclasses.field(default_factory=dict)
    calls: int = 0
    chain_typed: list[str] = dataclasses.field(default_factory=list)
    checked_front: bool = False
    chunk: list[Action] = dataclasses.field(default_factory=list)
    counts: dict[str, int] = dataclasses.field(
        default_factory=lambda: {"reused": 0, "verified": 0, "verify_changed": 0})
    covered: int = 0
    failures: dict[tuple, int] = dataclasses.field(default_factory=dict)
    feedback: str = ""
    finished: bool = False
    history: list[str] = dataclasses.field(default_factory=list)
    idle_waits: int = 0
    image: dict | None = None
    index: int = 0
    keypad_refusals: int = 0
    last_done: tuple | None = None
    out: dict = dataclasses.field(default_factory=dict)
    read_rows: list[str] | None = None
    refusals: dict[str, int] = dataclasses.field(default_factory=dict)
    repeats: int = 0
    request_checked: bool = False
    reserve: float = 0.0
    result: dict = dataclasses.field(default_factory=lambda: {"status": "max_steps", "answer": None})
    snapshot: Snapshot | None = None
    spent: float | None = None
    step: int = 0
    touched: list[Element] = dataclasses.field(default_factory=list)
    turn_rows: set[str] | None = None
    turn_screen: Snapshot | None = None
    unproven: bool = False
    untaken: tuple[str, str] | None = None
    plan: str = ""
    notes: list[str] = dataclasses.field(default_factory=list)
    max_actions: int = 0
    source_reads: tuple = (None, None)
    compiled: list = dataclasses.field(default_factory=list)
    worker: threading.Thread | None = None
    pending: bool = False
    unpriced: str = ""
    unrunnable: int = 0
    final: str | None = None
    capped: bool = False
    operations: tuple[str, ...] = ()
    lines: list[str] = dataclasses.field(default_factory=list)
    prep_ms: int = 0
    messages: list | None = None
    verify: _Verify | None = None
    programmed: bool = False
    proposed: list[Action] = dataclasses.field(default_factory=list)
    unrun: str | None = None
    usage: dict = dataclasses.field(default_factory=dict)
    latency_ms: int = 0


# What one run has done so far; run() starts a new one, so nothing carries over from an earlier run.
@dataclasses.dataclass(slots=True, kw_only=True)
class RunState:
    started: float
    initial: InitialState | None = None
    held_acts: frozenset = frozenset()
    notes: list[str] = dataclasses.field(default_factory=list)
    plan: str = ""
    compile_worker: threading.Thread | None = None
    calls: int = 0
    timing: dict[str, float] = dataclasses.field(default_factory=dict)
    cost_offset: float = 0.0
    recovery_spent: float = 0.0
    stall_spent: float = 0.0
    unlocks: int = 0
    secrets: set[str] = dataclasses.field(default_factory=set)
    secret_rects: list[tuple] = dataclasses.field(default_factory=list)
    skill_images: list[tuple] = dataclasses.field(default_factory=list)
    imaged_app: str | None = None
    blind: int = 0
    image_why: str = "off"
    cards: int = 0
    commits: int = 0
    held_items: dict[str, str] = dataclasses.field(default_factory=dict)
    injected: bool = False
    ask_reason: str | None = None
    acting_as: str | None = None
    media_checked: bool | None = None  # a commit_check preview's verdict on the commit's screen; None: none asked
    approved_send: dict | None = None
    declined_on: Snapshot | None = None
    revealed: int | None = None  # the turn whose recorded values were checked for a hidden row (_reveal_found)
    redirect: tuple[str, str] | None = None
    part: Any = None  # a learned.parts.PartRun; the engine does not import learned
    dialog_checked: tuple | None = None
    battery_pause: bool = False
    submitted_at: float | None = None
    dismissed: tuple = (None, 0)
    receipts: dict = dataclasses.field(default_factory=dict)
    recoveries: int = 0
    last_receipt: dict | None = None
    acts_done: dict = dataclasses.field(default_factory=dict)
    act_left: dict = dataclasses.field(default_factory=dict)
    act_seen: dict = dataclasses.field(default_factory=dict)
    act_refused: dict = dataclasses.field(default_factory=dict)
    effects: EffectLedger = dataclasses.field(default_factory=EffectLedger)
    timed_out_reads: int = 0
    relaunches: int = 0
    read_bundle: str = ""
    start_resets: dict = dataclasses.field(default_factory=dict)
    fresh_launch: tuple | None = None
    listing: list | None = None
    macro_feedback: str | None = None
    list_read: tuple | None = None
    passed: list[str] = dataclasses.field(default_factory=list)
    turn_extra: str = ""
    steered: list = dataclasses.field(default_factory=list)
    asked: str | None = None
    turn_log: list[str] = dataclasses.field(default_factory=list)
    turn_notes: dict[int, list[str]] = dataclasses.field(default_factory=dict)
    turn_marks: dict[int, list[str]] = dataclasses.field(default_factory=dict)
    turn_plans: dict[int, str] = dataclasses.field(default_factory=dict)
    turn_reads: dict[int, str] = dataclasses.field(default_factory=dict)
    notes_before: list[str] = dataclasses.field(default_factory=list)
    provenance: Provenance | None = None
    readback: bool = False
    kept: bool = False
    lists: dict = dataclasses.field(default_factory=dict)
    system: str = ""
    stable_extra: str = ""
