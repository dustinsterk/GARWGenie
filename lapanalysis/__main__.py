"""Command line entry point.

    python -m lapanalysis session.vbo                 # launch the UI
    python -m lapanalysis session.vbo --report        # print analysis
    python -m lapanalysis session.vbo --report --lap 3 --ref 4
    python -m lapanalysis session.vbo --laps          # just list lap times
    python -m lapanalysis session.vbo --csv out.csv   # per-corner metrics
"""

from __future__ import annotations

import argparse
import sys


def _export_overlay(args, session) -> int:
    """Burn the overlay onto a video, on a worker thread, printing progress.

    The encode runs off the main thread so the status line can update live and
    Ctrl-C can cancel cleanly, which is the CLI shape the feature was asked
    for: a background job reporting to a status field.
    """
    import sys
    import threading
    import time

    from .insights import analyse
    from .overlay import OverlayData, LAYOUTS
    from .export_video import ExportJob, reveal_in_file_manager, find_ffmpeg
    from .units import IMPERIAL, METRIC
    from .video import VideoSync

    if find_ffmpeg() is None:
        print("ffmpeg was not found. Install it (brew install ffmpeg / "
              "apt install ffmpeg), or `pip install imageio-ffmpeg`.",
              file=sys.stderr)
        return 1

    units = IMPERIAL if args.imperial else METRIC
    result = analyse(session, lap_number=args.lap, reference_number=args.ref,
                     min_radius=args.min_radius, peak_radius=args.peak_radius,
                     min_sustained_m=args.min_sustained, units=units)
    data = OverlayData.from_analysis(result, units)
    layout = LAYOUTS[args.overlay_layout]()

    lap = result.lap
    # anchor the clip: by default its first frame is the lap start, matching
    # the GUI default for a hand-picked clip. --overlay-offset shifts that.
    anchor = float(lap.t[0])
    if args.overlay_offset:
        anchor -= args.overlay_offset
    sync = VideoSync.manual(anchor)

    out = args.out or _default_out(args.overlay_video)
    job = ExportJob(data=data, sync=sync, layout=layout,
                    source_video=args.overlay_video, out_path=out)

    error: list = []
    done = threading.Event()

    def work() -> None:
        try:
            job.run(progress=_print_progress)
        except Exception as exc:                          # noqa: BLE001
            error.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        while not done.wait(0.2):
            pass
    except KeyboardInterrupt:
        print("\ncancelling…", file=sys.stderr)
        job.cancel()
        worker.join(timeout=5.0)
        return 130

    sys.stdout.write("\n")
    if error:
        print(f"export failed: {error[0]}", file=sys.stderr)
        return 1
    print(f"wrote {out}")
    if args.reveal:
        reveal_in_file_manager(out)
    return 0


def _print_progress(fraction: float, message: str) -> None:
    """One rewriting status line, so the terminal shows a live percentage."""
    import sys
    bar_w = 24
    filled = int(bar_w * fraction)
    bar = "█" * filled + "░" * (bar_w - filled)
    sys.stdout.write(f"\r[{bar}] {fraction * 100:5.1f}%  {message:<32}")
    sys.stdout.flush()


def _default_out(clip: str) -> str:
    import os
    base, _ext = os.path.splitext(clip)
    return f"{base}_overlay.mp4"


def _confirm(assume_yes: bool, question: str) -> bool:
    """Ask before destroying anything, unless told not to."""
    if assume_yes:
        return True
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lapanalysis",
        description="Lap Analysis (GARW Genie) — VBOX lap analysis and driver coaching")
    p.add_argument("file", nargs="?",
                   help="path to a .vbo, Garmin .fit or CSV log")
    p.add_argument("--report", action="store_true", help="print a text report")
    p.add_argument("--laps", action="store_true", help="list detected laps and exit")
    p.add_argument("--lap", type=int, default=None, help="lap to analyse")
    p.add_argument("--ref", type=int, default=None,
                   help="reference lap (default: fastest clean lap)")
    p.add_argument("--csv", metavar="PATH", help="write per-corner metrics to CSV")
    p.add_argument("--html", metavar="PATH",
                   help="write a standalone HTML report (plots included)")
    p.add_argument("--save-history", action="store_true",
                   help="record this session so progress can be tracked")
    p.add_argument("--trends", action="store_true",
                   help="show progress across recorded sessions at this circuit")
    p.add_argument("--to-vbo", metavar="PATH",
                   help="write the log out as a .vbo (e.g. to open a Garmin "
                        ".fit in VBOX Circuit Tools)")
    p.add_argument("--sectors", type=int, default=3, metavar="N",
                   help="equal sectors to use when the file declares no split "
                        "lines (default 3)")
    p.add_argument("--name-corner", nargs=2, metavar=("CORNER", "NAME"),
                   help="name a corner, e.g. --name-corner T4 'The Bowl'. "
                        "Names follow the apex between sessions; an empty "
                        "name removes it")
    p.add_argument("--list-corners", action="store_true",
                   help="list detected corners and any names they carry")
    p.add_argument("--clear-names", action="store_true",
                   help="forget the corner names at this circuit")
    p.add_argument("--forget", action="store_true",
                   help="remove this session from the history")
    p.add_argument("--forget-circuit", action="store_true",
                   help="remove every recorded session at this circuit")
    p.add_argument("--clear-history", action="store_true",
                   help="remove all recorded sessions, at every circuit")
    p.add_argument("--yes", "-y", action="store_true",
                   help="skip the confirmation prompt for the above")
    p.add_argument("--label", metavar="NAME",
                   help="name this circuit when recording history")
    p.add_argument("--ds", type=float, default=1.0,
                   help="distance-domain resolution in meters (default 1.0)")
    p.add_argument("--min-radius", type=float, default=200.0,
                   help="radius below which the track counts as turning")
    p.add_argument("--peak-radius", type=float, default=130.0,
                   help="a region must reach this radius to be called a corner")
    p.add_argument("--min-sustained", type=float, default=20.0,
                   help="meters a corner must hold that radius for (guards "
                        "against noise flickering kinks in and out)")
    p.add_argument("--imperial", action="store_true",
                   help="report in mph and feet instead of km/h and meters")
    p.add_argument("--east-positive", action="store_true",
                   help="file uses East-positive longitude (non-standard)")
    p.add_argument("--degrees", action="store_true",
                   help="file stores lat/lon in degrees, not arc-minutes")
    p.add_argument("--sf", nargs=4, type=float, metavar=("LAT1", "LON1", "LAT2", "LON2"),
                   help="override the start/finish line, decimal degrees")

    video = p.add_argument_group("video overlay export")
    video.add_argument("--overlay-video", metavar="CLIP",
                       help="burn the telemetry overlay onto this video file")
    video.add_argument("--out", metavar="PATH",
                       help="output path for the overlaid video "
                            "(default: <clip>_overlay.mp4)")
    video.add_argument("--overlay-layout", choices=("landscape", "portrait"),
                       default="landscape",
                       help="overlay layout (default: landscape)")
    video.add_argument("--overlay-offset", type=float, default=None,
                       metavar="SECONDS",
                       help="seconds the video started before the lap; "
                            "positive means the clip begins before the lap. "
                            "Default: the clip starts at the analysed lap")
    video.add_argument("--reveal", action="store_true",
                       help="open the file manager on the finished video")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.clear_history:
        # handled before anything else: clearing everything needs no log file,
        # and without this it falls through to launching the GUI
        from . import history
        count = len(history.load())
        if count and not _confirm(args.yes,
                                  f"Remove all {count} recorded sessions, at "
                                  f"every circuit? The log files themselves "
                                  f"are not touched."):
            print("left untouched")
            return 0
        print(f"cleared {history.clear()} sessions from {history.STORE_PATH}")
        return 0

    if not args.file:
        from .ui.app import launch
        return launch(None)

    from .parser import open_log
    from .laps import build_session
    from .insights import analyse
    from .report import insight_report, session_summary

    if args.file.lower().endswith(".fit"):
        vbo = open_log(args.file)
    else:
        vbo = open_log(args.file,
                       coords_in_minutes=not args.degrees,
                       lon_positive_west=not args.east_positive)
    session = build_session(vbo, ds=args.ds, start_finish_latlon=args.sf)

    if args.overlay_video:
        return _export_overlay(args, session)

    if args.laps:
        print(session_summary(session))
        return 0

    if args.to_vbo:
        from .parser import write_vbo
        write_vbo(vbo, args.to_vbo)
        print(f"wrote {args.to_vbo} ({vbo.n_samples} samples, "
              f"{len(vbo.splits)} split lines)")
        if not (args.report or args.csv or args.html or args.laps
                or args.save_history or args.trends):
            return 0

    from .units import IMPERIAL, METRIC
    units = IMPERIAL if args.imperial else METRIC

    if args.name_corner or args.list_corners or args.clear_names:
        from . import history
        from .insights import analyse as _analyse
        result = _analyse(session, lap_number=args.lap,
                          reference_number=args.ref,
                          min_radius=args.min_radius,
                          peak_radius=args.peak_radius,
                          min_sustained_m=args.min_sustained)
        key = history.circuit_key(session)
        ref = result.reference or result.lap

        if args.clear_names:
            print(f"forgot {history.clear_names(key)} corner names here")

        if args.name_corner:
            target, label = args.name_corner
            match = next((c for c in result.corners
                          if c.number.lower() == target.lower()
                          or c.name.lower() == target.lower()), None)
            if match is None:
                print(f"no corner called {target!r}; "
                      f"detected: {', '.join(c.name for c in result.corners)}")
                return 1
            i = ref.idx(match.s_geo_apex)
            history.set_name(key, float(ref.lat[i]), float(ref.lon[i]),
                             label, match.direction)
            print(f"{match.number} is now {label!r}" if label.strip()
                  else f"{match.number} is back to its number")

        if args.list_corners:
            history.apply_names(key, result.corners, ref)
            print(f"{'Corner':<22}{'No.':<6}{'Dir':<5}"
                  f"{'Radius ' + units.dist_label:>11}")
            for c in result.corners:
                print(f"{c.name[:21]:<22}{c.number:<6}{c.direction:<5}"
                      f"{units.d(c.min_radius):>11.0f}")
        if not (args.report or args.csv or args.html or args.laps
                or args.save_history or args.trends):
            return 0

    if not (args.report or args.csv or args.html
            or args.save_history or args.trends
            or args.forget or args.forget_circuit
            or args.name_corner or args.list_corners or args.clear_names):
        from .ui.app import launch
        return launch(args.file, session=session)

    result = analyse(session, lap_number=args.lap, reference_number=args.ref,
                     units=units,
                     min_radius=args.min_radius, peak_radius=args.peak_radius,
                     min_sustained_m=args.min_sustained)

    if args.report:
        from .report import quality_block
        print(session_summary(session))
        print()
        print(quality_block(session, result.corners, units))
        print()
        print(insight_report(result, session, units=units))

    if args.save_history or args.trends or args.forget or args.forget_circuit:
        from . import history
        key = history.circuit_key(session)
        if args.forget:
            n = history.forget_session(args.file)
            print(f"removed {n} recorded session for this file"
                  if n else "this file was not in the history")
        if args.forget_circuit:
            count = len(history.sessions_for(key))
            if count and not _confirm(args.yes,
                                      f"Remove all {count} recorded sessions "
                                      f"at this circuit?"):
                print("left untouched")
            else:
                print(f"removed {history.forget_circuit(key)} sessions "
                      f"at this circuit")
        if args.save_history:
            rec = history.build_record(session, result.corners,
                                       result.reference or result.lap,
                                       label=args.label)
            fresh = history.record_session(rec)
            print(f"{'recorded' if fresh else 're-recorded'} session at "
                  f"{rec.label} ({history.STORE_PATH})")
        if args.trends:
            from .report import trend_report
            trend = history.trend_for(key)
            if trend is None:
                n = len(history.sessions_for(key))
                print(f"Only {n} session recorded at this circuit — trends "
                      "need at least two.")
            else:
                print()
                print(trend_report(trend, units))

    if args.html:
        from .export import session_html
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(session_html(result, session, units))
        print(f"wrote {args.html}")

    if args.csv:
        import csv
        rows = [m.as_row() for m in result.metrics]
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            if rows:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        print(f"wrote {args.csv} ({len(rows)} corners)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
