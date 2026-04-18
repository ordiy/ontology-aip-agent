"""Auto-visualization logic for query results.

Detects the most appropriate chart type for a given result set and renders it
using plotly. Called from the Streamlit UI after displaying the data table.

Chart selection logic:
  - 1 text column + 1 numeric column → bar chart (category vs value)
  - 1 date/datetime column + 1 numeric column → line chart (time series)
  - 1 text column with ≤8 distinct values + 1 numeric column → pie chart option
  - 2+ numeric columns → scatter or multi-bar
  - Otherwise → no chart (return None)
"""

import pandas as pd
from typing import Optional


def detect_chart_type(df: pd.DataFrame) -> Optional[str]:
    """Detect the most suitable chart type for the dataframe.

    Examines column types and cardinality to choose between:
    'bar', 'line', 'pie', 'area', 'stacked_bar', or None (no chart suitable).
    'area': date/time column present AND (column name contains 'cumul'/'running'/'total' OR values are monotonically non-decreasing)
    'stacked_bar': exactly 2 text/object columns + exactly 1 numeric column

    Args:
        df: Query result as a DataFrame

    Returns:
        Chart type string or None if no visualization is appropriate
    """
    if df.empty or len(df.columns) < 2:
        return None

    # Identify column categories
    numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
    text_cols = df.select_dtypes(include=["object"]).columns.tolist()
    # Detect date columns by name heuristic (actual dtype is often 'object' for SQLite dates)
    date_cols = [c for c in df.columns if any(kw in c.lower() for kw in ["date", "time", "_at", "created", "updated", "month", "year"]) or c.lower() == "at"]

    if not numeric_cols:
        return None  # Nothing to plot without numeric data

    # Stacked bar: exactly 2 category columns + 1 numeric → stacked bar
    # Shows how a metric breaks down across two dimensions simultaneously.
    if len(text_cols) == 2 and len(numeric_cols) == 1:
        return "stacked_bar"

    # Time series: date-like column + numeric → area or line chart.
    # Area charts suit cumulative/running totals; line suits point-in-time values.
    if date_cols and numeric_cols:
        value_col = numeric_cols[0]
        col_name_lower = value_col.lower()
        # Use area if column name suggests accumulation
        if any(kw in col_name_lower for kw in ["cumul", "running", "total", "sum"]):
            return "area"
        # Use area if the series is monotonically non-decreasing (cumulative pattern)
        if len(df) > 2:
            series = df[value_col].dropna()
            if len(series) > 2 and (series.diff().dropna() >= 0).all():
                return "area"
        return "line"

    # Category + single numeric: bar chart
    if text_cols and len(numeric_cols) == 1:
        label_col = text_cols[0]
        # Pie chart if few distinct categories (≤8) and all values positive
        n_unique = df[label_col].nunique()
        value_col = numeric_cols[0]
        if n_unique <= 8 and (df[value_col] >= 0).all():
            return "pie"
        return "bar"

    # Multiple numeric columns → bar chart (grouped)
    if len(numeric_cols) >= 2:
        return "bar"

    return None


def build_chart(df: pd.DataFrame, chart_type: str, title: str = ""):
    """Build a plotly figure for the given dataframe and chart type.

    Args:
        df: Query result DataFrame
        chart_type: One of 'bar', 'line', 'pie'
        title: Optional chart title (e.g. the user's query)

    Returns:
        plotly Figure object, or None if chart cannot be built
    """
    try:
        import plotly.express as px
    except ImportError:
        return None  # plotly not installed

    numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
    text_cols = df.select_dtypes(include=["object"]).columns.tolist()
    date_cols = [c for c in df.columns if any(kw in c.lower() for kw in ["date", "time", "_at", "created", "updated", "month", "year"]) or c.lower() == "at"]

    if not numeric_cols:
        return None

    value_col = numeric_cols[0]

    if chart_type == "line" and date_cols:
        x_col = date_cols[0]
        # Sort by date for a proper time series display
        df_sorted = df.sort_values(x_col)
        return px.line(df_sorted, x=x_col, y=value_col, title=title or f"{value_col} over time")

    if chart_type == "pie" and text_cols:
        label_col = text_cols[0]
        return px.pie(df, names=label_col, values=value_col, title=title or f"{value_col} distribution")

    if chart_type == "bar" and text_cols:
        label_col = text_cols[0]
        # Limit to top 20 rows for readability — too many bars clutter the chart
        df_plot = df.head(20)
        return px.bar(df_plot, x=label_col, y=value_col, title=title or f"{value_col} by {label_col}")

    if chart_type == "bar" and len(numeric_cols) >= 2:
        # Use first column as index if it's text, otherwise use row index
        if text_cols:
            return px.bar(df.head(20), x=text_cols[0], y=numeric_cols, title=title or "Comparison")
        return px.bar(df.head(20), y=numeric_cols, title=title or "Comparison")

    if chart_type == "area" and date_cols:
        x_col = date_cols[0]
        # Sort ascending so the area fills correctly left-to-right
        df_sorted = df.sort_values(x_col)
        return px.area(df_sorted, x=x_col, y=value_col, title=title or f"{value_col} over time (cumulative)")

    if chart_type == "stacked_bar" and len(text_cols) >= 2:
        # text_cols[0] = x-axis category, text_cols[1] = stack color dimension
        x_col = text_cols[0]
        color_col = text_cols[1]
        # Limit rows to keep chart readable — too many groups become unreadable
        df_plot = df.head(50)
        return px.bar(df_plot, x=x_col, y=value_col, color=color_col,
                      barmode="stack",
                      title=title or f"{value_col} by {x_col} stacked by {color_col}")

    return None
