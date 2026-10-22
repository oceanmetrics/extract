"""The fetch rules of fetchRetry.ts: a per-attempt timeout, one retry on a network error, a timeout or a 5xx, none on a 4xx,
an error that names the host. httpx.MockTransport stands in for the server: no network."""
import json

import httpx
import pytest

from om_extract.readers.erddap import ErddapError, ErddapUnreachable, fetch_axis, host_of, make_fetch

URL = "https://pae-paha.pacioos.hawaii.edu/erddap/griddap/dhw_5km.json?latitude"


def _fetch(script, **kw):
  """A fetch over a transport that plays `script` in order: a Response, or an exception to raise. Returns (fetch, calls, sleeps)."""
  calls, sleeps = [], []

  def handler(request: httpx.Request) -> httpx.Response:
    calls.append(str(request.url))
    step = script.pop(0)
    if isinstance(step, Exception):
      raise step
    return step

  client = httpx.Client(transport=httpx.MockTransport(handler))
  return make_fetch(client=client, sleep=sleeps.append, **kw), calls, sleeps


def test_fetch_returns_the_first_good_answer():
  fetch, calls, _ = _fetch([httpx.Response(200, content=b"a")])
  assert fetch(URL) == b"a" and len(calls) == 1


def test_fetch_retries_a_network_error_and_a_5xx():
  fetch, calls, sleeps = _fetch([httpx.ConnectError("boom"), httpx.Response(503), httpx.Response(200, content=b"b")], attempts=3,
                                backoff_s=1.5)
  assert fetch(URL) == b"b"
  assert len(calls) == 3 and sleeps == [1.5, 1.5]


def test_fetch_gives_up_after_the_attempts_with_the_host_and_the_reason():
  fetch, _, _ = _fetch([httpx.ConnectError("boom"), httpx.ConnectError("boom")])
  with pytest.raises(ErddapUnreachable) as e:
    fetch(URL)
  assert str(e.value) == "pae-paha.pacioos.hawaii.edu did not answer (network error: boom, 2 tries)"
  assert e.value.host == "pae-paha.pacioos.hawaii.edu" and e.value.url == URL


def test_fetch_names_a_5xx_when_every_attempt_got_one():
  fetch, _, _ = _fetch([httpx.Response(503), httpx.Response(503)])
  with pytest.raises(ErddapUnreachable, match=r"did not answer \(HTTP 503 Service Unavailable, 2 tries\)"):
    fetch(URL)


def test_fetch_times_out_a_stalled_attempt_and_says_so():
  fetch, calls, _ = _fetch([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")], timeout_s=20)
  with pytest.raises(ErddapUnreachable) as e:
    fetch(URL)
  assert str(e.value) == "pae-paha.pacioos.hawaii.edu did not answer (no response within 20 s, 2 tries)"
  assert len(calls) == 2


def test_fetch_does_not_retry_a_4xx_and_passes_the_servers_message_on():
  fetch, calls, _ = _fetch([httpx.Response(404, content=b'Error: "Start" is greater than the axis maximum'),
                            httpx.Response(200, content=b"never")])
  with pytest.raises(ErddapError, match='404 Not Found from ERDDAP: Error: "Start" is greater than the axis maximum') as e:
    fetch(URL)
  assert not isinstance(e.value, ErddapUnreachable) and len(calls) == 1


def test_host_of():
  assert host_of(URL) == "pae-paha.pacioos.hawaii.edu"
  assert host_of("not a url") == "not a url"


def test_fetch_axis_asks_a_descending_axis_high_to_low_and_returns_the_servers_order():
  seen = []

  def fetch(url):
    seen.append(url)
    return json.dumps({"table": {"columnNames": ["latitude"], "rows": [[21.025], [21.0]]}}).encode()

  assert fetch_axis("https://x.org/erddap/", "dhw_5km", "latitude", 21.0, 21.1, True, fetch=fetch) == [21.025, 21.0]
  assert seen == ["https://x.org/erddap/griddap/dhw_5km.json?latitude%5B(21.1):1:(21)%5D"]
