"""Plot recorded utilization against the pace line.

Answers the question the whole tool exists to serve: is consumption actually
tracking the line, and when did it run hot?

matplotlib is an optional extra -- it is imported lazily so nothing on the hot
path or in the daemon ever pays for it:

    uv tool install "niceclaude[plot]"
"""

import os
import sys
from datetime import datetime, timezone

from ._shared import DEFAULT_M0, DEFAULT_M1

# From the validated reference palette: the first three categorical slots, a
# recessive neutral for the threshold, and a status colour for the over-line
# region -- which carries a text label, never colour alone.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SERIES_1 = "#2a78d6"   # categorical slot 1 -- blue
SERIES_2 = "#eb6834"   # categorical slot 2 -- orange
SERIES_3 = "#1baf7a"   # categorical slot 3 -- aqua
CRITICAL = "#d03b3b"   # status -- over the line
GOOD = "#0ca30c"

# Fixed order, never cycled: a bucket keeps its hue no matter how many buckets
# the log happens to hold. Three is also the documented cap for a chart where
# every pair of series can be compared directly -- which the overlay panel is,
# since all its traces share one pair of axes. The per-bucket panels above it
# are the facets that carry anything past the third.
PALETTE = (SERIES_1, SERIES_2, SERIES_3)

RESET_JITTER = 120


def _segments(points):
    """Split a bucket's samples wherever its window rolls.

    Without this the line would draw a vertical connector straight down across
    a reset, implying a fall in usage that never happened.
    """
    out, cur, prev_reset = [], [], None
    for p in points:
        reset = p["resets_epoch"]
        rolled = (
            prev_reset is not None and reset is not None
            and abs(reset - prev_reset) > RESET_JITTER
        )
        if rolled and cur:
            out.append(cur)
            cur = []
        cur.append(p)
        prev_reset = reset if reset is not None else prev_reset
    if cur:
        out.append(cur)
    return out


def _draw_window(ax, run):
    """One live window: the pace line, plus shading only where usage exceeds it."""
    xs = [r[0] for r in run]
    ys = [r[1] for r in run]
    allowed = [r[2] for r in run]
    ax.plot(xs, allowed, color=MUTED, lw=1.4, ls=(0, (5, 3)), zorder=2)
    ax.fill_between(xs, allowed, ys, where=[y > a for y, a in zip(ys, allowed)],
                    color=CRITICAL, alpha=0.18, lw=0, zorder=1, interpolate=True)


def _allowed(p, m0, m1):
    """The pace line at this sample's instant, or None when unknowable."""
    if p["resets_epoch"] is None or p["window_seconds"] is None:
        return None
    start = p["resets_epoch"] - p["window_seconds"]
    ft = (p["ts_epoch"] - start) / p["window_seconds"]
    return m0 + ft * (100 - m0 - m1)


def per_model_keys(series):
    """The per-model weekly buckets in a log, in a stable order.

    `/usage` prints one of these only for a model that has a limit of its own,
    and names it with a server-supplied display name -- "week:Fable" on this
    account, "week:Sonnet only" on a max subscription -- so unlike
    "week:all models" the key cannot be written down in advance. Anything
    weekly that is not the shared bucket is model-scoped by construction,
    which is the rule `model_matches` already applies in the hook.
    """
    out = []
    for key in series:
        low = key.lower()
        if low.startswith("week:") and low[len("week:"):].strip() != "all models":
            out.append(key)
    return sorted(out, key=str.lower)


def panels_for(series):
    """Which buckets get a panel, in draw order.

    The per-model weekly bucket belongs here because it is routinely the line
    that actually binds: on the log this was written against, week:Fable stood
    at 54% while the shared weekly bucket enclosing it read 29%. Drawing only
    the shared bucket hides the constraint that stops the work, and leaves the
    plot disagreeing with what `status` prices.
    """
    return ([k for k in ("session", "week:all models") if k in series]
            + per_model_keys(series))


def _slot(i):
    """Hue for panel `i`, or a neutral once the fixed order is exhausted.

    Cycling back to slot 1 for a fourth bucket would paint two different
    buckets the same blue, which is the one thing a fixed categorical order
    exists to prevent. A bucket past the order keeps its identity from its
    panel title instead, and is left off the overlay.
    """
    return PALETTE[i] if i < len(PALETTE) else INK_2


def clip(records, days):
    """Keep the last `days` days of a log. None keeps all of it.

    Anchored on the newest sample, not on the wall clock. While `watch` runs
    those are the same instant; they diverge only once the log has gone stale,
    and there "the last week of the record" is what someone asking for a week
    wants, while "the last week of wall time" is an empty figure. It also means
    a positive --days can never empty a log that had anything in it, so the one
    remaining way to reach "nothing to plot" is the way that always existed: a
    log with no samples. Which view you are looking at stays legible either
    way, because the x-axis is dated and the title carries the span.

    Clipping happens before collect() rather than after, so the regex in
    parse_usage never runs over the records being thrown away -- on the log
    this was written against, `--days 1` parses 82 records instead of 10856.
    """
    if days is None:
        return records
    newest = max((r["ts_epoch"] for r in records
                  if r.get("exit_code") == 0 and r.get("ts_epoch") is not None),
                 default=None)
    if newest is None:                  # nothing usable; let collect say so
        return records
    cutoff = newest - days * 86400
    return [r for r in records if (r.get("ts_epoch") or 0) >= cutoff]


def collect(records, parse_usage):
    """records -> {bucket_key: [sample dicts sorted by time]}"""
    series = {}
    for rec in records:
        if rec.get("exit_code") != 0:
            continue
        when = datetime.fromtimestamp(rec["ts_epoch"], timezone.utc)
        buckets, _unparsed, _info = parse_usage(rec["raw"], when)
        for key, b in buckets.items():
            if b["pct"] is None:
                continue
            series.setdefault(key, []).append({
                "ts_epoch": rec["ts_epoch"],
                "pct": b["pct"],
                "resets_epoch": b["resets_epoch"],
                "window_seconds": b["window_seconds"],
            })
    for key in series:
        series[key].sort(key=lambda p: p["ts_epoch"])
    return series


def render(series, out_path, m0=DEFAULT_M0, m1=DEFAULT_M1):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        print('error: matplotlib not installed. Install with:\n'
              '  uv tool install "niceclaude[plot]"', file=sys.stderr)
        return 1

    panels = panels_for(series)
    if not panels:
        print("no plottable buckets in the log", file=sys.stderr)
        return 1

    fig = plt.figure(figsize=(13, 3.3 * len(panels) + 4.4), facecolor=SURFACE)
    gs = fig.add_gridspec(len(panels) + 1, 1,
                          height_ratios=[1] * len(panels) + [1.35], hspace=0.42)
    axes = [fig.add_subplot(gs[i]) for i in range(len(panels))]
    for a in axes[:-1]:
        a.sharex(axes[-1])
    norm_ax = fig.add_subplot(gs[len(panels)])

    def dt(ts):
        return datetime.fromtimestamp(ts, timezone.utc)

    def chrome(ax, title):
        ax.set_facecolor(SURFACE)
        ax.set_title(title, color=INK, fontsize=12, loc="left", pad=10,
                     fontweight="bold")
        ax.grid(True, color=GRID, lw=0.8, zorder=0)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=9)

    stats, windows = {}, {}
    for idx, (ax, key) in enumerate(zip(axes, panels)):
        colour = _slot(idx)
        pts = series[key]
        over_count = total = idle = 0
        worst = 0.0
        wins = []

        for seg in _segments(pts):
            xs = [dt(p["ts_epoch"]) for p in seg]
            ys = [p["pct"] for p in seg]
            ax.plot(xs, ys, color=colour, lw=2.0, zorder=4,
                    solid_capstyle="round")

            # A window that was never started has no resets_at, so no pace line
            # exists for it. Shading those spans says "nothing to adhere to"
            # rather than leaving a gap that reads as missing data.
            run, prev_active = [], None
            for p, x, y in zip(seg, xs, ys):
                a = _allowed(p, m0, m1)
                active = a is not None
                if active:
                    run.append((x, y, a))
                    total += 1
                    if y > a:
                        over_count += 1
                        worst = max(worst, y - a)
                else:
                    idle += 1
                if prev_active is True and not active and run:
                    _draw_window(ax, run); wins.append(run); run = []
                if prev_active is False and active:
                    ax.axvspan(xs[0] if not run else x, x, color=MUTED,
                               alpha=0.055, lw=0, zorder=0)
                prev_active = active
            if run:
                _draw_window(ax, run)
                wins.append(run)

        windows[key] = wins
        stats[key] = (over_count, total, idle, worst)
        ax.set_ylim(0, 100)
        ax.set_ylabel("% of budget", color=INK_2, fontsize=10)
        pct_over = 100.0 * over_count / total if total else 0.0
        share_idle = 100.0 * idle / (idle + total) if (idle + total) else 0.0
        chrome(ax, f"{key}    —    {pct_over:.1f}% of live samples above the line"
                   f"    \u00b7    {share_idle:.0f}% of the log had no window running")

    for a in axes[:-1]:          # shared x: only the bottom time panel is labelled
        a.tick_params(labelbottom=False)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M", tz=timezone.utc))
    axes[-1].set_xlabel("UTC", color=INK_2, fontsize=10)
    for lbl in axes[-1].get_xticklabels():
        lbl.set_rotation(30)
        lbl.set_horizontalalignment("right")

    # Every live window overlaid on window-progress. This is the direct answer
    # to "is utilization sticking to the line": the diagonal IS the line, so
    # anything above it is over budget regardless of which window it came from.
    norm_ax.set_facecolor(SURFACE)
    diag_x = [0, 100]
    diag_y = [m0, 100 - m1]
    norm_ax.plot(diag_x, diag_y, color=MUTED, lw=1.6, ls=(0, (5, 3)), zorder=3)
    norm_ax.fill_between(diag_x, diag_y, [100, 100], color=CRITICAL, alpha=0.07,
                         lw=0, zorder=1)
    norm_ax.annotate("over budget", xy=(3, 97), color=CRITICAL, fontsize=10,
                     va="top", ha="left", fontweight="bold")
    norm_ax.annotate("the pace line", xy=(72, (m0 + (100 - m1)) * 0.5 - 2),
                     color=INK_2, fontsize=9, rotation=26, va="top")

    # Sliced, not zipped against the palette: zip() would drop the extra bucket
    # silently, and a bucket vanishing from a budget chart is the failure this
    # whole change was made to fix.
    overlaid = panels[:len(PALETTE)]
    omitted = panels[len(PALETTE):]
    for idx, key in enumerate(overlaid):
        colour = _slot(idx)
        drawn = 0
        for run in windows.get(key, []):
            if len(run) < 2:
                continue
            fx, fy = [], []
            for x, y, a in run:
                # invert allowed() back to window progress
                fx.append(100.0 * (a - m0) / (100 - m0 - m1))
                fy.append(y)
            # Later buckets draw on top. A 26-day log holds well over a
            # hundred 5-hour session windows against three weekly ones, so
            # without this the weekly traces -- the ones that actually bind --
            # are laid under a hairball of session lines and cannot be read.
            norm_ax.plot(fx, fy, color=colour, lw=1.8, alpha=0.75,
                         zorder=4 + idx, solid_capstyle="round",
                         label=key if drawn == 0 else None)
            drawn += 1
    norm_ax.set_xlim(0, 100)
    norm_ax.set_ylim(0, 100)
    norm_ax.set_xlabel("% through the window", color=INK_2, fontsize=10)
    norm_ax.set_ylabel("% of budget used", color=INK_2, fontsize=10)
    chrome(norm_ax, "Every live window, overlaid on window progress")
    # A log in which no bucket ever carried a reset clause labels nothing, and
    # legend() then warns on an empty call rather than drawing anything.
    if norm_ax.get_legend_handles_labels()[0]:
        # Opaque, because the corner it sits in is full of traces: this legend
        # is the only thing tying a hue back to a bucket name, so it cannot be
        # the part of the figure that is hardest to read.
        leg = norm_ax.legend(loc="lower right", fontsize=10, frameon=True,
                             facecolor=SURFACE, edgecolor=GRID, framealpha=1.0)
        leg.set_zorder(10)
        for t in leg.get_texts():
            t.set_color(INK_2)

    span_h = (series[panels[0]][-1]["ts_epoch"] - series[panels[0]][0]["ts_epoch"]) / 3600
    fig.suptitle(
        f"niceclaude — utilization vs pace line   "
        f"(m0={m0:g}, m1={m1:g};  {span_h:.0f}h)",
        color=INK, fontsize=14, fontweight="bold", x=0.012, ha="left", y=0.995)
    fig.subplots_adjust(left=0.075, right=0.985, top=0.935, bottom=0.075)
    fig.savefig(out_path, dpi=150, facecolor=SURFACE)
    print(f"wrote {out_path}")
    for key, (over, total, idle, worst) in stats.items():
        pc = 100.0 * over / total if total else 0.0
        print(f"  {key:22} {over}/{total} live samples over the line ({pc:.1f}%), "
              f"worst overshoot {worst:.1f} pts; {idle} samples with no window running")
    if omitted:
        print(f"  note: {', '.join(omitted)} {'has' if len(omitted) == 1 else 'have'} "
              f"a panel but no trace on the overlay -- the fixed colour order "
              f"stops at {len(PALETTE)}")
    return 0
