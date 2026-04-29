"""
synastry_chart.py — Streamlit app that takes two persons' birth details
(DOB, time, place) and produces:

  • Each person's Lagna (ascendant sign) and Rasi (moon sign)
  • Chart 1: Person 1's Lagna as House 1 — both persons' planets overlaid
             with [1] / [2] suffix.
  • Chart 2: Person 2's Lagna as House 1 — same overlay.

Aspects from the seven non-shadow planets are shown with offset percentages.
Rahu/Ketu do NOT cast aspects (matching `daily_nps.py` behavior).

Run:
    streamlit run Server/logic/synastry_chart.py
"""

from __future__ import annotations

import json
from datetime import datetime, time as dtime
from math import atan2, cos, degrees, radians, sin, tan

import pandas as pd
import pytz
import streamlit as st
import swisseph as swe
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim
from timezonefinder import TimezoneFinder


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

SIGN_NAMES = [
    'Aries', 'Taurus', 'Gemini', 'Cancer', 'Leo', 'Virgo',
    'Libra', 'Scorpio', 'Sagittarius', 'Capricorn', 'Aquarius', 'Pisces',
]

PLANET_ORDER = ['Sun', 'Moon', 'Mercury', 'Venus', 'Mars',
                'Jupiter', 'Saturn', 'Rahu', 'Ketu']

PLANET_IDS = {
    'Sun': swe.SUN, 'Moon': swe.MOON, 'Mercury': swe.MERCURY,
    'Venus': swe.VENUS, 'Mars': swe.MARS, 'Jupiter': swe.JUPITER,
    'Saturn': swe.SATURN, 'Rahu': swe.MEAN_NODE,
}

SIGN_LORDS = {
    'Aries': 'Mars', 'Taurus': 'Venus', 'Gemini': 'Mercury',
    'Cancer': 'Moon', 'Leo': 'Sun', 'Virgo': 'Mercury',
    'Libra': 'Venus', 'Scorpio': 'Mars', 'Sagittarius': 'Jupiter',
    'Capricorn': 'Saturn', 'Aquarius': 'Saturn', 'Pisces': 'Jupiter',
}

# House offsets each planet aspects (drishti). Rahu/Ketu omitted on purpose.
ASPECT_HOUSES = {
    'Sun':     [7],
    'Moon':    [7],
    'Mercury': [7],
    'Venus':   [7],
    'Mars':    [4, 7, 8],
    'Jupiter': [5, 7, 9],
    'Saturn':  [3, 7, 10],
}

# Aspect-percentage rules (mirrors logic.py _PROMPT_ASPECT_RULES).
# Key = aspecting planet, value = {offset_house: percentage}.
ASPECT_PCT_RULES = {
    'Saturn':  {3: 25,  7: 100, 10: 75},
    'Mars':    {4: 40,  7: 100, 8:  25},
    'Sun':     {7: 50},
    'Jupiter': {5: 100, 7: 100, 9:  100},
    'Venus':   {7: 100},
    'Mercury': {7: 100},
    'Moon':    {4: 25,  6: 50,  7:  100, 8: 50, 10: 25},
}

CITIES_FALLBACK = {
    'Chennai':   {'lat': 13.08, 'lon': 80.27},
    'Mumbai':    {'lat': 19.07, 'lon': 72.88},
    'Delhi':     {'lat': 28.61, 'lon': 77.23},
    'Bangalore': {'lat': 12.97, 'lon': 77.59},
    'Kolkata':   {'lat': 22.57, 'lon': 88.36},
    'Hyderabad': {'lat': 17.39, 'lon': 78.49},
}

# ──────────────────────────────────────────────────────────────────────
# Status / Parivardhana tables (mirrors daily_nps.py / logic.py)
# ──────────────────────────────────────────────────────────────────────

STATUS_DATA = {
    'Sun':     {'Uchcham': 'Aries',      'Moolathirigonam': None,          'Aatchi': 'Leo',        'Neecham': 'Libra'},
    'Moon':    {'Uchcham': 'Taurus',     'Moolathirigonam': None,          'Aatchi': 'Cancer',     'Neecham': 'Scorpio'},
    'Jupiter': {'Uchcham': 'Cancer',     'Moolathirigonam': 'Sagittarius', 'Aatchi': 'Pisces',     'Neecham': 'Capricorn'},
    'Venus':   {'Uchcham': 'Pisces',     'Moolathirigonam': 'Libra',       'Aatchi': 'Taurus',     'Neecham': 'Virgo'},
    'Mercury': {'Uchcham': 'Virgo',      'Moolathirigonam': None,          'Aatchi': 'Gemini',     'Neecham': 'Pisces'},
    'Mars':    {'Uchcham': 'Capricorn',  'Moolathirigonam': 'Aries',       'Aatchi': 'Scorpio',    'Neecham': 'Cancer'},
    'Saturn':  {'Uchcham': 'Libra',      'Moolathirigonam': 'Aquarius',    'Aatchi': 'Capricorn',  'Neecham': 'Aries'},
}

# Uchcham sign for each planet — used for Neechabhangam co-occupant check
_UCHCHA_SIGN = {p: v['Uchcham'] for p, v in STATUS_DATA.items()}


def _planet_status(planet: str, sign: str) -> str:
    """Return base status string for a planet in the given sign."""
    m = STATUS_DATA.get(planet)
    if not m:
        return '-'
    if sign == m['Uchcham']:
        return 'Uchcham'
    if sign == m['Neecham']:
        return 'Neecham'
    if m['Moolathirigonam'] and sign == m['Moolathirigonam']:
        return 'Moolathirigonam'
    if sign == m['Aatchi']:
        return 'Aatchi'
    return '-'


def compute_statuses(planets: dict) -> dict[str, str]:
    """Return {planet: final_status} including Neechabhangam upgrades."""
    base = {p: _planet_status(p, planets[p]['sign']) for p in PLANET_ORDER
            if p not in ('Rahu', 'Ketu')}
    base['Rahu'] = '-'
    base['Ketu'] = '-'

    updated = dict(base)
    for p, status in base.items():
        if status != 'Neecham':
            continue
        sign = planets[p]['sign']
        house_lord = SIGN_LORDS[sign]
        lord_status = base.get(house_lord, '-')
        if lord_status in ('Uchcham', 'Moolathirigonam', 'Aatchi'):
            updated[p] = 'Neechabhangam'
            continue
        # Co-occupant with Uchcham or Moolathirigonam planet in same sign
        for other, other_sign in ((q, planets[q]['sign']) for q in PLANET_ORDER):
            if other == p:
                continue
            if other_sign == sign and base.get(other, '-') in ('Uchcham', 'Moolathirigonam'):
                updated[p] = 'Neechabhangam'
                break

    return updated


def compute_parivardhana(planets: dict) -> dict[str, str]:
    """Return {planet: exchange_partner} for mutual sign-lord pairs."""
    pmap = {}
    for pa in ('Sun', 'Moon', 'Mars', 'Mercury', 'Jupiter', 'Venus', 'Saturn'):
        sg_a   = planets[pa]['sign']
        lord_a = SIGN_LORDS[sg_a]
        if lord_a not in planets:
            continue
        sg_lord   = planets[lord_a]['sign']
        lord_back = SIGN_LORDS[sg_lord]
        if lord_back == pa and pa != lord_a:
            pmap[pa] = lord_a
    return pmap


# ──────────────────────────────────────────────────────────────────────
# Geocoding / timezone helpers
# ──────────────────────────────────────────────────────────────────────

@st.cache_resource
def _get_geocoder():
    geolocator = Nominatim(user_agent="synastry_chart_app")
    return RateLimiter(geolocator.geocode, min_delay_seconds=1)


@st.cache_resource
def _get_timezone_finder():
    return TimezoneFinder()


def resolve_place(place: str):
    """Return (lat, lon, display_name) for a place string."""
    if place in CITIES_FALLBACK:
        c = CITIES_FALLBACK[place]
        return c['lat'], c['lon'], place
    try:
        loc = _get_geocoder()(place)
    except Exception:
        loc = None
    if loc is None:
        raise ValueError(f"Could not geocode place: {place!r}")
    return loc.latitude, loc.longitude, loc.address


def tz_for_latlon(lat: float, lon: float):
    tf = _get_timezone_finder()
    name = tf.timezone_at(lng=lon, lat=lat)
    if not name:
        return pytz.UTC
    return pytz.timezone(name)


# ──────────────────────────────────────────────────────────────────────
# Astronomy
# ──────────────────────────────────────────────────────────────────────

def _datetime_to_jd_utc(dt_utc: datetime) -> float:
    y, m, d = dt_utc.year, dt_utc.month, dt_utc.day
    h = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
    cal = (swe.GREG_CAL
           if (y > 1582 or (y == 1582 and (m > 10 or (m == 10 and d >= 15))))
           else swe.JUL_CAL)
    return swe.julday(y, m, d, h, cal)


def compute_chart(date_str: str, time_str: str, place: str) -> dict:
    """Compute sidereal planet longitudes + ascendant for one person."""
    lat, lon, addr = resolve_place(place)
    tz = tz_for_latlon(lat, lon)

    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    local_dt = tz.localize(naive)
    utc_dt   = local_dt.astimezone(pytz.UTC)

    swe.set_sid_mode(swe.SIDM_LAHIRI)
    jd = _datetime_to_jd_utc(utc_dt)

    flags = swe.FLG_SIDEREAL | swe.FLG_SWIEPH
    longitudes = {}
    for name, pid in PLANET_IDS.items():
        result, _flag = swe.calc_ut(jd, pid, flags)
        longitudes[name] = result[0] % 360.0
    longitudes['Ketu'] = (longitudes['Rahu'] + 180.0) % 360.0

    # Ascendant: tropical from swe.houses minus Lahiri ayanamsa
    cusps, asmc = swe.houses(jd, lat, lon, b'P')
    asc_trop = asmc[0]
    ayan     = swe.get_ayanamsa_ut(jd)
    asc_sid  = (asc_trop - ayan) % 360.0

    planets = {p: {'longitude': longitudes[p],
                   'sign': SIGN_NAMES[int(longitudes[p] / 30)]}
               for p in PLANET_ORDER}

    return {
        'place':      addr,
        'lat':        lat,
        'lon':        lon,
        'tz':         str(tz),
        'local_dt':   local_dt.isoformat(),
        'utc_dt':     utc_dt.isoformat(),
        'jd':         jd,
        'asc_long':   asc_sid,
        'asc_sign':   SIGN_NAMES[int(asc_sid / 30)],
        'moon_sign':  planets['Moon']['sign'],
        'planets':    planets,
    }


# ──────────────────────────────────────────────────────────────────────
# Combined house table (P1 + P2 overlaid on a chosen lagna)
# ──────────────────────────────────────────────────────────────────────

def _tag(planet: str, who: int) -> str:
    return f"{planet}[{who}]"


def build_combined_house_table(p1: dict, p2: dict, lagna_sign: str) -> pd.DataFrame:
    """Build a 12-house table where each house = lagna-relative position;
    occupants and aspects show planets from BOTH persons with [1]/[2] tags."""
    lagna_idx = SIGN_NAMES.index(lagna_sign)
    house_sign = {n: SIGN_NAMES[(lagna_idx + n - 1) % 12] for n in range(1, 13)}
    sign_house = {s: n for n, s in house_sign.items()}

    occupants = {n: [] for n in range(1, 13)}
    aspects   = {n: [] for n in range(1, 13)}  # list of (planet_name, who, pct or None)

    for who, person in ((1, p1), (2, p2)):
        for p in PLANET_ORDER:
            s = person['planets'][p]['sign']
            h = sign_house[s]
            occupants[h].append(_tag(p, who))

            # Rahu/Ketu skipped (no aspects)
            if p in ('Rahu', 'Ketu'):
                continue
            for off in ASPECT_HOUSES.get(p, [7]):
                target = ((h - 1 + off - 1) % 12) + 1
                pct = ASPECT_PCT_RULES.get(p, {}).get(off)
                aspects[target].append((p, who, pct))

    rows = []
    for n in range(1, 13):
        sg = house_sign[n]

        if n == 1:
            occ_str = "Asc" + ((" + " + ", ".join(occupants[n])) if occupants[n] else "")
        else:
            occ_str = ", ".join(occupants[n]) if occupants[n] else "Empty"

        if aspects[n]:
            asp_strs = []
            for ap, who, pct in aspects[n]:
                tag = _tag(ap, who)
                asp_strs.append(f"{tag}({pct}%)" if pct is not None else tag)
            asp_str = ("Aspects from " if len(asp_strs) > 1 else "Aspect from ") \
                      + ", ".join(asp_strs)
        else:
            asp_str = "No Aspects"

        lord = SIGN_LORDS[sg]
        # Lord placement: use Person 1's chart for Chart-1, Person 2's for Chart-2.
        # We don't know which here, so report both: e.g. "Mercury (P1: H8, P2: H3)".
        lord_p1_house = sign_house.get(p1['planets'][lord]['sign'], '-')
        lord_p2_house = sign_house.get(p2['planets'][lord]['sign'], '-')

        lord_str = (f"Lord: {lord} (P1: H{lord_p1_house}, P2: H{lord_p2_house})")

        rows.append({
            'House':    f"House {n} ({sg})",
            'Contains': occ_str,
            'Aspects':  asp_str,
            'Lord':     lord_str,
        })

    return pd.DataFrame(rows)


def build_chart_text(p1: dict, p2: dict, lagna_sign: str, owner: int) -> str:
    """Return the chart as a plain text block matching the requested format."""
    lagna_idx  = SIGN_NAMES.index(lagna_sign)
    house_sign = {n: SIGN_NAMES[(lagna_idx + n - 1) % 12] for n in range(1, 13)}
    sign_house = {s: n for n, s in house_sign.items()}

    occupants = {n: [] for n in range(1, 13)}
    aspects   = {n: [] for n in range(1, 13)}

    for who, person in ((1, p1), (2, p2)):
        for p in PLANET_ORDER:
            s = person['planets'][p]['sign']
            h = sign_house[s]
            occupants[h].append(_tag(p, who))
            if p in ('Rahu', 'Ketu'):
                continue
            for off in ASPECT_HOUSES.get(p, [7]):
                target = ((h - 1 + off - 1) % 12) + 1
                pct = ASPECT_PCT_RULES.get(p, {}).get(off)
                aspects[target].append((p, who, pct))

    owner_person = p1 if owner == 1 else p2
    lines = [f"=== CHART (House 1 = Person {owner}'s Lagna: {lagna_sign}) ==="]
    for n in range(1, 13):
        sg = house_sign[n]

        if n == 1:
            occ_str = "Contains Asc" + ((" + " + ", ".join(occupants[n])) if occupants[n] else "")
        else:
            occ_str = ("Contains " + ", ".join(occupants[n])) if occupants[n] else "Empty"

        if aspects[n]:
            asp_parts = []
            for ap, who, pct in aspects[n]:
                tag = _tag(ap, who)
                asp_parts.append(f"{tag}({pct}%)" if pct is not None else tag)
            asp_str = ("Aspects from " if len(asp_parts) > 1 else "Aspect from ") \
                      + ", ".join(asp_parts)
        else:
            asp_str = "No Aspects"

        lord = SIGN_LORDS[sg]
        lord_house = sign_house.get(owner_person['planets'][lord]['sign'], '-')
        lord_str = f"Lord: {lord} (placed in House {lord_house})"

        lines.append(f"House {n} ({sg}): {occ_str} | {asp_str} | {lord_str}")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Streamlit UI
# ──────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────
# Export text builder
# ──────────────────────────────────────────────────────────────────────

def build_export_text(p1: dict, p2: dict,
                      p1_name: str, p2_name: str,
                      p1_statuses: dict, p2_statuses: dict,
                      p1_pari: dict, p2_pari: dict) -> str:
    """Build the full copyable export string."""
    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────────────
    lines.append("=== SYNASTRY CHART EXPORT ===")
    lines.append("")

    # ── Lagna & Rasi summary ────────────────────────────────────────
    lines.append(f"Person 1 ({p1_name})")
    lines.append(f"  Lagna : {p1['asc_sign']}")
    lines.append(f"  Rasi  : {p1['moon_sign']}")
    lines.append("")
    lines.append(f"Person 2 ({p2_name})")
    lines.append(f"  Lagna : {p2['asc_sign']}")
    lines.append(f"  Rasi  : {p2['moon_sign']}")
    lines.append("")

    # ── Planetary positions for each person ─────────────────────────
    for who, label, person, statuses, pari in (
        (1, p1_name, p1, p1_statuses, p1_pari),
        (2, p2_name, p2, p2_statuses, p2_pari),
    ):
        lines.append(f"--- Person {who} ({label}) Planetary Positions ---")
        lines.append(f"Asc: Sign: {person['asc_sign']} | Deg: {person['asc_long']:.2f}")
        for p in PLANET_ORDER:
            d     = person['planets'][p]
            parts = [f"Sign: {d['sign']}", f"Deg: {d['longitude']:.2f}"]

            st_val = statuses.get(p, '-')
            if st_val and st_val != '-':
                parts.append(f"Status: {st_val}")

            if p in pari:
                partner  = pari[p]
                h_self   = SIGN_NAMES.index(d['sign']) + 1          # sign index as house proxy
                h_partner = SIGN_NAMES.index(person['planets'][partner]['sign']) + 1
                parts.append(f"Parivardhana: {partner}")

            lines.append(f"{p}: {' | '.join(parts)}")
        lines.append("")

    # ── Chart 1 (P1 Lagna as House 1) ───────────────────────────────
    lines.append(build_chart_text(p1, p2, p1['asc_sign'], owner=1))
    lines.append("")

    # ── Chart 2 (P2 Lagna as House 1) ───────────────────────────────
    lines.append(build_chart_text(p1, p2, p2['asc_sign'], owner=2))
    lines.append("")

    return "\n".join(lines)


def _person_input(label: str, default_place: str, key_prefix: str):
    st.markdown(f"### {label}")
    name = st.text_input("Name", value=label, key=f"{key_prefix}_name")
    col1, col2 = st.columns(2)
    with col1:
        date_val = st.date_input("Date of Birth",
                                 value=datetime(1995, 1, 1).date(),
                                 min_value=datetime(1900, 1, 1).date(),
                                 max_value=datetime(2100, 12, 31).date(),
                                 format="DD/MM/YYYY",
                                 key=f"{key_prefix}_dob")
    with col2:
        time_val = st.time_input("Time of Birth",
                                 value=dtime(12, 0),
                                 key=f"{key_prefix}_tob")
    place_val = st.text_input("Place of Birth", value=default_place,
                              key=f"{key_prefix}_pob")
    return {
        'name':  name,
        'date':  date_val.strftime("%Y-%m-%d"),
        'time':  time_val.strftime("%H:%M"),
        'place': place_val,
    }


def _run_app():
    st.set_page_config(page_title="Synastry Chart", layout="wide")
    st.title("Synastry Chart — Two-Person Overlay")
    st.caption(
        "Enter both persons' birth details. The app computes each person's "
        "Lagna and Rasi (moon-sign), then draws two charts: one with "
        "Person 1's Lagna as House 1, the other with Person 2's Lagna as "
        "House 1. Both charts overlay all 18 planets with [1] / [2] tags."
    )

    col_a, col_b = st.columns(2)
    with col_a:
        p1_in = _person_input("Person 1", "Chennai", "p1")
    with col_b:
        p2_in = _person_input("Person 2", "Mumbai", "p2")

    if not st.button("Compute Synastry Charts", type="primary"):
        st.info("Fill in both persons' details and click the button.")
        return

    with st.spinner("Geocoding and computing both charts..."):
        try:
            p1 = compute_chart(p1_in['date'], p1_in['time'], p1_in['place'])
            p2 = compute_chart(p2_in['date'], p2_in['time'], p2_in['place'])
        except Exception as exc:
            st.error(f"Computation failed: {exc}")
            return

    # ── Summary ─────────────────────────────────────────────────────
    st.subheader("Summary")
    summary = pd.DataFrame([
        {'Person': p1_in['name'],
         'Lagna': p1['asc_sign'],
         'Rasi (Moon)': p1['moon_sign'],
         'Place': p1['place'],
         'Local Birth': p1['local_dt'],
         'TZ': p1['tz']},
        {'Person': p2_in['name'],
         'Lagna': p2['asc_sign'],
         'Rasi (Moon)': p2['moon_sign'],
         'Place': p2['place'],
         'Local Birth': p2['local_dt'],
         'TZ': p2['tz']},
    ])
    st.dataframe(summary, use_container_width=True, hide_index=True)

    st.info(
        "**Notation:**  `Jupiter[1]` = Person 1's Jupiter, "
        "`Jupiter[2]` = Person 2's Jupiter. Same convention applies to all "
        "planets. Rahu and Ketu do not cast aspects."
    )

    # ── Per-person planet positions ─────────────────────────────────
    with st.expander("Planet positions (both persons)"):
        rows = []
        for who, label, person in ((1, p1_in['name'], p1), (2, p2_in['name'], p2)):
            rows.append({'Who': f"P{who} — {label}", 'Body': 'Asc',
                         'Sign': person['asc_sign'],
                         'Long°': round(person['asc_long'], 2)})
            for p in PLANET_ORDER:
                rows.append({'Who': f"P{who} — {label}", 'Body': p,
                             'Sign': person['planets'][p]['sign'],
                             'Long°': round(person['planets'][p]['longitude'], 2)})
        st.dataframe(pd.DataFrame(rows),
                     use_container_width=True, hide_index=True)

    # ── Chart 1 ─────────────────────────────────────────────────────
    st.subheader(f"Chart 1 — House 1 = Person 1's Lagna ({p1['asc_sign']})")
    df1 = build_combined_house_table(p1, p2, p1['asc_sign'])
    st.dataframe(df1, use_container_width=True, hide_index=True)

    txt1 = build_chart_text(p1, p2, p1['asc_sign'], owner=1)
    st.download_button(
        "⬇ Download Chart 1 (.txt)",
        data=txt1,
        file_name=f"chart1_P1_{p1['asc_sign']}.txt",
        mime="text/plain",
        key="dl_chart1",
    )
    with st.expander("Preview Chart 1 text"):
        st.text(txt1)

    # ── Chart 2 ─────────────────────────────────────────────────────
    st.subheader(f"Chart 2 — House 1 = Person 2's Lagna ({p2['asc_sign']})")
    df2 = build_combined_house_table(p1, p2, p2['asc_sign'])
    st.dataframe(df2, use_container_width=True, hide_index=True)

    txt2 = build_chart_text(p1, p2, p2['asc_sign'], owner=2)
    st.download_button(
        "⬇ Download Chart 2 (.txt)",
        data=txt2,
        file_name=f"chart2_P2_{p2['asc_sign']}.txt",
        mime="text/plain",
        key="dl_chart2",
    )
    with st.expander("Preview Chart 2 text"):
        st.text(txt2)

    # ── Combined Export ──────────────────────────────────────────────
    st.divider()
    st.subheader("Export — Combined Chart Data")
    st.caption(
        "Full export: Lagna, Rasi, planetary positions (with Status and "
        "Parivardhana), and both house charts. Copy from the box or download."
    )

    p1_statuses = compute_statuses(p1['planets'])
    p2_statuses = compute_statuses(p2['planets'])
    p1_pari     = compute_parivardhana(p1['planets'])
    p2_pari     = compute_parivardhana(p2['planets'])

    export_txt = build_export_text(
        p1, p2,
        p1_in['name'], p2_in['name'],
        p1_statuses, p2_statuses,
        p1_pari, p2_pari,
    )

    st.text_area("Copy Export", value=export_txt, height=400,
                 label_visibility="collapsed")
    st.download_button(
        "⬇ Download Export (.txt)",
        data=export_txt,
        file_name=f"synastry_{p1['asc_sign']}_{p2['asc_sign']}.txt",
        mime="text/plain",
        key="dl_export",
    )

    try:
        import streamlit.runtime.scriptrunner as _ssr  # noqa: F401
        _run_app()
    except Exception:
        # Fallback CLI for quick smoke testing.
        import json as _json
        demo_p1 = compute_chart("1995-01-01", "12:00", "Chennai")
        demo_p2 = compute_chart("1996-06-15", "08:30", "Mumbai")
        print(_json.dumps({'p1_lagna': demo_p1['asc_sign'],
                           'p1_rasi':  demo_p1['moon_sign'],
                           'p2_lagna': demo_p2['asc_sign'],
                           'p2_rasi':  demo_p2['moon_sign']},
                          indent=2))
        print()
        print(build_chart_text(demo_p1, demo_p2, demo_p1['asc_sign'], owner=1))
        print()
        print(build_chart_text(demo_p1, demo_p2, demo_p2['asc_sign'], owner=2))
