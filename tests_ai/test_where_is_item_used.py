"""where_is_item_used: the one question a filter cannot answer.

`.list` rejects materials.item_id, so "which BOMs use this item" can only be answered by reading the
BOMs and comparing in code. Doing that in the model does not work: a BOM row carries its materials and
runs ~3,300 characters, so 11 of 100 fit in one reply — and seen live on 2026-09-29, handed those 11 the
model reported the item was used nowhere. It is used in three.
"""
import pytest

from prod_agent import domain
from prod_agent.mcp_client import McpError, RowPage

ITEM_ID = "597e4991-71f4-4725-bb8e-707674ec5c68"


def _item(**kw):
    base = {"id": ITEM_ID, "number": "ITEM-2026-00011", "code": "RM-BOLT-M8",
            "name": "Hex Bolt M8x25 Zinc 8.8"}
    return {**base, **kw}


def _bom(number, makes=None, materials=(), item_id=None, is_active=1, is_default=0):
    return {"id": f"id-{number}", "number": number, "item_id": item_id,
            "_item_id_display": makes, "is_active": is_active, "is_default": is_default,
            "materials": [dict(m) for m in materials]}


class FakeMcp:
    def __init__(self, items=(), boms=(), tools=("BOM.list", "Item.list"),
                 total=None, truncated=False, get_raises=None):
        self._items, self._boms, self._tools = list(items), list(boms), set(tools)
        self._total, self._truncated, self._get_raises = total, truncated, get_raises
        self.calls = []
        self.session = type("S", (), {"base": "https://example.invalid", "token": None,
                                      "with_reauth": staticmethod(lambda fn: fn())})()

    def has_tool(self, name):
        return name in self._tools

    def tool_names(self):
        return set(self._tools)

    def call(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        if name == "Item.get":
            if self._get_raises:
                raise self._get_raises
            return next((i for i in self._items if i["id"] == arguments["id"]), None)
        raise AssertionError(f"unexpected call {name}")

    def list_all(self, entity, page=200, max_rows=None, **filters):
        self.calls.append((f"{entity}.list_all", dict(filters)))
        rows = RowPage(self._items if entity == "Item" else self._boms)
        rows.total = self._total if self._total is not None else len(rows)
        rows.truncated = self._truncated if entity == "BOM" else False
        return rows


def _live_shape():
    """The real Suryodaya answer, verified 2026-09-29: consumed by three, produced by one."""
    return FakeMcp(
        items=[_item()],
        boms=[_bom("BOM-2026-00006", "Motor Controller Housing Assembly",
                   [{"item_id": ITEM_ID, "qty": 6}], is_default=1),
              _bom("BOM-2026-00012", "Heat Sink Assembly 1200W",
                   [{"item_id": ITEM_ID, "qty": 4}], is_default=1),
              _bom("BOM-2026-00058", "V-Block Pair 252mm (Set)",
                   [{"item_id": ITEM_ID, "qty": 9}], is_default=1),
              _bom("BOM-2026-00099", "Hex Bolt M8x25 Zinc 8.8", [], item_id=ITEM_ID),
              _bom("BOM-2026-00077", "Something Else", [{"item_id": "other-id", "qty": 2}])])


# --------------------------------------------------------------- the answer is complete and correct

def test_finds_every_consuming_bom():
    out = domain.where_is_item_used(_live_shape(), "Hex Bolt M8x25 Zinc 8.8")
    assert [c["bom"] for c in out["consumed_in_boms"]] == \
           ["BOM-2026-00006", "BOM-2026-00012", "BOM-2026-00058"]
    assert out["complete"] is True and out["boms_scanned"] == 5


def test_consumed_and_produced_are_reported_separately():
    """The live failure: asked which BOMs USE the item, the model named the BOM that MAKES it."""
    out = domain.where_is_item_used(_live_shape(), "RM-BOLT-M8")
    assert [p["bom"] for p in out["produced_by_boms"]] == ["BOM-2026-00099"]
    assert "BOM-2026-00099" not in [c["bom"] for c in out["consumed_in_boms"]]


def test_an_unrelated_bom_is_not_reported():
    out = domain.where_is_item_used(_live_shape(), "RM-BOLT-M8")
    assert "BOM-2026-00077" not in [c["bom"] for c in out["consumed_in_boms"]]


def test_each_hit_carries_what_the_bom_makes_and_how_many_are_needed():
    out = domain.where_is_item_used(_live_shape(), ITEM_ID)
    hit = next(c for c in out["consumed_in_boms"] if c["bom"] == "BOM-2026-00058")
    assert hit["makes"] == "V-Block Pair 252mm (Set)" and hit["qty_per_build"] == 9


def test_quantities_on_repeated_lines_are_summed():
    mcp = FakeMcp(items=[_item()],
                  boms=[_bom("BOM-1", "Thing", [{"item_id": ITEM_ID, "qty": 2},
                                                {"item_id": ITEM_ID, "qty": 3}])])
    hit = domain.where_is_item_used(mcp, ITEM_ID)["consumed_in_boms"][0]
    assert hit["qty_per_build"] == 5 and hit["lines"] == 2


def test_a_critical_line_is_flagged():
    mcp = FakeMcp(items=[_item()],
                  boms=[_bom("BOM-1", "Thing", [{"item_id": ITEM_ID, "qty": 1, "is_critical": True}])])
    assert domain.where_is_item_used(mcp, ITEM_ID)["consumed_in_boms"][0]["is_critical"] is True


def test_an_item_used_nowhere_returns_an_empty_list_not_an_error():
    mcp = FakeMcp(items=[_item()], boms=[_bom("BOM-1", "Thing", [{"item_id": "other", "qty": 1}])])
    out = domain.where_is_item_used(mcp, ITEM_ID)
    assert out["found"] is True and out["consumed_in_boms"] == [] and out["complete"] is True


# --------------------------------------------------------------- inactive BOMs are shown, not hidden

def test_inactive_boms_are_included_and_flagged():
    """15 of 100 BOMs are inactive on Suryodaya. Dropping them would be a hidden editorial decision."""
    mcp = FakeMcp(items=[_item()],
                  boms=[_bom("BOM-OLD", "Old", [{"item_id": ITEM_ID, "qty": 1}], is_active=0),
                        _bom("BOM-NEW", "New", [{"item_id": ITEM_ID, "qty": 1}], is_active=1)])
    out = domain.where_is_item_used(mcp, ITEM_ID)
    assert {c["bom"] for c in out["consumed_in_boms"]} == {"BOM-OLD", "BOM-NEW"}
    assert [c["bom"] for c in out["consumed_in_boms"]] == ["BOM-NEW", "BOM-OLD"], "active first"
    assert next(c for c in out["consumed_in_boms"] if c["bom"] == "BOM-OLD")["bom_is_active"] is False


# --------------------------------------------------------------- resolution

@pytest.mark.parametrize("ref", [ITEM_ID, "RM-BOLT-M8", "ITEM-2026-00011", "Hex Bolt M8x25 Zinc 8.8"])
def test_an_item_resolves_by_id_code_number_or_name(ref):
    out = domain.where_is_item_used(_live_shape(), ref)
    assert out["found"] is True and out["item"]["id"] == ITEM_ID


@pytest.mark.parametrize("ref", ["rm-bolt-m8", "  RM-BOLT-M8  ", "hex bolt m8x25 zinc 8.8"])
def test_resolution_ignores_case_and_surrounding_space(ref):
    assert domain.where_is_item_used(_live_shape(), ref)["found"] is True


def test_a_partial_name_does_not_resolve():
    """An exact key only: "Hex Bolt" could mean M8x25 or M8x30, and guessing is worse than asking."""
    out = domain.where_is_item_used(_live_shape(), "Hex Bolt")
    assert out["found"] is False and "exact name" in out["detail"]


@pytest.mark.parametrize("ref", ["", "   ", "nonsense-item"])
def test_an_unknown_reference_is_reported_not_guessed(ref):
    out = domain.where_is_item_used(_live_shape(), ref)
    assert out["found"] is False and out["item"] == ref


def test_a_missing_uuid_is_not_found_rather_than_raising():
    mcp = FakeMcp(items=[], boms=[], get_raises=McpError("not_found", "record not found"))
    assert domain.where_is_item_used(mcp, "11111111-2222-3333-4444-555555555555")["found"] is False


def test_an_unexpected_error_on_get_is_not_swallowed():
    mcp = FakeMcp(items=[], boms=[], get_raises=McpError(-32000, "gateway exploded"))
    with pytest.raises(McpError):
        domain.where_is_item_used(mcp, "11111111-2222-3333-4444-555555555555")


# --------------------------------------------------------------- honest when it cannot finish

def test_a_ceilinged_scan_forbids_the_negative_answer():
    """A partial scan can prove use; it can never prove absence. That asymmetry has to be stated."""
    mcp = FakeMcp(items=[_item()], boms=[_bom("BOM-1", "T", [{"item_id": "other", "qty": 1}])],
                  total=99999, truncated=True)
    out = domain.where_is_item_used(mcp, ITEM_ID)
    assert out["complete"] is False
    assert out["boms_scanned"] == 1 and out["boms_total"] == 99999
    assert "do not say the item is unused" in out["instruction"]


def test_a_complete_scan_carries_no_warning():
    assert "instruction" not in domain.where_is_item_used(_live_shape(), ITEM_ID)


def test_boms_outside_the_seat_are_refused_rather_than_answered_emptily():
    mcp = FakeMcp(items=[_item()], boms=[], tools=("Item.list",))
    out = domain.where_is_item_used(mcp, ITEM_ID)
    assert out["error"] == "BOM not readable by this seat"
    assert "consumed_in_boms" not in out, "an empty list here would read as 'used nowhere'"


# --------------------------------------------------------------- payload and wiring

def test_only_the_answer_is_returned_never_the_bom_rows():
    """The whole point: 100 BOM rows are 327k characters, the answer is a few hundred."""
    import json
    out = domain.where_is_item_used(_live_shape(), ITEM_ID)
    assert "materials" not in json.dumps(out)
    assert len(json.dumps(out)) < 4000


def test_the_prompt_routes_the_question_here_and_names_the_two_directions():
    from prod_agent.agent import ProductionAgent, SYSTEM_PROMPT
    assert "where_is_item_used" in ProductionAgent.PARALLEL_SAFE
    assert "where_is_item_used" in ProductionAgent.REPEAT_GUARDED
    assert "consumed_in_boms" in SYSTEM_PROMPT and "produced_by_boms" in SYSTEM_PROMPT
    assert "Never answer this from query_records" in SYSTEM_PROMPT
