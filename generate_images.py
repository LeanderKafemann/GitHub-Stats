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
    for date_str, (adds, dels) in weekly.items():
        month_key = date_str[:7]  # YYYY-MM
        monthly_adds[month_key] = monthly_adds.get(month_key, 0) + adds
        monthly_dels[month_key] = monthly_dels.get(month_key, 0) + dels

    all_months = sorted(set(monthly_adds.keys()) | set(monthly_dels.keys()))

    # Compute cumulative lines for running totals
    cumulative_adds = 0
    cumulative_dels = 0

    for month in all_months:
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

        backfilled.append({
            "date": synth_date,
            "synthetic": True,
            "stargazers": snapshot.get("stargazers", 0),
            "forks": snapshot.get("forks", 0),
            "total_contributions": year_contribs,
            "repo_count": snapshot.get("repo_count", 0),
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
    history = [snap for snap in history if snap.get("date") != today]
    history.append(current_snapshot)
    history.sort(key=lambda snap: snap.get("date", ""))

    save_history(history)

    # ── Determine top 5 languages across all snapshots ───────────────────
    lang_totals: Dict[str, int] = {}
    lang_color_map: Dict[str, str] = {}
    for snap in history:
        for name, data in snap.get("languages", {}).items():
            lang_totals[name] = lang_totals.get(name, 0) + data.get("size", 0)
            if data.get("color"):
                lang_color_map[name] = data["color"]
    top_langs = sorted(lang_totals.keys(), key=lambda n: lang_totals[n], reverse=True)[
        :5
    ]

    # ── Prepare time series ──────────────────────────────────────────────
    dates = [snap.get("date", "") for snap in history]
    n = len(dates)

    # Monthly lines changed (from per-snapshot monthly fields or delta)
    monthly_lines: List[int] = []
    for i, snap in enumerate(history):
        m_add = snap.get("lines_added_month", 0)
        m_del = snap.get("lines_deleted_month", 0)
        if m_add or m_del:
            monthly_lines.append(m_add + m_del)
        else:
            # For real snapshots: compute delta from previous
            total_now = snap.get("lines_added", 0) + snap.get("lines_deleted", 0)
            if i > 0:
                prev = history[i - 1]
                total_prev = prev.get("lines_added", 0) + prev.get(
                    "lines_deleted", 0
                )
                monthly_lines.append(max(0, total_now - total_prev))
            else:
                monthly_lines.append(total_now)

    # Cumulative lines changed
    cumulative_lines: List[int] = []
    running = 0
    for val in monthly_lines:
        running += val
        cumulative_lines.append(running)

    # Language proportions per snapshot
    lang_series: Dict[str, List[float]] = {lang: [] for lang in top_langs}
    for snap in history:
        langs = snap.get("languages", {})
        for lang in top_langs:
            lang_series[lang].append(langs.get(lang, {}).get("prop", 0.0))

    # Stars over time
    stars_series = [snap.get("stargazers", 0) for snap in history]

    # ── Chart dimensions ─────────────────────────────────────────────────
    svg_width = 900
    svg_height = 620
    margin_top = 60
    margin_bottom = 90
    margin_left = 65
    margin_right = 180
    chart_w = svg_width - margin_left - margin_right
    chart_h_top = 180  # Lines changed chart
    chart_h_bottom = 180  # Language proportions chart
    gap = 60
    chart1_top = margin_top
    chart2_top = margin_top + chart_h_top + gap

    # ── Color scheme ─────────────────────────────────────────────────────
    card_bg = "#161b22"
    text_color = "#c9d1d9"
    grid_color = "#21262d"
    line_color = "#58a6ff"
    area_color = "#58a6ff"
    bar_color = "#3fb950"
    star_color = "#e3b341"

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
</defs>
<style>
  .title {{ font: bold 16px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .subtitle {{ font: 600 12px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.8; }}
  .axis-label {{ font: 11px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.7; }}
  .legend-text {{ font: 12px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .value-text {{ font: bold 11px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
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
        f'Lines changed per month &amp; language trends over time</text>'
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

    # ── Chart 1: Monthly lines changed (area) ───────────────────────────
    max_monthly = max(monthly_lines) if monthly_lines else 1
    if max_monthly == 0:
        max_monthly = 1

    draw_grid(chart1_top, chart_h_top, max_monthly)

    points: List[str] = []
    for i, val in enumerate(monthly_lines):
        x = margin_left + i * step_x
        y = chart1_top + chart_h_top - (val / max_monthly) * chart_h_top
        points.append(f"{x:.1f},{y:.1f}")

    # Area fill
    bottom1 = chart1_top + chart_h_top
    area_poly = (
        f"{margin_left:.1f},{bottom1:.1f} "
        + " ".join(points)
        + f" {margin_left + (n - 1) * step_x:.1f},{bottom1:.1f}"
    )
    svg.append(
        f'<polygon points="{area_poly}" fill="url(#areaGrad)" '
        f'class="anim" style="animation-delay:200ms;"/>'
    )
    svg.append(
        f'<polyline points="{" ".join(points)}" fill="none" '
        f'stroke="{line_color}" stroke-width="2" stroke-linejoin="round" '
        f'class="anim" style="animation-delay:300ms;"/>'
    )

    # Stars overlay (secondary axis, right side)
    max_stars = max(stars_series) if stars_series else 1
    if max_stars == 0:
        max_stars = 1
    star_pts: List[str] = []
    for i, val in enumerate(stars_series):
        x = margin_left + i * step_x
        y = chart1_top + chart_h_top - (val / max_stars) * chart_h_top
        star_pts.append(f"{x:.1f},{y:.1f}")
    svg.append(
        f'<polyline points="{" ".join(star_pts)}" fill="none" '
        f'stroke="{star_color}" stroke-width="1.5" stroke-dasharray="4,3" '
        f'stroke-linejoin="round" class="anim" style="animation-delay:400ms;"/>'
    )

    # Chart 1 label
    svg.append(
        f'<text x="{margin_left}" y="{chart1_top - 6}" '
        f'class="subtitle">Lines Changed / Month</text>'
    )

    # ── Chart 2: Language proportions (stacked area) ─────────────────────
    svg.append(
        f'<text x="{margin_left}" y="{chart2_top - 6}" '
        f'class="subtitle">Top Language Proportions (%)</text>'
    )
    draw_grid(chart2_top, chart_h_bottom, 100.0, "{:.0f}%")

    # Build stacked values
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

        # Polygon: top line forward, bottom line backward
        poly = " ".join(area_pts_top) + " " + " ".join(reversed(area_pts_bottom))
        svg.append(
            f'<polygon points="{poly}" fill="{color}" opacity="0.6" '
            f'class="anim" style="animation-delay:{500 + lang_idx * 100}ms;"/>'
        )

    # ── X-axis labels (shared) ───────────────────────────────────────────
    label_step = max(1, n // 10)
    for i in range(0, n, label_step):
        x = margin_left + i * step_x
        label_y = chart2_top + chart_h_bottom + 18
        # Show YYYY-MM
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

    # Lines changed legend
    svg.append(
        f'<line x1="{legend_x}" y1="{legend_y}" '
        f'x2="{legend_x + 20}" y2="{legend_y}" '
        f'stroke="{line_color}" stroke-width="2"/>'
    )
    svg.append(
        f'<text x="{legend_x + 26}" y="{legend_y + 4}" '
        f'class="legend-text">Lines Changed</text>'
    )

    # Stars legend
    svg.append(
        f'<line x1="{legend_x}" y1="{legend_y + 22}" '
        f'x2="{legend_x + 20}" y2="{legend_y + 22}" '
        f'stroke="{star_color}" stroke-width="1.5" stroke-dasharray="4,3"/>'
    )
    svg.append(
        f'<text x="{legend_x + 26}" y="{legend_y + 26}" '
        f'class="legend-text">Stars</text>'
    )

    # Language legend
    lang_legend_y = legend_y + 60
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

    # Contributions-by-year mini summary
    contribs_by_year = current_snapshot.get("contributions_by_year", {})
    sorted_years = sorted(contribs_by_year.keys())
    if sorted_years:
        cy_y = lang_legend_y + 20 + len(top_langs) * 22 + 20
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

    # ── Summary footer ───────────────────────────────────────────────────
    total_lines = current_snapshot.get("lines_added", 0) + current_snapshot.get(
        "lines_deleted", 0
    )
    total_contribs = current_snapshot.get("total_contributions", 0)
    total_repos = current_snapshot.get("repo_count", 0)
    total_stars = current_snapshot.get("stargazers", 0)

    summary_y = svg_height - 14
    svg.append(
        f'<text x="{margin_left}" y="{summary_y}" class="axis-label">'
        f"Total: {total_lines:,} lines changed \u00b7 "
        f"{total_contribs:,} contributions \u00b7 "
        f"{total_repos} repos \u00b7 "
        f"\u2605 {total_stars:,}</text>"
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
        # access_token = os.getenv("GITHUB_TOKEN")
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
            generate_history(s),
        )


if __name__ == "__main__":
    asyncio.run(main())