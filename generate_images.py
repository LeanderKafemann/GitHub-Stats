#!/usr/bin/python3

import asyncio
import os
import re
from typing import Dict, List, Tuple

import aiohttp

from github_stats import Stats


################################################################################
# Helper Functions
################################################################################


def generate_output_folder() -> None:
    """
    Create the output folder if it does not already exist
    """
    if not os.path.isdir("generated"):
        os.mkdir("generated")


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
    Generate an SVG line chart showing:
    - Weekly lines changed over time (additions + deletions)
    - Contributions per year
    - Top language proportions per year (approximate, based on current snapshot)
    :param s: Represents user's GitHub statistics
    """
    # ── Collect data ─────────────────────────────────────────────────────
    weekly_data = await s.lines_changed_by_week
    contributions_by_year = await s.contributions_by_year
    languages = await s.languages

    # Sort top languages by size (max 5 for readability)
    sorted_langs = sorted(
        languages.items(), reverse=True, key=lambda t: t[1].get("size", 0)
    )[:5]
    lang_colors = {
        name: data.get("color", "#000000") or "#000000"
        for name, data in sorted_langs
    }
    lang_props = {
        name: data.get("prop", 0) for name, data in sorted_langs
    }

    # ── Chart dimensions ─────────────────────────────────────────────────
    svg_width = 840
    svg_height = 480
    margin_top = 60
    margin_bottom = 80
    margin_left = 70
    margin_right = 200  # extra space for legend
    chart_w = svg_width - margin_left - margin_right
    chart_h = svg_height - margin_top - margin_bottom

    # ── Aggregate weekly lines changed into monthly buckets ──────────────
    monthly: Dict[str, int] = {}
    for date_str, (adds, dels) in weekly_data.items():
        month_key = date_str[:7]  # YYYY-MM
        monthly[month_key] = monthly.get(month_key, 0) + adds + dels

    sorted_months = sorted(monthly.keys())
    if not sorted_months:
        sorted_months = ["2025-01"]
        monthly["2025-01"] = 0

    month_values = [monthly[m] for m in sorted_months]
    max_lines = max(month_values) if month_values else 1
    if max_lines == 0:
        max_lines = 1

    # ── Build contributions-by-year bar data ─────────────────────────────
    sorted_years = sorted(contributions_by_year.keys())
    year_values = [contributions_by_year[y] for y in sorted_years]
    max_contribs = max(year_values) if year_values else 1
    if max_contribs == 0:
        max_contribs = 1

    # ── SVG construction ─────────────────────────────────────────────────
    # Color scheme
    bg_color = "#0d1117"
    card_bg = "#161b22"
    text_color = "#c9d1d9"
    grid_color = "#21262d"
    line_color = "#58a6ff"
    area_color = "#58a6ff"
    bar_color = "#3fb950"

    svg_parts: List[str] = []
    svg_parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{svg_width}" height="{svg_height}" '
        f'viewBox="0 0 {svg_width} {svg_height}">'
    )

    # Styles
    svg_parts.append(f"""
<defs>
  <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="{area_color}" stop-opacity="0.4"/>
    <stop offset="100%" stop-color="{area_color}" stop-opacity="0.0"/>
  </linearGradient>
</defs>
<style>
  .title {{ font: bold 16px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .subtitle {{ font: 600 12px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.8; }}
  .axis-label {{ font: 11px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; opacity: 0.7; }}
  .legend-text {{ font: 12px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  .value-text {{ font: bold 11px 'Segoe UI', Ubuntu, Sans-Serif; fill: {text_color}; }}
  @keyframes fadeIn {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
  .chart-element {{ animation: fadeIn 0.8s ease-in-out forwards; opacity: 0; }}
</style>
""")

    # Background card
    svg_parts.append(
        f'<rect x="0.5" y="0.5" rx="4.5" width="{svg_width - 1}" '
        f'height="{svg_height - 1}" fill="{card_bg}" stroke="{grid_color}"/>'
    )

    # Title
    svg_parts.append(
        f'<text x="{margin_left}" y="30" class="title">Activity History</text>'
    )
    svg_parts.append(
        f'<text x="{margin_left}" y="48" class="subtitle">'
        f'Lines changed per month &amp; contributions per year</text>'
    )

    # ── Chart 1: Monthly lines changed (area + line chart) ──────────────
    n = len(sorted_months)
    step_x = chart_w / max(n - 1, 1)

    # Grid lines (horizontal)
    num_grid = 5
    for i in range(num_grid + 1):
        y = margin_top + chart_h - (i / num_grid) * chart_h
        val = int(max_lines * i / num_grid)
        svg_parts.append(
            f'<line x1="{margin_left}" y1="{y}" '
            f'x2="{margin_left + chart_w}" y2="{y}" '
            f'stroke="{grid_color}" stroke-width="1"/>'
        )
        svg_parts.append(
            f'<text x="{margin_left - 8}" y="{y + 4}" '
            f'text-anchor="end" class="axis-label">{val:,}</text>'
        )

    # Build polyline points and area polygon
    points: List[str] = []
    area_points: List[str] = []
    for i, m in enumerate(sorted_months):
        x = margin_left + i * step_x
        y = margin_top + chart_h - (monthly[m] / max_lines) * chart_h
        points.append(f"{x:.1f},{y:.1f}")
        area_points.append(f"{x:.1f},{y:.1f}")

    # Close area polygon
    area_start_x = margin_left
    area_end_x = margin_left + (n - 1) * step_x
    bottom_y = margin_top + chart_h
    area_polygon = (
        f"{area_start_x:.1f},{bottom_y:.1f} "
        + " ".join(area_points)
        + f" {area_end_x:.1f},{bottom_y:.1f}"
    )

    svg_parts.append(
        f'<polygon points="{area_polygon}" fill="url(#areaGrad)" '
        f'class="chart-element" style="animation-delay: 200ms;"/>'
    )
    svg_parts.append(
        f'<polyline points="{" ".join(points)}" fill="none" '
        f'stroke="{line_color}" stroke-width="2" stroke-linejoin="round" '
        f'class="chart-element" style="animation-delay: 400ms;"/>'
    )

    # X-axis labels (show every Nth month to avoid clutter)
    label_step = max(1, n // 8)
    for i in range(0, n, label_step):
        x = margin_left + i * step_x
        svg_parts.append(
            f'<text x="{x}" y="{margin_top + chart_h + 18}" '
            f'text-anchor="middle" class="axis-label" '
            f'transform="rotate(-30 {x} {margin_top + chart_h + 18})">'
            f'{sorted_months[i]}</text>'
        )

    # ── Chart 2: Contributions per year (small bar chart in legend area) ─
    bar_area_x = margin_left + chart_w + 20
    bar_area_y = margin_top + 10
    bar_area_w = 140
    bar_area_h = 100

    svg_parts.append(
        f'<text x="{bar_area_x}" y="{bar_area_y - 2}" '
        f'class="subtitle">Contributions / Year</text>'
    )

    if sorted_years:
        bar_w = min(20, bar_area_w // len(sorted_years) - 4)
        bar_total_w = len(sorted_years) * (bar_w + 4)
        bar_start_x = bar_area_x + (bar_area_w - bar_total_w) / 2

        for i, year in enumerate(sorted_years):
            val = contributions_by_year[year]
            h = (val / max_contribs) * (bar_area_h - 20)
            x = bar_start_x + i * (bar_w + 4)
            y = bar_area_y + bar_area_h - h
            svg_parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w}" '
                f'height="{h:.1f}" rx="2" fill="{bar_color}" opacity="0.85" '
                f'class="chart-element" style="animation-delay: {600 + i * 100}ms;"/>'
            )
            svg_parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{bar_area_y + bar_area_h + 14}" '
                f'text-anchor="middle" class="axis-label">{year}</text>'
            )
            svg_parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{y - 4:.1f}" '
                f'text-anchor="middle" class="value-text">{val:,}</text>'
            )

    # ── Legend: Top languages ────────────────────────────────────────────
    legend_x = bar_area_x
    legend_y = bar_area_y + bar_area_h + 50

    svg_parts.append(
        f'<text x="{legend_x}" y="{legend_y}" class="subtitle">'
        f'Top Languages</text>'
    )

    for i, (lang_name, prop) in enumerate(lang_props.items()):
        ly = legend_y + 22 + i * 24
        color = lang_colors.get(lang_name, "#000000")
        svg_parts.append(
            f'<circle cx="{legend_x + 6}" cy="{ly - 4}" r="5" fill="{color}" '
            f'class="chart-element" style="animation-delay: {800 + i * 100}ms;"/>'
        )
        svg_parts.append(
            f'<text x="{legend_x + 16}" y="{ly}" class="legend-text" '
            f'class="chart-element">{lang_name} ({prop:.1f}%)</text>'
        )

    # ── Summary stats at the bottom ─────────────────────────────────────
    total_lines = await s.lines_changed
    total_changed = total_lines[0] + total_lines[1]
    total_contribs = await s.total_contributions
    total_repos = len(await s.repos)

    summary_y = svg_height - 16
    svg_parts.append(
        f'<text x="{margin_left}" y="{summary_y}" class="axis-label">'
        f'Total: {total_changed:,} lines changed · '
        f'{total_contribs:,} contributions · '
        f'{total_repos} repos</text>'
    )

    svg_parts.append("</svg>")

    generate_output_folder()
    with open("generated/history.svg", "w") as f:
        f.write("\n".join(svg_parts))


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