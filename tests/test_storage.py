"""om_extract.storage: the storage url rule, held to the platform's case table (the TS twin's) and its own cases."""
import json
import os
from pathlib import Path

import pytest

from om_extract import storage as st

# the case table of @oceanmetrics/catalog/storage, from the sibling platform checkout (CI checks it out beside this repo);
# OM_STORAGE_CASES points at another copy (a platform branch in a worktree)
CASES_PATH = Path(os.environ.get("OM_STORAGE_CASES") or
                  Path(__file__).resolve().parents[2] / "platform/packages/catalog/fixtures/storage/cases.json")
CASES = json.loads(CASES_PATH.read_text()) if CASES_PATH.exists() else {"parse": [], "storageUrl": [], "rewrite": []}
needs_cases = pytest.mark.skipif(not CASES_PATH.exists(), reason=f"{CASES_PATH} not found (the platform checkout)")


def test_the_hosts_are_the_ts_constants():
  assert st.STORAGE_HOST == "https://storage.oceanmetrics.io"
  assert st.S3_PATH_STYLE == "https://s3.us-east-1.amazonaws.com/oceanmetrics.io-public"
  assert st.S3_VIRTUAL_HOST == "https://oceanmetrics.io-public.s3.amazonaws.com"
  assert st.WORKER_PREVIEW == "https://om-gazetteer.ben-f07.workers.dev"
  assert list(st.STORAGE_HOSTS) == ["storage", "s3", "s3-virtual", "preview"]
  assert st.DEFAULT_STORAGE_HOST == st.STORAGE_HOST  # since the cutover of 2026-10-23 (S3_PATH_STYLE in 0.6.1)
  assert st.storage_url("indicators/indicators.json") == "https://storage.oceanmetrics.io/indicators/indicators.json"
  assert st.STORAGE_ENV == "OM_STORAGE"


@needs_cases
@pytest.mark.parametrize("c", CASES["parse"], ids=lambda c: c["name"])
def test_parse_case_table(c):
  assert st.parse_storage_host(c["value"]) == c["output"]


@needs_cases
@pytest.mark.parametrize("c", CASES["storageUrl"], ids=lambda c: c["name"])
def test_storage_url_case_table(c):
  assert st.storage_url(c["key"], host=c.get("host"), api_key=c.get("apiKey")) == c["output"]


@needs_cases
@pytest.mark.parametrize("c", CASES["rewrite"], ids=lambda c: c["name"])
def test_rewrite_case_table(c):
  out, n = st.rewrite_storage_urls_counted(c["input"], c["from"], c["to"])
  assert out == c["output"]
  assert n == c["count"]


def test_both_directions():
  s3 = f"{st.S3_PATH_STYLE}/indicators/indicators.json"
  storage = "https://storage.oceanmetrics.io/indicators/indicators.json"
  assert st.rewrite_storage_urls(s3, "S3_PATH_STYLE", "STORAGE_HOST") == storage
  assert st.rewrite_storage_urls(storage, "STORAGE_HOST", "S3_PATH_STYLE") == s3


def test_urls_that_must_not_change():
  mr = "https://geo.vliz.be/geoserver/gwc/service/wmts?layer=MarineRegions:eez&TileMatrix=EPSG:900913:{z}&TileCol={x}&TileRow={y}"
  erddap = "https://erddap.oceanmetrics.io/erddap/griddap/jplMURSST41.csv?analysed_sst%5B(last)%5D"
  other_bucket = "https://s3.us-east-1.amazonaws.com/oceanmetrics.io-public-logs/x.json"
  for u in (mr, erddap, other_bucket):
    assert st.rewrite_storage_urls_counted(u, ["s3", "s3-virtual", "storage", "preview"], "storage") == (u, 0)


def test_json_values_walked_keys_kept_input_untouched():
  doc = {f"{st.S3_PATH_STYLE}/k": 1, "links": [{"href": f"{st.S3_PATH_STYLE}/obis-h3/collection.json"}], "n": 2, "t": (f"{st.S3_PATH_STYLE}/a",)}
  out, n = st.rewrite_storage_urls_counted(doc, "s3", "storage")
  assert n == 2
  assert out["links"][0]["href"] == "https://storage.oceanmetrics.io/obis-h3/collection.json"
  assert out["t"] == ("https://storage.oceanmetrics.io/a",)
  assert list(out) == list(doc) and out["n"] == 2
  assert doc["links"][0]["href"].startswith(st.S3_PATH_STYLE)
  assert st.rewrite_storage_urls(out, "storage", "s3") == doc


def test_env_override_read_once():
  assert st.storage_host_from_env({"OM_STORAGE": "storage"}) == st.STORAGE_HOST
  assert st.storage_host_from_env({"OM_STORAGE": "ftp://x"}) == st.DEFAULT_STORAGE_HOST
  assert st.storage_host_from_env({}) == st.DEFAULT_STORAGE_HOST
  assert st.resolve_storage_host(None, "nope", "preview", "storage") == st.WORKER_PREVIEW


def test_a_bad_host_names_it():
  with pytest.raises(ValueError, match="not a host: 'bucket'"):
    st.rewrite_storage_urls("x", "bucket", "storage")
  with pytest.raises(ValueError, match="not a host: 'ftp://x'"):
    st.rewrite_storage_urls("x", "s3", "ftp://x")
