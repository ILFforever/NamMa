"""GISTDA flood history copy (ingest/gistda_history.py): parsing shapes, paging onto disk, building cells and
sub-districts. Shapes are built to the plan's description of features/flood-freq (y_2011… flags, a frequency,
pv/ap/tb names and codes); paging goes through httpx.MockTransport, so nothing calls GISTDA. DB tests skip
without Postgres."""
import gzip
from pathlib import Path
from types import SimpleNamespace

import h3
import httpx
import pytest

from ingest import gistda_history as gh

KEY = "k-secret-123"
BANG_BAN = (14.41, 100.46)


def _square(lat: float, lon: float, half: float) -> dict:
    ring = [[lon - half, lat - half], [lon + half, lat - half], [lon + half, lat + half], [lon - half, lat + half],
            [lon - half, lat - half]]
    return {"type": "Polygon", "coordinates": [ring]}


def _shape(years, at=BANG_BAN, half=0.005, tb="140501", tambon="บางบาล", freq=None, pv=14) -> dict:
    props = {f"y_{y}": int(y in years) for y in range(2011, 2025)}
    props.update(freq=len(years) if freq is None else freq, tb_idn=tb, tb_tn=tambon, ap_tn="อำเภอบางบาล",
                 pv_tn="พระนครศรีอยุธยา", pv_idn=pv, link="https://example.org/?api_key=" + KEY)
    return {"type": "Feature", "geometry": _square(*at, half), "properties": props}


def _province(n: int) -> list[dict]:
    return [_shape([2011, 2017], at=(BANG_BAN[0] + 0.02 * i, BANG_BAN[1]), tb=f"1405{i:02d}", tambon=f"t{i}")
            for i in range(n)]


def _gistda(pages: dict[int, list[dict]], total: int | None, statuses: list[int] | None = None):
    """A fake GISTDA: pages by offset; `statuses` are answered first, one per request. Records each request."""
    seen: list[httpx.Request] = []
    queue = list(statuses or [])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if queue:
            return httpx.Response(queue.pop(0), headers={"Retry-After": "1"}, json={"status": "slow down"})
        feats = pages.get(int(request.url.params["offset"]), [])
        body = {"type": "FeatureCollection", "features": feats, "numberReturned": len(feats),
                "links": [{"rel": "self", "href": str(request.url)}]}
        if total is not None:
            body["numberMatched"] = total
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler), headers={"API-Key": KEY}), seen


@pytest.fixture()
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(gh, "PAGE", 3)
    monkeypatch.setattr(gh, "MIN_INTERVAL_S", 0)
    monkeypatch.setattr(gh, "RAW_DIR", tmp_path)
    monkeypatch.setattr(gh.time, "sleep", lambda s: None)
    feats = _province(7)
    return {0: feats[:3], 3: feats[3:6], 6: feats[6:]}


# --- parsing -----------------------------------------------------------------------------------------

def test_parse_shape():
    s = gh.parse_shape(_shape([2011, 2013, 2024]))
    assert gh.years_of(s["years"]) == [2011, 2013, 2024] and s["freq_gistda"] == 3
    assert (s["pv_code"], s["ap_code"], s["tb_code"]) == ("14", "1405", "140501")
    assert (s["tambon"], s["amphoe"]) == ("บางบาล", "บางบาล")
    odd = gh.parse_shape({"properties": {"Y2554": "1", "y_2012": "0", "year_2017": True, "y_2018": None}})
    assert gh.years_of(odd["years"]) == [2011, 2017] and odd["tb_code"] is None    # Buddhist year, other spellings


def test_cells_of():
    big = gh.cells_of(_square(*BANG_BAN, 0.005))                 # ~1.1 km square: several ~0.1 km² cells
    assert len(big) > 5 and all(h3.get_resolution(c) == 9 for c in big)
    tiny = gh.cells_of(_square(*BANG_BAN, 0.0002))               # holds no cell centre
    assert tiny == [h3.latlng_to_cell(*BANG_BAN, 9)]
    assert gh.cells_of(None) == []


def test_build_merges_shapes_into_cells_and_subdistricts():
    shapes = [_shape([2011, 2012]), _shape([2017, 2024], half=0.003), _shape([2013], at=(14.6, 100.5), tb=None),
              _shape([2011], freq=4), _shape([])]
    cells, tambons, n = gh.build([{"features": shapes}], "14")
    assert n == {"shapes": 5, "no_years": 1, "no_tb_code": 1, "freq_mismatch": 1}
    [t] = tambons
    assert gh.years_of(t["years"]) == [2011, 2012, 2017, 2024] and t["freq"] == 4 and t["shapes"] == 3
    assert t["pv_code"] == "14" and set(t["rai_by_year"]) == {"2011", "2012", "2017", "2024"}
    assert t["rai_by_year"]["2011"] > t["rai_by_year"]["2017"] > 0                # the second shape is smaller
    by_h3 = {c["h3"]: c for c in cells}
    centre = by_h3[h3.latlng_to_cell(*BANG_BAN, 9)]
    assert gh.years_of(centre["years"]) == [2011, 2012, 2017, 2024] and centre["freq"] == 4
    assert by_h3[h3.latlng_to_cell(14.6, 100.5, 9)]["tb_code"] is None


# --- fetching ----------------------------------------------------------------------------------------

def test_download_pages_onto_disk_and_resumes(fast, tmp_path):
    client, seen = _gistda(fast, total=7)
    d = tmp_path / "pv14"
    got = gh.download("14", d, KEY, client)
    assert got["pages"] == 3 and got["fetched"] == 3 and got["number_matched"] == 7
    assert [int(r.url.params["offset"]) for r in seen] == [0, 3, 6]
    assert all(r.url.params["pv_idn"] == "14" and KEY not in str(r.url) for r in seen)
    text = "".join(gzip.open(p, "rt", encoding="utf-8").read() for p in sorted(d.glob("*.json.gz")))
    assert KEY not in text and '"links"' not in text

    (d / "0000003.json.gz").unlink()                          # a run stopped part-way
    client, seen = _gistda(fast, total=7)
    assert gh.download("14", d, KEY, client)["fetched"] == 1 and [r.url.params["offset"] for r in seen] == ["3"]
    assert gh.download("14", d, None)["fetched"] == 0         # everything on disk: no key, no client needed


def test_download_waits_out_a_rate_limit(fast, tmp_path):
    client, seen = _gistda(fast, total=7, statuses=[429, 429])
    assert gh.download("14", tmp_path / "pv14", KEY, client)["fetched"] == 3 and len(seen) == 5
    client, _ = _gistda(fast, total=7, statuses=[429] * gh.RATE_LIMIT_TRIES)
    with pytest.raises(gh.GistdaError, match="rate-limited"):
        gh.download("14", tmp_path / "pv10", KEY, client)


def test_download_refuses_an_ignored_filter_or_a_short_province(fast, tmp_path):
    client, _ = _gistda({0: [_shape([2011], pv=10)]}, total=1)
    with pytest.raises(gh.GistdaError, match="filter looks ignored"):
        gh.download("14", tmp_path / "a", KEY, client)
    client, _ = _gistda({0: fast[0], 3: fast[3]}, total=9)
    with pytest.raises(gh.GistdaError, match="ended at 6 of its 9"):
        gh.download("14", tmp_path / "b", KEY, client)
    client, _ = _gistda({0: fast[0], 3: fast[0]}, total=9)
    with pytest.raises(gh.GistdaError, match="offset looks ignored"):
        gh.download("14", tmp_path / "c", KEY, client)


# --- OBS ---------------------------------------------------------------------------------------------

class FakeObs:
    """listObjects / putFile as esdk-obs-python answers them (resp.status, resp.body), over a dict."""

    def __init__(self, objects=None, fail_put=False):
        self.objects, self.fail_put, self.puts = dict(objects or {}), fail_put, []

    def listObjects(self, bucket, prefix=None, marker=None, max_keys=None):
        keys = sorted(k for k in self.objects if k.startswith(prefix) and (marker is None or k > marker))
        page = keys[:2]                                       # tiny pages, to exercise the marker
        contents = [SimpleNamespace(key=k, size=self.objects[k]) for k in page]
        return SimpleNamespace(status=200, body=SimpleNamespace(contents=contents, is_truncated=len(keys) > 2,
                                                                next_marker=None))

    def putFile(self, bucket, key, path):
        if self.fail_put:
            return SimpleNamespace(status=403, errorCode="AccessDenied", errorMessage="no agency", body=None)
        self.puts.append(key)
        self.objects[key] = Path(path).stat().st_size
        return SimpleNamespace(status=200, body=None)


def test_upload_sends_only_missing_or_changed_pages(fast, tmp_path):
    d = tmp_path / "pv14"
    gh.download("14", d, KEY, _gistda(fast, total=7)[0])
    obs = FakeObs()
    assert gh.upload("14", d, obs, "namma") == 3
    assert obs.puts == [f"gistda/flood-freq/pv14/{o:07d}.json.gz" for o in (0, 3, 6)]
    assert gh.upload("14", d, obs, "namma") == 0                  # all there, across two list pages
    obs.objects["gistda/flood-freq/pv14/0000003.json.gz"] = 1     # a refetched page differs
    assert gh.upload("14", d, obs, "namma") == 1
    with pytest.raises(gh.GistdaError, match="AccessDenied"):
        gh.upload("14", d, FakeObs(fail_put=True), "namma")


def test_obs_client_uses_the_vm_agency(monkeypatch):
    monkeypatch.delenv("OBS_BUCKET", raising=False)
    assert gh.obs_client() == (None, None)
    pytest.importorskip("obs")
    monkeypatch.setenv("OBS_BUCKET", "namma")
    monkeypatch.delenv("OBS_ENDPOINT", raising=False)
    client, bucket = gh.obs_client()
    assert bucket == "namma" and client.security_provider_policy == "ECS" and client.ssl_verify.endswith(".pem")
    assert client.server == "obs.ap-southeast-2.myhuaweicloud.com"


# --- storing (Postgres) ------------------------------------------------------------------------------

def test_run_province_stores_and_rebuilds_from_disk(api, fast):
    _, conn = api
    client, _ = _gistda(fast, total=7)
    run = gh.run_province(conn, "14", KEY, client)
    assert (run["shapes"], run["tambons"], run["fetched"]) == (7, 7, 3)
    assert conn.execute("SELECT count(*) FROM flood_history_cells WHERE pv_code = '14'").fetchone()[0] == run["cells"]
    assert gh.show(conn, "t3") == ["ต.t3 อ.บางบาล จ.พระนครศรีอยุธยา: 2011, 2017 (2 ปี)"]

    again = gh.run_province(conn, "14")                       # all pages on disk: no GISTDA call, same rows
    assert again["fetched"] == 0 and again["cells"] == run["cells"]
    row = conn.execute("SELECT number_matched, shapes, pages, download_s IS NOT NULL FROM flood_history_runs").fetchone()
    assert row == (7, 7, 3, True)                             # the first run's download time is kept
    assert conn.execute("SELECT count(*) FROM flood_history_tambon").fetchone()[0] == 7
