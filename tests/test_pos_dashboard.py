"""Automated tests of the data layer, validation, figure builders and CLI."""

from __future__ import annotations

import itertools
import math
import socket
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import pytest

import pos_dashboard as app

DATA_FILE = Path(app.__file__).resolve().parent / "pos_data.csv"
SCHEME = app.SCHEMES[app.DEFAULT_SCHEME]


@pytest.fixture(scope="module")
def data() -> app.RunData:
    return app.load_run_data(DATA_FILE)


@pytest.fixture(scope="module")
def full(data: app.RunData) -> app.Selection:
    return app.select_runs(data, app.RunFilter.default(data.bounds))


@pytest.fixture(scope="module")
def empty(data: app.RunData) -> app.Selection:
    run_filter, _ = _filter(data, player="#nobody#")
    return app.select_runs(data, run_filter)


def _filter(data: app.RunData, **overrides) -> tuple[app.RunFilter, list[app.Alert]]:
    default = app.RunFilter.default(data.bounds)
    values = {
        "start": default.start,
        "end": default.end,
        "key_range": [default.key_min, default.key_max],
        "factions": list(default.factions),
        "affix": app.ALL,
        "classes": [],
        "min_ilvl": None,
        "player": "",
    }
    values.update(overrides)
    return app.build_filter(data.bounds, **values)


# --- formatting -------------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(27.5, "27:30"), (1800999 / 60000, "30:00"), (0.0, "0:00"), (-1.5, "−1:30"), (None, "—")],
)
def test_fmt_duration(minutes, expected):
    assert app.fmt_duration(minutes) == expected


def test_czech_number_formatting():
    nbsp = app.NBSP
    assert app.fmt_int(19692) == f"19{nbsp}692"
    assert app.fmt_float(1234.56, 1) == f"1{nbsp}234,6"
    assert app.fmt_pct(12.345) == f"12,3{nbsp}%"
    assert app.format_metric(17.0, "level") == "+17"
    assert app.format_metric(17.26, "level") == "+17,3"


def test_runs_word():
    assert [app.runs_word(n) for n in (1, 3, 5)] == ["run", "runy", "runů"]


# --- loading ------------------------------------------------------------------


def test_load_real_data(data: app.RunData):
    assert len(data.runs) == 19692
    assert data.runs["keystone_run_id"].is_unique
    assert len(data.members) == 5 * len(data.runs)
    assert data.bounds.key_min == 16 and data.bounds.key_max == 22
    # Zero item levels are treated as missing values.
    assert data.missing_ilvl > 0
    assert (data.members["ilvl"].dropna() > 0).all()


def test_load_missing_file(tmp_path: Path):
    with pytest.raises(app.DataLoadError, match="neexistuje"):
        app.load_run_data(tmp_path / "missing.csv")


def test_load_empty_file(tmp_path: Path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(app.DataLoadError, match="prázdný"):
        app.load_run_data(path)


def test_load_missing_columns(tmp_path: Path):
    path = tmp_path / "bad.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(app.DataLoadError, match="chybí povinné sloupce"):
        app.load_run_data(path)


def test_invalid_rows_are_dropped(tmp_path: Path):
    raw = pd.read_csv(DATA_FILE, nrows=20)
    raw.loc[0, "completed_at"] = "not a date"
    raw.loc[1, "clear_time_ms"] = -5
    raw.loc[2, "keystone_run_id"] = raw.loc[3, "keystone_run_id"]
    path = tmp_path / "dirty.csv"
    raw.to_csv(path, index=False)
    data = app.load_run_data(path)
    assert data.dropped_rows == 3
    assert len(data.runs) == 17


# --- filter validation --------------------------------------------------------


def test_default_filter_matches_everything(data: app.RunData, full: app.Selection):
    run_filter, alerts = _filter(data)
    assert alerts == []
    assert len(full.runs) == len(data.runs)
    assert app.RunFilter.from_store(run_filter.to_store()) == run_filter


def test_swapped_dates_are_reported(data: app.RunData):
    first, last = data.bounds.dates[0].date().isoformat(), data.bounds.dates[-1].date().isoformat()
    run_filter, alerts = _filter(data, start=last, end=first)
    assert (run_filter.start, run_filter.end) == (first, last)
    assert any("prohozena" in alert.message for alert in alerts)


def test_dates_out_of_range_are_clamped(data: app.RunData):
    run_filter, alerts = _filter(data, start="2000-01-01", end="garbage")
    assert run_filter.start == data.bounds.dates[0].date().isoformat()
    assert {alert.level for alert in alerts} == {"warning"}
    assert len(alerts) == 2


def test_key_range_is_clamped(data: app.RunData):
    run_filter, alerts = _filter(data, key_range=[2, 40])
    assert (run_filter.key_min, run_filter.key_max) == (data.bounds.key_min, data.bounds.key_max)
    assert alerts and alerts[0].level == "warning"


def test_no_faction_is_an_error(data: app.RunData):
    run_filter, alerts = _filter(data, factions=[])
    assert any(alert.level == "error" for alert in alerts)
    assert app.select_runs(data, run_filter).empty


def test_too_many_classes_is_an_error(data: app.RunData):
    _, alerts = _filter(data, classes=list(data.bounds.classes[:6]))
    assert any(alert.level == "error" and "5 členů" in alert.message for alert in alerts)


@pytest.mark.parametrize("value", [500, -1, "abc"])
def test_invalid_item_level_is_ignored(data: app.RunData, value):
    run_filter, alerts = _filter(data, min_ilvl=value)
    assert run_filter.min_ilvl is None
    assert alerts and alerts[0].level == "warning"


def test_filters_narrow_the_selection(data: app.RunData):
    run_filter, alerts = _filter(data, key_range=[18, 18], classes=["Mage"], min_ilvl=275, affix="Tyrannical")
    assert alerts == []
    runs = app.select_runs(data, run_filter).runs
    assert not runs.empty
    assert (runs["mythic_level"] == 18).all()
    assert (runs["avg_ilvl"] >= 275).all()
    assert (runs["affix"] == "Tyrannical").all()
    class_columns = runs[[f"{slot}_class" for slot in app.SLOT_ROLES]]
    assert class_columns.eq("Mage").any(axis=1).all()


def test_player_search_is_case_insensitive(data: app.RunData):
    name = data.runs["tank_name"].iloc[0]
    run_filter, _ = _filter(data, player=name.upper())
    runs = app.select_runs(data, run_filter).runs
    assert len(runs) >= 1
    assert runs["player_search"].str.contains(name.casefold(), regex=False).all()


def test_empty_selection_is_explained(data: app.RunData, empty: app.Selection):
    run_filter, _ = _filter(data, player="#nobody#")
    alerts = app.describe_selection(empty, run_filter)
    assert alerts[0].level == "error" and "#nobody#" in alerts[0].message


@pytest.mark.parametrize(
    ("value", "expected", "has_alert"),
    [(10, 10, False), ("7", 7, False), (0, 1, True), (5000, 100, True), (None, 15, True), (2.5, 15, True)],
)
def test_coerce_int(value, expected, has_alert):
    result, alert = app.coerce_int(value, minimum=1, maximum=100, default=15, label="Test")
    assert result == expected
    assert (alert is not None) == has_alert


def test_choice_falls_back_to_default():
    assert app.choice("week", app.TIMELINE_GRANULARITY, "day") == "week"
    assert app.choice("<script>", app.TIMELINE_GRANULARITY, "day") == "day"


# --- tables and KPIs ----------------------------------------------------------


def test_kpis(full: app.Selection, empty: app.Selection):
    kpis = app.compute_kpis(full)
    assert len(kpis) == 7
    assert kpis[0].value == app.fmt_int(len(full.runs))
    assert all(kpi.value == "—" for kpi in app.compute_kpis(empty)[1:])


def test_top_runs_order(full: app.Selection):
    fastest = app.top_runs(full.runs, sort="clear", limit=5)
    assert [row.rank for row in fastest] == [1, 2, 3, 4, 5]
    assert fastest[0].time == app.fmt_duration(full.runs["clear_min"].min())
    highest = app.top_runs(full.runs, sort="key", descending=True, limit=1)
    assert highest[0].key == f"+{full.runs['mythic_level'].max()}"
    assert len(highest[0].team) == 5


# --- figure builders ----------------------------------------------------------

BUILDER_OPTIONS = [
    (app.build_timeline_figure, {"metric": app.TIMELINE_METRICS, "split": app.TIMELINE_SPLITS,
                                 "granularity": app.TIMELINE_GRANULARITY}),
    (app.build_leaderboard_figure, {"dimension": app.LEADER_DIMENSIONS, "role": app.LEADER_ROLES,
                                    "metric": app.LEADER_METRICS}),
    (app.build_share_figure, {"dimension": app.SHARE_DIMENSIONS, "labels": app.SHARE_LABELS}),
    (app.build_heatmap_figure, {"mode": app.HEATMAP_MODES, "metric": app.HEATMAP_METRICS}),
    (app.build_distribution_figure, {"group": app.DISTRIBUTION_GROUPS, "kind": app.DISTRIBUTION_KINDS,
                                     "points": app.DISTRIBUTION_POINTS}),
    (app.build_scatter_figure, {"color_by": app.SCATTER_COLORS}),
    (app.build_compositions_figure, {"level": app.COMPOSITION_LEVELS, "metric": app.COMPOSITION_METRICS}),
    (app.build_chord_figure, {"mode": app.CHORD_MODES, "highlight": (app.CHORD_NONE, "Monk"),
                              "class_colors": (True, False)}),
    (app.build_flow_figure, {"stages": app.FLOW_STAGES, "detail": app.FLOW_DETAIL, "timing": app.FLOW_TIMING}),
]


@pytest.mark.parametrize(("builder", "options"), BUILDER_OPTIONS, ids=lambda v: getattr(v, "__name__", ""))
def test_builders_handle_all_options(full: app.Selection, builder, options):
    for values in itertools.product(*options.values()):
        result = builder(full, SCHEME, **dict(zip(options, values, strict=True)))
        assert isinstance(result.figure, go.Figure)
        assert result.figure.data, f"{builder.__name__} produced no traces for {values}"


@pytest.mark.parametrize(("builder", "_"), BUILDER_OPTIONS, ids=lambda v: getattr(v, "__name__", ""))
@pytest.mark.parametrize("scheme", list(app.SCHEMES.values()), ids=list(app.SCHEMES))
def test_builders_show_message_for_empty_selection(empty: app.Selection, builder, _, scheme):
    figure = builder(empty, scheme).figure
    assert not figure.data
    assert figure.layout.annotations[0].text == app.NO_DATA


def test_invalid_combinations_produce_notes(full: app.Selection):
    timeline = app.build_timeline_figure(full, SCHEME, metric="median_time", cumulative=True)
    assert any("Kumulativní" in note.message for note in timeline.notes)
    heatmap = app.build_heatmap_figure(full, SCHEME, mode="dps_key", metric="mean_key")
    assert heatmap.notes and heatmap.figure.data
    distribution = app.build_distribution_figure(full, SCHEME, points="all")
    assert any(note.level == "warning" for note in distribution.notes)


def test_compositions_threshold(full: app.Selection):
    result = app.build_compositions_figure(full, SCHEME, min_runs=100000)
    assert not result.figure.data
    assert result.notes[0].level == "warning"


def test_scatter_sampling(full: app.Selection):
    result = app.build_scatter_figure(full, SCHEME, max_points=500, trend=False)
    points = sum(len(trace.x) for trace in result.figure.data)
    assert points == 500


# --- chord and flow charts ------------------------------------------------------


def test_class_pairs_count_each_run_once(full: app.Selection):
    pairs = app.class_pairs(full, "all")
    assert (pairs["a"] < pairs["b"]).all()
    assert pairs["count"].max() <= len(full.runs)
    assert pairs["count"].is_monotonic_decreasing
    tank_healer = app.class_pairs(full, "tank_healer")
    assert tank_healer["count"].sum() == len(full.runs)
    expected = int(((full.runs["tank_class"] == "Monk") & (full.runs["healer_class"] == "Druid")).sum())
    row = tank_healer[(tank_healer["a"] == "Monk") & (tank_healer["b"] == "Druid")]
    assert int(row["count"].iloc[0]) == expected


def test_class_presence(full: app.Selection):
    presence = app.class_presence(full, "all")
    with_monk = full.runs[[f"{slot}_class" for slot in app.SLOT_ROLES]].eq("Monk").any(axis=1).sum()
    assert presence["Monk"] == with_monk


@pytest.mark.parametrize("method", list(app.FLOW_TIMING))
def test_time_categories_are_ordered(full: app.Selection, method):
    categories = app.time_categories(full.runs, method)
    assert categories.cat.ordered and len(categories) == len(full.runs)
    medians = full.runs.groupby(categories, observed=True)["clear_min"].median()
    assert medians.is_monotonic_increasing


def test_chord_threshold_and_highlight(full: app.Selection):
    strict = app.build_chord_figure(full, SCHEME, min_share=app.CHORD_MAX_SHARE + 50)
    assert not strict.figure.data
    unknown = app.build_chord_figure(full, SCHEME, highlight="Bard")
    assert any("Bard" in note.message for note in unknown.notes)
    loose = app.build_chord_figure(full, SCHEME, min_share=0)
    ribbons = [trace for trace in loose.figure.data if trace.hoveron == "fills"]
    assert len(ribbons) == len(app.class_pairs(full, "all"))


def test_chord_labels_do_not_overlap():
    positions = app._chord_label_positions([0.0, 0.02, 0.04, 6.25, 6.27, math.pi])
    for right_side in (True, False):
        ys = sorted(y for x, y in positions if (x >= 0) == right_side)
        assert all(b - a >= app.CHORD_LABEL_SPACING - 1e-9 for a, b in zip(ys, ys[1:], strict=False))


def test_flow_links_conserve_runs(full: app.Selection):
    sankey = app.build_flow_figure(full, SCHEME, stages="tank_healer_time").figure.data[0]
    healers = [i for i, label in enumerate(sankey.node.label) if label.startswith("Healer")]
    incoming = sum(v for t, v in zip(sankey.link.target, sankey.link.value, strict=True) if t in healers)
    outgoing = sum(v for s, v in zip(sankey.link.source, sankey.link.value, strict=True) if s in healers)
    assert incoming == outgoing == len(full.runs)


@pytest.mark.parametrize(
    ("click", "expected"),
    [({"points": [{"customdata": ["Monk", "1", "2"]}]}, "Monk"), ({"points": [{"curveNumber": 3}]}, None), (None, None)],
)
def test_clicked_class(click, expected):
    assert app.clicked_class(click) == expected


# --- application and CLI --------------------------------------------------------


def test_create_app(data: app.RunData):
    dash_app = app.create_app(data, "light")
    assert "scheme-light" in dash_app.layout.className
    assert "scheme-saronite" in dash_app.index_string
    assert "page-next" in dash_app.index_string


# --- paging ---------------------------------------------------------------------


def test_every_card_is_on_exactly_one_page():
    card_ids = [card_id for _, ids in app.PAGES for card_id in ids]
    expected = ["timeline", "heatmap", "leaders", "share", "dist", "scatter", "comps", "chord", "flow", "runs"]
    assert sorted(card_ids) == sorted(expected)


@pytest.mark.parametrize(
    ("current", "step", "expected"),
    [(1, 1, 2), (1, -1, 1), (len(app.PAGES), 1, len(app.PAGES)), (3, 0, 3), (None, 0, 1), (99, 0, len(app.PAGES))],
)
def test_step_page(current, step, expected):
    assert app.step_page(current, step) == expected


def test_page_class():
    assert app.page_class(1, 1) == "page double"
    assert app.page_class(2, 1) == "page double hidden"
    single = next(number for number, (_, ids) in enumerate(app.PAGES, 1) if len(ids) == 1)
    assert app.page_class(single, single) == "page single"


def test_cli_defaults():
    args = app.parse_args([])
    assert args.data == app.DEFAULT_DATA_PATH
    assert (args.host, args.port, args.scheme) == (app.DEFAULT_HOST, app.DEFAULT_PORT, app.DEFAULT_SCHEME)


@pytest.mark.parametrize("argv", [["--port", "70000"], ["--port", "abc"], ["--timezone", "Mars/Olympus"],
                                  ["--scheme", "pink"]])
def test_cli_rejects_invalid_values(argv):
    with pytest.raises(SystemExit):
        app.parse_args(argv)


def test_main_reports_missing_data(tmp_path: Path, capsys):
    assert app.main(["--data", str(tmp_path / "missing.csv")]) == 1
    assert "Chyba" in capsys.readouterr().err


def test_check_port_detects_busy_port():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert "obsazen" in app.check_port("127.0.0.1", port)
