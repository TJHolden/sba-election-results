/* State-feed adapter for the SBA election-night tracker.
 *
 * civicAPI stopped answering on election night, so the tracker had no data.
 * This serves it the same shape it already expects, built from results.json —
 * the mirror of each wired state's own election authority, refreshed every two
 * minutes by a GitHub Action. The tracker is unchanged; it still believes it is
 * reading civicAPI, so the map, tabs, SBA list and projections work as built.
 *
 * States that are not wired up yet simply have no races here. The tracker shows
 * them empty, which is true, rather than showing stale or invented numbers.
 */
(function () {
  "use strict";

  var RESULTS = "results.json";
  var TTL = 25000;

  var nativeFetch = window.fetch.bind(window);
  var cache = null, cacheAt = 0, inflight = null;

  function raceId(name) {
    var h = 2166136261;
    for (var i = 0; i < name.length; i++) {
      h ^= name.charCodeAt(i);
      h = (h * 16777619) >>> 0;
    }
    return 900000000 + (h % 90000000);
  }

  var PARTY_FULL = {
    R: "Republican", D: "Democratic", L: "Libertarian", I: "Independent",
    G: "Green", C: "Constitution", RF: "Reform", WF: "Working Families"
  };

  var STATE_NAMES = {
    AL: "Alabama", AZ: "Arizona", GA: "Georgia", IL: "Illinois", IA: "Iowa",
    KS: "Kansas", KY: "Kentucky", MD: "Maryland", MI: "Michigan",
    NE: "Nebraska", NY: "New York", NC: "North Carolina", OH: "Ohio",
    PA: "Pennsylvania", UT: "Utah"
  };

  function toCivic(r) {
    var total = 0, i;
    for (i = 0; i < r.c.length; i++) total += r.c[i][2] || 0;

    var cands = r.c.map(function (c) {
      // A Yes/No option gets no party: the tracker discards any race where
      // every candidate's name equals its party, which is how it recognises
      // whole-chamber tallies.
      var isYN = /^(yes|no|for|against)$/i.test(String(c[0]).trim());
      return {
        name: c[0],
        party: isYN ? "" : (PARTY_FULL[c[1]] || c[1] || ""),
        votes: c[2] || 0,
        percent: total > 0 ? +((c[2] || 0) / total * 100).toFixed(2) : 0,
        winner: !!c[3],
        incumbent: !!c[4]
      };
    });

    // Prefix the state so the tracker files the race under it and strips the
    // name for display, the way civicAPI's own rows are shaped.
    var sn = STATE_NAMES[r.st] || r.st;
    var nm = String(r.n || "").trim();
    var name = nm.indexOf(sn) === 0 ? nm : sn + " " + nm;

    return {
      id: raceId(r.st + "|" + nm),
      election_name: name,
      election_type: nm,
      type: nm,
      election_scope: r.d ? "Local" : "Statewide",
      election_date: "2026-11-03T05:00:00.000Z",
      country: "US",
      province: r.st,
      district: r.d || "",
      municipality: null,
      percent_reporting: r.tu > 0 ? Math.round((r.ru / r.tu) * 100)
                                  : (total > 0 ? 100 : 0),
      registered_voters: null,
      test_data: false,
      has_breakdown: false,
      region_results: {},
      candidates: cands
    };
  }

  function load() {
    if (cache && Date.now() - cacheAt < TTL) return Promise.resolve(cache);
    if (inflight) return inflight;
    inflight = nativeFetch(RESULTS + "?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("results.json " + r.status);
        return r.json();
      })
      .then(function (d) {
        cache = (d.races || []).map(toCivic);
        cacheAt = Date.now();
        inflight = null;
        return cache;
      })
      .catch(function (e) {
        inflight = null;
        if (cache) return cache;
        throw e;
      });
    return inflight;
  }

  function json(body, status) {
    return new Response(JSON.stringify(body), {
      status: status || 200,
      headers: { "Content-Type": "application/json" }
    });
  }

  window.fetch = function (input, init) {
    var url = String((input && input.url) ? input.url : input || "");
    if (url.indexOf("civicapi.org") === -1) return nativeFetch(input, init);

    return load().then(function (races) {
      if (/\/race\/search/.test(url)) {
        var q;
        try { q = new URL(url, location.href).searchParams; } catch (e) { q = null; }
        var prov = q && q.get("province");
        var query = q && q.get("query");
        var out = races;
        if (prov) {
          var want = prov.toUpperCase();
          out = out.filter(function (r) { return r.province === want; });
        }
        if (query) {
          var needle = query.toLowerCase();
          out = out.filter(function (r) {
            return r.election_name.toLowerCase().indexOf(needle) !== -1;
          });
        }
        return json({ races: out, total: out.length });
      }
      var m = /\/race\/(\d+)/.exec(url);
      if (m) {
        for (var i = 0; i < races.length; i++) {
          if (String(races[i].id) === m[1]) return json(races[i]);
        }
        return json({ error: "not found" }, 404);
      }
      return json({ races: [], total: 0 });
    }).catch(function () {
      return json({ races: [], total: 0 });
    });
  };

  window.__STATE_FEED__ = {
    source: RESULTS,
    peek: function () { return cache; },
    states: function () {
      var s = {};
      (cache || []).forEach(function (r) { s[r.province] = (s[r.province] || 0) + 1; });
      return s;
    }
  };
})();
