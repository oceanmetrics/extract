"""Parity of om_extract.mask with erddap-places' gridMask.ts, measured on output the TypeScript wrote itself.

tests/fixtures/mask/make_expected.mjs imports erddap-places' own gridMask.ts / wkb.ts / gazetteer.ts and, per
case, writes the place as WKB (`<place>.wkb.gz`) and `<case>.json.gz`: the lobes placeLobes() made, the axes
used per lobe and the gridMask() rows. Here the same WKB goes through `geom.place_lobes()` and
`mask.grid_mask()` with the same axes, and every row must be equal: same lobes, same rows in the same order,
same counts and method, and `weight` equal with `==` (bit for bit, no tolerance).

The fixtures were generated with node 26.10 on arm64 (the Mac mini runner's architecture), where V8's
Math.sin is fdlibm with fused multiply-adds; om_extract reproduces that exactly on every platform (see
_v8math.py), so these pass on x86 CI too. Regenerate with the command in make_expected.mjs.
"""
import gzip
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from om_extract.geom import place_lobes
from om_extract.mask import grid_mask

FIX = Path(__file__).parent / "fixtures" / "mask"
CASES = sorted(p.name[:-len(".json.gz")] for p in FIX.glob("*.json.gz") if p.name != "v8_sin.json.gz")
SIBLING = Path(os.environ.get("OM_ERDDAP_PLACES_DIR", Path(__file__).resolve().parents[2] / "erddap-places"))


def load(name: str) -> dict:
  return json.loads(gzip.decompress((FIX / f"{name}.json.gz").read_bytes()))


def place_wkb(exp: dict) -> bytes:
  return gzip.decompress((FIX / exp["place_wkb"]).read_bytes())


def test_the_parity_fixtures_are_all_there():
  assert CASES == sorted([
    "cinms_crw005_asc", "cinms_crw005_centres", "cinms_crw005_desc", "cinms_crw005_turf", "fknms_crw005",
    "hihwnms_cmems083", "hihwnms_crw005", "holed_deg1", "pmnm_cmems083", "pmnm_crw005", "square_am_deg1",
    "square_deg1", "subcell_q025", "subcell_q025_centres", "unit_q025"])


@pytest.mark.parametrize("name", CASES)
def test_mask_equals_the_typescript(name: str):
  exp = load(name)
  lobes = place_lobes(place_wkb(exp), f"TEST:{exp['place']}")
  assert [(lb.id, list(lb.bbox), lb.n_parts) for lb in lobes] == [(e["id"], e["bbox"], e["n_parts"]) for e in exp["lobes"]]
  for lobe, e in zip(lobes, exp["lobes"]):
    m = grid_mask(lobe, e["lat"], e["lon"], **exp["opts"])
    assert (m.method, m.n_candidates, m.n_inside) == (e["method"], e["n_candidates"], e["n_inside"])
    assert m.lat.tolist() == e["cells"]["lat"]
    assert m.lon.tolist() == e["cells"]["lon"]
    assert m.weight.tolist() == e["cells"]["weight"]  # exact float equality, row by row


def test_the_antimeridian_cases_split_into_two_lobes():
  for name in ("pmnm_crw005", "pmnm_cmems083", "square_am_deg1"):
    assert [e["id"].rsplit(":", 1)[1] for e in load(name)["lobes"]] == ["W", "E"]


def test_fknms_masks_in_under_two_seconds():
  # maskPerf.test.ts: decode -> lobes -> weighted mask, 0.24 s in node (67 s through a Svelte proxy)
  exp = load("fknms_crw005")
  wkb = place_wkb(exp)
  t0 = time.perf_counter()
  lobes = place_lobes(wkb, "NMS:FKNMS")
  m = grid_mask(lobes[0], exp["lobes"][0]["lat"], exp["lobes"][0]["lon"])
  dt = time.perf_counter() - t0
  assert (len(lobes), lobes[0].n_parts, m.n_inside, len(m)) == (1, 13, 352, 462)
  assert dt < 2.0, f"FKNMS mask took {dt:.2f} s"


def _sha(text: str) -> str:
  return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.skipif(not (SIBLING / "app" / "src" / "lib" / "gridMask.ts").is_file(),
                    reason=f"no erddap-places checkout at {SIBLING}")
def test_fixtures_are_from_the_current_typescript():
  # a change to the TS rule makes the fixtures stale: regenerate them (make_expected.mjs) and port the change
  lib = SIBLING / "app" / "src" / "lib"
  gaz = (lib / "gazetteer.ts").read_text()
  a = gaz.index("export function placeLobes(")
  lobe_src = gaz[a:gaz.index("\n}\n", a) + 3]
  now = {"gridMask.ts": _sha((lib / "gridMask.ts").read_text()), "wkb.ts": _sha((lib / "wkb.ts").read_text()),
         "gazetteer.ts placeLobes()": _sha(lobe_src)}
  for name in CASES:
    src = load(name)["sources"]
    assert {k: src[k] for k in now} == now, f"{name}: erddap-places changed since the fixture was written"
