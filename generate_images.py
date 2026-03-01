#!/usr/bin/python3

import asyncio
from datetime import datetime, timedelta
import json
import os
import re
from typing import Dict, List, Tuple, Any

import aiohttp

from github_stats import Stats


################################################################################
# Constants
################################################################################

HISTORY_FILE = "generated/history.json"

# Validation thresholds for detecting incomplete GitHub API data
SUSPICIOUS_DROP_THRESHOLD = 50000  # Absolute line count drop threshold
SUSPICIOUS_DROP_PERCENTAGE = 0.10  # Relative drop threshold (10%)


################################################################################
# Helper Functions
################################################################################


def generate_output_folder() -> None:
    """
    Create the output folder if it does not already exist
    """
    if not os.path.isdir("generated"):
        os.mkdir("generated")


def load_history() -> List[Dict[str, Any]]:
    """
    Load previously saved snapshots from the history file.
    :return: list of snapshot dicts, sorted by date
    """
    if os.path.isfile(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                return sorted(data, key=lambda s: s.get("date", ""))
        except (json.JSONDecodeError, IOError):
            print(f"Warning: Could not read {HISTORY_FILE}, starting fresh.")
    return []


def save_history(snapshots: List[Dict[str, Any]]) -> None:
    """
    Persist snapshot list to disk as JSON.
    """
    generate_output_folder()
    with open(HISTORY_FILE, "w") as f:
        json.dump(snapshots, f, indent=2, ensure_ascii=False)


def validate_snapshot(
    current: Dict[str, Any], previous: Dict[str, Any]
) -> Tuple[bool, str]:
    """
    Validate that the current snapshot has complete data by comparing to previous.
    Returns (is_valid, reason).
    
    A snapshot is considered suspicious if:
    - Lines added/deleted dropped by more than 50,000 (likely incomplete API data)
    - The drop is more than 10% of the previous value
    """
    if not previous:
        # No previous data to compare against
        return True, "No previous snapshot to compare"
    
    curr_lines = current.get("lines_added", 0) + current.get("lines_deleted", 0)
    prev_lines = previous.get("lines_added", 0) + previous.get("lines_deleted", 0)
    
    # Allow for small variations due to deletions/force pushes
    if curr_lines < prev_lines:
        diff = prev_lines - curr_lines
        # If the drop is significant, it's suspicious
        if diff > SUSPICIOUS_DROP_THRESHOLD or (
            prev_lines > 0 and diff / prev_lines > SUSPICIOUS_DROP_PERCENTAGE
        ):
            return False, (
                f"Suspicious drop in line count: {prev_lines:,} -> {curr_lines:,} "
                f"(diff: {diff:,}). This likely indicates incomplete GitHub API data."
            )
    
    return True, "Validation passed"


def backfill_from_api_data(
    snapshot: Dict[str, Any],
    existing_dates: set,
) -> List[Dict[str, Any]]:
    """
    Generate synthetic historical snapshots from the weekly lines-changed data
    and contributions-by-year data that the GitHub API already provides.
    This fills in the past so that the first run already has a populated chart.

    :param snapshot: the current full snapshot containing lines_changed_by_week
                     and contributions_by_year
    :param existing_dates: set of date strings already present in history
    :return: list of synthetic monthly snapshots
    """
    backfilled: List[Dict[str, Any]] = []

    weekly = snapshot.get("lines_changed_by_week", {})
    contribs_by_year = snapshot.get("contributions_by_year", {})
    languages = snapshot.get("languages", {})

    # Aggregate weekly data into monthly buckets
    monthly_adds: Dict[str, int] = {}
    monthly_dels: Dict[str, int] = {}
    for date_str, changes in weekly.items():
        # Handle both list [a, d] and tuple (a, d) formats
        if isinstance(changes, (list, tuple)) and len(changes) >= 2:
            adds, dels = int(changes[0]), int(changes[1])
        else:
            continue
        month_key = date_str[:7]  # YYYY-MM
        monthly_adds[month_key] = monthly_adds.get(month_key, 0) + adds
        monthly_dels[month_key] = monthly_dels.get(month_key, 0) + dels

    all_months = sorted(set(monthly_adds.keys()) | set(monthly_dels.keys()))

    # Count repos by tracking the earliest week per repo from the weekly data.
    # Since we don't have per-repo info here, we approximate repo_count as
    # growing linearly from 1 to the current count over the observed months.
    total_repo_count = snapshot.get("repo_count", 0)
    num_months = len(all_months)

    # Compute cumulative lines for running totals
    cumulative_adds = 0
    cumulative_dels = 0

    for month_idx, month in enumerate(all_months):
        # Use last day of month as the snapshot date
        synth_date = f"{month}-28"
        if synth_date in existing_dates:
            cumulative_adds += monthly_adds.get(month, 0)
            cumulative_dels += monthly_dels.get(month, 0)
            continue

        cumulative_adds += monthly_adds.get(month, 0)
        cumulative_dels += monthly_dels.get(month, 0)

        year_str = month[:4]
        year_contribs = contribs_by_year.get(year_str, 0)

        # Approximate repo count growing over time
        approx_repos = max(
            1, int(total_repo_count * (month_idx + 1) / num_months)
        ) if num_months > 0 else total_repo_count

        backfilled.append({
            "date": synth_date,
            "synthetic": True,
            "stargazers": snapshot.get("stargazers", 0),
            "forks": snapshot.get("forks", 0),
            "total_contributions": year_contribs,
            "repo_count": approx_repos,
            "lines_added": cumulative_adds,
            "lines_deleted": cumulative_dels,
            "lines_added_month": monthly_adds.get(month, 0),
            "lines_deleted_month": monthly_dels.get(month, 0),
            "languages": languages,
            "contributions_by_year": contribs_by_year,
        })

    return backfilled


################################################################################
# Individual Image Generation Functions
################################################################################


async def generate_overview(s: Stats) -> None:
    """
    Generate an SVG badge with summary statistics
    :param s: Represents user's GitHub statistics
    """
    with open("templates/overview.svg", "r") as f:
        output = f.read()

    output = re.sub("{{ name }}", await s.name, output)
    output = re.sub("{{ stars }}", f"{await s.stargazers:,}", output)
    output = re.sub("{{ forks }}", f"{await s.forks:,}", output)
    output = re.sub("{{ contributions }}", f"{await s.total_contributions:,}", output)
    changed = (await s.lines_changed)[0] + (await s.lines_changed)[1]
    output = re.sub("{{ lines_changed }}", f"{changed:,}", output)
    output = re.sub("{{ views }}", f"{await s.views:,}", output)
    output = re.sub("{{ repos }}", f"{len(await s.repos):,}", output)

    generate_output_folder()
    with open("generated/overview.svg", "w") as f:
        f.write(output)


async def generate_languages(s: Stats) -> None:
    """
    Generate an SVG badge with summary languages used
    :param s: Represents user's GitHub statistics
    """
    with open("templates/languages.svg", "r") as f:
        output = f.read()

    progress = ""
    lang_list = ""
    sorted_languages = sorted(
        (await s.languages).items(), reverse=True, key=lambda t: t[1].get("size")
    )
    delay_between = 150
    for i, (lang, data) in enumerate(sorted_languages):
        color = data.get("color")
        color = color if color is not None else "#000000"
        progress += (
            f'<span style="background-color: {color};'
            f'width: {data.get("prop", 0):0.3f}%;" '
            f'class="progress-item"></span>'
        )
        lang_list += f"""
<li style="animation-delay: {i * delay_between}ms;">
<svg xmlns="http://www.w3.org/2000/svg" class="octicon" style="fill:{color};"
viewBox="0 0 16 16" version="1.1" width="32" height="32"><path
fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8z"></path></svg>
<span class="lang">{lang}</span>
<span class="percent">{data.get("prop", 0):0.2f}%</span>
</li>

"""

    output = re.sub(r"{{ progress }}", progress, output)
    output = re.sub(r"{{ lang_list }}", lang_list, output)

    generate_output_folder()
    with open("generated/languages.svg", "w") as f:
        f.write(output)


async def generate_history(s: Stats) -> None:
    """
    Generate history.svg from persisted snapshots in history.json.
    On the first run, backfills historical data from the GitHub API's weekly
    contributor stats so that the chart is immediately populated.
    """
    generate_output_folder()

    # ── Load existing history & build current snapshot ────────────────────
    history = load_history()
    existing_dates = {snap.get("date") for snap in history}
    current_snapshot = await s.build_snapshot()

    # ── Backfill past data from API on first run ─────────────────────────
    if len(history) == 0:
        print("First run detected – backfilling history from API data...")
        synthetic = backfill_from_api_data(current_snapshot, existing_dates)
        history.extend(synthetic)
        existing_dates = {snap.get("date") for snap in history}

    # ── Append today's snapshot (replace if same date) ───────────────────
    today = current_snapshot["date"]
    
    # Find the most recent previous snapshot (not today)
    previous_snapshots = [snap for snap in history if snap.get("date") != today]
    previous_snapshot = previous_snapshots[-1] if previous_snapshots else None
    
    # Validate the current snapshot
    is_valid, reason = validate_snapshot(current_snapshot, previous_snapshot)
    
    if not is_valid:
        # Validation failed - skip update to prevent polluting history with bad data
        print(f"⚠️  WARNING: {reason}")
        print("   Keeping previous snapshot instead of updating with potentially incomplete data.")
        if previous_snapshot:
            print(f"   Previous valid snapshot: {previous_snapshot.get('date')}")
        # Note: We intentionally do NOT append current_snapshot here
    else:
        # Validation passed - safe to update history
        print(f"✓ Snapshot validation passed: {reason}")
        # Remove any existing snapshot for today and add the new one
        history = [snap for snap in history if snap.get("date") != today]
        history.append(current_snapshot)
    
    history.sort(key=lambda snap: snap.get("date", ""))
    
    save_history(history)

    # ── Guard: need at least 2 data points for a chart ───────────────────
    if len(history) < 2:
        print("Not enough data points for history chart yet.")
        # Write a minimal placeholder SVG
        with open("generated/history.svg", "w") as f:
            f.write(
                '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="150" '
                'style="max-width: 100%;" viewBox="0 0 1000 150">'
                '<text x="40" y="80" fill="#c9d1d9" '
                'font-family="Segoe UI, Ubuntu, Sans-Serif" font-size="22">'
                "Not enough data yet – chart will appear after the next run."
                "</text></svg>"
            )
        return

    # ── Determine top languages across all snapshots ─────────────────────
    # Include up to 8 languages: global top-8 by cumulative size, plus any
    # language that was ever in the top 5 for an individual snapshot (so that
    # language shifts over time are fully captured in the chart).
    MAX_CHART_LANGS = 8
    lang_totals: Dict[str, int] = {}
    lang_color_map: Dict[str, str] = {}
    for snap in history:
        for name, data in snap.get("languages", {}).items():
            lang_totals[name] = lang_totals.get(name, 0) + data.get("size", 0)
            if data.get("color"):
                lang_color_map[name] = data["color"]

    # Collect languages that were ever in the per-snapshot top 5
    ever_top5_langs: set = set()
    for snap in history:
        snap_langs = snap.get("languages", {})
        if snap_langs:
            snap_sorted = sorted(
                snap_langs.keys(),
                key=lambda n: snap_langs[n].get("size", 0),
                reverse=True,
            )
            ever_top5_langs.update(snap_sorted[:5])

    # Union of global top-8 and ever-top-5 languages, sorted by global total
    global_top8 = set(
        sorted(lang_totals.keys(), key=lambda n: lang_totals[n], reverse=True)[
            :MAX_CHART_LANGS
        ]
    )
    candidate_langs = global_top8 | ever_top5_langs
    top_langs = sorted(
        candidate_langs, key=lambda n: lang_totals.get(n, 0), reverse=True
    )[:MAX_CHART_LANGS]

    # ── Prepare time series ──────────────────────────────────────────────
    dates = [snap.get("date", "") for snap in history]
    n = len(dates)

    # Monthly lines changed – compute from the weekly data embedded in each
    # snapshot so that every data-point reflects actual activity instead of
    # a (possibly negative / zero) cumulative delta.
    monthly_adds_series: List[int] = []
    monthly_dels_series: List[int] = []
    monthly_lines: List[int] = []
    for snap in history:
        weekly = snap.get("lines_changed_by_week", {})
        if weekly:
            total_a = sum(
                int(v[0]) for v in weekly.values()
                if isinstance(v, (list, tuple)) and len(v) >= 2
            )
            total_d = sum(
                int(v[1]) for v in weekly.values()
                if isinstance(v, (list, tuple)) and len(v) >= 2
            )
        else:
            total_a = snap.get("lines_added", 0)
            total_d = snap.get("lines_deleted", 0)
        monthly_adds_series.append(total_a)
        monthly_dels_series.append(total_d)
        monthly_lines.append(total_a + total_d)

    # Language proportions per snapshot
    lang_series: Dict[str, List[float]] = {lang: [] for lang in top_langs}
    for snap in history:
        langs = snap.get("languages", {})
        for lang in top_langs:
            lang_series[lang].append(langs.get(lang, {}).get("prop", 0.0))

    has_lang_data = any(
        any(vals) for vals in lang_series.values()
    ) if top_langs else False

    # Stars over time
    stars_series = [snap.get("stargazers", 0) for snap in history]

    # Repo count over time
    repo_series = [snap.get("repo_count", 0) for snap in history]

    # Total contributions over time
    contribs_series = [snap.get("total_contributions", 0) for snap in history]

    # ── Forecast: dynamically determined horizon ──────────────────────────
    n_real = n  # number of actual historical data points
    forecast_dates: List[str] = []
    forecast_labels: List[str] = []
    if dates:
        # ── Linear regression helpers (no external dependencies) ──────────
        def _linreg_slope(ys: List[float]) -> float:
            """Return the OLS slope for a sequence y[0..m-1] vs x=0..m-1."""
            m = len(ys)
            if m < 2:
                return 0.0
            sx = m * (m - 1) / 2          # sum of 0..m-1
            sx2 = m * (m - 1) * (2 * m - 1) / 6
            sy = sum(ys)
            sxy = sum(i * ys[i] for i in range(m))
            denom = m * sx2 - sx * sx
            return (m * sxy - sx * sy) / denom if denom != 0 else 0.0

        # Trend-based projection using linear regression over ALL real data.
        # Keep float precision so small slopes are not truncated to zero.
        avg_delta_adds = max(0.0, _linreg_slope([float(v) for v in monthly_adds_series[:n_real]]))
        avg_delta_dels = max(0.0, _linreg_slope([float(v) for v in monthly_dels_series[:n_real]]))

        base_adds = monthly_adds_series[n_real - 1] if monthly_adds_series else 0
        base_dels = monthly_dels_series[n_real - 1] if monthly_dels_series else 0

        # Per-language trend: linear regression only over snapshots that have
        # actual language data, to avoid zeros from early snapshots skewing the slope.
        first_lang_idx = next(
            (i for i, snap in enumerate(history) if snap.get("languages")),
            n_real,
        )
        lang_trends: Dict[str, float] = {}
        for lang in top_langs:
            vals = lang_series[lang][first_lang_idx:n_real]
            lang_trends[lang] = _linreg_slope(vals) if len(vals) >= 2 else 0.0

        # Stars trend: linear regression over all real snapshots
        stars_slope = _linreg_slope([float(v) for v in stars_series[:n_real]])
        base_stars = stars_series[n_real - 1] if stars_series else 0

        # ── Dynamic forecast horizon ──────────────────────────────────────
        # Compute the average interval between real snapshots in days so that
        # slopes (which are in "per snapshot step" units) can be converted to
        # a per-day rate.
        try:
            d_first = datetime.strptime(dates[0], "%Y-%m-%d")
            d_last = datetime.strptime(dates[n_real - 1], "%Y-%m-%d")
            avg_step_days = (
                max(1.0, (d_last - d_first).days / (n_real - 1))
                if n_real > 1
                else 1.0
            )
        except ValueError:
            avg_step_days = 1.0

        # Find how many snapshot steps are required for any tracked metric
        # to change by ≥10% of its current value.  Use the shortest such
        # horizon so the chart always shows a "meaningful" change.
        MIN_FORECAST_DAYS = 14
        MAX_FORECAST_DAYS = 365
        candidate_steps: List[float] = []
        if avg_delta_adds > 0 and base_adds > 0:
            candidate_steps.append(0.10 * base_adds / avg_delta_adds)
        for lang in top_langs:
            slope = abs(lang_trends.get(lang, 0.0))
            base_val = lang_series[lang][n_real - 1] if lang_series[lang] else 0.0
            if slope > 0 and base_val > 0:
                candidate_steps.append(0.10 * base_val / slope)

        if candidate_steps:
            h1_steps = min(candidate_steps)
        else:
            # No active trend detected; fall back to the minimum horizon so
            # the chart still shows two forecast points (flat extrapolation).
            h1_steps = MIN_FORECAST_DAYS / avg_step_days

        # Clamp horizon to [MIN_FORECAST_DAYS, MAX_FORECAST_DAYS]
        h1_days = max(float(MIN_FORECAST_DAYS), h1_steps * avg_step_days)
        h1_days = min(h1_days, float(MAX_FORECAST_DAYS))
        h1_steps = h1_days / avg_step_days
        h2_steps = h1_steps * 2.0

        try:
            last_date_obj = datetime.strptime(dates[n_real - 1], "%Y-%m-%d")
            h1_date = last_date_obj + timedelta(days=int(h1_days))
            h2_date = last_date_obj + timedelta(days=int(h1_days * 2))
            forecast_dates = [h1_date.strftime("%Y-%m-%d"), h2_date.strftime("%Y-%m-%d")]
            forecast_labels = [h1_date.strftime("%b %d, %Y"), h2_date.strftime("%b %d, %Y")]
        except ValueError:
            pass

        for mult, forecast_date_str in zip([h1_steps, h2_steps], forecast_dates):
            dates.append(forecast_date_str)
            monthly_adds_series.append(max(0, int(base_adds + mult * avg_delta_adds)))
            monthly_dels_series.append(max(0, int(base_dels + mult * avg_delta_dels)))
            monthly_lines.append(monthly_adds_series[-1] + monthly_dels_series[-1])

            # Language forecast: extrapolate trend from real data, then
            # re-normalise so the proportions still sum to the same total as
            # the last real snapshot.
            raw: Dict[str, float] = {}
            for lang in top_langs:
                base_val = lang_series[lang][n_real - 1] if lang_series[lang] else 0.0
                raw[lang] = max(0.0, base_val + mult * lang_trends[lang])
            raw_total = sum(raw.values())
            real_total = sum(lang_series[lang][n_real - 1] for lang in top_langs)
            if raw_total > 0 and real_total > 0:
                scale = real_total / raw_total
                for lang in top_langs:
                    raw[lang] *= scale
            for lang in top_langs:
                lang_series[lang].append(raw.get(lang, 0.0))

            stars_series.append(max(0, int(base_stars + mult * stars_slope)))
        n = len(dates)

    # ── Chart dimensions ─────────────────────────────────────────────────
    svg_width = 1000
    svg_height = 760
    margin_top = 100
    margin_bottom = 100
    margin_left = 90
    margin_right = 220
    chart_w = svg_width - margin_left - margin_right
    chart_h_top = 240
    chart_h_bottom = 240
    gap = 70
    chart1_top = margin_top
    chart2_top = margin_top + chart_h_top + gap

    # ── Color scheme ─────────────────────────────────────────────────────
    card_bg = "#161b22"
    text_color = "#c9d1d9"
    grid_color = "#21262d"
    line_color = "#58a6ff"
    area_color = "#58a6ff"
    star_color = "#e3b341"
    add_color = "#3fb950"
    del_color = "#f85149"
    contrib_color = "#bc8cff"

    # ── SVG construction ─────────────────────────────────────────────────
    svg: List[str] = []
    svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{svg_width}" height="{svg_height}" '
        f'style="max-width: 100%;" '
        f'viewBox="0 0 {svg_width} {svg_height}">'
    )

    svg.append(f"""
<defs>
  <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="{area_color}" stop-opacity="0.35"/>
    <stop offset="100%" stop-color="{area_color}" stop-opacity="0.02"/>
  </linearGradient>
  <linearGradient id="addGrad" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="{add_color}" stop-opacity="0.35"/>
    <stop offset="100%" stop-color="{add_color}" stop-opacity="0.02"/>
  </linearGradient>
  <linearGradient id="delGrad" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="{del_color}" stop-opacity="0.25"/>
    <stop offset="100%" stop-color="{del_color}" stop-opacity="0.02"/>
  </linearGradient>
</defs>
<style>
  .title {{ font: bold 28px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .subtitle {{ font: 600 20px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.8; }}
  .axis-label {{ font: 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.7; }}
  .legend-text {{ font: 14px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .value-text {{ font: bold 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .stat-value {{ font: bold 22px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .stat-label {{ font: 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.7; }}
  @keyframes fadeIn {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
  .anim {{ animation: fadeIn 0.6s ease-in-out forwards; opacity: 0; }}
</style>
""")

    # Background
    svg.append(
        f'<rect x="0.5" y="0.5" rx="4.5" width="{svg_width - 1}" '
        f'height="{svg_height - 1}" fill="{card_bg}" stroke="{grid_color}"/>'
    )

    # Title
    svg.append(
        f'<text x="{margin_left}" y="45" class="title">Activity History</text>'
    )
    svg.append(
        f'<text x="{margin_left}" y="72" class="subtitle">'
        f'Lines added vs deleted &amp; activity trends over time</text>'
    )

    step_x = chart_w / max(n - 1, 1)

    # ── Helper: compact number formatting ───────────────────────────────
    def compact_number(val: float) -> str:
        """Format large numbers compactly (e.g. 2,836,638 → 2.8M)."""
        abs_val = abs(val)
        if abs_val >= 1_000_000:
            return f"{val / 1_000_000:,.1f}M"
        if abs_val >= 10_000:
            return f"{val / 1_000:,.0f}K"
        if abs_val >= 1_000:
            return f"{val / 1_000:,.1f}K"
        return f"{val:,.0f}"

    # ── Helper: draw grid + axis ─────────────────────────────────────────
    def draw_grid(
        top_y: float, height: float, max_val: float, label_fmt: str = ""
    ) -> None:
        num_grid = 4
        for i in range(num_grid + 1):
            y = top_y + height - (i / num_grid) * height
            val = max_val * i / num_grid
            if label_fmt:
                label_text = label_fmt.format(val)
            else:
                label_text = compact_number(val)
            svg.append(
                f'<line x1="{margin_left}" y1="{y:.1f}" '
                f'x2="{margin_left + chart_w}" y2="{y:.1f}" '
                f'stroke="{grid_color}" stroke-width="1"/>'
            )
            svg.append(
                f'<text x="{margin_left - 8}" y="{y + 4:.1f}" '
                f'text-anchor="end" class="axis-label">'
                f'{label_text}</text>'
            )

    # ── Chart 1: Lines added vs deleted (area) ─────────────────────────
    max_monthly = max(monthly_lines) if monthly_lines else 1
    if max_monthly == 0:
        max_monthly = 1

    draw_grid(chart1_top, chart_h_top, float(max_monthly))

    # Lines-added area (green)
    add_pts: List[str] = []
    for i, val in enumerate(monthly_adds_series):
        x = margin_left + i * step_x
        y = chart1_top + chart_h_top - (val / max_monthly) * chart_h_top
        add_pts.append(f"{x:.1f},{y:.1f}")

    bottom1 = chart1_top + chart_h_top
    add_area = (
        f"{margin_left:.1f},{bottom1:.1f} "
        + " ".join(add_pts)
        + f" {margin_left + (n - 1) * step_x:.1f},{bottom1:.1f}"
    )
    svg.append(
        f'<polygon points="{add_area}" fill="url(#addGrad)" '
        f'class="anim" style="animation-delay:200ms;"/>'
    )
    svg.append(
        f'<polyline points="{" ".join(add_pts[:n_real])}" fill="none" '
        f'stroke="{add_color}" stroke-width="2" stroke-linejoin="round" '
        f'class="anim" style="animation-delay:250ms;"/>'
    )
    if n > n_real:
        svg.append(
            f'<polyline points="{" ".join(add_pts[n_real - 1:])}" fill="none" '
            f'stroke="{add_color}" stroke-width="1.5" stroke-dasharray="5,4" '
            f'stroke-linejoin="round" opacity="0.6" '
            f'class="anim" style="animation-delay:250ms;"/>'
        )

    # Lines-deleted area (red)
    del_pts: List[str] = []
    for i, val in enumerate(monthly_dels_series):
        x = margin_left + i * step_x
        y = chart1_top + chart_h_top - (val / max_monthly) * chart_h_top
        del_pts.append(f"{x:.1f},{y:.1f}")

    del_area = (
        f"{margin_left:.1f},{bottom1:.1f} "
        + " ".join(del_pts)
        + f" {margin_left + (n - 1) * step_x:.1f},{bottom1:.1f}"
    )
    svg.append(
        f'<polygon points="{del_area}" fill="url(#delGrad)" '
        f'class="anim" style="animation-delay:300ms;"/>'
    )
    svg.append(
        f'<polyline points="{" ".join(del_pts[:n_real])}" fill="none" '
        f'stroke="{del_color}" stroke-width="1.5" stroke-linejoin="round" '
        f'class="anim" style="animation-delay:350ms;"/>'
    )
    if n > n_real:
        svg.append(
            f'<polyline points="{" ".join(del_pts[n_real - 1:])}" fill="none" '
            f'stroke="{del_color}" stroke-width="1" stroke-dasharray="5,4" '
            f'stroke-linejoin="round" opacity="0.6" '
            f'class="anim" style="animation-delay:350ms;"/>'
        )

    # Stars overlay (secondary axis, right side)
    max_stars = max(stars_series) if stars_series else 1
    min_stars = min(stars_series) if stars_series else 0
    star_range = max_stars - min_stars
    if star_range == 0:
        star_range = max(max_stars, 1)
        star_base = min_stars - star_range * 0.5
    else:
        star_base = min_stars
    star_pts: List[str] = []
    for i, val in enumerate(stars_series):
        x = margin_left + i * step_x
        y = chart1_top + chart_h_top - ((val - star_base) / star_range) * chart_h_top * 0.8
        star_pts.append(f"{x:.1f},{y:.1f}")
    svg.append(
        f'<polyline points="{" ".join(star_pts[:n_real])}" fill="none" '
        f'stroke="{star_color}" stroke-width="1.5" stroke-dasharray="4,3" '
        f'stroke-linejoin="round" class="anim" style="animation-delay:400ms;"/>'
    )
    if n > n_real:
        svg.append(
            f'<polyline points="{" ".join(star_pts[n_real - 1:])}" fill="none" '
            f'stroke="{star_color}" stroke-width="1" stroke-dasharray="3,5" '
            f'stroke-linejoin="round" opacity="0.5" '
            f'class="anim" style="animation-delay:400ms;"/>'
        )

    # Chart 1 label
    svg.append(
        f'<text x="{margin_left}" y="{chart1_top - 10}" '
        f'class="subtitle">Lines Added vs Deleted (per snapshot)</text>'
    )

    # ── Chart 2: Language proportions over time ────────────────────────
    # Always show language chart (with fallback message if no data)
    svg.append(
        f'<text x="{margin_left}" y="{chart2_top - 10}" '
        f'class="subtitle">Programming Language Development (%)</text>'
    )
    
    if has_lang_data and top_langs:
        draw_grid(chart2_top, chart_h_bottom, 100.0, "{:.0f}%")

        # Build stacked area chart showing language proportion changes over time
        stacked_bottoms = [0.0] * n
        for lang_idx, lang in enumerate(top_langs):
            color = lang_color_map.get(lang, "#888888")
            area_pts_top: List[str] = []
            area_pts_bottom: List[str] = []

            for i in range(n):
                x = margin_left + i * step_x
                val = lang_series[lang][i]
                y_bottom = (
                    chart2_top
                    + chart_h_bottom
                    - (stacked_bottoms[i] / 100.0) * chart_h_bottom
                )
                y_top = (
                    chart2_top
                    + chart_h_bottom
                    - ((stacked_bottoms[i] + val) / 100.0) * chart_h_bottom
                )
                area_pts_top.append(f"{x:.1f},{y_top:.1f}")
                area_pts_bottom.append(f"{x:.1f},{y_bottom:.1f}")
                stacked_bottoms[i] += val

            poly = " ".join(area_pts_top) + " " + " ".join(reversed(area_pts_bottom))
            svg.append(
                f'<polygon points="{poly}" fill="{color}" opacity="0.6" '
                f'class="anim" style="animation-delay:{500 + lang_idx * 100}ms;"/>'
            )
    else:
        # Show a message when no language data is available yet
        center_x = margin_left + chart_w / 2
        center_y = chart2_top + chart_h_bottom / 2
        svg.append(
            f'<text x="{center_x}" y="{center_y}" '
            f'text-anchor="middle" class="subtitle" opacity="0.5">'
            f'Language data will appear after repositories are analyzed</text>'
        )

    # ── Forecast separator ────────────────────────────────────────────────
    if n > n_real:
        fx = margin_left + (n_real - 1) * step_x
        svg.append(
            f'<line x1="{fx:.1f}" y1="{chart1_top}" '
            f'x2="{fx:.1f}" y2="{chart2_top + chart_h_bottom}" '
            f'stroke="{text_color}" stroke-width="1" stroke-dasharray="4,3" '
            f'opacity="0.35"/>'
        )
        last_fx = -999.0
        for fi, flabel in enumerate(forecast_labels):
            fx_label = margin_left + (n_real + fi) * step_x
            if fx_label - last_fx < 70:
                continue
            svg.append(
                f'<text x="{fx_label:.1f}" '
                f'y="{chart1_top + 12}" '
                f'text-anchor="middle" class="axis-label" opacity="0.6">{flabel}</text>'
            )
            last_fx = fx_label

    # ── X-axis labels (shared) ───────────────────────────────────────────
    # Determine label format: use full date if month prefix is identical
    date_prefixes = {d[:7] for d in dates[:n_real] if len(d) >= 7}
    use_full_date = len(date_prefixes) <= 1

    # Ensure minimum pixel spacing between labels to prevent overlap
    min_label_spacing = 70  # minimum px between label centers
    label_step = max(1, n // 10)
    # Adjust step so labels are at least min_label_spacing apart
    while label_step > 1 and label_step * step_x < min_label_spacing:
        label_step += 1
    if label_step * step_x < min_label_spacing:
        label_step = max(1, int(min_label_spacing / step_x) + 1)

    shown_indices = set(range(0, n_real, label_step))
    if n > n_real:
        shown_indices.add(n_real - 1)  # always show last real data point
        for fi in range(n_real, n):
            shown_indices.add(fi)  # always show forecast labels

    last_rendered_x = -999.0
    last_rendered_label = ""
    for i in sorted(shown_indices):
        x = margin_left + i * step_x
        label_y = chart2_top + chart_h_bottom + 18
        if i >= n_real:
            fi = i - n_real
            label_text = forecast_labels[fi] if fi < len(forecast_labels) else dates[i][:7]
        elif use_full_date:
            label_text = dates[i]
        else:
            label_text = dates[i][:7] if len(dates[i]) >= 7 else dates[i]

        # Skip labels that are too close together or identical to previous
        if x - last_rendered_x < min_label_spacing and last_rendered_label:
            continue
        if label_text == last_rendered_label:
            continue

        svg.append(
            f'<text x="{x:.1f}" y="{label_y}" '
            f'text-anchor="middle" class="axis-label" '
            f'transform="rotate(-35 {x:.1f} {label_y})">'
            f'{label_text}</text>'
        )
        last_rendered_x = x
        last_rendered_label = label_text

    # ── Legend (right side) ──────────────────────────────────────────────
    legend_x = margin_left + chart_w + 16
    legend_y = chart1_top + 10

    # Lines added legend
    svg.append(
        f'<line x1="{legend_x}" y1="{legend_y}" '
        f'x2="{legend_x + 28}" y2="{legend_y}" '
        f'stroke="{add_color}" stroke-width="2"/>'
    )
    svg.append(
        f'<text x="{legend_x + 36}" y="{legend_y + 6}" '
        f'class="legend-text">Lines Added</text>'
    )

    # Lines deleted legend
    svg.append(
        f'<line x1="{legend_x}" y1="{legend_y + 30}" '
        f'x2="{legend_x + 28}" y2="{legend_y + 30}" '
        f'stroke="{del_color}" stroke-width="1.5"/>'
    )
    svg.append(
        f'<text x="{legend_x + 36}" y="{legend_y + 36}" '
        f'class="legend-text">Lines Deleted</text>'
    )

    # Stars legend
    svg.append(
        f'<line x1="{legend_x}" y1="{legend_y + 60}" '
        f'x2="{legend_x + 28}" y2="{legend_y + 60}" '
        f'stroke="{star_color}" stroke-width="1.5" stroke-dasharray="4,3"/>'
    )
    svg.append(
        f'<text x="{legend_x + 36}" y="{legend_y + 66}" '
        f'class="legend-text">&#x2605; Stars</text>'
    )

    # Language legend for chart 2
    if has_lang_data and top_langs:
        lang_legend_y = legend_y + 90
        svg.append(
            f'<text x="{legend_x}" y="{lang_legend_y}" '
            f'class="subtitle">Languages</text>'
        )
        max_label_chars = 22  # max characters for legend label
        for i, lang in enumerate(top_langs):
            ly = lang_legend_y + 22 + i * 22
            color = lang_color_map.get(lang, "#888888")
            current_prop = lang_series[lang][n_real - 1] if len(lang_series[lang]) >= n_real else (lang_series[lang][-1] if lang_series[lang] else 0.0)
            forecast_prop = lang_series[lang][-1] if lang_series[lang] else 0.0
            if n > n_real and abs(forecast_prop - current_prop) >= 0.05:
                label = f"{lang} ({current_prop:.0f}%→{forecast_prop:.0f}%)"
            else:
                label = f"{lang} ({current_prop:.1f}%)"
            if len(label) > max_label_chars:
                label = label[:max_label_chars - 1] + "…"
            svg.append(
                f'<rect x="{legend_x}" y="{ly - 10}" width="12" height="12" '
                f'rx="2" fill="{color}" opacity="0.8" '
                f'class="anim" style="animation-delay:{800 + i * 80}ms;"/>'
            )
            svg.append(
                f'<text x="{legend_x + 18}" y="{ly + 1}" '
                f'class="legend-text">{label}</text>'
            )
        next_section_y = lang_legend_y + 22 + len(top_langs) * 22 + 14
    else:
        next_section_y = legend_y + 72

    # Contributions-by-year mini summary
    max_sidebar_y = svg_height - 40  # leave room for summary footer
    contribs_by_year = current_snapshot.get("contributions_by_year", {})
    sorted_years = sorted(contribs_by_year.keys())
    if sorted_years and next_section_y < max_sidebar_y:
        cy_y = next_section_y
        svg.append(
            f'<text x="{legend_x}" y="{cy_y}" '
            f'class="subtitle">Contributions</text>'
        )
        for i, year in enumerate(sorted_years):
            item_y = cy_y + 20 + i * 20
            if item_y > max_sidebar_y:
                break
            val = contribs_by_year[year]
            svg.append(
                f'<text x="{legend_x}" y="{item_y}" '
                f'class="axis-label">{year}: {val:,}</text>'
            )
        next_section_y = cy_y + 20 + len(sorted_years) * 20 + 10

    # ── Additional statistics (right side) ───────────────────────────────
    # Contribution streak (consecutive days with contributions from weekly data)
    weekly_data = current_snapshot.get("lines_changed_by_week", {})
    if weekly_data and next_section_y < max_sidebar_y:
        sorted_weeks = sorted(weekly_data.keys())
        # Most active week
        most_active_week = max(
            sorted_weeks,
            key=lambda w: sum(
                int(x) for x in weekly_data[w]
            ) if isinstance(weekly_data[w], (list, tuple)) else 0
        )
        most_active_val = sum(int(x) for x in weekly_data[most_active_week])
        active_weeks = len(sorted_weeks)

        # Add/delete ratio
        total_a = sum(
            int(v[0]) for v in weekly_data.values()
            if isinstance(v, (list, tuple)) and len(v) >= 2
        )
        total_d = sum(
            int(v[1]) for v in weekly_data.values()
            if isinstance(v, (list, tuple)) and len(v) >= 2
        )
        ratio_str = f"{total_a / total_d:.1f}x" if total_d > 0 else "N/A"

        # Average lines per week
        avg_lines = (total_a + total_d) / active_weeks if active_weeks > 0 else 0

        stat_y = next_section_y
        svg.append(
            f'<text x="{legend_x}" y="{stat_y}" '
            f'class="subtitle">Statistics</text>'
        )
        stats_items = [
            f"Active weeks: {active_weeks}",
            f"Add/Del ratio: {ratio_str}",
            f"Avg lines/wk: {compact_number(avg_lines)}",
            f"Peak: {most_active_week}",
            f"  ({compact_number(most_active_val)} lines)",
        ]
        for i, text in enumerate(stats_items):
            item_y = stat_y + 20 + i * 20
            if item_y > max_sidebar_y:
                break
            svg.append(
                f'<text x="{legend_x}" y="{item_y}" '
                f'class="axis-label">{text}</text>'
            )

    # ── Summary footer ───────────────────────────────────────────────────
    total_adds = current_snapshot.get("lines_added", 0)
    total_dels = current_snapshot.get("lines_deleted", 0)
    total_lines = total_adds + total_dels
    total_contribs = current_snapshot.get("total_contributions", 0)
    total_repos = current_snapshot.get("repo_count", 0)
    total_stars = current_snapshot.get("stargazers", 0)

    summary_y = svg_height - 18
    svg.append(
        f'<text x="{margin_left}" y="{summary_y}" class="axis-label">'
        f"Total: +{total_adds:,} / -{total_dels:,} lines &#xb7; "
        f"{total_contribs:,} contributions &#xb7; "
        f"{total_repos} repos &#xb7; "
        f"&#x2605; {total_stars:,}</text>"
    )

    svg.append("</svg>")

    with open("generated/history.svg", "w") as f:
        f.write("\n".join(svg))


################################################################################
# Milestones Chart
################################################################################


def generate_milestones() -> None:
    """
    Generate milestones.svg from history data.
    Detects when contribution/star/repo thresholds were first crossed and
    draws a vertical timeline.
    """
    generate_output_folder()
    history = load_history()

    # ── Derive milestones from history ────────────────────────────────────
    # Thresholds to watch for (label, metric key, value)
    CONTRIB_THRESHOLDS = [100, 250, 500, 1000, 1500, 2000, 2500]
    STAR_THRESHOLDS = [1, 5, 10, 20, 30, 50]
    REPO_THRESHOLDS = [5, 10, 25, 50]

    milestones: List[Dict[str, Any]] = []

    # Sort history by date
    sorted_history = sorted(history, key=lambda s: s.get("date", ""))

    # Track cumulative contributions across years (use max seen per year to avoid
    # regressions from API noise).
    cum_contribs_seen: Dict[str, int] = {}
    prev_total_contribs = 0
    prev_stars = 0
    prev_repos = 0
    contrib_thresholds_left = sorted(CONTRIB_THRESHOLDS)
    star_thresholds_left = sorted(STAR_THRESHOLDS)
    repo_thresholds_left = sorted(REPO_THRESHOLDS)

    for snap in sorted_history:
        date = snap.get("date", "")

        # Compute best estimate of total contributions at this snapshot
        by_year = snap.get("contributions_by_year", {})
        for yr, val in by_year.items():
            if val > cum_contribs_seen.get(yr, 0):
                cum_contribs_seen[yr] = val
        total_contribs = sum(cum_contribs_seen.values())

        stars = snap.get("stargazers", 0)
        repos = snap.get("repo_count", 0)

        # Check contribution thresholds
        for thr in list(contrib_thresholds_left):
            if total_contribs >= thr:
                if thr >= prev_total_contribs:
                    milestones.append({
                        "date": date,
                        "icon": "🎯",
                        "label": f"{thr:,} Contributions",
                        "value": f"{total_contribs:,}",
                        "color": "#bc8cff",
                    })
                contrib_thresholds_left.remove(thr)

        # Check star thresholds
        for thr in list(star_thresholds_left):
            if stars >= thr:
                milestones.append({
                    "date": date,
                    "icon": "⭐",
                    "label": f"{thr} Stars",
                    "value": f"{stars:,} stars",
                    "color": "#e3b341",
                })
                star_thresholds_left.remove(thr)

        # Check repo thresholds
        for thr in list(repo_thresholds_left):
            if repos >= thr:
                milestones.append({
                    "date": date,
                    "icon": "📁",
                    "label": f"{thr} Repos",
                    "value": f"{repos} repos",
                    "color": "#58a6ff",
                })
                repo_thresholds_left.remove(thr)

        prev_total_contribs = total_contribs
        prev_stars = stars
        prev_repos = repos

    # Deduplicate (keep first occurrence per label)
    seen_labels: set = set()
    unique_milestones: List[Dict[str, Any]] = []
    for m in milestones:
        if m["label"] not in seen_labels:
            seen_labels.add(m["label"])
            unique_milestones.append(m)
    milestones = unique_milestones

    # ── SVG layout (fixed 1000×760 – same as history.svg) ────────────────
    card_bg = "#161b22"
    text_color = "#c9d1d9"
    grid_color = "#21262d"

    svg_width = 1000
    svg_height = 760
    header_h = 100
    footer_h = 45
    content_h = svg_height - header_h - footer_h  # 615 px

    # Two-column layout so that many milestones still fit at 1000×760.
    n_ms = len(milestones)
    n_rows = max((n_ms + 1) // 2, 1)
    row_h = min(160, content_h // n_rows) if n_ms > 0 else 160

    # Column origins (timeline circle x, content-text x)
    col_tl_x = [50, 550]        # timeline circle x for left / right column
    col_txt_x = [col_tl_x[0] + 44, col_tl_x[1] + 44]
    col_sep_x = 500             # vertical separator between columns

    svg: List[str] = []
    svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{svg_width}" height="{svg_height}" '
        f'style="max-width: 100%;" '
        f'viewBox="0 0 {svg_width} {svg_height}">'
    )
    svg.append(f"""<style>
  .ms-title {{ font: bold 28px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .ms-sub   {{ font: 600 20px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.75; }}
  .ms-label {{ font: bold 22px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .ms-date  {{ font: 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.6; }}
  .ms-value {{ font: 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.75; }}
  @keyframes slideIn {{ from {{ opacity: 0; transform: translateX(-8px); }}
                        to   {{ opacity: 1; transform: translateX(0); }} }}
  .ms-row {{ animation: slideIn 0.4s ease forwards; opacity: 0; }}
</style>""")
    svg.append(
        f'<rect x="0.5" y="0.5" rx="4.5" width="{svg_width-1}" '
        f'height="{svg_height-1}" fill="{card_bg}" stroke="{grid_color}"/>'
    )
    svg.append(f'<text x="20" y="50" class="ms-title">🏆 Milestones</text>')
    svg.append(
        f'<text x="20" y="78" class="ms-sub">'
        f'Achievements automatically detected from activity data</text>'
    )

    if not milestones:
        svg.append(
            f'<text x="20" y="{header_h + 45}" class="ms-date">'
            f'No milestones detected yet – check back after more activity.</text>'
        )
    else:
        # Column separator
        svg.append(
            f'<line x1="{col_sep_x}" y1="{header_h + 8}" '
            f'x2="{col_sep_x}" y2="{svg_height - footer_h - 8}" '
            f'stroke="{grid_color}" stroke-width="1" opacity="0.5"/>'
        )
        # Timeline vertical lines per column
        for col in range(2):
            tl_x = col_tl_x[col]
            n_in_col = (n_ms + 1 - col) // 2
            if n_in_col <= 0:
                continue
            tl_top = header_h + 10
            tl_bot = header_h + n_in_col * row_h - 10
            svg.append(
                f'<line x1="{tl_x}" y1="{tl_top}" x2="{tl_x}" y2="{tl_bot}" '
                f'stroke="{grid_color}" stroke-width="2"/>'
            )

        for idx, m in enumerate(milestones):
            col = idx % 2
            row = idx // 2
            tl_x = col_tl_x[col]
            txt_x = col_txt_x[col]
            y = header_h + row * row_h
            delay = idx * 60
            cy = y + row_h // 2
            color = m.get("color", "#c9d1d9")
            svg.append(
                f'<g class="ms-row" style="animation-delay:{delay}ms;">'
            )
            svg.append(
                f'<circle cx="{tl_x}" cy="{cy}" r="12" '
                f'fill="{color}" opacity="0.85"/>'
            )
            svg.append(
                f'<line x1="{tl_x + 12}" y1="{cy}" x2="{tl_x + 38}" y2="{cy}" '
                f'stroke="{color}" stroke-width="2" opacity="0.5"/>'
            )
            svg.append(
                f'<text x="{txt_x}" y="{cy - 9}" class="ms-label">'
                f'{m["icon"]} {m["label"]}</text>'
            )
            svg.append(
                f'<text x="{txt_x}" y="{cy + 20}" class="ms-date">'
                f'{m["date"]}  ·  {m["value"]}</text>'
            )
            svg.append('</g>')

    # Footer
    now_str = datetime.utcnow().strftime("%Y-%m-%d")
    svg.append(
        f'<text x="{svg_width - 10}" y="{svg_height - 12}" '
        f'text-anchor="end" class="ms-date">Updated {now_str}</text>'
    )
    svg.append("</svg>")

    with open("generated/milestones.svg", "w") as f:
        f.write("\n".join(svg))


################################################################################
# Achievements / Top-Rankings Chart
################################################################################


def generate_achievements() -> None:
    """
    Generate achievements.svg: a 'Top Rankings' card.
    Derives facts such as top language per year, best contribution year,
    most active coding week, add/delete ratio, etc. from history.json.
    """
    generate_output_folder()
    history = load_history()

    card_bg = "#161b22"
    text_color = "#c9d1d9"
    grid_color = "#21262d"
    gold = "#e3b341"
    silver = "#c0c0c0"
    bronze = "#cd7f32"
    accent = "#58a6ff"
    green = "#3fb950"
    red = "#f85149"
    purple = "#bc8cff"

    # ── Compute achievements ───────────────────────────────────────────────

    achievements: List[Dict[str, Any]] = []

    # 1. Best contribution year
    contribs_by_year: Dict[str, int] = {}
    for snap in history:
        for yr, val in snap.get("contributions_by_year", {}).items():
            if val > contribs_by_year.get(yr, 0):
                contribs_by_year[yr] = val
    if contribs_by_year:
        best_year = max(contribs_by_year, key=lambda y: contribs_by_year[y])
        achievements.append({
            "icon": "🏆",
            "label": f"Most Active Year: {best_year}",
            "value": f"{contribs_by_year[best_year]:,} contributions",
            "color": gold,
        })

    # 2. Top language per year (only from snapshots that have language data)
    lang_by_year: Dict[str, Dict[str, float]] = {}
    for snap in history:
        langs = snap.get("languages", {})
        if not langs:
            continue
        yr = snap.get("date", "")[:4]
        if yr not in lang_by_year:
            lang_by_year[yr] = {}
        for lname, ldata in langs.items():
            prop = ldata.get("prop", 0.0)
            if prop > lang_by_year[yr].get(lname, 0.0):
                lang_by_year[yr][lname] = prop

    rank_colors = [gold, silver, bronze]
    for yr in sorted(lang_by_year.keys(), reverse=True)[:3]:
        data = lang_by_year[yr]
        if data:
            top = max(data, key=lambda l: data[l])
            color = rank_colors.pop(0) if rank_colors else accent
            achievements.append({
                "icon": "💻",
                "label": f"Top Language {yr}: {top}",
                "value": f"{data[top]:.1f}% of code",
                "color": color,
            })

    # 3. Most active week (lines changed)
    all_weekly: Dict[str, List[int]] = {}
    for snap in history:
        for wk, vals in snap.get("lines_changed_by_week", {}).items():
            if isinstance(vals, (list, tuple)) and len(vals) >= 2:
                total = int(vals[0]) + int(vals[1])
                if total > sum(all_weekly.get(wk, [0, 0])):
                    all_weekly[wk] = [int(vals[0]), int(vals[1])]
    if all_weekly:
        peak_week = max(all_weekly, key=lambda w: sum(all_weekly[w]))
        peak_vals = all_weekly[peak_week]
        achievements.append({
            "icon": "🔥",
            "label": f"Peak Week: {peak_week}",
            "value": f"+{peak_vals[0]:,} / -{peak_vals[1]:,} lines",
            "color": red,
        })

    # 4. Total lines written (all-time adds)
    total_adds_all = sum(v[0] for v in all_weekly.values() if isinstance(v, list))
    total_dels_all = sum(v[1] for v in all_weekly.values() if isinstance(v, list))
    if total_adds_all > 0:
        achievements.append({
            "icon": "📝",
            "label": "Total Lines Written (all-time)",
            "value": f"+{total_adds_all:,} added / -{total_dels_all:,} deleted",
            "color": green,
        })

    # 5. Add/Delete ratio
    if total_dels_all > 0:
        ratio = total_adds_all / total_dels_all
        achievements.append({
            "icon": "⚖️",
            "label": "Add / Delete Ratio",
            "value": f"{ratio:.2f}x (higher = more net new code)",
            "color": accent,
        })

    # 6. Language with highest growth (last snapshot vs earliest snapshot with data)
    first_snap_langs = None
    last_snap_langs = None
    for snap in sorted(history, key=lambda s: s.get("date", "")):
        if snap.get("languages"):
            if first_snap_langs is None:
                first_snap_langs = snap["languages"]
            last_snap_langs = snap["languages"]
    if first_snap_langs and last_snap_langs and first_snap_langs != last_snap_langs:
        growth: Dict[str, float] = {}
        for lang in last_snap_langs:
            old_prop = first_snap_langs.get(lang, {}).get("prop", 0.0)
            new_prop = last_snap_langs[lang].get("prop", 0.0)
            growth[lang] = new_prop - old_prop
        fastest_growing = max(growth, key=lambda l: growth[l])
        fastest_shrinking = min(growth, key=lambda l: growth[l])
        if growth[fastest_growing] > 0:
            achievements.append({
                "icon": "📈",
                "label": f"Fastest Growing Language",
                "value": f"{fastest_growing} (+{growth[fastest_growing]:.1f}pp)",
                "color": green,
            })
        if growth[fastest_shrinking] < 0:
            achievements.append({
                "icon": "📉",
                "label": f"Most Reduced Language",
                "value": f"{fastest_shrinking} ({growth[fastest_shrinking]:.1f}pp)",
                "color": red,
            })

    # 7. Max stars / repos from any snapshot
    max_stars = max((s.get("stargazers", 0) for s in history), default=0)
    max_repos = max((s.get("repo_count", 0) for s in history), default=0)
    if max_stars > 0:
        achievements.append({
            "icon": "⭐",
            "label": "Peak Stars",
            "value": f"{max_stars:,} stars across all repos",
            "color": gold,
        })
    if max_repos > 0:
        achievements.append({
            "icon": "📦",
            "label": "Peak Repository Count",
            "value": f"{max_repos} repos",
            "color": purple,
        })

    # ── SVG layout (fixed 1000×760 – same as history.svg) ────────────────
    svg_width = 1000
    svg_height = 760
    header_h = 100
    footer_h = 45
    content_h = svg_height - header_h - footer_h  # 615 px

    # Two-column layout: left col x=12..490, right col x=512..992
    n_ach = len(achievements)
    n_rows = max((n_ach + 1) // 2, 1)
    row_h = min(165, content_h // n_rows) if n_ach > 0 else 165

    col_badge_x = [12, 512]     # badge stripe x for left / right column
    col_txt_x = [34, 534]       # label/value text x
    col_sep_x = 500

    svg = []
    svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{svg_width}" height="{svg_height}" '
        f'style="max-width: 100%;" '
        f'viewBox="0 0 {svg_width} {svg_height}">'
    )
    svg.append(f"""<style>
  .ach-title {{ font: bold 28px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .ach-sub   {{ font: 600 20px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.75; }}
  .ach-label {{ font: bold 22px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .ach-value {{ font: 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.75; }}
  .ach-note  {{ font: 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.5; }}
  @keyframes popIn {{ from {{ opacity: 0; transform: scale(0.92); }}
                      to   {{ opacity: 1; transform: scale(1);    }} }}
  .ach-row {{ animation: popIn 0.35s ease forwards; opacity: 0; }}
</style>""")
    svg.append(
        f'<rect x="0.5" y="0.5" rx="4.5" width="{svg_width-1}" '
        f'height="{svg_height-1}" fill="{card_bg}" stroke="{grid_color}"/>'
    )
    svg.append(f'<text x="20" y="50" class="ach-title">🎖️ Top Rankings &amp; Records</text>')
    svg.append(
        f'<text x="20" y="78" class="ach-sub">'
        f'Highlights automatically derived from GitHub activity data</text>'
    )

    if not achievements:
        svg.append(
            f'<text x="20" y="{header_h + 45}" class="ach-note">'
            f'No achievements detected yet.</text>'
        )
    else:
        # Column separator
        svg.append(
            f'<line x1="{col_sep_x}" y1="{header_h + 12}" '
            f'x2="{col_sep_x}" y2="{svg_height - footer_h - 12}" '
            f'stroke="{grid_color}" stroke-width="1" opacity="0.5"/>'
        )
        for idx, ach in enumerate(achievements):
            col = idx % 2
            row = idx // 2
            bx = col_badge_x[col]
            tx = col_txt_x[col]
            y = header_h + row * row_h
            delay = idx * 55
            color = ach.get("color", accent)
            svg.append(
                f'<rect x="{bx}" y="{y + 10}" width="6" height="{row_h - 20}" '
                f'rx="2" fill="{color}" opacity="0.9" '
                f'class="ach-row" style="animation-delay:{delay}ms;"/>'
            )
            svg.append(
                f'<g class="ach-row" style="animation-delay:{delay + 30}ms;">'
            )
            svg.append(
                f'<text x="{tx}" y="{y + 38}" class="ach-label">'
                f'{ach["icon"]} {ach["label"]}</text>'
            )
            svg.append(
                f'<text x="{tx}" y="{y + 64}" class="ach-value">'
                f'{ach["value"]}</text>'
            )
            svg.append('</g>')

    # Separator above footer
    svg.append(
        f'<line x1="8" y1="{svg_height - footer_h}" x2="{svg_width - 8}" '
        f'y2="{svg_height - footer_h}" stroke="{grid_color}" stroke-width="1"/>'
    )
    now_str = datetime.utcnow().strftime("%Y-%m-%d")
    svg.append(
        f'<text x="{svg_width - 10}" y="{svg_height - 12}" '
        f'text-anchor="end" class="ach-note">Updated {now_str}</text>'
    )
    svg.append("</svg>")

    with open("generated/achievements.svg", "w") as f:
        f.write("\n".join(svg))





async def main() -> None:
    """
    Generate all badges
    """
    access_token = os.getenv("ACCESS_TOKEN")
    if not access_token:
        raise Exception("A personal access token is required to proceed!")
    user = os.getenv("GITHUB_ACTOR")
    if user is None:
        raise RuntimeError("Environment variable GITHUB_ACTOR must be set.")
    exclude_repos = os.getenv("EXCLUDED")
    excluded_repos = (
        {x.strip() for x in exclude_repos.split(",")} if exclude_repos else None
    )
    exclude_langs = os.getenv("EXCLUDED_LANGS")
    excluded_langs = (
        {x.strip() for x in exclude_langs.split(",")} if exclude_langs else None
    )
    # Convert a truthy value to a Boolean
    raw_ignore_forked_repos = os.getenv("EXCLUDE_FORKED_REPOS")
    ignore_forked_repos = (
        not not raw_ignore_forked_repos
        and raw_ignore_forked_repos.strip().lower() != "false"
    )
    async with aiohttp.ClientSession() as session:
        s = Stats(
            user,
            access_token,
            session,
            exclude_repos=excluded_repos,
            exclude_langs=excluded_langs,
            ignore_forked_repos=ignore_forked_repos,
        )
        await asyncio.gather(
            generate_languages(s),
            generate_overview(s),
        )
        await generate_history(s)
        generate_milestones()
        generate_achievements()


if __name__ == "__main__":
    asyncio.run(main())