"""where the public bucket is read: the python twin of `@oceanmetrics/catalog/storage` (build week 6, step 0).

every pipeline builds its storage urls with `storage_url()` and rebases hrefs with `rewrite_storage_urls()`, so the
storage cutover (path-style S3 -> storage.oceanmetrics.io on R2) is one setting: `DEFAULT_STORAGE_HOST` below, or per
run the `OM_STORAGE` environment variable (`storage_host_from_env()`). both implementations are held to the platform's
case table, `packages/catalog/fixtures/storage/cases.json` (tests/test_storage.py).
"""
from __future__ import annotations

import os
import re
import urllib.parse
from collections.abc import Iterable, Mapping
from typing import Any

#: the public host: the om-gazetteer Worker over R2 (since the cutover of 2026-10-23; before it, Caddy 302'd to S3)
STORAGE_HOST = "https://storage.oceanmetrics.io"
#: the S3 bucket, path-style: what the apps and pipelines read until the cutover; written until the freeze
S3_PATH_STYLE = "https://s3.us-east-1.amazonaws.com/oceanmetrics.io-public"
#: the S3 bucket, virtual-hosted style: a legacy form some older hrefs carry
S3_VIRTUAL_HOST = "https://oceanmetrics.io-public.s3.amazonaws.com"
#: the om-gazetteer Worker on workers.dev: the preview and the rehearsal host
WORKER_PREVIEW = "https://om-gazetteer.ben-f07.workers.dev"

#: every host the bucket answers on, by the names a setting may use
STORAGE_HOSTS = {"storage": STORAGE_HOST, "s3": S3_PATH_STYLE, "s3-virtual": S3_VIRTUAL_HOST, "preview": WORKER_PREVIEW}
_CONSTANT_NAMES = {"STORAGE_HOST": STORAGE_HOST, "S3_PATH_STYLE": S3_PATH_STYLE, "S3_VIRTUAL_HOST": S3_VIRTUAL_HOST,
                   "WORKER_PREVIEW": WORKER_PREVIEW}

#: the host every storage url uses unless a setting overrides it: storage.oceanmetrics.io since the cutover of
#: 2026-10-23 (week 6 step 2; path-style S3 in 0.6.1 and 0.6.2), flipped with the TS twin (catalog 0.6.0). a rollback is
#: OM_STORAGE=s3 per run, or this line flipped back in a release
DEFAULT_STORAGE_HOST = STORAGE_HOST

#: the environment variable a run reads once for its override
STORAGE_ENV = "OM_STORAGE"

_LOCAL = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)")
# a host ends at a path, a query, a fragment, a quote, a bracket, whitespace or the end
_END = r"(?=[/?#\"'\s)<>\]\\]|$)"


def parse_storage_host(value: str | None) -> str | None:
  """a setting as a host url (no trailing slash), or None when it is empty or malformed: a name of STORAGE_HOSTS, a
  constant name (`STORAGE_HOST`, `S3_PATH_STYLE`, ...), an https url, or an http url on localhost."""
  v = (value or "").strip()
  if not v:
    return None
  if v in STORAGE_HOSTS:
    return STORAGE_HOSTS[v]
  if v in _CONSTANT_NAMES:
    return _CONSTANT_NAMES[v]
  if not re.match(r"^https://[^/?#\s]+", v) and not _LOCAL.match(v):
    return None
  if re.search(r"[?#\s]", v):
    return None
  return v.rstrip("/")


def resolve_storage_host(*candidates: str | None) -> str:
  """the first well-formed setting of `candidates` (in precedence order), else DEFAULT_STORAGE_HOST."""
  for c in candidates:
    h = parse_storage_host(c)
    if h:
      return h
  return DEFAULT_STORAGE_HOST


def storage_host_from_env(env: Mapping[str, str] | None = None) -> str:
  """the host a run uses: `OM_STORAGE` (from `env`, default os.environ) when well formed, else the default."""
  return resolve_storage_host((os.environ if env is None else env).get(STORAGE_ENV))


def storage_url(key: str, host: str | None = None, api_key: str | None = None) -> str:
  """the url of `key` (an object key or a prefix, e.g. `indicators/indicators.json` or `gazetteer/`) on the storage
  host. leading slashes are dropped, the key used as given; `api_key` adds `?key=` (the Worker's keyed reads)."""
  url = f"{resolve_storage_host(host)}/{key.lstrip('/')}"
  if not api_key:
    return url
  q = urllib.parse.quote(api_key, safe="-_.!~*'()")  # encodeURIComponent's unescaped set
  return f"{url}{'&' if '?' in url else '?'}key={q}"


def _pattern(from_: str | Iterable[str]) -> re.Pattern[str]:
  hosts = []
  for h in [from_] if isinstance(from_, str) else list(from_):
    p = parse_storage_host(h)
    if not p:
      raise ValueError(f"rewrite_storage_urls: not a host: {h!r}")
    hosts.append(p)
  alt = "|".join(re.escape(h) for h in sorted(set(hosts), key=len, reverse=True))
  return re.compile(f"(?:{alt}){_END}")


def _to(to: str) -> str:
  t = parse_storage_host(to)
  if not t:
    raise ValueError(f"rewrite_storage_urls: not a host: {to!r}")
  return t


def _walk(value: Any, pat: re.Pattern[str], to: str, count: list[int]) -> Any:
  if isinstance(value, str):
    out, n = pat.subn(lambda _m: to, value)
    count[0] += n
    return out
  if isinstance(value, list):
    return [_walk(v, pat, to, count) for v in value]
  if isinstance(value, tuple):
    return tuple(_walk(v, pat, to, count) for v in value)
  if isinstance(value, dict):
    return {k: _walk(v, pat, to, count) for k, v in value.items()}
  return value


def rewrite_storage_urls_counted(value: Any, from_: str | Iterable[str], to: str) -> tuple[Any, int]:
  """`rewrite_storage_urls` and the number of urls it moved."""
  count = [0]
  out = _walk(value, _pattern(from_), _to(to), count)
  return out, count[0]


def rewrite_storage_urls(value: Any, from_: str | Iterable[str], to: str) -> Any:
  """`value` with every url on a `from_` host moved to the `to` host, the path, query and fragment kept. a string is
  rewritten as text; a list, tuple or dict is walked and its string values rewritten (keys are left alone) into a new
  value. a url on any other host (Marine Regions, ERDDAP, a host that only begins with a storage host's name) stays."""
  return rewrite_storage_urls_counted(value, from_, to)[0]


def count_storage_urls(value: Any, from_: str | Iterable[str]) -> int:
  """how many urls on a `from_` host `value` carries."""
  return rewrite_storage_urls_counted(value, from_, STORAGE_HOST)[1]
