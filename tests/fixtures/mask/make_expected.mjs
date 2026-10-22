// make_expected.mjs: the parity fixtures for om_extract.mask, written BY THE TYPESCRIPT ITSELF.
//
// it imports erddap-places' own gridMask.ts / wkb.ts / gazetteer.ts, so what the python port is
// compared with is what the browser and the precompute compute, not a re-derivation. per case it
//   1. encodes the place polygon as WKB (little-endian ISO MULTIPOLYGON, as places.parquet stores it)
//      and writes <place>.wkb.gz,
//   2. decodes that WKB with wkb.ts wkbToGeoJSON() and splits it with gazetteer.ts placeLobes(),
//   3. builds the ERDDAP axis vectors for each lobe's bbox (padded one cell, clamped to the grid) and
//      runs gridMask(lobe.geojson, lon, lat, opts),
//   4. writes <case>.json.gz: the lobes (id, bbox, n_parts), the axes used and the mask rows.
//
// run from erddap-places/app, where tsx and the turf deps are installed:
//   cd ../erddap-places/app
//   npx tsx ../../extract/tests/fixtures/mask/make_expected.mjs \
//     --lib src/lib --geojson src/lib/__fixtures__ --out ../../extract/tests/fixtures/mask [--case cinms_crw005_desc ...]
// numbers are only comparable on the CPU that ran this (see the README "Mask and statistics": V8's
// Math.sin is fdlibm compiled with fused multiply-add on arm64).
import fs from 'node:fs'
import path from 'node:path'
import zlib from 'node:zlib'
import os from 'node:os'
import { pathToFileURL } from 'node:url'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'

// ── arguments ─────────────────────────────────────────────────────────────────
const argv = process.argv.slice(2)
const opt = (k, d) => { const i = argv.indexOf(`--${k}`); return i >= 0 ? argv[i + 1] : d }
const only = argv.flatMap((a, i) => (a === '--case' ? [argv[i + 1]] : []))
const LIB = path.resolve(opt('lib', 'src/lib'))
const GJ  = path.resolve(opt('geojson', path.join(LIB, '__fixtures__')))
const OUT = path.resolve(opt('out', '.'))

const { gridMask, polygonParts } = await import(pathToFileURL(path.join(LIB, 'gridMask.ts')).href)
const { wkbToGeoJSON }           = await import(pathToFileURL(path.join(LIB, 'wkb.ts')).href)
const { placeLobes, plainPlace } = await import(pathToFileURL(path.join(LIB, 'gazetteer.ts')).href)

// ── grids: cell centres at origin + k*res, k in [0, n) ─────────────────────────
const GRIDS = {
  // NOAA CRW dhw_5km: 0.05°, centres at -179.975 + k*0.05, three decimals (crwAxes() in gridMask.test.ts)
  crw005  : { origin: -179.975, res: 0.05,   n: 7200, digits: 3 },
  // CMEMS global 1/12°: centres at -180 + k/12, five decimals as ERDDAP prints the float32 axis
  cmems083: { origin: -180,     res: 1 / 12, n: 4320, digits: 5 },
  // a coarse synthetic 1° grid: centres at -179.5 + k
  deg1    : { origin: -179.5,   res: 1,      n: 360,  digits: 3 },
}
function axis(g, lo, hi) {
  const k = (v) => Math.round((v - g.origin) / g.res)
  const a = Math.max(0, k(lo) - 1), b = Math.min(g.n - 1, k(hi) + 1)
  const out = []
  for (let i = a; i <= b; i++) out.push(Number((g.origin + i * g.res).toFixed(g.digits)))
  return out
}

// ── synthetic places ──────────────────────────────────────────────────────────
const sq = (x0, y0, x1, y1) => [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]
const SYNTH = {
  // a square on the 1° grid, edges between the centres: interior, edge and corner cells
  square: { type: 'MultiPolygon', coordinates: [sq(10.3, 20.2, 12.6, 21.7)] },
  // two parts cut at ±180 (the wkb.test.ts MULTI case, widened): two lobes, W and E
  square_am: { type: 'MultiPolygon', coordinates: [sq(-180, 20.2, -178.4, 21.6), sq(178.3, 20.2, 180, 21.6)] },
  // a 0.1° square inside one 0.25° cell, between the centres (gridMask.test.ts, the Gray's Reef case on CMEMS)
  subcell: { type: 'MultiPolygon', coordinates: [sq(-80.95, 31.3, -80.85, 31.4)] },
  // the unit square of gridMask.test.ts 'picks turf for a small place'
  unit: { type: 'MultiPolygon', coordinates: [sq(0, 0, 1, 1)] },
  // a square with a square hole, on the 1° grid: the hole is subtracted from the overlap (turf polygonArea)
  holed: { type: 'MultiPolygon', coordinates: [[sq(10.2, 20.2, 13.8, 22.8)[0], sq(11.2, 21.1, 12.4, 21.9)[0].reverse()]] },
}

// ── cases ─────────────────────────────────────────────────────────────────────
const CASES = [
  { name: 'cinms_crw005_desc',    place: 'CINMS',   grid: 'crw005',   latDesc: true  },
  { name: 'cinms_crw005_asc',     place: 'CINMS',   grid: 'crw005',   latDesc: false },
  { name: 'cinms_crw005_turf',    place: 'CINMS',   grid: 'crw005',   latDesc: true, opts: { method: 'turf' } },
  { name: 'cinms_crw005_centres', place: 'CINMS',   grid: 'crw005',   latDesc: true, opts: { weights: false } },
  { name: 'fknms_crw005',         place: 'FKNMS',   grid: 'crw005',   latDesc: true  },
  { name: 'hihwnms_crw005',       place: 'HIHWNMS', grid: 'crw005',   latDesc: true  },
  { name: 'hihwnms_cmems083',     place: 'HIHWNMS', grid: 'cmems083', latDesc: false },
  { name: 'pmnm_crw005',          place: 'PMNM',    grid: 'crw005',   latDesc: true  },
  { name: 'pmnm_cmems083',        place: 'PMNM',    grid: 'cmems083', latDesc: false },
  { name: 'square_deg1',          place: 'square',  grid: 'deg1',     latDesc: true  },
  { name: 'square_am_deg1',       place: 'square_am', grid: 'deg1',   latDesc: false },
  { name: 'holed_deg1',           place: 'holed',   grid: 'deg1',     latDesc: true  },
  // explicit axes, as in gridMask.test.ts
  { name: 'subcell_q025',         place: 'subcell', axes: { lon: [-81, -80.75], lat: [31.25, 31.5] } },
  { name: 'subcell_q025_centres', place: 'subcell', axes: { lon: [-81, -80.75], lat: [31.25, 31.5] }, opts: { weights: false } },
  { name: 'unit_q025',            place: 'unit',    axes: { lon: [0.25, 0.5, 0.75], lat: [0.75, 0.5, 0.25] } },
]

// ── WKB writer: little-endian ISO MULTIPOLYGON ────────────────────────────────
function multiPolygonWkb(polys) {
  let n = 9
  for (const rings of polys) { n += 9; for (const r of rings) n += 4 + 16 * r.length }
  const b = Buffer.alloc(n)
  let o = 0
  const u8 = (v) => { b.writeUInt8(v, o); o += 1 }
  const u32 = (v) => { b.writeUInt32LE(v, o); o += 4 }
  const f64 = (v) => { b.writeDoubleLE(v, o); o += 8 }
  u8(1); u32(6); u32(polys.length)
  for (const rings of polys) {
    u8(1); u32(3); u32(rings.length)
    for (const r of rings) { u32(r.length); for (const [x, y] of r) { f64(x); f64(y) } }
  }
  return b
}

const gz = (file, buf) => fs.writeFileSync(file, zlib.gzipSync(buf, { level: 9, mtime: 0 }))
const geometryOf = (place) => {
  if (SYNTH[place]) return SYNTH[place]
  const fc = JSON.parse(fs.readFileSync(path.join(GJ, `${place}.geojson`), 'utf8'))
  return { type: 'MultiPolygon', coordinates: polygonParts(fc).map((p) => p.rings) }   // as maskPerf.test.ts asPlace()
}

// what the fixtures were generated from: tests/test_mask_parity.py compares these with the sibling
// erddap-places checkout (when present) so a change to the TypeScript rule shows up as a stale fixture
const sha = (s) => createHash('sha256').update(s).digest('hex')
const gazSrc = fs.readFileSync(path.join(LIB, 'gazetteer.ts'), 'utf8')
const lobeSrc = gazSrc.slice(gazSrc.indexOf('export function placeLobes('), gazSrc.indexOf('\n}\n', gazSrc.indexOf('export function placeLobes(')) + 3)
const turfVersion = createRequire(path.join(LIB, 'gridMask.ts'))('@turf/area/package.json').version
const SOURCES = {
  'gridMask.ts': sha(fs.readFileSync(path.join(LIB, 'gridMask.ts'), 'utf8')),
  'wkb.ts'     : sha(fs.readFileSync(path.join(LIB, 'wkb.ts'), 'utf8')),
  'gazetteer.ts placeLobes()': sha(lobeSrc),
  turf: turfVersion,
}

fs.mkdirSync(OUT, { recursive: true })

// Math.sin itself, the one transcendental in the mask (turf ringArea): 4,000 latitudes in radians from a
// fixed LCG, half of them on a 0.001 degree lattice like the cell edges, with V8's answers
if (!only.length || only.includes('v8_sin')) {
  let seed = 20261022
  const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648 }
  const x = []
  for (let i = 0; i < 4_000; i++) {
    const deg = i % 2 ? rnd() * 180 - 90 : Number((rnd() * 180 - 90).toFixed(3))
    x.push(deg * (Math.PI / 180))
  }
  gz(path.join(OUT, 'v8_sin.json.gz'), Buffer.from(JSON.stringify({
    generator: { node: process.version, arch: os.arch(), platform: os.platform() }, x, sin: x.map(Math.sin) })))
  console.log('v8_sin: 4000 values')
}
const written = new Set()
for (const c of CASES) {
  if (only.length && !only.includes(c.name)) continue
  const geom = geometryOf(c.place)
  const wkb  = multiPolygonWkb(geom.coordinates)
  const back = wkbToGeoJSON(new Uint8Array(wkb))
  if (JSON.stringify(back.coordinates) !== JSON.stringify(geom.coordinates)) throw new Error(`${c.place}: WKB round trip changed the coordinates`)
  const placeFile = `${c.place.toLowerCase()}.wkb.gz`
  if (!written.has(placeFile)) { gz(path.join(OUT, placeFile), wkb); written.add(placeFile) }

  const place = { place_id: `TEST:${c.place}`, gazetteer: 'TEST', name: c.place, area_km2: 0, bbox: [0, 0, 0, 0], geometry: back }
  const lobes = placeLobes(plainPlace(place))
  const g = c.grid ? GRIDS[c.grid] : null
  const out = { case: c.name, place: c.place, place_wkb: placeFile, grid: c.grid ?? null, grid_spec: g, lat_descending: c.latDesc ?? null,
    opts: c.opts ?? {}, generator: { node: process.version, arch: os.arch(), platform: os.platform() }, sources: SOURCES, lobes: [] }
  const ms = {}
  for (const l of lobes) {
    const lon = c.axes ? c.axes.lon : axis(g, l.bbox[0], l.bbox[2])
    const latA = c.axes ? c.axes.lat : axis(g, l.bbox[1], l.bbox[3])
    const lat = !c.axes && c.latDesc ? [...latA].reverse() : latA
    const m = gridMask(l.geojson, lon, lat, c.opts ?? {})
    ms[l.id] = m.ms.toFixed(0)   // logged, not written: the fixture regenerates byte for byte
    out.lobes.push({
      id: l.id, bbox: l.bbox, n_parts: l.nParts, lon, lat,
      method: m.method, n_candidates: m.nCandidates, n_inside: m.nInside,
      cells: { lon: m.cells.map((x) => x.lon), lat: m.cells.map((x) => x.lat), weight: m.cells.map((x) => x.weight) },
    })
  }
  gz(path.join(OUT, `${c.name}.json.gz`), Buffer.from(JSON.stringify(out)))
  console.log(`${c.name}: ${out.lobes.map((l) => `${l.id} ${l.method} ${l.cells.lon.length} rows (${l.n_inside} inside, ${l.n_candidates} candidates, ${ms[l.id]} ms)`).join('; ')}`)
}
