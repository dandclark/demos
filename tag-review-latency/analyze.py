"""Analyze a GitHub REST issue snapshot using only the Python standard library."""

import argparse
import collections
import csv
import datetime as dt
import html
import json
import math
from pathlib import Path
import statistics


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def quantile(values, probability):
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def describe(values):
    if not values:
        return {"n": 0, "mean_days": None, "median_days": None, "p90_days": None}
    return {
        "n": len(values),
        "mean_days": statistics.mean(values),
        "median_days": statistics.median(values),
        "p90_days": quantile(values, 0.9),
    }


def km_median(rows):
    times = collections.defaultdict(lambda: [0, 0])
    for row in rows:
        times[row["observed_days"]][0 if row["closed"] else 1] += 1
    risk, survival = len(rows), 1.0
    for time, (events, censored) in sorted(times.items()):
        survival *= 1 - events / risk
        if survival <= 0.5 or math.isclose(survival, 0.5, rel_tol=1e-12):
            return time
        risk -= events + censored
    return None


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def is_non_charter_review(row):
    return "charter" not in row["labels"] and "Review type: question" not in row["labels"]


def draw_chart(title, subtitle, rows, series, xmax, ymax):
    width, height, left, top, right, bottom = 1050, 370, 75, 65, 25, 55
    xmin = 2013
    plot_width, plot_height = width - left - right, height - top - bottom

    def x(value):
        return left + (value - xmin) / (xmax - xmin) * plot_width

    def y(value):
        return height - bottom - value / ymax * plot_height

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img">'
        f"<title>{html.escape(title)}</title>",
        '<rect width="100%" height="100%" fill="white"/>',
        '<g font-family="system-ui,sans-serif" font-size="12" fill="#333">',
        f'<text x="{left}" y="24" font-size="19" font-weight="bold">{html.escape(title)}</text>',
        f'<text x="{left}" y="44">{html.escape(subtitle)}</text>',
    ]
    for tick in range(0, ymax + 1, 100):
        parts.append(
            f'<path d="M{left},{y(tick)} H{width-right}" stroke="#ddd"/>'
            f'<text x="{left-10}" y="{y(tick)+4}" text-anchor="end">{tick}</text>'
        )
    for year in range(xmin, math.floor(xmax) + 1):
        parts.append(
            f'<text x="{x(year)}" y="{height-bottom+22}" text-anchor="middle">{year}</text>'
        )
    parts.append(
        f'<text transform="translate(18,{top+plot_height/2}) rotate(-90)" text-anchor="middle">Calendar days</text>'
    )
    for index, (key, name, color) in enumerate(series):
        points = [
            (x(row["plot_year"]), y(row[key]), row)
            for row in rows
            if row[key] is not None
        ]
        coords = " ".join(f"{px:.2f},{py:.2f}" for px, py, _ in points)
        parts.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        for px, py, row in points:
            parts.append(
                f'<circle cx="{px}" cy="{py}" r="3" fill="{color}">'
                f'<title>{row["period"]}: {name} {row[key]:.1f} days</title></circle>'
            )
        lx = left + index * 250
        parts.append(
            f'<path d="M{lx},{height-10} h20" stroke="{color}" stroke-width="3"/>'
            f'<text x="{lx+27}" y="{height-6}">{html.escape(name)}</text>'
        )
    parts.append("</g></svg>")
    return "".join(parts)


def table(rows, columns):
    output = ["<table><thead><tr>"]
    output.extend(f"<th>{html.escape(label)}</th>" for _, label in columns)
    output.append("</tr></thead><tbody>")
    for row in rows:
        output.append("<tr>")
        for key, _ in columns:
            value = row[key]
            if value is None:
                value = "Not estimable"
            elif isinstance(value, float):
                value = f"{value:.1f}"
            output.append(f"<td>{html.escape(str(value))}</td>")
        output.append("</tr>")
    output.append("</tbody></table>")
    return "".join(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(__file__).with_name("issues.jsonl"))
    parser.add_argument("--as-of", default="2026-10-19T22:13:04Z")
    args = parser.parse_args()
    as_of = timestamp(args.as_of)
    output = args.input.parent
    raw = [json.loads(line) for line in args.input.read_text().splitlines()]
    rows = []
    for issue in raw:
        if issue["is_pull_request"]:
            continue
        created = timestamp(issue["created_at"])
        closed = timestamp(issue["closed_at"]) if issue["closed_at"] else None
        if created > as_of or (closed and closed > as_of):
            raise ValueError("The snapshot contains events after --as-of")
        assert (closed is not None) == (issue["state"] == "closed")
        elapsed = ((closed or as_of) - created).total_seconds() / 86400
        assert elapsed >= 0
        rows.append({
            **issue,
            "created": created,
            "closed": closed,
            "observed_days": elapsed,
        })
    assert len({row["number"] for row in rows}) == len(rows)
    closed = [row for row in rows if row["closed"]]
    opened = [row for row in rows if not row["closed"]]
    annual, cohorts, quarterly, sensitivity, filtered_cohorts = [], [], [], [], []
    substantive = {
        "Resolution: satisfied", "Resolution: satisfied with concerns",
        "Resolution: unsatisfied", "Resolution: ambivalent", "Resolution: validated",
        "Resolution: object", "Resolution: lack of consensus",
    }
    for year in range(min(row["created"].year for row in rows), as_of.year + 1):
        completed = [row for row in closed if row["closed"].year == year]
        cohort = [row for row in rows if row["created"].year == year]
        annual.append({
            "period": str(year) + (" YTD" if year == as_of.year else ""),
            "plot_year": year,
            **describe([row["observed_days"] for row in completed]),
            "over_365_days_n": sum(row["observed_days"] > 365 for row in completed),
        })
        cohort_closed = [row for row in cohort if row["closed"]]
        cohort_stats = {
            "period": str(year) + (" YTD" if year == as_of.year else ""),
            "plot_year": year,
            "filed_n": len(cohort),
            "still_open_n": len(cohort) - len(cohort_closed),
            **describe([row["observed_days"] for row in cohort_closed]),
            "km_median_days": km_median(cohort),
        }
        for horizon in [90, 180, 365]:
            fully_observed = all(
                row["closed"] or (as_of - row["created"]).total_seconds() / 86400 >= horizon
                for row in cohort
            )
            cohort_stats[f"closed_within_{horizon}_pct"] = (
                100 * sum(row["closed"] is not None and row["observed_days"] <= horizon for row in cohort) / len(cohort)
                if fully_observed and cohort else None
            )
        cohorts.append(cohort_stats)
        filtered_cohort = [row for row in cohort if is_non_charter_review(row)]
        filtered_cohorts.append({
            "period": cohort_stats["period"],
            "plot_year": year,
            "filed_n": len(filtered_cohort),
            "still_open_n": sum(row["closed"] is None for row in filtered_cohort),
            **describe([row["observed_days"] for row in filtered_cohort if row["closed"]]),
            "km_median_days": km_median(filtered_cohort),
        })
        for name, subset in [
            ("all_issues", completed),
            ("exclude_charters_and_questions", [
                row for row in completed if is_non_charter_review(row)
            ]),
            ("substantive_resolution_labels_only", [
                row for row in completed if substantive.intersection(row["labels"])
            ]),
        ]:
            sensitivity.append({
                "period": annual[-1]["period"],
                "subset": name,
                **describe([row["observed_days"] for row in subset]),
            })
        for quarter in range(1, 5):
            start = dt.datetime(year, quarter * 3 - 2, 1, tzinfo=dt.timezone.utc)
            end = (
                dt.datetime(year + 1, 1, 1, tzinfo=dt.timezone.utc)
                if quarter == 4 else dt.datetime(year, quarter * 3 + 1, 1, tzinfo=dt.timezone.utc)
            )
            if start > as_of:
                continue
            period_end = min(end, as_of)
            trailing_start = period_end.replace(year=period_end.year - 1)
            in_quarter = [row for row in closed if start <= row["closed"] < period_end]
            trailing = [row for row in closed if trailing_start <= row["closed"] < period_end]
            quarterly.append({
                "period": f"{year} Q{quarter}" + (" partial" if end > as_of else ""),
                "plot_year": year + (quarter - 1) / 4,
                **describe([row["observed_days"] for row in in_quarter]),
                **{f"trailing_12m_{key}": value for key, value in describe([row["observed_days"] for row in trailing]).items()},
            })
    summary = {
        "repository": "w3ctag/design-reviews",
        "as_of": args.as_of,
        "bot_commit": "003aff0719a7e383f0425f9baac1f56770768059",
        "issues_n": len(rows),
        "pull_requests_excluded_n": len(raw) - len(rows),
        "closed": describe([row["observed_days"] for row in closed]),
        "excluding_charters_and_questions": describe([
            row["observed_days"] for row in closed if is_non_charter_review(row)
        ]),
        "open_age": describe([row["observed_days"] for row in opened]),
        "closed_within_days_pct": {
            str(days): 100 * sum(row["observed_days"] <= days for row in closed) / len(closed)
            for days in [30, 90, 180, 365, 730]
        },
        "open_over_180_days_n": sum(row["observed_days"] > 180 for row in opened),
        "open_over_365_days_n": sum(row["observed_days"] > 365 for row in opened),
        "longest_closed": [
            {key: row[key] for key in ["number", "title", "created_at", "closed_at", "observed_days", "html_url"]}
            for row in sorted(closed, key=lambda row: row["observed_days"], reverse=True)[:10]
        ],
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for name, data in [
        ("closure-year.csv", annual), ("filing-year.csv", cohorts),
        ("quarterly.csv", quarterly), ("sensitivity.csv", sensitivity),
        ("filing-year-excluding-charters-and-questions.csv", filtered_cohorts),
    ]:
        write_csv(output / name, data)
    write_csv(output / "issue-latency.csv", [
        {
            "number": row["number"], "title": row["title"], "created_at": row["created_at"],
            "closed_at": row["closed_at"], "state": row["state"],
            "latency_days": row["observed_days"] if row["closed"] else None,
            "open_age_days": row["observed_days"] if not row["closed"] else None,
            "labels": ";".join(row["labels"]), "url": row["html_url"],
        }
        for row in rows
    ])
    charts = [
        ("closure-year.svg", draw_chart(
            "TAG design-review time to closure, by closure year",
            "All repository issues; 2026 is year-to-date through October 19",
            annual,
            [("mean_days", "Mean", "#b45309"), ("median_days", "Median", "#0369a1"), ("p90_days", "90th percentile", "#7e22ce")],
            as_of.year + 0.25, math.ceil(max(row["p90_days"] for row in annual) / 100) * 100,
        )),
        ("filing-year.svg", draw_chart(
            "Time to closure, by year the review was filed",
            "Kaplan-Meier includes still-open reviews as right-censored; recent cohorts have incomplete follow-up",
            cohorts,
            [("median_days", "Closed-only median", "#94a3b8"), ("km_median_days", "Kaplan-Meier median", "#0369a1")],
            as_of.year + 0.25, math.ceil(max(row["km_median_days"] or 0 for row in cohorts) / 100) * 100,
        )),
        ("rolling-latency.svg", draw_chart(
            "Trailing 12-month closure latency, sampled quarterly",
            "Issue-weighted windows, not an average of monthly averages; final quarter is partial",
            quarterly,
            [("trailing_12m_mean_days", "Mean", "#b45309"), ("trailing_12m_median_days", "Median", "#0369a1")],
            as_of.year + 1, math.ceil(max(row["trailing_12m_mean_days"] or 0 for row in quarterly) / 100) * 100,
        )),
        ("filtered-closure-year.svg", draw_chart(
            "Closure latency excluding charter and question issues",
            "Same baseline as the all-issues chart before 2025; 2026 is year-to-date",
            [
                {**row, "plot_year": int(row["period"][:4])}
                for row in sensitivity if row["subset"] == "exclude_charters_and_questions"
            ],
            [("mean_days", "Mean", "#b45309"), ("median_days", "Median", "#0369a1"), ("p90_days", "90th percentile", "#7e22ce")],
            as_of.year + 0.25,
            math.ceil(max(row["p90_days"] for row in sensitivity if row["subset"] == "exclude_charters_and_questions") / 100) * 100,
        )),
        ("filtered-filing-year.svg", draw_chart(
            "Filing cohorts excluding charter and question issues",
            "2026: 35 of 66 still open; closed-only medians understate eventual cohort latency",
            filtered_cohorts,
            [("median_days", "Closed-only median", "#94a3b8"), ("km_median_days", "Kaplan-Meier median", "#0369a1")],
            as_of.year + 0.25,
            math.ceil(max(row["km_median_days"] or 0 for row in filtered_cohorts) / 100) * 100,
        )),
    ]
    for name, svg in charts:
        (output / name).write_text(svg + "\n")
    report = [
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>TAG review latency</title>',
        "<style>body{font:16px system-ui;max-width:1100px;margin:35px auto;padding:0 20px;color:#172033}"
        "svg{width:100%;height:auto}table{border-collapse:collapse;margin:20px 0}"
        "td,th{padding:7px 12px;border:1px solid #ddd;text-align:right}th{background:#f1f5f9}"
        "code{background:#f1f5f9}p,li{line-height:1.5}</style>",
        "<h1>W3C TAG design-review closure latency</h1>",
        f"<p>Snapshot: {html.escape(args.as_of)}. {len(rows):,} issues; {len(closed):,} closed; "
        f"{len(opened)} open. Excludes {len(raw)-len(rows)} pull requests.</p>",
        f"<p>All-time mean: <strong>{summary['closed']['mean_days']:.1f} days</strong>; "
        f"median: <strong>{summary['closed']['median_days']:.1f} days</strong>; "
        f"90th percentile: <strong>{summary['closed']['p90_days']:.1f} days</strong>.</p>",
        "<p><strong>Important scope effect:</strong> the bot includes charter reviews and questions. "
        "Excluding those labeled issues, the 2025 and 2026 YTD closure-year medians are both about "
        "89 days and means are about 211 and 214 days. Thus the apparent 2025-to-2026 improvement "
        "in the all-issues average does not persist under this scope restriction. This is a label-based "
        "sensitivity analysis, not a hand-classification of every issue.</p>",
        "<p>Long-term improvement is not monotonic. Among non-charter, non-question filing cohorts, "
        "the censoring-adjusted median is about 76 days for 2025 and 112 days for 2026 YTD. "
        "The latter is preliminary: 35 of 66 requests remain open, and the estimate assumes "
        "filing-date-related censoring is independent of eventual duration within the cohort. "
        "The 2026 closed-only filing-cohort mean of 54 days is not a valid estimate of "
        "the eventual mean for all 2026 requests.</p>",
        "<h2>Method and interpretation</h2><ul>",
        "<li>Latency is elapsed calendar days between created_at and the current closed_at timestamp. "
        "It includes external waiting and any closed intervals before reopening; it is not staff effort "
        "or a measure of cumulative time in the open state. No individual closure-actor attribution is made.</li>",
        "<li>Baseline matches the bot metrics page: all issues in w3ctag/design-reviews, regardless of labels. "
        "Closure does not necessarily mean a substantive review or approval: declined, withdrawn, timed-out, "
        "charter and question issues are included.</li>",
        "<li>Closure-year results answer how long the issues closed that year took. "
        "Filing-year results answer how long a cohort of requests took. Recent closed-only filing cohorts "
        "favor quick completions; Kaplan-Meier accounts for administrative right censoring of open issues. "
        "It assumes censoring is independent of eventual duration within each cohort. "
        "Survival medians are estimates without confidence intervals, not forecasts.</li>",
        "<li>For a filing cohort, fixed-horizon completion percentages are withheld if any still-open issue "
        "has less follow-up than that horizon. Percentiles of completed issues use linear interpolation. "
        "Kaplan-Meier uses the first time estimated survival reaches 0.5, which can differ slightly "
        "from an interpolated sample median even without censoring.</li>",
        "<li>Annual numbers are issue-weighted, not averages of monthly averages. "
        "Sensitivity subsets use labels as observed at snapshot time; label coverage and review mix "
        "change over time. These are descriptive trends, not causal claims about TAG productivity.</li></ul>",
        '<p>Sources: <a href="https://github.com/w3ctag/design-reviews">review repository</a>; '
        '<a href="https://github.com/w3ctag/w3ctagbot/blob/003aff0719a7e383f0425f9baac1f56770768059/src/pages/metrics.astro">'
        'bot metrics source</a>; '
        '<a href="https://github.com/w3ctag/w3ctagbot/blob/003aff0719a7e383f0425f9baac1f56770768059/github-gql/get-recent-issues.graphql">'
        "bot issue query</a>. Raw API metadata and reproducible Python analysis are saved alongside this report.</p>",
        "<h2>By closure year</h2>", charts[0][1],
        table(annual, [("period", "Closed in"), ("n", "Closed issues"), ("mean_days", "Mean days"),
                       ("median_days", "Median days"), ("p90_days", "P90 days"), ("over_365_days_n", ">365 days")]),
        "<h2>By filing year</h2>", charts[1][1],
        table(cohorts, [("period", "Filed in"), ("filed_n", "Filed"), ("still_open_n", "Still open"),
                        ("mean_days", "Closed-only mean"), ("median_days", "Closed-only median"),
                        ("km_median_days", "KM median"), ("closed_within_180_pct", "Closed <=180 days %"),
                        ("closed_within_365_pct", "Closed <=365 days %")]),
        "<h2>Smoothed closure trend</h2>", charts[2][1],
        "<h2>Sensitivity to issue selection</h2>",
        charts[3][1],
        table([row for row in sensitivity if int(row["period"][:4]) >= 2018],
              [("period", "Closed in"), ("subset", "Subset"), ("n", "Closed issues"),
               ("mean_days", "Mean days"), ("median_days", "Median days")]),
        "<h2>Filing cohorts excluding charter and question issues</h2>",
        charts[4][1],
        table(filtered_cohorts, [
            ("period", "Filed in"), ("filed_n", "Filed"), ("still_open_n", "Still open"),
            ("mean_days", "Closed-only mean"), ("median_days", "Closed-only median"),
            ("km_median_days", "KM median"),
        ]),
        "<h2>Current backlog</h2>",
        f"<p>{len(opened)} open issues; mean age {summary['open_age']['mean_days']:.1f} days; "
        f"median age {summary['open_age']['median_days']:.1f} days. "
        f"{summary['open_over_180_days_n']} older than 180 days; "
        f"{summary['open_over_365_days_n']} older than 365 days.</p>",
        "</html>",
    ]
    (output / "report.html").write_text("\n".join(report) + "\n")
    print(json.dumps(summary, indent=2))
    print("\nFILING COHORTS")
    print(json.dumps(cohorts[-9:], indent=2))
    print("\nRECENT SENSITIVITY")
    print(json.dumps(sensitivity[-12:], indent=2))
    print("\nFILTERED FILING COHORTS")
    print(json.dumps(filtered_cohorts[-3:], indent=2))


if __name__ == "__main__":
    main()
