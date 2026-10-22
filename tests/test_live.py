"""Live checks against PacIOOS ERDDAP and the bucket (skipped unless OM_LIVE=1)."""
import pytest

from om_extract.chunks import mask_key
from om_extract.engine import _mask_lobe, value_expr
from om_extract.geom import place_lobes
from om_extract.layers import dataset_of, load_collection
from om_extract.places import GAZETTEER_BASE, place_geometry, place_ids
from om_extract.readers.erddap import ErddapGrid


@pytest.mark.live
def test_the_published_places_are_the_18_sanctuaries():
  assert len(place_ids("places", GAZETTEER_BASE, "NMS:")) == 18


@pytest.mark.live
def test_the_live_cinms_key_is_the_runners_directory():
  coll = load_collection(f"{GAZETTEER_BASE}/erddap/dhw_5km/collection.json")
  ds = dataset_of(coll)
  [lobe] = place_lobes(place_geometry("NMS:CINMS"), "NMS:CINMS")
  m = _mask_lobe(lobe, ErddapGrid(coll).axes(lobe.bbox, live=True), ds.lon_range)
  cells = zip(m["longitude"].to_pylist(), m["latitude"].to_pylist(), m["weight"].to_pylist())
  assert mask_key(ds.base_url, ds.dataset_id, ds.depth, "CRW_SST", value_expr("CRW_SST", "Celsius"), "stats_daily",
                  cells) == "a91ae222cbfe"
