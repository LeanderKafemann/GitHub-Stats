#!/usr/bin/python3

import asyncio
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
viewBox="0 0 16 16" version="1.1" width="16" height="16"><path
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
                '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="100">'
                '<text x="20" y="50" fill="#c9d1d9" '
                'font-family="Segoe UI, Ubuntu, Sans-Serif" font-size="14">'
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

    # ── Chart dimensions ─────────────────────────────────────────────────
    svg_width = 900
    svg_height = 680
    margin_top = 60
    margin_bottom = 90
    margin_left = 65
    margin_right = 180
    chart_w = svg_width - margin_left - margin_right
    chart_h_top = 180
    chart_h_bottom = 180
    gap = 60
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
  .title {{ font: bold 16px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .subtitle {{ font: 600 12px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.8; }}
  .axis-label {{ font: 11px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.7; }}
  .legend-text {{ font: 12px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .value-text {{ font: bold 11px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .stat-value {{ font: bold 13px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .stat-label {{ font: 11px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.7; }}
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
        f'<text x="{margin_left}" y="30" class="title">Activity History</text>'
    )
    svg.append(
        f'<text x="{margin_left}" y="48" class="subtitle">'
        f'Lines added vs deleted &amp; activity trends over time</text>'
    )

    step_x = chart_w / max(n - 1, 1)

    # ── Helper: draw grid + axis ─────────────────────────────────────────
    def draw_grid(
        top_y: float, height: float, max_val: float, label_fmt: str = "{:,.0f}"
    ) -> None:
        num_grid = 4
        for i in range(num_grid + 1):
            y = top_y + height - (i / num_grid) * height
            val = max_val * i / num_grid
            svg.append(
                f'<line x1="{margin_left}" y1="{y:.1f}" '
                f'x2="{margin_left + chart_w}" y2="{y:.1f}" '
                f'stroke="{grid_color}" stroke-width="1"/>'
            )
            svg.append(
                f'<text x="{margin_left - 8}" y="{y + 4:.1f}" '
                f'text-anchor="end" class="axis-label">'
                f'{label_fmt.format(val)}</text>'
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
        f'<polyline points="{" ".join(add_pts)}" fill="none" '
        f'stroke="{add_color}" stroke-width="2" stroke-linejoin="round" '
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
        f'<polyline points="{" ".join(del_pts)}" fill="none" '
        f'stroke="{del_color}" stroke-width="1.5" stroke-linejoin="round" '
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
        f'<polyline points="{" ".join(star_pts)}" fill="none" '
        f'stroke="{star_color}" stroke-width="1.5" stroke-dasharray="4,3" '
        f'stroke-linejoin="round" class="anim" style="animation-delay:400ms;"/>'
    )

    # Chart 1 label
    svg.append(
        f'<text x="{margin_left}" y="{chart1_top - 6}" '
        f'class="subtitle">Lines Added vs Deleted (per snapshot)</text>'
    )

    # ── Chart 2: Language proportions over time ────────────────────────
    # Always show language chart (with fallback message if no data)
    svg.append(
        f'<text x="{margin_left}" y="{chart2_top - 6}" '
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

    # ── X-axis labels (shared) ───────────────────────────────────────────
    # Determine label format: use full date if month prefix is identical
    date_prefixes = {d[:7] for d in dates if len(d) >= 7}
    use_full_date = len(date_prefixes) <= 1

    label_step = max(1, n // 10)
    for i in range(0, n, label_step):
        x = margin_left + i * step_x
        label_y = chart2_top + chart_h_bottom + 18
        if use_full_date:
            label_text = dates[i]
        else:
            label_text = dates[i][:7] if len(dates[i]) >= 7 else dates[i]
        svg.append(
            f'<text x="{x:.1f}" y="{label_y}" '
            f'text-anchor="middle" class="axis-label" '
            f'transform="rotate(-35 {x:.1f} {label_y})">'
            f'{label_text}</text>'
        )

    # ── Legend (right side) ──────────────────────────────────────────────
    legend_x = margin_left + chart_w + 16
    legend_y = chart1_top + 10

    # Lines added legend
    svg.append(
        f'<line x1="{legend_x}" y1="{legend_y}" '
        f'x2="{legend_x + 20}" y2="{legend_y}" '
        f'stroke="{add_color}" stroke-width="2"/>'
    )
    svg.append(
        f'<text x="{legend_x + 26}" y="{legend_y + 4}" '
        f'class="legend-text">Lines Added</text>'
    )

    # Lines deleted legend
    svg.append(
        f'<line x1="{legend_x}" y1="{legend_y + 18}" '
        f'x2="{legend_x + 20}" y2="{legend_y + 18}" '
        f'stroke="{del_color}" stroke-width="1.5"/>'
    )
    svg.append(
        f'<text x="{legend_x + 26}" y="{legend_y + 22}" '
        f'class="legend-text">Lines Deleted</text>'
    )

    # Stars legend
    svg.append(
        f'<line x1="{legend_x}" y1="{legend_y + 38}" '
        f'x2="{legend_x + 20}" y2="{legend_y + 38}" '
        f'stroke="{star_color}" stroke-width="1.5" stroke-dasharray="4,3"/>'
    )
    svg.append(
        f'<text x="{legend_x + 26}" y="{legend_y + 42}" '
        f'class="legend-text">&#x2605; Stars</text>'
    )

    # Language legend for chart 2
    if has_lang_data and top_langs:
        lang_legend_y = legend_y + 72
        svg.append(
            f'<text x="{legend_x}" y="{lang_legend_y}" '
            f'class="subtitle">Languages</text>'
        )
        for i, lang in enumerate(top_langs):
            ly = lang_legend_y + 20 + i * 22
            color = lang_color_map.get(lang, "#888888")
            prop = lang_series[lang][-1] if lang_series[lang] else 0
            svg.append(
                f'<rect x="{legend_x}" y="{ly - 9}" width="12" height="12" '
                f'rx="2" fill="{color}" opacity="0.8" '
                f'class="anim" style="animation-delay:{800 + i * 80}ms;"/>'
            )
            svg.append(
                f'<text x="{legend_x + 18}" y="{ly + 1}" '
                f'class="legend-text">{lang} ({prop:.1f}%)</text>'
            )
        next_section_y = lang_legend_y + 20 + len(top_langs) * 22 + 20
    else:
        next_section_y = legend_y + 72

    # Contributions-by-year mini summary
    contribs_by_year = current_snapshot.get("contributions_by_year", {})
    sorted_years = sorted(contribs_by_year.keys())
    if sorted_years:
        cy_y = next_section_y
        svg.append(
            f'<text x="{legend_x}" y="{cy_y}" '
            f'class="subtitle">Contributions</text>'
        )
        for i, year in enumerate(sorted_years):
            val = contribs_by_year[year]
            svg.append(
                f'<text x="{legend_x}" y="{cy_y + 18 + i * 16}" '
                f'class="axis-label">{year}: {val:,}</text>'
            )
        next_section_y = cy_y + 18 + len(sorted_years) * 16 + 10

    # ── Additional statistics (right side) ───────────────────────────────
    # Contribution streak (consecutive days with contributions from weekly data)
    weekly_data = current_snapshot.get("lines_changed_by_week", {})
    if weekly_data:
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
            (f"Active weeks: {active_weeks}",),
            (f"Add/Del ratio: {ratio_str}",),
            (f"Avg lines/week: {avg_lines:,.0f}",),
            (f"Peak: {most_active_week}",),
            (f"  ({most_active_val:,} lines)",),
        ]
        for i, (text,) in enumerate(stats_items):
            svg.append(
                f'<text x="{legend_x}" y="{stat_y + 18 + i * 15}" '
                f'class="axis-label">{text}</text>'
            )

    # ── Summary footer ───────────────────────────────────────────────────
    total_adds = current_snapshot.get("lines_added", 0)
    total_dels = current_snapshot.get("lines_deleted", 0)
    total_lines = total_adds + total_dels
    total_contribs = current_snapshot.get("total_contributions", 0)
    total_repos = current_snapshot.get("repo_count", 0)
    total_stars = current_snapshot.get("stargazers", 0)

    summary_y = svg_height - 14
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
# Main Function
################################################################################


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


if __name__ == "__main__":
    asyncio.run(main())