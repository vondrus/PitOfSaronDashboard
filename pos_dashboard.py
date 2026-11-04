#!/usr/bin/env python3
"""Pit of Saron Dashboard - interactive analysis of Mythic+ runs.

The application visualises Mythic+ runs of the *Pit of Saron* dungeon
(World of Warcraft) downloaded from the public Raider.IO API. Everything lives
in a single module, but the code is split into independent layers so that data
processing and Plotly figures do not depend on Dash at all:

1. configuration - constants, Czech UI labels and colour schemes,
2. formatting helpers - Czech number, date and duration formatting,
3. data layer - loading, cleaning, validation and filtering (pandas only),
4. figure builders - pure functions returning Plotly figures (no Dash),
5. Dash layout - composition of controls, figures and tables,
6. Dash callbacks - wiring user input to the data layer and figure builders,
7. command-line interface.

Run ``python pos_dashboard.py --help`` to list the command-line options.
"""

from __future__ import annotations

import argparse
import html as html_lib
import logging
import math
import os
import socket
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from dash import Dash, Input, Output, State, ctx, dcc, html
from plotly.colors import sample_colorscale

__version__ = "1.0.0"

logger = logging.getLogger("pos_dashboard")


# =============================================================================
# 1. Configuration
# =============================================================================

APP_TITLE = "Pit of Saron Dashboard"
DEFAULT_DATA_PATH = Path(__file__).resolve().parent / "pos_data.csv"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8050
DEFAULT_TIMEZONE = "Europe/Prague"
DEFAULT_SCHEME = "frost"

# Team slots used in the CSV file and the role each slot represents.
SLOT_ROLES: dict[str, str] = {
    "tank": "tank",
    "healer": "healer",
    "dps_1": "dps",
    "dps_2": "dps",
    "dps_3": "dps",
}
DPS_SLOTS = tuple(slot for slot, role in SLOT_ROLES.items() if role == "dps")
SLOT_FIELDS = ("name", "race", "class", "spec", "ilvl")
REQUIRED_COLUMNS = (
    "keystone_run_id",
    "completed_at",
    "mythic_level",
    "keystone_time_ms",
    "clear_time_ms",
    "faction",
    "modifier_1",
    *(f"{slot}_{name}" for slot in SLOT_ROLES for name in SLOT_FIELDS),
)

ALL = "all"
UNKNOWN = "Neuvedeno"
OTHERS = "Ostatní"
NBSP = "\u00a0"

FACTION_LABELS = {"mixed": "Smíšená", "alliance": "Aliance", "horde": "Horda", "unknown": UNKNOWN}
SEASON_LABELS = {"season-mn-1": "Midnight – sezóna 1"}
WEEKDAYS = ("Pondělí", "Úterý", "Středa", "Čtvrtek", "Pátek", "Sobota", "Neděle")

# Official in-game class colours (used optionally instead of the scheme palette).
CLASS_COLORS = {
    "Death Knight": "#C41E3A",
    "Demon Hunter": "#A330C9",
    "Druid": "#FF7C0A",
    "Evoker": "#33937F",
    "Hunter": "#AAD372",
    "Mage": "#3FC7EB",
    "Monk": "#00FF98",
    "Paladin": "#F48CBA",
    "Priest": "#FFFFFF",
    "Rogue": "#FFF468",
    "Shaman": "#0070DD",
    "Warlock": "#8788EE",
    "Warrior": "#C69B6D",
}

MIN_GROUP_RUNS = 5  # smallest sample for which medians/means are shown in rankings
MIN_CELL_RUNS = 3  # smallest sample for heatmap cells with medians/means
MAX_POINTS_ALL = 5000  # rendering every run as a point above this gets sluggish
MAX_PLAYER_QUERY = 40
MAX_TEAM_SIZE = 5
SMALL_SELECTION = 30
SCATTER_SEED = 42

FONT_FAMILY = '"Segoe UI", system-ui, -apple-system, Roboto, "Helvetica Neue", Arial, sans-serif'


@dataclass(frozen=True)
class Metric:
    """Aggregated metric shown in charts."""

    label: str
    kind: str  # "count", "percent", "duration", "level" or "ilvl"

    @property
    def zero_based(self) -> bool:
        """Whether bar charts of this metric should start at zero."""
        return self.kind in ("count", "percent")


METRICS: dict[str, Metric] = {
    "count": Metric("Počet runů", "count"),
    "share": Metric("Podíl runů", "percent"),
    "column_share": Metric("Podíl ve sloupci", "percent"),
    "median_time": Metric("Medián času dokončení", "duration"),
    "best_time": Metric("Nejrychlejší čas", "duration"),
    "median_remaining": Metric("Medián rezervy času", "duration"),
    "mean_key": Metric("Průměrná úroveň klíče", "level"),
    "max_key": Metric("Nejvyšší klíč", "level"),
    "mean_ilvl": Metric("Průměrný item level", "ilvl"),
}


def metric_options(keys: Iterable[str]) -> dict[str, str]:
    """Return ``{metric key: Czech label}`` for the given metric keys."""
    return {key: METRICS[key].label for key in keys}


TIMELINE_METRICS = metric_options(("count", "median_time", "best_time", "mean_key", "max_key", "mean_ilvl"))
TIMELINE_GRANULARITY = {"day": "Den", "week": "Týden"}
TIMELINE_SPLITS = {
    "none": "Nerozdělovat",
    "key_label": "Úroveň klíče",
    "affix": "Affix týdne",
    "faction_label": "Frakce",
    "tank_label": "Tank specializace",
}

LEADER_DIMENSIONS = {"spec": "Specializace", "class": "Třída", "player": "Hráč", "race": "Rasa"}
LEADER_COLUMNS = {"spec": "label", "class": "class", "player": "player", "race": "race"}
LEADER_ROLES = {ALL: "Všechny role", "tank": "Tank", "healer": "Healer", "dps": "DPS"}
LEADER_METRICS = metric_options(
    ("count", "share", "median_time", "best_time", "median_remaining", "mean_key", "max_key", "mean_ilvl")
)
SORT_ORDER = {"desc": "Sestupně", "asc": "Vzestupně"}

SHARE_DIMENSIONS = {
    "tank": "Tank specializace",
    "healer": "Healer specializace",
    "dps_class": "DPS třídy",
    "key": "Úroveň klíče",
    "affix": "Affix týdne",
    "faction": "Frakce",
    "race": "Rasy hráčů",
}
SHARE_LABELS = {"none": "Bez popisků", "percent": "Procenta", "label": "Název a procenta"}

HEATMAP_MODES = {
    "weekday_hour": "Den v týdnu × hodina",
    "tank_healer": "Tank × healer",
    "dps_key": "DPS třída × úroveň klíče",
}
HEATMAP_METRICS = metric_options(("count", "column_share", "median_time", "mean_key"))

DISTRIBUTION_GROUPS = {
    "key_label": "Úroveň klíče",
    "affix": "Affix týdne",
    "faction_label": "Frakce",
    "tank_label": "Tank specializace",
    "healer_label": "Healer specializace",
}
DISTRIBUTION_KINDS = {"box": "Krabicový", "violin": "Houslový"}
DISTRIBUTION_POINTS = {"none": "Bez bodů", "outliers": "Odlehlé", "all": "Všechny"}

SCATTER_COLORS = {
    "key_label": "Úroveň klíče",
    "affix": "Affix týdne",
    "faction_label": "Frakce",
    "tank_label": "Tank specializace",
}

COMPOSITION_LEVELS = {"spec": "Specializace", "class": "Třídy"}
COMPOSITION_METRICS = metric_options(("count", "median_time", "best_time", "mean_key", "max_key"))

RUN_SORTS = {
    "clear": ("Čas dokončení", "clear_time_ms"),
    "key": ("Úroveň klíče", "mythic_level"),
    "remaining": ("Rezerva času", "remaining_min"),
    "ilvl": ("Průměrný item level", "avg_ilvl"),
    "date": ("Datum dokončení", "completed_at"),
}

DROPDOWN_LABELS = {
    "select_all": "Vybrat vše",
    "deselect_all": "Zrušit výběr",
    "selected_count": "Vybráno: {num_selected}",
    "search": "Hledat",
    "clear_search": "Vymazat hledání",
    "clear_selection": "Vymazat výběr",
    "no_options_found": "Nic nenalezeno",
}

# Czech translation of Plotly.js UI strings (mode bar, box statistics, dates).
PLOTLY_LOCALE_CS = {
    "dictionary": {
        "Autoscale": "Automatické měřítko",
        "Box Select": "Obdélníkový výběr",
        "Compare data on hover": "Porovnat hodnoty při najetí myší",
        "Double-click on legend to isolate one trace": "Dvojklikem na legendu zobrazíte jen jednu řadu",
        "Double-click to zoom back out": "Dvojklikem vrátíte původní zobrazení",
        "Download plot": "Stáhnout graf",
        "Download plot as a png": "Stáhnout graf jako PNG",
        "Download plot as a PNG": "Stáhnout graf jako PNG",
        "Lasso Select": "Výběr lasem",
        "Pan": "Posun",
        "Produced with Plotly.js": "Vytvořeno pomocí Plotly.js",
        "Reset": "Obnovit",
        "Reset axes": "Obnovit osy",
        "Show closest data on hover": "Zobrazit nejbližší hodnotu při najetí myší",
        "Snapshot succeeded": "Snímek byl vytvořen",
        "Sorry, there was a problem downloading your snapshot!": "Snímek se bohužel nepodařilo stáhnout.",
        "Taking snapshot - this may take a few seconds": "Vytvářím snímek – může to chvíli trvat",
        "Toggle Spike Lines": "Přepnout vodicí čáry",
        "Zoom": "Přiblížení",
        "Zoom in": "Přiblížit",
        "Zoom out": "Oddálit",
        "kde:": "hustota:",
        "lower fence:": "dolní mez:",
        "max:": "max.:",
        "mean ± σ:": "průměr ± σ:",
        "mean:": "průměr:",
        "median:": "medián:",
        "min:": "min.:",
        "q1:": "1. kvartil:",
        "q3:": "3. kvartil:",
        "trace": "řada",
        "upper fence:": "horní mez:",
    },
    "format": {
        "days": ["neděle", "pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota"],
        "shortDays": ["ne", "po", "út", "st", "čt", "pá", "so"],
        "months": [
            "leden", "únor", "březen", "duben", "květen", "červen",
            "červenec", "srpen", "září", "říjen", "listopad", "prosinec",
        ],
        "shortMonths": ["led", "úno", "bře", "dub", "kvě", "čvn", "čvc", "srp", "zář", "říj", "lis", "pro"],
        "date": "%d.%m.%Y",
        "decimal": ",",
        "thousands": NBSP,
    },
}

GRAPH_CONFIG = {
    "displaylogo": False,
    "locale": "cs",
    "locales": {"cs": PLOTLY_LOCALE_CS},
    # "sendChartToCloud" would upload chart data to an external Plotly server.
    "modeBarButtonsToRemove": ["select2d", "lasso2d", "sendChartToCloud"],
    "toImageButtonOptions": {"format": "png", "scale": 2, "filename": "pit-of-saron"},
}


@dataclass(frozen=True)
class ColorScheme:
    """Visual theme shared by the page (CSS variables) and Plotly figures."""

    key: str
    label: str
    description: str
    dark: bool
    background: str
    surface: str
    input_background: str
    text: str
    muted: str
    border: str
    border_strong: str
    accent: str
    info: str
    warning: str
    danger: str
    colorway: tuple[str, ...]
    sequential: tuple[str, ...]

    def color(self, index: int) -> str:
        """Return the qualitative colour for the given category index."""
        return self.colorway[index % len(self.colorway)]

    def ordinal_colors(self, count: int) -> list[str]:
        """Return ``count`` colours of increasing intensity for ordered categories."""
        low, high = (0.5, 1.0) if self.dark else (0.0, 0.85)
        positions = [0.5 * (low + high)] if count == 1 else np.linspace(low, high, count).tolist()
        return sample_colorscale(list(self.sequential), positions)

    @cached_property
    def template(self) -> go.layout.Template:
        """Plotly template derived from the built-in light or dark template."""
        template = go.layout.Template(pio.templates["plotly_dark" if self.dark else "plotly_white"])
        axis = {
            "gridcolor": self.border,
            "linecolor": self.border_strong,
            "zerolinecolor": self.border_strong,
            "title": {"font": {"color": self.muted}},
        }
        template.layout.update(
            paper_bgcolor=self.surface,
            plot_bgcolor=self.surface,
            font={"family": FONT_FAMILY, "color": self.text, "size": 12},
            colorway=list(self.colorway),
            colorscale={"sequential": list(self.sequential)},
            xaxis=axis,
            yaxis=axis,
            hoverlabel={
                "bgcolor": self.input_background,
                "bordercolor": self.border_strong,
                "font": {"family": FONT_FAMILY, "color": self.text},
            },
            legend={"bgcolor": "rgba(0,0,0,0)"},
        )
        return template

    def css_variables(self) -> dict[str, str]:
        """CSS custom properties for the page and Dash 4 core components."""
        def tint(percent: int) -> str:
            return f"color-mix(in srgb, {self.accent} {percent}%, transparent)"

        shade = "0, 0, 0" if self.dark else "15, 23, 42"
        return {
            "--bg": self.background,
            "--surface": self.surface,
            "--input-bg": self.input_background,
            "--text": self.text,
            "--muted": self.muted,
            "--border": self.border,
            "--border-strong": self.border_strong,
            "--accent": self.accent,
            "--accent-soft": tint(14),
            "--info": self.info,
            "--warning": self.warning,
            "--danger": self.danger,
            "--shadow": f"0 1px 3px rgba({shade}, {0.4 if self.dark else 0.08})",
            "--Dash-Fill-Inverse-Strong": self.input_background,
            "--Dash-Fill-Interactive-Strong": self.accent,
            "--Dash-Fill-Interactive-Weak": tint(10),
            "--Dash-Fill-Primary-Hover": tint(14),
            "--Dash-Fill-Primary-Active": tint(24),
            "--Dash-Fill-Disabled": self.border,
            "--Dash-Text-Strong": self.text,
            "--Dash-Text-Primary": self.text,
            "--Dash-Text-Weak": self.muted,
            "--Dash-Text-Disabled": self.muted,
            "--Dash-Stroke-Strong": self.border_strong,
            "--Dash-Stroke-Weak": self.border,
            "--Dash-Shading-Strong": f"rgba({shade}, 0.45)",
            "--Dash-Shading-Weak": f"rgba({shade}, 0.25)",
            "--Dash-Tooltip-Background-Color": self.input_background,
            "--Dash-Tooltip-Border-Color": self.border_strong,
        }


SCHEMES: dict[str, ColorScheme] = {
    scheme.key: scheme
    for scheme in (
        ColorScheme(
            key="frost",
            label="Ledová koruna",
            description="Tmavé modré schéma inspirované ledovci Icecrownu.",
            dark=True,
            background="#0a1020",
            surface="#111a2e",
            input_background="#0d1526",
            text="#e3ecf7",
            muted="#8fa3bf",
            border="#1f2c45",
            border_strong="#36496b",
            accent="#5cc8f0",
            info="#5cc8f0",
            warning="#f2b84b",
            danger="#f26b6b",
            colorway=(
                "#5cc8f0", "#b39cff", "#7ee0c3", "#f2b84b", "#f28fb3",
                "#8fb3ff", "#e6edf5", "#f26b6b", "#9ad46a", "#c89b6d",
            ),
            sequential=("#0f1d38", "#173a6b", "#1f6aa8", "#3fa3d9", "#8fdcf5", "#e8fbff"),
        ),
        ColorScheme(
            key="saronite",
            label="Saronit",
            description="Tmavé zelenofialové schéma podle saronitové rudy.",
            dark=True,
            background="#0d0f0c",
            surface="#151a14",
            input_background="#10140f",
            text="#e6eedf",
            muted="#97a68f",
            border="#263023",
            border_strong="#3d4b37",
            accent="#9ad14b",
            info="#6cc4a8",
            warning="#e3b341",
            danger="#e0604a",
            colorway=(
                "#9ad14b", "#a776d9", "#4fc3a1", "#e3b341", "#e0604a",
                "#6fa8dc", "#d9d36f", "#c27ba0", "#a3b09c", "#3fb8af",
            ),
            sequential=("#141d12", "#24401e", "#3f6e2a", "#6ea53a", "#a8dc5c", "#effbd6"),
        ),
        ColorScheme(
            key="light",
            label="Denní světlo",
            description="Světlé schéma s paletou Okabe–Ito vhodnou pro barvoslepé.",
            dark=False,
            background="#f3f5f8",
            surface="#ffffff",
            input_background="#ffffff",
            text="#1d2533",
            muted="#5d6b7e",
            border="#e1e6ee",
            border_strong="#b9c2cf",
            accent="#0072b2",
            info="#0072b2",
            warning="#b26b00",
            danger="#c0392b",
            colorway=("#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442", "#000000"),
            sequential=("#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"),
        ),
    )
}


# =============================================================================
# 2. Formatting helpers (Czech conventions)
# =============================================================================


def is_missing(value: Any) -> bool:
    """Return True for ``None`` and NaN-like values."""
    return value is None or bool(pd.isna(value))


def fmt_int(value: float) -> str:
    """Format an integer with a non-breaking space as thousands separator."""
    return f"{int(round(value)):,}".replace(",", NBSP)


def fmt_float(value: float, digits: int = 1) -> str:
    """Format a float with a decimal comma and grouped thousands."""
    return f"{value:,.{digits}f}".replace(",", NBSP).replace(".", ",")


def fmt_pct(value: float, digits: int = 1) -> str:
    """Format a percentage value (0-100)."""
    return f"{fmt_float(value, digits)}{NBSP}%"


def fmt_duration(minutes: float | None) -> str:
    """Format minutes as ``m:ss`` (seconds truncated like in-game timers)."""
    if is_missing(minutes):
        return "—"
    seconds = math.floor(abs(minutes) * 60 + 1e-6)
    sign = "−" if minutes < 0 else ""
    return f"{sign}{seconds // 60}:{seconds % 60:02d}"


def fmt_date(value: pd.Timestamp) -> str:
    """Format a date as ``d. m. yyyy``."""
    return f"{value.day}. {value.month}. {value.year}"


def fmt_datetime(value: pd.Timestamp) -> str:
    """Format a timestamp as ``d. m. yyyy hh:mm``."""
    return f"{fmt_date(value)} {value.hour:02d}:{value.minute:02d}"


def format_metric(value: float | None, kind: str) -> str:
    """Format a metric value according to its kind."""
    if is_missing(value):
        return "—"
    if kind == "count":
        return fmt_int(value)
    if kind == "percent":
        return fmt_pct(value)
    if kind == "duration":
        return fmt_duration(value)
    if kind == "level":
        return "+" + (fmt_int(value) if float(value).is_integer() else fmt_float(value, 1))
    return fmt_float(value, 1)


def runs_word(count: int) -> str:
    """Czech plural of the word *run* for the given count."""
    if count == 1:
        return "run"
    if 2 <= count <= 4:
        return "runy"
    return "runů"


def duration_ticks(low: float, high: float, target: int = 6) -> tuple[list[float], list[str]]:
    """Return tick values (minutes) and ``m:ss`` labels spanning ``low``-``high``."""
    span = max(high - low, 1 / 60)
    step = next((s for s in (0.25, 0.5, 1, 2, 5, 10, 15, 30) if span / s <= target), 30)
    start = math.floor(low / step) * step
    values = np.arange(start, high + step * 1.001, step).tolist()
    return values, [fmt_duration(v) for v in values]


# =============================================================================
# 3. Data layer
# =============================================================================


class DataLoadError(RuntimeError):
    """Raised when the CSV file cannot be turned into a usable dataset."""


@dataclass(frozen=True)
class Alert:
    """User-facing message; ``level`` is ``info``, ``warning`` or ``error``."""

    level: str
    message: str


@dataclass(frozen=True)
class DataBounds:
    """Value ranges of the dataset used for control defaults and validation."""

    dates: tuple[pd.Timestamp, ...]
    key_min: int
    key_max: int
    ilvl_min: int
    ilvl_max: int
    factions: tuple[str, ...]
    affixes: tuple[str, ...]
    classes: tuple[str, ...]


@dataclass(frozen=True, eq=False)
class RunData:
    """Cleaned dataset: one row per run plus one row per team member."""

    runs: pd.DataFrame
    members: pd.DataFrame
    bounds: DataBounds
    source: Path
    timezone: str
    season: str
    dropped_rows: int
    missing_ilvl: int


@dataclass(frozen=True)
class RunFilter:
    """Validated global filter; hashable so selections can be cached."""

    start: str
    end: str
    key_min: int
    key_max: int
    factions: tuple[str, ...]
    affix: str
    classes: tuple[str, ...]
    min_ilvl: float | None
    player: str

    @classmethod
    def default(cls, bounds: DataBounds) -> RunFilter:
        """Filter that matches the whole dataset."""
        return cls(
            start=bounds.dates[0].date().isoformat(),
            end=bounds.dates[-1].date().isoformat(),
            key_min=bounds.key_min,
            key_max=bounds.key_max,
            factions=bounds.factions,
            affix=ALL,
            classes=(),
            min_ilvl=None,
            player="",
        )

    def to_store(self) -> dict[str, Any]:
        """JSON-serialisable representation for ``dcc.Store``."""
        return asdict(self)

    @classmethod
    def from_store(cls, data: Mapping[str, Any]) -> RunFilter:
        """Inverse of :meth:`to_store`."""
        values = dict(data)
        values["factions"] = tuple(values["factions"])
        values["classes"] = tuple(values["classes"])
        return cls(**values)


@dataclass(frozen=True, eq=False)
class Selection:
    """Filtered subset of runs and their team members."""

    runs: pd.DataFrame
    members: pd.DataFrame
    total_runs: int
    timezone: str

    @property
    def empty(self) -> bool:
        """True when no run matches the filter."""
        return self.runs.empty


def load_run_data(path: Path | str, timezone: str = DEFAULT_TIMEZONE) -> RunData:
    """Load, validate and prepare the CSV export of Raider.IO runs."""
    path = Path(path)
    if not path.exists():
        raise DataLoadError(f"Datový soubor „{path}“ neexistuje.")
    if not path.is_file():
        raise DataLoadError(f"Cesta „{path}“ neodkazuje na soubor.")
    try:
        raw = pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError as exc:
        raise DataLoadError(f"Datový soubor „{path}“ je prázdný.") from exc
    except (OSError, UnicodeDecodeError, pd.errors.ParserError) as exc:
        raise DataLoadError(f"Soubor „{path}“ se nepodařilo načíst jako CSV: {exc}") from exc

    missing = [column for column in REQUIRED_COLUMNS if column not in raw.columns]
    if missing:
        raise DataLoadError("V datech chybí povinné sloupce: " + ", ".join(missing) + ".")

    runs, dropped = prepare_runs(raw, timezone)
    if runs.empty:
        raise DataLoadError(f"Soubor „{path}“ neobsahuje žádný platný run.")
    members = build_members(runs)
    season = "–"
    if "season" in runs.columns and runs["season"].notna().any():
        code = str(runs["season"].mode().iat[0])
        season = SEASON_LABELS.get(code, code)
    return RunData(
        runs=runs,
        members=members,
        bounds=compute_bounds(runs, members),
        source=path,
        timezone=timezone,
        season=season,
        dropped_rows=dropped,
        missing_ilvl=int(members["ilvl"].isna().sum()),
    )


def _sorted_join(frame: pd.DataFrame, columns: Sequence[str]) -> pd.Series:
    """Join row values of ``columns`` in alphabetical order (order-free key)."""
    values = np.sort(frame[list(columns)].to_numpy(dtype=str), axis=1)
    return pd.Series([", ".join(row) for row in values], index=frame.index)


def prepare_runs(raw: pd.DataFrame, timezone: str) -> tuple[pd.DataFrame, int]:
    """Clean raw rows and add derived columns; return runs and dropped row count."""
    df = raw.copy()
    df["completed_at"] = pd.to_datetime(df["completed_at"], utc=True, errors="coerce", format="ISO8601")
    for column in ("mythic_level", "keystone_time_ms", "clear_time_ms"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    valid = (
        df["completed_at"].notna()
        & df["mythic_level"].ge(2)
        & df["clear_time_ms"].gt(0)
        & df["keystone_time_ms"].gt(0)
        & ~df["keystone_run_id"].duplicated()
    )
    dropped = int((~valid).sum())
    df = df.loc[valid].sort_values("completed_at").reset_index(drop=True)
    df["run"] = df.index
    df["mythic_level"] = df["mythic_level"].astype(int)

    for slot in SLOT_ROLES:
        for name in ("name", "race", "class", "spec"):
            column = f"{slot}_{name}"
            df[column] = df[column].fillna(UNKNOWN).astype(str).str.strip().replace("", UNKNOWN)
        ilvl = pd.to_numeric(df[f"{slot}_ilvl"], errors="coerce")
        # Raider.IO reports 0 when the item level is unknown.
        df[f"{slot}_ilvl"] = ilvl.where(ilvl > 0)
        df[f"{slot}_label"] = df[f"{slot}_spec"] + " " + df[f"{slot}_class"]

    local = df["completed_at"].dt.tz_convert(timezone)
    df["completed_local"] = local
    df["date"] = local.dt.tz_localize(None).dt.normalize()
    df["week_start"] = df["date"] - pd.to_timedelta(df["date"].dt.day_of_week, unit="D")
    df["weekday"] = local.dt.day_of_week
    df["hour"] = local.dt.hour

    df["clear_min"] = df["clear_time_ms"] / 60_000
    df["timer_min"] = df["keystone_time_ms"] / 60_000
    df["remaining_min"] = df["timer_min"] - df["clear_min"]
    df["avg_ilvl"] = df[[f"{slot}_ilvl" for slot in SLOT_ROLES]].mean(axis=1)
    df["key_label"] = "+" + df["mythic_level"].astype(str)
    df["faction"] = df["faction"].fillna("unknown").astype(str).str.lower()
    df["faction_label"] = df["faction"].map(lambda code: FACTION_LABELS.get(code, code))
    df["affix"] = df["modifier_1"].fillna(UNKNOWN).astype(str)
    df["dps_specs"] = _sorted_join(df, [f"{slot}_label" for slot in DPS_SLOTS])
    df["dps_classes"] = _sorted_join(df, [f"{slot}_class" for slot in DPS_SLOTS])

    names = [df[f"{slot}_name"] for slot in SLOT_ROLES]
    search = names[0]
    for name in names[1:]:
        search = search + "\n" + name
    df["player_search"] = search.str.casefold()
    return df, dropped


def build_members(runs: pd.DataFrame) -> pd.DataFrame:
    """Reshape runs into one row per team member (long format)."""
    shared = ["run", "clear_min", "remaining_min", "mythic_level"]
    parts = []
    for slot, role in SLOT_ROLES.items():
        part = runs[shared].copy()
        part["role"] = role
        for name in ("name", "race", "class", "spec", "label", "ilvl"):
            part[name] = runs[f"{slot}_{name}"]
        parts.append(part)
    members = pd.concat(parts, ignore_index=True)
    members["player"] = members["name"] + " (" + members["class"] + ")"
    return members


def compute_bounds(runs: pd.DataFrame, members: pd.DataFrame) -> DataBounds:
    """Collect value ranges used by the filter controls."""
    ilvl = runs["avg_ilvl"].dropna()
    return DataBounds(
        dates=tuple(pd.DatetimeIndex(runs["date"].unique()).sort_values()),
        key_min=int(runs["mythic_level"].min()),
        key_max=int(runs["mythic_level"].max()),
        ilvl_min=math.floor(ilvl.min()) if not ilvl.empty else 0,
        ilvl_max=math.ceil(ilvl.max()) if not ilvl.empty else 0,
        factions=tuple(runs["faction"].value_counts().index),
        affixes=tuple(sorted(runs["affix"].unique())),
        classes=tuple(sorted(members["class"].unique())),
    )


def _parse_date(value: Any, fallback: pd.Timestamp, label: str, bounds: DataBounds, alerts: list[Alert]) -> pd.Timestamp:
    """Parse a date control value and clamp it into the dataset range."""
    if is_missing(value) or value == "":
        return fallback
    try:
        parsed = pd.Timestamp(value).normalize()
    except (TypeError, ValueError):
        alerts.append(Alert("warning", f"{label} „{value}“ není platné datum; použito {fmt_date(fallback)}."))
        return fallback
    if parsed.tzinfo is not None:
        parsed = parsed.tz_localize(None)
    first, last = bounds.dates[0], bounds.dates[-1]
    if parsed < first or parsed > last:
        clamped = min(max(parsed, first), last)
        alerts.append(
            Alert(
                "warning",
                f"{label} {fmt_date(parsed)} je mimo rozsah dat ({fmt_date(first)} – {fmt_date(last)}); "
                f"použito {fmt_date(clamped)}.",
            )
        )
        return clamped
    return parsed


def build_filter(
    bounds: DataBounds,
    *,
    start: Any,
    end: Any,
    key_range: Any,
    factions: Any,
    affix: Any,
    classes: Any,
    min_ilvl: Any,
    player: Any,
) -> tuple[RunFilter, list[Alert]]:
    """Validate raw control values and turn them into a :class:`RunFilter`."""
    alerts: list[Alert] = []

    start_date = _parse_date(start, bounds.dates[0], "Počáteční datum", bounds, alerts)
    end_date = _parse_date(end, bounds.dates[-1], "Koncové datum", bounds, alerts)
    if start_date > end_date:
        alerts.append(
            Alert(
                "warning",
                f"Počáteční datum ({fmt_date(start_date)}) je pozdější než koncové ({fmt_date(end_date)}); "
                "data byla pro výpočet prohozena.",
            )
        )
        start_date, end_date = end_date, start_date

    try:
        key_low, key_high = sorted(int(value) for value in key_range)
    except (TypeError, ValueError):
        alerts.append(Alert("warning", "Rozsah úrovně klíče je neplatný; použit celý rozsah."))
        key_low, key_high = bounds.key_min, bounds.key_max
    if key_low < bounds.key_min or key_high > bounds.key_max:
        key_low = min(max(key_low, bounds.key_min), bounds.key_max)
        key_high = min(max(key_high, bounds.key_min), bounds.key_max)
        alerts.append(
            Alert(
                "warning",
                f"Úroveň klíče musí být v rozsahu +{bounds.key_min} až +{bounds.key_max}; rozsah byl upraven.",
            )
        )

    selected_factions = tuple(f for f in (factions or ()) if f in bounds.factions)
    if not selected_factions:
        alerts.append(Alert("error", "Není vybrána žádná frakce – vyberte alespoň jednu."))

    if affix != ALL and affix not in bounds.affixes:
        alerts.append(Alert("warning", "Neznámý affix týdne; filtr affixu byl zrušen."))
        affix = ALL

    requested = list(dict.fromkeys(classes or ()))
    selected_classes = tuple(c for c in requested if c in bounds.classes)
    if len(selected_classes) < len(requested):
        alerts.append(Alert("warning", "Některé vybrané třídy v datech neexistují a byly ignorovány."))
    if len(selected_classes) > MAX_TEAM_SIZE:
        alerts.append(
            Alert(
                "error",
                f"Tým má pouze {MAX_TEAM_SIZE} členů – nelze současně vyžadovat "
                f"{len(selected_classes)} různých tříd.",
            )
        )

    ilvl_limit: float | None = None
    if not is_missing(min_ilvl) and min_ilvl != "":
        try:
            ilvl_value = float(min_ilvl)
        except (TypeError, ValueError):
            ilvl_value = math.nan
        if not math.isfinite(ilvl_value):
            alerts.append(Alert("warning", "Minimální item level musí být číslo; filtr nebyl použit."))
        elif not bounds.ilvl_min <= ilvl_value <= bounds.ilvl_max:
            alerts.append(
                Alert(
                    "warning",
                    f"Minimální item level {format_metric(ilvl_value, 'ilvl').removesuffix(',0')} je mimo rozsah dat "
                    f"({bounds.ilvl_min}–{bounds.ilvl_max}); filtr nebyl použit.",
                )
            )
        else:
            ilvl_limit = ilvl_value

    query = str(player or "").strip()
    if len(query) > MAX_PLAYER_QUERY:
        query = query[:MAX_PLAYER_QUERY]
        alerts.append(Alert("warning", f"Hledané jméno bylo zkráceno na {MAX_PLAYER_QUERY} znaků."))

    run_filter = RunFilter(
        start=start_date.date().isoformat(),
        end=end_date.date().isoformat(),
        key_min=key_low,
        key_max=key_high,
        factions=selected_factions,
        affix=affix,
        classes=selected_classes,
        min_ilvl=ilvl_limit,
        player=query,
    )
    return run_filter, alerts


def filter_runs(runs: pd.DataFrame, run_filter: RunFilter) -> pd.DataFrame:
    """Return runs matching the filter (classes are combined with AND)."""
    mask = runs["date"].between(pd.Timestamp(run_filter.start), pd.Timestamp(run_filter.end))
    mask &= runs["mythic_level"].between(run_filter.key_min, run_filter.key_max)
    mask &= runs["faction"].isin(run_filter.factions)
    if run_filter.affix != ALL:
        mask &= runs["affix"].eq(run_filter.affix)
    if run_filter.classes:
        class_columns = runs[[f"{slot}_class" for slot in SLOT_ROLES]]
        for class_name in run_filter.classes:
            mask &= class_columns.eq(class_name).any(axis=1)
    if run_filter.min_ilvl is not None:
        mask &= runs["avg_ilvl"].ge(run_filter.min_ilvl)
    if run_filter.player:
        mask &= runs["player_search"].str.contains(run_filter.player.casefold(), regex=False)
    return runs.loc[mask]


def select_runs(data: RunData, run_filter: RunFilter) -> Selection:
    """Apply the filter to runs and members."""
    runs = filter_runs(data.runs, run_filter)
    members = data.members[data.members["run"].isin(runs["run"])]
    return Selection(runs=runs, members=members, total_runs=len(data.runs), timezone=data.timezone)


def describe_selection(selection: Selection, run_filter: RunFilter) -> list[Alert]:
    """Explain empty or very small selections to the user."""
    count = len(selection.runs)
    if count == 0:
        if run_filter.player:
            return [
                Alert(
                    "error",
                    f"Hráč, jehož jméno obsahuje „{run_filter.player}“, se při zvolených filtrech "
                    "v žádném runu nevyskytuje.",
                )
            ]
        return [Alert("error", "Zvoleným filtrům neodpovídá žádný run – upravte je nebo je zrušte.")]
    if count < SMALL_SELECTION:
        return [
            Alert(
                "warning",
                f"Výběr obsahuje jen {fmt_int(count)} {runs_word(count)} – statistiky mohou být nespolehlivé.",
            )
        ]
    return []


def coerce_int(value: Any, *, minimum: int, maximum: int, default: int, label: str) -> tuple[int, Alert | None]:
    """Validate an integer control value; return the value to use and an optional alert."""
    if is_missing(value) or value == "":
        return default, Alert(
            "warning", f"{label}: zadejte celé číslo {minimum}–{maximum}. Použita výchozí hodnota {default}."
        )
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number) or not number.is_integer():
        return default, Alert(
            "warning", f"{label}: hodnota „{value}“ není celé číslo. Použita výchozí hodnota {default}."
        )
    if not minimum <= number <= maximum:
        clamped = int(min(max(number, minimum), maximum))
        return clamped, Alert(
            "warning",
            f"{label}: hodnota {int(number)} je mimo povolený rozsah {minimum}–{maximum}. Použita hodnota {clamped}.",
        )
    return int(number), None


def choice(value: Any, options: Mapping[str, Any], default: str) -> str:
    """Return ``value`` if it is a known option key, otherwise ``default``."""
    return value if isinstance(value, str) and value in options else default


def aggregate_runs(frame: pd.DataFrame, by: str | list[str], *, ilvl_column: str) -> pd.DataFrame:
    """Compute all supported metrics for each group of ``frame``."""
    grouped = frame.groupby(by, observed=True, sort=False)
    return pd.DataFrame(
        {
            "count": grouped.size(),
            "median_time": grouped["clear_min"].median(),
            "best_time": grouped["clear_min"].min(),
            "median_remaining": grouped["remaining_min"].median(),
            "mean_key": grouped["mythic_level"].mean(),
            "max_key": grouped["mythic_level"].max(),
            "mean_ilvl": grouped[ilvl_column].mean(),
        }
    )


def category_order(frame: pd.DataFrame, column: str) -> list[str]:
    """Natural order of categories: key levels ascending, others by frequency."""
    if column == "key_label":
        return [f"+{level}" for level in sorted(frame["mythic_level"].unique())]
    return frame[column].value_counts().index.tolist()


@dataclass(frozen=True)
class Kpi:
    """Headline indicator shown above the charts."""

    label: str
    value: str
    detail: str
    hint: str
    compact: bool = False  # long textual value rendered with a smaller font


def compute_kpis(selection: Selection) -> list[Kpi]:
    """Compute the headline indicators for the current selection."""
    hints = {
        "runs": "Počet runů odpovídajících aktuálním filtrům.",
        "median": "Prostřední čas dokončení; rezerva je čas zbývající do limitu klíče.",
        "fastest": "Nejkratší čas dokončení ve výběru.",
        "highest": "Nejvyšší dokončená úroveň klíče (při shodě rozhoduje čas).",
        "ilvl": "Průměr item levelů všech pěti členů týmu; chybějící údaje jsou vynechány.",
        "tank": "Nejčastější tank specializace a podíl runů, ve kterých byla zastoupena.",
        "healer": "Nejčastější healer specializace a podíl runů, ve kterých byla zastoupena.",
    }
    runs = selection.runs
    total = selection.total_runs
    if runs.empty:
        labels = {
            "runs": "Runů ve výběru", "median": "Medián času", "fastest": "Nejrychlejší run",
            "highest": "Nejvyšší klíč", "ilvl": "Ø item level týmu", "tank": "Nejhranější tank",
            "healer": "Nejhranější healer",
        }
        return [
            Kpi(labels["runs"], "0", f"z {fmt_int(total)} runů", hints["runs"]),
            *(Kpi(labels[key], "—", "žádná data", hints[key]) for key in list(labels)[1:]),
        ]

    count = len(runs)
    fastest = runs.loc[runs["clear_min"].idxmin()]
    highest = runs.sort_values(["mythic_level", "clear_min"], ascending=[False, True]).iloc[0]
    tanks = runs["tank_label"].value_counts()
    healers = runs["healer_label"].value_counts()
    ilvl = runs["avg_ilvl"].dropna()
    return [
        Kpi("Runů ve výběru", fmt_int(count), f"z {fmt_int(total)} ({fmt_pct(count / total * 100)})", hints["runs"]),
        Kpi(
            "Medián času",
            fmt_duration(runs["clear_min"].median()),
            f"Ø rezerva {fmt_duration(runs['remaining_min'].mean())}",
            hints["median"],
        ),
        Kpi(
            "Nejrychlejší run",
            fmt_duration(fastest["clear_min"]),
            f"{fastest['key_label']} · {fmt_date(fastest['completed_local'])}",
            hints["fastest"],
        ),
        Kpi(
            "Nejvyšší klíč",
            highest["key_label"],
            f"{fmt_duration(highest['clear_min'])} · {fmt_date(highest['completed_local'])}",
            hints["highest"],
        ),
        Kpi(
            "Ø item level týmu",
            format_metric(ilvl.mean(), "ilvl") if not ilvl.empty else "—",
            f"medián {format_metric(ilvl.median(), 'ilvl')}" if not ilvl.empty else "údaj chybí",
            hints["ilvl"],
        ),
        Kpi("Nejhranější tank", tanks.index[0], f"{fmt_pct(tanks.iloc[0] / count * 100)} runů", hints["tank"], True),
        Kpi(
            "Nejhranější healer", healers.index[0], f"{fmt_pct(healers.iloc[0] / count * 100)} runů", hints["healer"], True
        ),
    ]


@dataclass(frozen=True)
class RunRow:
    """Display-ready row of the run leaderboard table."""

    rank: int
    key: str
    time: str
    remaining: str
    ilvl: str
    affix: str
    faction: str
    completed: str
    team: tuple[tuple[str, str, str, str], ...]  # (role, player name, spec label, class)


def top_runs(runs: pd.DataFrame, *, sort: str = "clear", descending: bool = False, limit: int = 15) -> list[RunRow]:
    """Return the first ``limit`` runs ordered by ``sort`` (ties broken by clear time)."""
    column = RUN_SORTS[sort][1]
    by = [column] if column == "clear_time_ms" else [column, "clear_time_ms"]
    ascending = [not descending] + [True] * (len(by) - 1)
    ordered = runs.sort_values(by, ascending=ascending, na_position="last").head(limit)
    rows = []
    for rank, record in enumerate(ordered.to_dict("records"), start=1):
        team = tuple(
            (SLOT_ROLES[slot], record[f"{slot}_name"], record[f"{slot}_label"], record[f"{slot}_class"])
            for slot in SLOT_ROLES
        )
        rows.append(
            RunRow(
                rank=rank,
                key=record["key_label"],
                time=fmt_duration(record["clear_min"]),
                remaining=fmt_duration(record["remaining_min"]),
                ilvl=format_metric(record["avg_ilvl"], "ilvl"),
                affix=record["affix"],
                faction=record["faction_label"],
                completed=fmt_datetime(record["completed_local"]),
                team=team,
            )
        )
    return rows


# =============================================================================
# 4. Figure builders (Plotly only)
# =============================================================================

NO_DATA = "Pro zvolené filtry nejsou k dispozici žádná data."


@dataclass
class ChartResult:
    """Figure together with notes that should be shown next to it."""

    figure: go.Figure
    notes: list[Alert] = field(default_factory=list)


def base_figure(scheme: ColorScheme, **layout: Any) -> go.Figure:
    """Empty figure with the scheme template and shared layout defaults."""
    figure = go.Figure()
    figure.update_layout(
        template=scheme.template,
        separators="," + NBSP,
        margin={"l": 12, "r": 16, "t": 40, "b": 12},
        legend={"orientation": "h", "x": 0, "y": 1.02, "yanchor": "bottom", "title": {"text": ""}},
        hoverlabel={"namelength": -1},
    )
    figure.update_layout(**layout)
    return figure


def message_figure(scheme: ColorScheme, message: str) -> go.Figure:
    """Placeholder figure that only displays a message."""
    figure = base_figure(scheme)
    figure.update_xaxes(visible=False)
    figure.update_yaxes(visible=False)
    figure.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"size": 15, "color": scheme.muted},
    )
    return figure


def apply_duration_axis(figure: go.Figure, axis: str, values: Iterable[float]) -> None:
    """Label a numeric minute axis with ``m:ss`` ticks."""
    finite = pd.Series(list(values), dtype=float).dropna()
    if finite.empty:
        return
    tickvals, ticktext = duration_ticks(finite.min(), finite.max())
    update = figure.update_xaxes if axis == "x" else figure.update_yaxes
    update(tickvals=tickvals, ticktext=ticktext)


def apply_value_axis(figure: go.Figure, values: pd.Series, metric: Metric, axis: str = "x") -> None:
    """Configure the value axis of a bar chart for the given metric."""
    update = figure.update_xaxes if axis == "x" else figure.update_yaxes
    if metric.zero_based:
        # Extra headroom keeps the outside value labels inside the plot area.
        update(title={"text": metric.label}, range=[0, float(values.max()) * 1.18], tickformat=",d")
        return
    low, high = float(values.min()), float(values.max())
    pad = max((high - low) * 0.25, 0.5 if metric.kind == "duration" else 0.3)
    update(title={"text": f"{metric.label} (osa nezačíná nulou)"}, range=[low - pad, high + pad * 1.6])
    if metric.kind == "duration":
        apply_duration_axis(figure, axis, [low - pad, high + pad * 1.6])


def escape(series: pd.Series) -> pd.Series:
    """HTML-escape strings that are embedded into Plotly hover templates."""
    return series.astype(str).map(html_lib.escape)


def run_hover_text(runs: pd.DataFrame) -> pd.Series:
    """Detailed hover text (HTML) describing each run and its team."""
    def member(slot: str) -> pd.Series:
        return escape(runs[f"{slot}_name"]) + " · " + escape(runs[f"{slot}_label"])

    return (
        "<b>" + runs["key_label"] + " · " + runs["clear_min"].map(fmt_duration) + "</b>"
        + " (rezerva " + runs["remaining_min"].map(fmt_duration) + ")<br>"
        + "Ø item level: " + runs["avg_ilvl"].map(lambda v: format_metric(v, "ilvl")) + "<br>"
        + "Dokončeno: " + runs["completed_local"].map(fmt_datetime) + "<br>"
        + "Affix týdne: " + escape(runs["affix"]) + " · frakce: " + runs["faction_label"] + "<br>"
        + "<br>Tank: " + member("tank")
        + "<br>Healer: " + member("healer")
        + "<br>DPS: " + member("dps_1")
        + "<br>DPS: " + member("dps_2")
        + "<br>DPS: " + member("dps_3")
    )


def build_timeline_figure(
    selection: Selection,
    scheme: ColorScheme,
    *,
    metric: str = "count",
    granularity: str = "day",
    split: str = "none",
    cumulative: bool = False,
) -> ChartResult:
    """Line chart of a metric over days or weeks, optionally split by a category."""
    runs = selection.runs
    if runs.empty:
        return ChartResult(message_figure(scheme, NO_DATA))
    notes: list[Alert] = []
    info = METRICS[metric]
    if cumulative and metric != "count":
        notes.append(
            Alert("info", "Kumulativní součet má smysl jen pro metriku „Počet runů“ – pro zvolenou metriku je vypnut.")
        )
        cumulative = False

    weekly = granularity == "week"
    period = "week_start" if weekly else "date"
    periods = pd.date_range(runs[period].min(), runs[period].max(), freq="7D" if weekly else "D")
    if weekly:
        first, last = runs["date"].min(), runs["date"].max()
        if first.weekday() != 0 or last.weekday() != 6:
            notes.append(
                Alert(
                    "info",
                    f"Krajní týdny mohou být neúplné – výběr obsahuje dny {fmt_date(first)} až {fmt_date(last)}.",
                )
            )

    if split == "none":
        groups = [("Všechny runy", runs)]
        colors = [scheme.accent]
    else:
        order = category_order(runs, split)
        groups = [(name, runs[runs[split] == name]) for name in order]
        colors = [scheme.color(i) for i in range(len(order))]

    figure = base_figure(scheme, hovermode="x unified")
    all_values: list[float] = []
    for (name, frame), color in zip(groups, colors, strict=True):
        stats = aggregate_runs(frame, period, ilvl_column="avg_ilvl").reindex(periods)
        stats["count"] = stats["count"].fillna(0)
        values = stats[metric].cumsum() if cumulative else stats[metric]
        all_values.extend(values.dropna().tolist())
        customdata = np.column_stack(
            [stats["count"].map(fmt_int), values.map(lambda v: format_metric(v, info.kind))]
        )
        prefix = "" if split == "none" else f"<b>{html_lib.escape(str(name))}</b>: "
        figure.add_trace(
            go.Scatter(
                x=periods,
                y=values,
                name=str(name),
                mode="lines+markers",
                line={"width": 2.5, "color": color, "shape": "linear"},
                marker={"size": 6 if not weekly else 9, "color": color},
                connectgaps=False,
                customdata=customdata,
                hovertemplate=prefix + info.label + ": %{customdata[1]} (runů v období: %{customdata[0]})<extra></extra>",
            )
        )

    figure.update_xaxes(
        type="date",
        tickformat="%-d. %-m.",
        hoverformat="Týden od %-d. %-m. %Y" if weekly else "%A %-d. %-m. %Y",
        title={"text": "Týden (začátek v pondělí)" if weekly else "Den dokončení"},
    )
    y_title = info.label + (" – kumulativně" if cumulative else "")
    figure.update_yaxes(title={"text": y_title}, rangemode="tozero" if info.zero_based else "normal")
    if info.kind == "count":
        figure.update_yaxes(tickformat=",d")
    if info.kind == "duration":
        apply_duration_axis(figure, "y", all_values)
    figure.update_layout(showlegend=split != "none")
    return ChartResult(figure, notes)


def build_leaderboard_figure(
    selection: Selection,
    scheme: ColorScheme,
    *,
    dimension: str = "spec",
    role: str = ALL,
    metric: str = "count",
    top_n: int = 10,
    descending: bool = True,
    class_colors: bool = True,
) -> ChartResult:
    """Horizontal bar ranking of specs, classes, players or races."""
    members = selection.members
    if role != ALL:
        members = members[members["role"] == role]
    if members.empty:
        return ChartResult(message_figure(scheme, NO_DATA))
    notes: list[Alert] = []
    info = METRICS[metric]
    column = LEADER_COLUMNS[dimension]
    # A run with two identical DPS specs must count only once for that spec.
    unique = members.drop_duplicates([column, "run"])
    stats = aggregate_runs(unique, column, ilvl_column="ilvl")
    stats["share"] = stats["count"] / len(selection.runs) * 100
    stats["class"] = unique.groupby(column, observed=True, sort=False)["class"].first()

    if not info.zero_based:
        small = stats["count"] < MIN_GROUP_RUNS
        if small.any():
            notes.append(
                Alert(
                    "info",
                    f"Skryto {fmt_int(small.sum())} položek s méně než {MIN_GROUP_RUNS} runy (nespolehlivá statistika).",
                )
            )
            stats = stats[~small]
    stats = stats.dropna(subset=[metric])
    if stats.empty:
        return ChartResult(message_figure(scheme, f"Žádná položka nemá alespoň {MIN_GROUP_RUNS} runů."), notes)

    stats = stats.sort_values([metric, "count"], ascending=[not descending, False]).head(top_n)
    if class_colors and dimension != "race":
        colors: Any = [CLASS_COLORS.get(name, scheme.accent) for name in stats["class"]]
    else:
        colors = scheme.accent
    customdata = np.column_stack(
        [
            stats["count"].map(fmt_int),
            stats["share"].map(fmt_pct),
            stats["median_time"].map(fmt_duration),
            stats["best_time"].map(fmt_duration),
            stats["mean_key"].map(lambda v: format_metric(v, "level")),
            stats["max_key"].map(lambda v: format_metric(v, "level")),
            stats["mean_ilvl"].map(lambda v: format_metric(v, "ilvl")),
            stats["median_remaining"].map(fmt_duration),
        ]
    )
    figure = base_figure(scheme, showlegend=False, bargap=0.25)
    figure.add_trace(
        go.Bar(
            x=stats[metric],
            y=stats.index.astype(str),
            orientation="h",
            marker={"color": colors, "line": {"color": scheme.border_strong, "width": 1}},
            text=[format_metric(value, info.kind) for value in stats[metric]],
            textposition="outside",
            cliponaxis=False,
            customdata=customdata,
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Počet runů: %{customdata[0]} (%{customdata[1]} výběru)<br>"
                "Medián času: %{customdata[2]} · nejrychlejší: %{customdata[3]}<br>"
                "Medián rezervy: %{customdata[7]}<br>"
                "Úroveň klíče: Ø %{customdata[4]} · max %{customdata[5]}<br>"
                "Ø item level: %{customdata[6]}<extra></extra>"
            ),
        )
    )
    figure.update_yaxes(autorange="reversed", automargin=True, title=None, type="category", ticksuffix="  ")
    apply_value_axis(figure, stats[metric], info)
    return ChartResult(figure, notes)


def _share_source(selection: Selection, dimension: str) -> tuple[pd.DataFrame, str, str | None, str]:
    """Frame, category column, class column and counted unit for the share chart."""
    runs, members = selection.runs, selection.members
    if dimension == "tank":
        return runs, "tank_label", "tank_class", "runů"
    if dimension == "healer":
        return runs, "healer_label", "healer_class", "runů"
    if dimension == "dps_class":
        return members[members["role"] == "dps"], "class", "class", "DPS hráčů"
    if dimension == "key":
        return runs, "key_label", None, "runů"
    if dimension == "affix":
        return runs, "affix", None, "runů"
    if dimension == "faction":
        return runs, "faction_label", None, "runů"
    return members, "race", None, "hráčů"


def build_share_figure(
    selection: Selection,
    scheme: ColorScheme,
    *,
    dimension: str = "tank",
    max_slices: int = 8,
    labels: str = "percent",
    class_colors: bool = True,
) -> ChartResult:
    """Donut chart with the composition of the selection by a category."""
    frame, column, class_column, unit = _share_source(selection, dimension)
    if frame.empty:
        return ChartResult(message_figure(scheme, NO_DATA))
    notes: list[Alert] = []
    grouped = frame.groupby(column, observed=True)
    stats = pd.DataFrame({"count": grouped.size(), "median_time": grouped["clear_min"].median()})
    if dimension == "key":
        stats = stats.sort_index(key=lambda index: index.str[1:].astype(int))
    else:
        stats = stats.sort_values("count", ascending=False)

    if len(stats) > max_slices:
        head, tail = stats.iloc[: max_slices - 1], stats.iloc[max_slices - 1 :]
        rest = frame[frame[column].isin(tail.index)]
        other = pd.DataFrame(
            {"count": [tail["count"].sum()], "median_time": [rest["clear_min"].median()]},
            index=[f"{OTHERS} ({len(tail)})"],
        )
        stats = pd.concat([head, other])

    if class_column and class_colors:
        class_of = dict(zip(frame[column], frame[class_column], strict=True))
        colors = [CLASS_COLORS.get(class_of.get(name, ""), scheme.muted) for name in stats.index]
    elif dimension == "key":
        colors = scheme.ordinal_colors(len(stats))
    else:
        colors = [scheme.color(i) for i in range(len(stats))]

    if dimension == "dps_class":
        notes.append(Alert("info", "Každý run má tři DPS hráče – započítán je každý z nich."))
    elif dimension == "race":
        notes.append(Alert("info", "Započítán je každý z pěti členů týmu."))

    total = int(stats["count"].sum())
    figure = base_figure(
        scheme,
        legend={"orientation": "v", "x": 1.0, "y": 0.5, "yanchor": "middle", "xanchor": "left"},
        uniformtext={"minsize": 10, "mode": "hide"},
        margin={"l": 12, "r": 12, "t": 24, "b": 12},
    )
    figure.add_trace(
        go.Pie(
            labels=stats.index.astype(str),
            values=stats["count"],
            hole=0.55,
            sort=False,
            direction="clockwise",
            textinfo={"none": "none", "percent": "percent", "label": "label+percent"}[labels],
            textposition="inside",
            insidetextorientation="horizontal",
            marker={"colors": colors, "line": {"color": scheme.surface, "width": 2}},
            customdata=np.column_stack([stats["count"].map(fmt_int), stats["median_time"].map(fmt_duration)]),
            hovertemplate=(
                "<b>%{label}</b><br>Počet " + unit + ": %{customdata[0]}"
                + "<br>Podíl: %{percent}<br>Medián času: %{customdata[1]}<extra></extra>"
            ),
        )
    )
    figure.add_annotation(
        text=f"<b>{fmt_int(total)}</b><br>{runs_word(total) if unit == 'runů' else unit}",
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"size": 16, "color": scheme.text},
    )
    return ChartResult(figure, notes)


def build_heatmap_figure(
    selection: Selection,
    scheme: ColorScheme,
    *,
    mode: str = "weekday_hour",
    metric: str = "count",
) -> ChartResult:
    """Heatmap of a metric across two categorical dimensions."""
    if selection.empty:
        return ChartResult(message_figure(scheme, NO_DATA))
    notes: list[Alert] = []
    if mode == "dps_key" and metric == "mean_key":
        notes.append(
            Alert("info", "Průměrná úroveň klíče nedává smysl, když je úroveň klíče na ose; zobrazen je počet runů.")
        )
        metric = "count"
    info = METRICS[metric]

    runs = selection.runs
    if mode == "weekday_hour":
        frame, x_column, y_column = runs, "hour", "weekday"
        x_order, y_order = list(range(24)), list(range(7))
        x_labels, y_labels = [str(hour) for hour in x_order], list(WEEKDAYS)
        x_title, y_title = f"Hodina dokončení ({selection.timezone})", ""
        cell = "<b>%{y}</b>, %{x}:00–%{x}:59"
    elif mode == "tank_healer":
        frame, x_column, y_column = runs, "healer_label", "tank_label"
        x_order = runs[x_column].value_counts().index.tolist()
        y_order = runs[y_column].value_counts().index.tolist()
        x_labels, y_labels = x_order, y_order
        x_title, y_title = "Healer", "Tank"
        cell = "<b>%{y}</b> + <b>%{x}</b>"
    else:
        frame = selection.members[selection.members["role"] == "dps"].drop_duplicates(["run", "class"])
        x_column, y_column = "mythic_level", "class"
        x_order = sorted(frame["mythic_level"].unique())
        y_order = frame[y_column].value_counts().index.tolist()
        x_labels, y_labels = [f"+{level}" for level in x_order], y_order
        x_title, y_title = "Úroveň klíče", "DPS třída"
        cell = "<b>%{y}</b> · klíč %{x}"

    grouped = frame.groupby([y_column, x_column], observed=True)
    counts = grouped.size().unstack(x_column).reindex(index=y_order, columns=x_order).fillna(0)
    if metric == "count":
        values = counts
    elif metric == "column_share":
        totals = counts.sum(axis=0)
        values = counts.div(totals.where(totals > 0), axis=1) * 100
    else:
        source = grouped["clear_min"].median() if metric == "median_time" else grouped["mythic_level"].mean()
        values = source.unstack(x_column).reindex(index=y_order, columns=x_order)
        sparse = counts < MIN_CELL_RUNS
        if (sparse & values.notna()).to_numpy().any():
            notes.append(Alert("info", f"Buňky s méně než {MIN_CELL_RUNS} runy jsou kvůli spolehlivosti skryty."))
        values = values.mask(sparse)

    value_text = values.map(lambda v: format_metric(v, info.kind))
    customdata = np.dstack([value_text.to_numpy(dtype=object), counts.map(fmt_int).to_numpy(dtype=object)])
    colorbar: dict[str, Any] = {"title": {"text": info.label, "side": "right"}, "thickness": 12}
    if info.kind == "duration" and values.notna().to_numpy().any():
        tickvals, ticktext = duration_ticks(np.nanmin(values.to_numpy()), np.nanmax(values.to_numpy()), target=5)
        colorbar.update(tickvals=tickvals, ticktext=ticktext)
    show_text = len(x_order) * len(y_order) <= 120

    figure = base_figure(scheme, margin={"l": 12, "r": 12, "t": 24, "b": 12})
    figure.add_trace(
        go.Heatmap(
            z=values.to_numpy(dtype=float),
            x=x_labels,
            y=y_labels,
            colorscale=list(scheme.sequential),
            xgap=2,
            ygap=2,
            hoverongaps=False,
            colorbar=colorbar,
            customdata=customdata,
            text=value_text.to_numpy() if show_text else None,
            texttemplate="%{text}" if show_text else None,
            textfont={"size": 10},
            hovertemplate=cell + "<br>" + info.label + ": %{customdata[0]}<br>Počet runů: %{customdata[1]}<extra></extra>",
        )
    )
    figure.update_xaxes(type="category", title={"text": x_title}, showgrid=False)
    figure.update_yaxes(type="category", title={"text": y_title}, autorange="reversed", showgrid=False, automargin=True)
    return ChartResult(figure, notes)


def build_distribution_figure(
    selection: Selection,
    scheme: ColorScheme,
    *,
    group: str = "key_label",
    kind: str = "box",
    points: str = "outliers",
) -> ChartResult:
    """Box or violin plots of clear times grouped by a category."""
    runs = selection.runs
    if runs.empty:
        return ChartResult(message_figure(scheme, NO_DATA))
    notes: list[Alert] = []
    if points == "all" and len(runs) > MAX_POINTS_ALL:
        notes.append(
            Alert(
                "warning",
                f"Vykreslení všech {fmt_int(len(runs))} runů by bylo příliš pomalé (limit {fmt_int(MAX_POINTS_ALL)}); "
                "zobrazeny jsou jen odlehlé hodnoty. Zužte výběr filtry.",
            )
        )
        points = "outliers"
    point_mode: Any = {"none": False, "outliers": "outliers", "all": "all"}[points]

    if group == "key_label":
        order = category_order(runs, group)
        colors = scheme.ordinal_colors(len(order))
    else:
        order = runs.groupby(group)["clear_min"].median().sort_values().index.tolist()
        colors = [scheme.color(i) for i in range(len(order))]

    figure = base_figure(scheme, showlegend=False, hovermode="x")
    summary = []
    for name, color in zip(order, colors, strict=True):
        values = runs.loc[runs[group] == name, "clear_min"]
        common = {
            "y": values,
            "name": name,
            "marker": {"color": color, "size": 3, "opacity": 0.55},
            "line": {"color": color},
            "hoverinfo": "skip",
        }
        if kind == "violin":
            figure.add_trace(
                go.Violin(**common, points=point_mode, box={"visible": True}, meanline={"visible": True}, spanmode="hard")
            )
        else:
            figure.add_trace(
                go.Box(**common, boxpoints=point_mode, boxmean=True, jitter=0.4 if points == "all" else 0, pointpos=0)
            )
        quartiles = values.quantile([0.0, 0.25, 0.5, 0.75, 1.0]).tolist()
        summary.append((name, len(values), quartiles, values.mean()))

    # Invisible markers carry a Czech hover summary for each group.
    figure.add_trace(
        go.Scatter(
            x=[name for name, *_ in summary],
            y=[quartiles[2] for _, _, quartiles, _ in summary],
            mode="markers",
            marker={"size": 24, "opacity": 0},
            customdata=[
                [
                    f"{fmt_int(n)} {runs_word(n)}",
                    *(fmt_duration(q) for q in quartiles),
                    fmt_duration(mean),
                    html_lib.escape(str(name)),
                ]
                for name, n, quartiles, mean in summary
            ],
            hovertemplate=(
                "<b>%{customdata[7]}</b> · %{customdata[0]}<br>"
                "Medián: %{customdata[3]} · průměr: %{customdata[6]}<br>"
                "Kvartily (Q1–Q3): %{customdata[2]} – %{customdata[4]}<br>"
                "Nejrychlejší: %{customdata[1]} · nejpomalejší: %{customdata[5]}<extra></extra>"
            ),
        )
    )
    timer = float(runs["timer_min"].max())
    figure.add_hline(
        y=timer,
        line={"color": scheme.danger, "dash": "dash", "width": 1.5},
        annotation_text=f"Časový limit {fmt_duration(timer)}",
        annotation_position="top left",
        annotation_font_color=scheme.danger,
    )
    figure.update_xaxes(
        type="category",
        categoryorder="array",
        categoryarray=order,
        tickvals=order,
        ticktext=[f"{name}<br>n = {fmt_int(n)}" for name, n, _, _ in summary],
        title={"text": DISTRIBUTION_GROUPS[group]},
    )
    figure.update_yaxes(title={"text": "Čas dokončení"})
    apply_duration_axis(figure, "y", [runs["clear_min"].min(), timer])
    return ChartResult(figure, notes)


def build_scatter_figure(
    selection: Selection,
    scheme: ColorScheme,
    *,
    color_by: str = "key_label",
    max_points: int = 3000,
    trend: bool = True,
) -> ChartResult:
    """Scatter plot of team item level against clear time with optional trend line."""
    if selection.empty:
        return ChartResult(message_figure(scheme, NO_DATA))
    runs = selection.runs.dropna(subset=["avg_ilvl"])
    if runs.empty:
        return ChartResult(message_figure(scheme, "Vybrané runy nemají platný item level."))
    notes: list[Alert] = []
    sample = runs
    if len(runs) > max_points:
        sample = runs.sample(n=max_points, random_state=SCATTER_SEED)
        notes.append(
            Alert(
                "info",
                f"Zobrazen náhodný vzorek {fmt_int(max_points)} z {fmt_int(len(runs))} runů; "
                "trend se počítá ze všech runů.",
            )
        )

    order = category_order(sample, color_by)
    colors = [scheme.color(i) for i in range(len(order))]
    hover = run_hover_text(sample)
    figure = base_figure(scheme, hovermode="closest")
    for name, color in zip(order, colors, strict=True):
        part = sample[sample[color_by] == name]
        # SVG scatter instead of WebGL: works in browsers and VMs without GPU support.
        figure.add_trace(
            go.Scatter(
                x=part["avg_ilvl"],
                y=part["clear_min"],
                mode="markers",
                name=str(name),
                marker={"color": color, "size": 6, "opacity": 0.7},
                customdata=hover.loc[part.index],
                hovertemplate="%{customdata}<extra></extra>",
            )
        )

    x_low = float(runs["avg_ilvl"].quantile(0.005)) - 0.5
    x_high = float(runs["avg_ilvl"].max()) + 0.5
    hidden = int((runs["avg_ilvl"] < x_low).sum())
    if hidden:
        notes.append(
            Alert(
                "info",
                f"{fmt_int(hidden)} {runs_word(hidden)} s velmi nízkým item levelem leží mimo výchozí zobrazení; "
                "zobrazíte je tlačítkem „Automatické měřítko“.",
            )
        )

    if trend:
        if len(runs) >= 10 and runs["avg_ilvl"].nunique() > 1:
            slope, intercept = np.polyfit(runs["avg_ilvl"], runs["clear_min"], 1)
            correlation = runs["avg_ilvl"].corr(runs["clear_min"])
            xs = np.array([x_low, x_high])
            figure.add_trace(
                go.Scatter(
                    x=xs,
                    y=slope * xs + intercept,
                    mode="lines",
                    name="Lineární trend",
                    line={"color": scheme.text, "dash": "dash", "width": 2},
                    hoverinfo="skip",
                )
            )
            seconds = slope * 60
            sign = "−" if seconds < 0 else "+"
            notes.append(
                Alert(
                    "info",
                    f"Trend: každý item level navíc znamená v průměru {sign}{fmt_float(abs(seconds), 1)} s "
                    f"času dokončení (korelace r = {fmt_float(correlation, 2)}).",
                )
            )
            if runs["mythic_level"].nunique() > 1:
                notes.append(
                    Alert(
                        "info",
                        "Vyšší klíče hrají týmy s vyšším item levelem, ale trvají déle – trend je tím zkreslen. "
                        "Pro čistší srovnání zužte filtr úrovně klíče na jednu hodnotu.",
                    )
                )
        else:
            notes.append(Alert("info", "Pro výpočet trendu je ve výběru příliš málo runů."))

    figure.update_xaxes(title={"text": "Průměrný item level týmu"}, range=[x_low, x_high])
    figure.update_yaxes(title={"text": "Čas dokončení"})
    apply_duration_axis(figure, "y", runs["clear_min"])
    figure.update_layout(legend={"title": {"text": SCATTER_COLORS[color_by] + ": "}})
    return ChartResult(figure, notes)


def build_compositions_figure(
    selection: Selection,
    scheme: ColorScheme,
    *,
    level: str = "spec",
    metric: str = "count",
    min_runs: int = 10,
    top_n: int = 10,
) -> ChartResult:
    """Ranking of team compositions (tank + healer + unordered trio of DPS)."""
    runs = selection.runs
    if runs.empty:
        return ChartResult(message_figure(scheme, NO_DATA))
    info = METRICS[metric]
    keys = ["tank_label", "healer_label", "dps_specs"] if level == "spec" else ["tank_class", "healer_class", "dps_classes"]
    stats = aggregate_runs(runs, keys, ilvl_column="avg_ilvl").reset_index()
    total = len(stats)
    stats = stats[stats["count"] >= min_runs]
    if stats.empty:
        message = f"Žádné složení nemá alespoň {min_runs} {runs_word(min_runs)} – snižte minimální počet runů."
        return ChartResult(message_figure(scheme, message), [Alert("warning", message)])

    eligible = len(stats)
    ascending = info.kind == "duration"
    stats = stats.sort_values([metric, "count"], ascending=[ascending, False]).head(top_n)
    notes = [
        Alert(
            "info",
            f"Zobrazeno {len(stats)} z {fmt_int(eligible)} složení s alespoň {min_runs} {'runem' if min_runs == 1 else 'runy'} "
            f"(ve výběru je celkem {fmt_int(total)} různých složení).",
        )
    ]
    if info.kind == "duration" and runs["mythic_level"].nunique() > 1:
        notes.append(
            Alert("info", "Časy závisí na úrovni klíče – pro férové srovnání zužte filtr úrovně klíče na jednu hodnotu.")
        )

    labels = (
        "<b>" + escape(stats[keys[0]]) + " · " + escape(stats[keys[1]]) + "</b><br>"
        + f"<span style='color:{scheme.muted}'>" + escape(stats[keys[2]]) + "</span>"
    ).tolist()
    customdata = np.column_stack(
        [
            escape(stats[keys[0]]),
            escape(stats[keys[1]]),
            escape(stats[keys[2]]),
            stats["count"].map(fmt_int),
            stats["median_time"].map(fmt_duration),
            stats["best_time"].map(fmt_duration),
            stats["mean_key"].map(lambda v: format_metric(v, "level")),
            stats["max_key"].map(lambda v: format_metric(v, "level")),
        ]
    )
    figure = base_figure(scheme, showlegend=False, bargap=0.3)
    figure.add_trace(
        go.Bar(
            x=stats[metric],
            y=labels,
            orientation="h",
            marker={"color": scheme.accent, "line": {"color": scheme.border_strong, "width": 1}},
            text=[format_metric(value, info.kind) for value in stats[metric]],
            textposition="outside",
            cliponaxis=False,
            customdata=customdata,
            hovertemplate=(
                "<b>Tank:</b> %{customdata[0]}<br><b>Healer:</b> %{customdata[1]}<br>"
                "<b>DPS:</b> %{customdata[2]}<br><br>"
                "Počet runů: %{customdata[3]}<br>"
                "Medián času: %{customdata[4]} · nejrychlejší: %{customdata[5]}<br>"
                "Úroveň klíče: Ø %{customdata[6]} · max %{customdata[7]}<extra></extra>"
            ),
        )
    )
    figure.update_yaxes(autorange="reversed", automargin=True, type="category", title=None, ticksuffix="  ")
    apply_value_axis(figure, stats[metric], info)
    return ChartResult(figure, notes)


# =============================================================================
# 5. Dash layout
# =============================================================================

FILTER_STORE = "filter-store"

# Dashboard pages sized for one screen: (tab title, ids of the cards shown on the page).
PAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Vývoj v čase", ("timeline", "heatmap")),
    ("Specializace a třídy", ("leaders", "share")),
    ("Časy a výkon", ("dist", "scatter")),
    ("Složení týmů", ("comps",)),
    ("Žebříček runů", ("runs",)),
)


def step_page(current: Any, step: int, count: int = len(PAGES)) -> int:
    """Return the page number reached from ``current`` by ``step``, kept within ``1..count``."""
    page, _ = coerce_int(current, minimum=1, maximum=count, default=1, label="Strana")
    return min(max(page + step, 1), count)


def page_class(number: int, active: int) -> str:
    """CSS classes of a page section; inactive pages stay mounted but hidden."""
    layout = "double" if len(PAGES[number - 1][1]) > 1 else "single"
    return f"page {layout}" + ("" if number == active else " hidden")


BASE_CSS = """
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { font-family: __FONT__; }
.app { height: 100vh; display: grid; grid-template-rows: auto minmax(0, 1fr); overflow: hidden;
  background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.4;
  transition: background-color .2s ease, color .2s ease; }
.app a { color: var(--accent); }
.topbar { display: flex; flex-wrap: wrap; gap: 6px 28px; align-items: center; justify-content: space-between;
  padding: 10px 20px; background: var(--surface); border-bottom: 1px solid var(--border); }
.brand h1 { margin: 0; font-size: 20px; letter-spacing: .02em; }
.brand h1 span { color: var(--accent); }
.brand p { margin: 0; color: var(--muted); font-size: 12.5px; }
.scheme-picker { display: flex; align-items: center; gap: 12px; }
.workspace { display: grid; grid-template-columns: 310px minmax(0, 1fr); min-height: 0; }
.sidebar { min-height: 0; overflow-y: auto; padding: 14px 18px; background: var(--surface);
  border-right: 1px solid var(--border); display: flex; flex-direction: column; gap: 16px; }
.content { min-width: 0; min-height: 0; display: flex; flex-direction: column; gap: 12px; padding: 14px 18px;
  overflow: hidden; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 16px;
  box-shadow: var(--shadow); min-width: 0; }
.card-header h2, .panel-header h2 { margin: 0; font-size: 16px; }
.subtitle { margin: 1px 0 0; color: var(--muted); font-size: 12.5px; }
.panel-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
.filters { display: flex; flex-direction: column; gap: 12px; margin-top: 12px; }
.filters .pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.info summary { cursor: pointer; font-weight: 600; }
.info[open] summary { margin-bottom: 6px; }
.info ul { margin: 0; padding-left: 18px; color: var(--muted); font-size: 12.5px; }
.info li { margin: 3px 0; }
.info strong { color: var(--text); }
.control { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.control > label, .control-label { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
  color: var(--muted); }
.controls { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: flex-end; margin: 8px 0 6px; }
.controls .control { flex: 1 1 150px; }
.controls .control.slider { flex: 2 1 200px; }
.controls .control:has(> .options) { flex: 0 0 auto; }
.controls .options { flex-wrap: nowrap; white-space: nowrap; }
.text-input { width: 100%; height: 36px; padding: 6px 10px; border-radius: 8px; border: 1px solid var(--border-strong);
  background: var(--input-bg); color: var(--text); font: inherit; }
.text-input:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.text-input::placeholder { color: var(--muted); opacity: .8; }
.btn { height: 34px; padding: 0 14px; border-radius: 8px; border: 1px solid var(--accent); background: transparent;
  color: var(--accent); font: inherit; font-weight: 600; cursor: pointer; white-space: nowrap; }
.btn:hover:not(:disabled) { background: var(--accent-soft); }
.btn:disabled { opacity: .35; cursor: default; }
.btn-small { height: 30px; padding: 0 12px; font-size: 13px; }
.options { display: flex; flex-wrap: wrap; gap: 4px 14px; align-items: center; min-height: 34px; }
.options label { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
.options input { accent-color: var(--accent); margin: 0; }
.sidebar .options { column-gap: 12px; }
.filter-status { display: flex; flex-direction: column; gap: 8px; }
.status { margin: 0; color: var(--muted); }
.status strong { color: var(--text); font-size: 15px; }
.kpis { flex: none; display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 10px; }
.kpi { padding: 8px 12px; cursor: help; }
.kpi-label, .kpi-value, .kpi-detail { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kpi-label { font-size: 10.5px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
.kpi-value { font-size: 20px; font-weight: 700; line-height: 1.3; }
.kpi-value.compact { font-size: 15px; padding: 3px 0 2px; }
.kpi-detail { font-size: 11.5px; color: var(--muted); }
.pager { flex: none; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; }
.pager-nav { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.page-tabs { flex: 1 1 auto; display: flex; flex-wrap: wrap; gap: 6px; }
.page-tabs label { display: inline-flex; align-items: center; height: 32px; padding: 0 13px; border-radius: 999px;
  border: 1px solid var(--border-strong); color: var(--muted); font-size: 13.5px; font-weight: 600; white-space: nowrap;
  cursor: pointer; user-select: none; }
.page-tabs label:hover { color: var(--text); border-color: var(--accent); }
.page-tabs label.selected, .page-tabs label:has(input:checked) { background: var(--accent); border-color: var(--accent);
  color: var(--surface); }
.page-tabs label:has(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
.page-tabs .dash-options-list-option-wrapper, .page-tabs input { position: absolute; width: 1px; height: 1px;
  opacity: 0; overflow: hidden; }
.page-indicator { color: var(--muted); font-size: 13px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.page { flex: 1 1 auto; min-height: 0; display: grid; gap: 14px; }
.page.double { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.page.single { grid-template-columns: minmax(0, 1fr); }
.page.hidden { display: none; }
.chart-card { display: flex; flex-direction: column; min-height: 0; }
.graph-wrap { flex: 1 1 auto; position: relative; min-height: 200px; }
/* dcc.Loading puts an inline position on its wrapper, hence !important. */
.graph-loading { position: absolute !important; inset: 0; }
.graph { width: 100%; height: 100%; }
.table-area { flex: 1 1 auto; min-height: 0; overflow: auto; }
.alerts { display: flex; flex-direction: column; gap: 6px; }
.alerts:empty { display: none; }
.chart-card .alerts { margin-bottom: 6px; }
.alert { padding: 6px 12px; border-radius: 8px; border-left: 4px solid; font-size: 13px; }
.alert ul { margin: 4px 0 0; padding-left: 18px; }
.alert.collapsible summary { cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.alert.collapsible[open] .preview { display: none; }
.alert-info { border-color: var(--info); background: color-mix(in srgb, var(--info) 10%, transparent); }
.alert-warning { border-color: var(--warning); background: color-mix(in srgb, var(--warning) 13%, transparent); }
.alert-error { border-color: var(--danger); background: color-mix(in srgb, var(--danger) 14%, transparent); }
table.runs { width: 100%; border-collapse: collapse; font-size: 13px; }
table.runs th { position: sticky; top: 0; z-index: 1; background: var(--surface); text-align: left; font-size: 11px;
  letter-spacing: .05em; text-transform: uppercase; color: var(--muted); padding: 8px 10px;
  border-bottom: 1px solid var(--border-strong); white-space: nowrap; }
table.runs td { padding: 7px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
table.runs tbody tr:hover { background: var(--accent-soft); }
table.runs .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
table.runs .key { font-weight: 700; color: var(--accent); }
.member { display: flex; align-items: center; gap: 6px; white-space: nowrap; }
.member + .member { margin-top: 2px; }
.class-dot { width: 9px; height: 9px; border-radius: 50%; flex: none; box-shadow: 0 0 0 1px var(--border-strong); }
.spec { color: var(--muted); font-size: 12px; }
.empty { color: var(--muted); padding: 16px 4px; }
.footer { margin-top: auto; color: var(--muted); font-size: 11.5px; }
@media (max-width: 1499px) {
  .workspace { grid-template-columns: 270px minmax(0, 1fr); }
  .filters .pair { grid-template-columns: minmax(0, 1fr); gap: 12px; }
  .page-indicator { display: none; }
}
@media (max-height: 819px) { .brand p { display: none; } }
/* Wide but low screens: keep the sidebar, scroll the page content with fixed chart heights. */
@media (max-height: 719px) {
  .content { overflow-y: auto; }
  .pager { position: sticky; top: 0; z-index: 5; padding: 6px 0; background: var(--bg); }
  .page { flex: none; }
  .graph-wrap { flex: none; height: 400px; }
  .table-area { flex: none; max-height: 70vh; }
}
/* Narrow screens: fall back to a normally scrolling page. */
@media (max-width: 1199px) {
  .app { display: block; height: auto; min-height: 100vh; overflow: visible; }
  .workspace { display: block; }
  .sidebar { overflow: visible; border-right: 0; border-bottom: 1px solid var(--border); }
  .filters { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); align-items: start; }
  .filters .pair { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .content { overflow: visible; }
  .kpis { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
  .page.double { grid-template-columns: minmax(0, 1fr); }
  .graph-wrap { flex: none; height: 440px; }
  .table-area { max-height: 75vh; }
}
""".replace("__FONT__", FONT_FAMILY)

# Arrow keys switch pages unless the user is interacting with a form control.
PAGER_KEYS_SCRIPT = """<script>
document.addEventListener("keydown", function (event) {
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  var target = event.target;
  if (target && target.closest && target.closest(
    "input, textarea, select, [contenteditable], [role=slider], [role=combobox], [role=listbox], .dash-dropdown")) return;
  var button = document.getElementById(event.key === "ArrowLeft" ? "page-prev" : "page-next");
  if (button && !button.disabled) { button.click(); event.preventDefault(); }
});
</script>"""

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="cs">
<head>
{%metas%}
<title>{%title%}</title>
{%favicon%}
{%css%}
<style>__STYLES__</style>
</head>
<body>
{%app_entry%}
<footer>
{%config%}
{%scripts%}
{%renderer%}
</footer>
__SCRIPTS__
</body>
</html>"""


def build_stylesheet() -> str:
    """Base CSS plus one class with CSS variables per colour scheme."""
    blocks = [BASE_CSS]
    for scheme in SCHEMES.values():
        declarations = "; ".join(f"{name}: {value}" for name, value in scheme.css_variables().items())
        blocks.append(f".scheme-{scheme.key} {{ {declarations}; color-scheme: {'dark' if scheme.dark else 'light'}; }}")
    return "\n".join(blocks)


def options(mapping: Mapping[Any, str]) -> list[dict[str, Any]]:
    """Convert ``{value: label}`` into Dash options."""
    return [{"label": label, "value": value} for value, label in mapping.items()]


def control(label: str, component: Any, *, html_for: str | None = None, class_name: str = "") -> html.Div:
    """Labelled control wrapper."""
    return html.Div([html.Label(label, htmlFor=html_for), component], className=f"control {class_name}".strip())


def dropdown(component_id: str, mapping: Mapping[str, str], value: str) -> dcc.Dropdown:
    """Single-select dropdown that cannot be cleared."""
    return dcc.Dropdown(
        id=component_id,
        options=options(mapping),
        value=value,
        clearable=False,
        searchable=False,
        labels=DROPDOWN_LABELS,
    )


def radio(component_id: str, mapping: Mapping[str, str], value: str) -> dcc.RadioItems:
    """Inline radio buttons."""
    return dcc.RadioItems(id=component_id, options=options(mapping), value=value, inline=True, className="options")


def toggle(component_id: str, label: str, enabled: bool) -> dcc.Checklist:
    """Single checkbox returning ``["on"]`` when checked."""
    return dcc.Checklist(
        id=component_id,
        options=[{"label": label, "value": "on"}],
        value=["on"] if enabled else [],
        inline=True,
        className="options",
    )


def slider(component_id: str, minimum: int, maximum: int, value: int, step: int, marks: Iterable[int]) -> dcc.Slider:
    """Integer slider with formatted marks."""
    return dcc.Slider(
        id=component_id,
        min=minimum,
        max=maximum,
        step=step,
        value=value,
        marks={mark: fmt_int(mark) for mark in marks},
        allow_direct_input=False,
        tooltip={"placement": "bottom", "always_visible": False},
    )


def chart_card(chart_id: str, title: str, subtitle: str, controls: list[Any]) -> html.Section:
    """Card with a title, local controls, notes and a graph filling the remaining height."""
    return html.Section(
        id=f"{chart_id}-card",
        className="card chart-card",
        children=[
            html.Header([html.H2(title), html.P(subtitle, className="subtitle")], className="card-header"),
            html.Div(controls, className="controls"),
            html.Div(id=f"{chart_id}-notes", className="alerts", **{"aria-live": "polite"}),
            html.Div(
                dcc.Loading(
                    dcc.Graph(id=f"{chart_id}-graph", config=GRAPH_CONFIG, responsive=True, className="graph"),
                    type="dot",
                    delay_show=300,
                    color="var(--accent)",
                    parent_className="graph-loading",
                ),
                className="graph-wrap",
            ),
        ],
    )


def render_alerts(alerts: Iterable[Alert | None]) -> list[html.Div]:
    """Render alerts as styled boxes, one box per severity level."""
    titles = {"error": "Chyba", "warning": "Upozornění", "info": "Informace"}
    grouped: dict[str, list[str]] = {}
    for alert in alerts:
        if alert is not None:
            grouped.setdefault(alert.level if alert.level in titles else "info", []).append(alert.message)
    boxes = []
    for level, title in titles.items():
        messages = grouped.get(level)
        if not messages:
            continue
        if level == "info":
            # Informational notes collapse to one line so that charts keep their height.
            boxes.append(
                html.Details(
                    [
                        html.Summary(
                            [
                                html.Strong(f"{title}" + (f" ({len(messages)})" if len(messages) > 1 else "") + ": "),
                                html.Span(messages[0], className="preview"),
                            ]
                        ),
                        html.Ul([html.Li(message) for message in messages]),
                    ],
                    className="alert alert-info collapsible",
                    role="status",
                )
            )
            continue
        body: Any = messages[0] if len(messages) == 1 else html.Ul([html.Li(message) for message in messages])
        boxes.append(
            html.Div(
                [html.Strong(f"{title}: "), body],
                className=f"alert alert-{level}",
                role="alert" if level == "error" else "status",
            )
        )
    return boxes


def render_status(selection: Selection, alerts: list[Alert]) -> list[Any]:
    """Filter summary line followed by validation alerts."""
    count, total = len(selection.runs), selection.total_runs
    summary = html.P(
        [
            "Filtrům odpovídá ",
            html.Strong(fmt_int(count)),
            f" z {fmt_int(total)} runů ({fmt_pct(count / total * 100)}).",
        ],
        className="status",
    )
    return [summary, *render_alerts(alerts)]


def render_kpis(kpis: list[Kpi]) -> list[html.Div]:
    """Render KPI tiles; the hint is shown as a native tooltip."""
    return [
        html.Div(
            [
                html.Div(kpi.label, className="kpi-label"),
                html.Div(kpi.value, className="kpi-value compact" if kpi.compact else "kpi-value"),
                html.Div(kpi.detail, className="kpi-detail"),
            ],
            className="card kpi",
            title=kpi.hint,
        )
        for kpi in kpis
    ]


def render_runs_table(rows: list[RunRow]) -> Any:
    """Render the run leaderboard as an HTML table."""
    if not rows:
        return html.P("Filtrům neodpovídá žádný run.", className="empty")
    headers = [
        ("#", "num"), ("Klíč", ""), ("Čas", "num"), ("Rezerva", "num"), ("Ø ilvl", "num"),
        ("Tank", ""), ("Healer", ""), ("DPS", ""), ("Affix týdne", ""), ("Frakce", ""), ("Dokončeno", ""),
    ]

    def members(row: RunRow, role: str) -> html.Td:
        return html.Td(
            [
                html.Div(
                    [
                        html.Span(className="class-dot", style={"background": CLASS_COLORS.get(class_name, "#888")}),
                        html.Span(name),
                        html.Span(label, className="spec"),
                    ],
                    className="member",
                    title=f"{name} – {label}",
                )
                for member_role, name, label, class_name in row.team
                if member_role == role
            ]
        )

    body = [
        html.Tr(
            [
                html.Td(str(row.rank), className="num"),
                html.Td(row.key, className="key"),
                html.Td(row.time, className="num"),
                html.Td(row.remaining, className="num"),
                html.Td(row.ilvl, className="num"),
                members(row, "tank"),
                members(row, "healer"),
                members(row, "dps"),
                html.Td(row.affix),
                html.Td(row.faction),
                html.Td(row.completed, className="num"),
            ]
        )
        for row in rows
    ]
    return html.Table(
        [html.Thead(html.Tr([html.Th(text, className=cls) for text, cls in headers])), html.Tbody(body)],
        className="runs",
    )


def _header(scheme_key: str) -> html.Header:
    return html.Header(
        className="topbar",
        children=[
            html.Div(
                [
                    html.H1(["Pit of Saron ", html.Span("Dashboard")]),
                    html.P("Analýza týmových Mythic+ runů dungeonu Pit of Saron (World of Warcraft) z Raider.IO API"),
                ],
                className="brand",
            ),
            html.Div(
                [
                    html.Label("Barevné schéma", htmlFor="scheme", className="control-label"),
                    dcc.RadioItems(
                        id="scheme",
                        options=[
                            {"label": html.Span(s.label, title=s.description), "value": s.key}
                            for s in SCHEMES.values()
                        ],
                        value=scheme_key,
                        inline=True,
                        className="options",
                    ),
                ],
                className="scheme-picker",
            ),
        ],
    )


def _data_info(data: RunData) -> html.Details:
    bounds = data.bounds
    runs = data.runs
    facts = [
        html.Li(
            [
                html.Strong(f"{fmt_int(len(runs))} runů"),
                f" ze souboru {data.source.name}, období {fmt_date(bounds.dates[0])} – {fmt_date(bounds.dates[-1])}, "
                f"sezóna {data.season}, úrovně klíče +{bounds.key_min} až +{bounds.key_max}.",
            ]
        ),
        html.Li(
            "Zdroj: veřejné Raider.IO API (endpointy mythic-plus/runs a mythic-plus/run-details). "
            f"Data, dny a hodiny jsou uvedeny v časové zóně {data.timezone}."
        ),
    ]
    if (runs["remaining_min"] >= 0).all():
        facts.append(
            html.Li(f"Všechny runy byly dokončeny v časovém limitu klíče {fmt_duration(runs['timer_min'].max())}.")
        )
    if data.dropped_rows:
        facts.append(html.Li(f"{fmt_int(data.dropped_rows)} neplatných nebo duplicitních řádků bylo při načítání vynecháno."))
    if data.missing_ilvl:
        facts.append(
            html.Li(f"U {fmt_int(data.missing_ilvl)} hráčů chyběl item level (hodnota 0) – v průměrech je vynechán.")
        )
    facts.append(
        html.Li(
            "Globální filtry ovlivňují všechny stránky, grafy, ukazatele i tabulku; ovládací prvky u grafu mění jen daný graf. "
            "Po najetí myší grafy zobrazí podrobnosti, kliknutím na legendu lze řady skrýt (dvojklikem izolovat)."
        )
    )
    facts.append(
        html.Li("Mezi stránkami lze přepínat záložkami, tlačítky Předchozí/Další nebo šipkami ← a → na klávesnici.")
    )
    return html.Details([html.Summary("O datech a ovládání"), html.Ul(facts)], className="info")


def _filter_panel(data: RunData, default: RunFilter) -> html.Section:
    bounds = data.bounds
    date_options = [{"label": fmt_date(day), "value": day.date().isoformat()} for day in bounds.dates]
    faction_counts = data.runs["faction"].value_counts()
    faction_options = [
        {"label": f"{FACTION_LABELS.get(code, code)} ({fmt_int(faction_counts[code])})", "value": code}
        for code in bounds.factions
    ]
    affix_options = {ALL: "Vše", **{affix: affix for affix in bounds.affixes}}
    return html.Section(
        className="filter-panel",
        children=[
            html.Header(
                [
                    html.H2("Filtry"),
                    html.Button("Zrušit filtry", id="f-reset", n_clicks=0, className="btn btn-small"),
                ],
                className="panel-header",
            ),
            html.Div(id="f-status", className="filter-status", **{"aria-live": "polite"}),
            html.Div(
                className="filters",
                children=[
                    html.Div(
                        [
                            control(
                                "Období od",
                                dcc.Dropdown(
                                    id="f-date-start", options=date_options, value=default.start,
                                    clearable=False, labels=DROPDOWN_LABELS,
                                ),
                            ),
                            control(
                                "Období do",
                                dcc.Dropdown(
                                    id="f-date-end", options=date_options, value=default.end,
                                    clearable=False, labels=DROPDOWN_LABELS,
                                ),
                            ),
                        ],
                        className="pair",
                    ),
                    control(
                        "Úroveň klíče",
                        dcc.RangeSlider(
                            id="f-keys",
                            min=bounds.key_min,
                            max=bounds.key_max,
                            step=1,
                            value=[default.key_min, default.key_max],
                            marks={level: f"+{level}" for level in range(bounds.key_min, bounds.key_max + 1)},
                            allow_direct_input=False,
                        ),
                    ),
                    control(
                        "Frakce",
                        dcc.Checklist(
                            id="f-factions", options=faction_options, value=list(default.factions),
                            inline=True, className="options",
                        ),
                    ),
                    control("Affix týdne", radio("f-affix", affix_options, ALL)),
                    control(
                        "Třídy v týmu (všechny vybrané)",
                        dcc.Dropdown(
                            id="f-classes",
                            options=list(bounds.classes),
                            value=[],
                            multi=True,
                            placeholder="Libovolné třídy",
                            labels=DROPDOWN_LABELS,
                        ),
                    ),
                    control(
                        f"Min. Ø item level týmu ({bounds.ilvl_min}–{bounds.ilvl_max})",
                        dcc.Input(
                            id="f-ilvl", type="number", step=0.5, debounce=True,
                            placeholder="bez omezení", className="text-input",
                        ),
                        html_for="f-ilvl",
                    ),
                    control(
                        "Hráč (část jména)",
                        dcc.Input(
                            id="f-player", type="text", debounce=True,
                            placeholder="např. Kombat", className="text-input",
                        ),
                        html_for="f-player",
                    ),
                ],
            ),
        ],
    )


def _cards() -> dict[str, html.Section]:
    """All dashboard cards keyed by id; pages pick from them."""
    charts = [
        chart_card(
            "timeline",
            "Vývoj v čase",
            "Aktivita a výkonnost týmů po dnech nebo týdnech.",
            [
                control("Metrika", dropdown("timeline-metric", TIMELINE_METRICS, "count")),
                control("Rozdělit podle", dropdown("timeline-split", TIMELINE_SPLITS, "none")),
                control("Granularita", radio("timeline-granularity", TIMELINE_GRANULARITY, "day")),
                control("Zobrazení", toggle("timeline-cumulative", "Kumulativně", False)),
            ],
        ),
        chart_card(
            "leaders",
            "Žebříček",
            "Nejhranější, nejrychlejší a nejúspěšnější specializace, třídy, hráči a rasy.",
            [
                control("Položky", dropdown("leaders-dimension", LEADER_DIMENSIONS, "spec")),
                control("Role", dropdown("leaders-role", LEADER_ROLES, ALL)),
                control("Řadit podle", dropdown("leaders-metric", LEADER_METRICS, "count")),
                control("Pořadí", radio("leaders-order", SORT_ORDER, "desc")),
                control("Počet položek", slider("leaders-top", 3, 25, 12, 1, (3, 10, 15, 20, 25)), class_name="slider"),
                control("Barvy", toggle("leaders-colors", "Herní barvy tříd", True)),
            ],
        ),
        chart_card(
            "share",
            "Podíl kategorií",
            "Složení vybraných runů podle zvolené kategorie.",
            [
                control("Kategorie", dropdown("share-dimension", SHARE_DIMENSIONS, "tank")),
                control("Popisky", radio("share-labels", SHARE_LABELS, "percent")),
                control("Max. výsečí", slider("share-slices", 3, 12, 8, 1, (3, 6, 9, 12)), class_name="slider"),
                control("Barvy", toggle("share-colors", "Herní barvy tříd", True)),
            ],
        ),
        chart_card(
            "heatmap",
            "Teplotní mapa",
            "Kdy se hraje nejvíc a které kombinace fungují nejlépe.",
            [
                control("Osy", dropdown("heatmap-mode", HEATMAP_MODES, "weekday_hour")),
                control("Hodnota", dropdown("heatmap-metric", HEATMAP_METRICS, "count")),
            ],
        ),
        chart_card(
            "dist",
            "Rozložení časů dokončení",
            "Medián, kvartily a odlehlé hodnoty času podle skupin.",
            [
                control("Skupiny", dropdown("dist-group", DISTRIBUTION_GROUPS, "key_label")),
                control("Typ grafu", radio("dist-kind", DISTRIBUTION_KINDS, "box")),
                control("Body", radio("dist-points", DISTRIBUTION_POINTS, "outliers")),
            ],
        ),
        chart_card(
            "scatter",
            "Item level vs. čas dokončení",
            "Každý bod je jeden run – po najetí myší se zobrazí složení týmu.",
            [
                control("Barva podle", dropdown("scatter-color", SCATTER_COLORS, "key_label")),
                control(
                    "Max. počet bodů",
                    slider("scatter-points", 500, 8000, 3000, 500, (500, 2000, 4000, 6000, 8000)),
                    class_name="slider",
                ),
                control("Trend", toggle("scatter-trend", "Lineární trend", True)),
            ],
        ),
        chart_card(
            "comps",
            "Nejúspěšnější složení týmů",
            "Tank + healer + tři DPS (na pořadí DPS nezáleží).",
            [
                control("Úroveň detailu", radio("comps-level", COMPOSITION_LEVELS, "spec")),
                control("Řadit podle", dropdown("comps-metric", COMPOSITION_METRICS, "count")),
                control(
                    "Min. počet runů (1–1000)",
                    dcc.Input(id="comps-min", type="number", value=10, step=1, debounce=True, className="text-input"),
                    html_for="comps-min",
                ),
                control("Počet složení", slider("comps-top", 3, 20, 10, 1, (3, 10, 15, 20)), class_name="slider"),
            ],
        ),
    ]
    cards = {card.id.removesuffix("-card"): card for card in charts}
    cards["runs"] = _runs_card()
    return cards


def _runs_card() -> html.Section:
    return html.Section(
        id="runs-card",
        className="card chart-card",
        children=[
            html.Header(
                [
                    html.H2("Žebříček runů"),
                    html.P("Nejlepší i nejhorší runy výběru včetně složení týmu.", className="subtitle"),
                ],
                className="card-header",
            ),
            html.Div(
                [
                    control("Řadit podle", dropdown("runs-sort", {k: v[0] for k, v in RUN_SORTS.items()}, "clear")),
                    control("Pořadí", radio("runs-order", SORT_ORDER, "asc")),
                    control(
                        "Počet řádků (1–100)",
                        dcc.Input(id="runs-limit", type="number", value=15, step=1, debounce=True, className="text-input"),
                        html_for="runs-limit",
                    ),
                ],
                className="controls",
            ),
            html.Div(id="runs-notes", className="alerts"),
            html.Div(
                dcc.Loading(html.Div(id="runs-table"), type="dot", delay_show=300, color="var(--accent)"),
                className="table-area",
            ),
        ],
    )


def _pager() -> html.Nav:
    count = len(PAGES)
    return html.Nav(
        className="pager",
        children=[
            dcc.RadioItems(
                id="page",
                options=[{"label": f"{number}. {title}", "value": number} for number, (title, _) in enumerate(PAGES, 1)],
                value=1,
                inline=True,
                className="page-tabs",
            ),
            html.Div(
                [
                    html.Span(f"Strana 1 z {count}", id="page-indicator", className="page-indicator"),
                    html.Button(
                        "‹ Předchozí", id="page-prev", n_clicks=0, disabled=True,
                        className="btn btn-small", title="Předchozí stránka (klávesa ←)",
                    ),
                    html.Button(
                        "Další ›", id="page-next", n_clicks=0, disabled=count < 2,
                        className="btn btn-small", title="Další stránka (klávesa →)",
                    ),
                ],
                className="pager-nav",
            ),
        ],
        **{"aria-label": "Stránky dashboardu"},
    )


def _pages() -> list[html.Section]:
    cards = _cards()
    return [
        html.Section(
            [cards[card_id] for card_id in card_ids],
            id=f"page-{number}",
            className=page_class(number, 1),
            **{"aria-label": title},
        )
        for number, (title, card_ids) in enumerate(PAGES, 1)
    ]


def build_layout(data: RunData, scheme_key: str) -> html.Div:
    """Compose the page: header, filter sidebar and paged content."""
    default = RunFilter.default(data.bounds)
    return html.Div(
        id="app-root",
        className=f"app scheme-{scheme_key}",
        children=[
            dcc.Store(id=FILTER_STORE, data=default.to_store()),
            _header(scheme_key),
            html.Div(
                className="workspace",
                children=[
                    html.Aside(
                        className="sidebar",
                        children=[
                            _filter_panel(data, default),
                            _data_info(data),
                            html.Footer(
                                "© 2026 Daniel Abraham, Ondřej Janeček, Petr Vondrus · licence MIT · data: Raider.IO · "
                                "World of Warcraft je ochranná známka společnosti Blizzard Entertainment.",
                                className="footer",
                            ),
                        ],
                    ),
                    html.Main(
                        className="content",
                        children=[
                            html.Div(id="kpis", className="kpis", **{"aria-live": "polite"}),
                            _pager(),
                            *_pages(),
                        ],
                    ),
                ],
            ),
        ],
    )


# =============================================================================
# 6. Dash callbacks
# =============================================================================


def register_callbacks(app: Dash, data: RunData) -> None:
    """Wire controls to the data layer and figure builders."""
    bounds = data.bounds
    select = lru_cache(maxsize=64)(lambda run_filter: select_runs(data, run_filter))

    def scheme_of(key: Any) -> ColorScheme:
        return SCHEMES[choice(key, SCHEMES, DEFAULT_SCHEME)]

    def chart(
        builder: Callable[..., ChartResult],
        filters: Mapping[str, Any],
        scheme_key: Any,
        extra: Iterable[Alert | None] = (),
        **kwargs: Any,
    ) -> tuple[go.Figure, list[html.Div]]:
        scheme = scheme_of(scheme_key)
        try:
            result = builder(select(RunFilter.from_store(filters)), scheme, **kwargs)
        except Exception:  # last-resort guard so one failing chart does not break the page
            logger.exception("Rendering of %s failed", builder.__name__)
            message = "Graf se nepodařilo vykreslit kvůli neočekávané chybě; podrobnosti jsou v logu aplikace."
            return message_figure(scheme, "Graf se nepodařilo vykreslit."), render_alerts([Alert("error", message)])
        return result.figure, render_alerts([*extra, *result.notes])

    @app.callback(Output("app-root", "className"), Input("scheme", "value"))
    def switch_scheme(scheme_key: Any) -> str:
        return f"app scheme-{scheme_of(scheme_key).key}"

    @app.callback(
        Output("page", "value"),
        Input("page-prev", "n_clicks"),
        Input("page-next", "n_clicks"),
        State("page", "value"),
        prevent_initial_call=True,
    )
    def turn_page(_prev: int, _next: int, current: Any) -> int:
        return step_page(current, -1 if ctx.triggered_id == "page-prev" else 1)

    @app.callback(
        *(Output(f"page-{number}", "className") for number in range(1, len(PAGES) + 1)),
        Output("page-prev", "disabled"),
        Output("page-next", "disabled"),
        Output("page-indicator", "children"),
        Input("page", "value"),
    )
    def show_page(value: Any) -> tuple[Any, ...]:
        active = step_page(value, 0)
        classes = [page_class(number, active) for number in range(1, len(PAGES) + 1)]
        return (*classes, active == 1, active == len(PAGES), f"Strana {active} z {len(PAGES)}")

    @app.callback(
        Output("f-date-start", "value"),
        Output("f-date-end", "value"),
        Output("f-keys", "value"),
        Output("f-factions", "value"),
        Output("f-affix", "value"),
        Output("f-classes", "value"),
        Output("f-ilvl", "value"),
        Output("f-player", "value"),
        Input("f-reset", "n_clicks"),
        prevent_initial_call=True,
    )
    def reset_filters(_: int) -> tuple[Any, ...]:
        default = RunFilter.default(bounds)
        return (
            default.start, default.end, [default.key_min, default.key_max],
            list(default.factions), ALL, [], None, "",
        )

    @app.callback(
        Output(FILTER_STORE, "data"),
        Output("f-status", "children"),
        Output("kpis", "children"),
        Input("f-date-start", "value"),
        Input("f-date-end", "value"),
        Input("f-keys", "value"),
        Input("f-factions", "value"),
        Input("f-affix", "value"),
        Input("f-classes", "value"),
        Input("f-ilvl", "value"),
        Input("f-player", "value"),
    )
    def update_filters(start, end, key_range, factions, affix, classes, min_ilvl, player):
        run_filter, alerts = build_filter(
            bounds, start=start, end=end, key_range=key_range, factions=factions,
            affix=affix, classes=classes, min_ilvl=min_ilvl, player=player,
        )
        selection = select(run_filter)
        alerts.extend(describe_selection(selection, run_filter))
        return run_filter.to_store(), render_status(selection, alerts), render_kpis(compute_kpis(selection))

    @app.callback(
        Output("timeline-graph", "figure"),
        Output("timeline-notes", "children"),
        Input(FILTER_STORE, "data"),
        Input("scheme", "value"),
        Input("timeline-metric", "value"),
        Input("timeline-split", "value"),
        Input("timeline-granularity", "value"),
        Input("timeline-cumulative", "value"),
    )
    def update_timeline(filters, scheme_key, metric, split, granularity, cumulative):
        return chart(
            build_timeline_figure, filters, scheme_key,
            metric=choice(metric, TIMELINE_METRICS, "count"),
            split=choice(split, TIMELINE_SPLITS, "none"),
            granularity=choice(granularity, TIMELINE_GRANULARITY, "day"),
            cumulative=bool(cumulative),
        )

    @app.callback(
        Output("leaders-graph", "figure"),
        Output("leaders-notes", "children"),
        Input(FILTER_STORE, "data"),
        Input("scheme", "value"),
        Input("leaders-dimension", "value"),
        Input("leaders-role", "value"),
        Input("leaders-metric", "value"),
        Input("leaders-order", "value"),
        Input("leaders-top", "value"),
        Input("leaders-colors", "value"),
    )
    def update_leaders(filters, scheme_key, dimension, role, metric, order, top_n, colors):
        top_n, alert = coerce_int(top_n, minimum=3, maximum=25, default=12, label="Počet položek")
        return chart(
            build_leaderboard_figure, filters, scheme_key, [alert],
            dimension=choice(dimension, LEADER_DIMENSIONS, "spec"),
            role=choice(role, LEADER_ROLES, ALL),
            metric=choice(metric, LEADER_METRICS, "count"),
            descending=choice(order, SORT_ORDER, "desc") == "desc",
            top_n=top_n,
            class_colors=bool(colors),
        )

    @app.callback(
        Output("share-graph", "figure"),
        Output("share-notes", "children"),
        Input(FILTER_STORE, "data"),
        Input("scheme", "value"),
        Input("share-dimension", "value"),
        Input("share-labels", "value"),
        Input("share-slices", "value"),
        Input("share-colors", "value"),
    )
    def update_share(filters, scheme_key, dimension, labels, slices, colors):
        slices, alert = coerce_int(slices, minimum=3, maximum=12, default=8, label="Max. výsečí")
        return chart(
            build_share_figure, filters, scheme_key, [alert],
            dimension=choice(dimension, SHARE_DIMENSIONS, "tank"),
            labels=choice(labels, SHARE_LABELS, "percent"),
            max_slices=slices,
            class_colors=bool(colors),
        )

    @app.callback(
        Output("heatmap-graph", "figure"),
        Output("heatmap-notes", "children"),
        Input(FILTER_STORE, "data"),
        Input("scheme", "value"),
        Input("heatmap-mode", "value"),
        Input("heatmap-metric", "value"),
    )
    def update_heatmap(filters, scheme_key, mode, metric):
        return chart(
            build_heatmap_figure, filters, scheme_key,
            mode=choice(mode, HEATMAP_MODES, "weekday_hour"),
            metric=choice(metric, HEATMAP_METRICS, "count"),
        )

    @app.callback(
        Output("dist-graph", "figure"),
        Output("dist-notes", "children"),
        Input(FILTER_STORE, "data"),
        Input("scheme", "value"),
        Input("dist-group", "value"),
        Input("dist-kind", "value"),
        Input("dist-points", "value"),
    )
    def update_distribution(filters, scheme_key, group, kind, points):
        return chart(
            build_distribution_figure, filters, scheme_key,
            group=choice(group, DISTRIBUTION_GROUPS, "key_label"),
            kind=choice(kind, DISTRIBUTION_KINDS, "box"),
            points=choice(points, DISTRIBUTION_POINTS, "outliers"),
        )

    @app.callback(
        Output("scatter-graph", "figure"),
        Output("scatter-notes", "children"),
        Input(FILTER_STORE, "data"),
        Input("scheme", "value"),
        Input("scatter-color", "value"),
        Input("scatter-points", "value"),
        Input("scatter-trend", "value"),
    )
    def update_scatter(filters, scheme_key, color_by, max_points, trend):
        max_points, alert = coerce_int(max_points, minimum=500, maximum=8000, default=3000, label="Max. počet bodů")
        return chart(
            build_scatter_figure, filters, scheme_key, [alert],
            color_by=choice(color_by, SCATTER_COLORS, "key_label"),
            max_points=max_points,
            trend=bool(trend),
        )

    @app.callback(
        Output("comps-graph", "figure"),
        Output("comps-notes", "children"),
        Input(FILTER_STORE, "data"),
        Input("scheme", "value"),
        Input("comps-level", "value"),
        Input("comps-metric", "value"),
        Input("comps-min", "value"),
        Input("comps-top", "value"),
    )
    def update_compositions(filters, scheme_key, level, metric, min_runs, top_n):
        min_runs, min_alert = coerce_int(min_runs, minimum=1, maximum=1000, default=10, label="Min. počet runů")
        top_n, top_alert = coerce_int(top_n, minimum=3, maximum=20, default=10, label="Počet složení")
        return chart(
            build_compositions_figure, filters, scheme_key, [min_alert, top_alert],
            level=choice(level, COMPOSITION_LEVELS, "spec"),
            metric=choice(metric, COMPOSITION_METRICS, "count"),
            min_runs=min_runs,
            top_n=top_n,
        )

    @app.callback(
        Output("runs-table", "children"),
        Output("runs-notes", "children"),
        Input(FILTER_STORE, "data"),
        Input("runs-sort", "value"),
        Input("runs-order", "value"),
        Input("runs-limit", "value"),
    )
    def update_runs_table(filters, sort, order, limit):
        limit, alert = coerce_int(limit, minimum=1, maximum=100, default=15, label="Počet řádků")
        try:
            rows = top_runs(
                select(RunFilter.from_store(filters)).runs,
                sort=choice(sort, RUN_SORTS, "clear"),
                descending=choice(order, SORT_ORDER, "asc") == "desc",
                limit=limit,
            )
        except Exception:  # last-resort guard, see chart()
            logger.exception("Rendering of the run table failed")
            error = Alert("error", "Tabulku se nepodařilo sestavit kvůli neočekávané chybě; podrobnosti jsou v logu.")
            return html.P("Tabulka není k dispozici.", className="empty"), render_alerts([error])
        return render_runs_table(rows), render_alerts([alert])


def create_app(data: RunData, scheme_key: str = DEFAULT_SCHEME) -> Dash:
    """Create the Dash application for an already loaded dataset."""
    app = Dash(__name__, title=APP_TITLE, update_title=None)
    app.index_string = (
        INDEX_TEMPLATE.replace("__STYLES__", build_stylesheet()).replace("__SCRIPTS__", PAGER_KEYS_SCRIPT)
    )
    app.layout = build_layout(data, choice(scheme_key, SCHEMES, DEFAULT_SCHEME))
    register_callbacks(app, data)
    return app


# =============================================================================
# 7. Command-line interface
# =============================================================================


def port_number(value: str) -> int:
    """argparse type for a TCP port."""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"„{value}“ není celé číslo") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port {port} je mimo rozsah 1–65535")
    return port


def timezone_name(value: str) -> str:
    """argparse type for an IANA time zone name."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise argparse.ArgumentTypeError(f"neznámá časová zóna „{value}“ (např. Europe/Prague nebo UTC)") from None
    return value


def check_port(host: str, port: int) -> str | None:
    """Return a Czech error message when the server cannot listen on ``host:port``."""
    try:
        family, socktype, proto, _, address = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
    except socket.gaierror as exc:
        return f"adresu „{host}“ nelze přeložit ({exc.strerror})"
    with socket.socket(family, socktype, proto) as probe:
        if os.name != "nt":  # mirrors Werkzeug; on Windows the option would allow port sharing
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(address)
        except OSError as exc:
            return f"port {port} na adrese {host} je obsazen nebo nedostupný ({exc.strerror})"
    return None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="pos_dashboard",
        description="Interaktivní dashboard Mythic+ runů dungeonu Pit of Saron (Plotly + Dash).",
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="zobrazí tuto nápovědu a skončí")
    parser.add_argument(
        "-d", "--data", type=Path, default=DEFAULT_DATA_PATH,
        help="cesta k CSV souboru s runy (výchozí: pos_data.csv ve složce aplikace)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="adresa, na které server naslouchá (výchozí: %(default)s)")
    parser.add_argument(
        "-p", "--port", type=port_number, default=DEFAULT_PORT, help="TCP port serveru 1–65535 (výchozí: %(default)s)"
    )
    parser.add_argument(
        "-s", "--scheme", choices=list(SCHEMES), default=DEFAULT_SCHEME,
        help="výchozí barevné schéma: frost = Ledová koruna, saronite = Saronit, light = Denní světlo "
        "(výchozí: %(default)s)",
    )
    parser.add_argument(
        "-t", "--timezone", type=timezone_name, default=DEFAULT_TIMEZONE,
        help="časová zóna (IANA) pro data, dny v týdnu a hodiny (výchozí: %(default)s)",
    )
    parser.add_argument("--debug", action="store_true", help="vývojový režim Dash – nepoužívat ve sdíleném prostředí")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}", help="zobrazí verzi a skončí")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: load data and start the web server."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    try:
        data = load_run_data(args.data, args.timezone)
    except DataLoadError as exc:
        print(f"Chyba: {exc}", file=sys.stderr)
        print("Cestu k datovému souboru lze zadat parametrem --data (viz --help).", file=sys.stderr)
        return 1
    port_error = check_port(args.host, args.port)
    if port_error:
        print(f"Chyba: server nelze spustit – {port_error}.", file=sys.stderr)
        print("Zkontrolujte parametry --host a --port.", file=sys.stderr)
        return 2
    logger.info(
        "Načteno %s runů ze souboru %s; dashboard poběží na http://%s:%s/",
        fmt_int(len(data.runs)), data.source, args.host, args.port,
    )
    app = create_app(data, args.scheme)
    try:
        app.run(host=args.host, port=args.port, debug=args.debug)
    except OSError as exc:
        print(f"Chyba: server nelze spustit na {args.host}:{args.port} ({exc}).", file=sys.stderr)
        print("Zkontrolujte parametry --host a --port.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
